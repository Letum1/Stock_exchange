import json
import os
import random
import sqlite3
from functools import wraps

import requests
import yfinance as yf
from flask import (
    Flask, g, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-change-me")

DB_PATH = "portfolio.db"
STARTING_CASH = 0.0          # admin must fund accounts


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT    NOT NULL UNIQUE,
                password TEXT    NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                created  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS portfolios (
                user_id  INTEGER PRIMARY KEY REFERENCES users(id),
                cash     REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS holdings (
                user_id  INTEGER NOT NULL REFERENCES users(id),
                ticker   TEXT    NOT NULL,
                shares   REAL    NOT NULL,
                PRIMARY KEY (user_id, ticker)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL REFERENCES users(id),
                ticker    TEXT    NOT NULL,
                action    TEXT    NOT NULL,
                shares    REAL    NOT NULL,
                price     REAL    NOT NULL,
                total     REAL    NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                description TEXT    DEFAULT '',
                emoji       TEXT    DEFAULT '📦',
                rarity      TEXT    DEFAULT 'common',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_items (
                user_id  INTEGER NOT NULL REFERENCES users(id),
                item_id  INTEGER NOT NULL REFERENCES items(id),
                quantity INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS item_listings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id        INTEGER NOT NULL REFERENCES users(id),
                item_id          INTEGER NOT NULL REFERENCES items(id),
                quantity         INTEGER NOT NULL,
                price            REAL,
                accepts_items    TEXT,
                status           TEXT NOT NULL DEFAULT 'OPEN',
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS item_trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id    INTEGER,
                seller_id     INTEGER NOT NULL,
                buyer_id      INTEGER NOT NULL,
                item_id       INTEGER NOT NULL,
                quantity      INTEGER NOT NULL,
                payment_type  TEXT NOT NULL,
                cash_amount   REAL,
                paid_item_id  INTEGER,
                paid_item_qty INTEGER,
                timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id    INTEGER NOT NULL REFERENCES users(id),
                recipient_id INTEGER NOT NULL REFERENCES users(id),
                content      TEXT NOT NULL,
                timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_read      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trade_sessions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a             INTEGER NOT NULL REFERENCES users(id),
                user_b             INTEGER NOT NULL REFERENCES users(id),
                status             TEXT NOT NULL DEFAULT 'OPEN',
                accept_a           INTEGER NOT NULL DEFAULT 0,
                accept_b           INTEGER NOT NULL DEFAULT 0,
                confirm_a          INTEGER NOT NULL DEFAULT 0,
                confirm_b          INTEGER NOT NULL DEFAULT 0,
                review_started_at  DATETIME,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at       DATETIME
            );

            CREATE TABLE IF NOT EXISTS trade_offers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id   INTEGER NOT NULL REFERENCES trade_sessions(id),
                side       TEXT NOT NULL,
                kind       TEXT NOT NULL,
                ref        TEXT,
                qty        REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_msg_pair ON messages(sender_id, recipient_id, id);
            CREATE INDEX IF NOT EXISTS idx_listings_status ON item_listings(status);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trade_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_offers_trade ON trade_offers(trade_id);
        """)
        db.commit()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not logged in"}), 401
            return redirect(url_for("login_page"))
        if session.get("is_banned"):
            session.clear()
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Account banned"}), 403
            return redirect(url_for("login_page") + "?banned=1")
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not logged in"}), 401
        if not session.get("is_admin"):
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated


def current_user_id():
    return session["user_id"]


# ── Stock helper ──────────────────────────────────────────────────────────────

def get_price(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        return None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template(
        "index.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    banned = request.args.get("banned")
    return render_template("auth.html", mode="login", banned=banned)


@app.route("/register")
def register_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("auth.html", mode="register", banned=None)


@app.route("/admin")
def admin_page():
    if not session.get("is_admin"):
        return redirect(url_for("index"))
    return render_template(
        "admin.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
    )


@app.route("/account")
@login_required
def account_page():
    return render_template(
        "account.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    captcha_input = data.get("captcha")

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    expected = session.get("captcha_answer")
    if expected is None:
        return jsonify({"error": "Captcha expired. Refresh the page and try again."}), 400
    try:
        if int(captcha_input) != int(expected):
            return jsonify({"error": "Wrong captcha answer. Try again."}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Please enter a number for the captcha"}), 400
    session.pop("captcha_answer", None)

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": "Username already taken"}), 400

    # First user ever becomes admin
    user_count = db.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    is_admin = 1 if user_count == 0 else 0

    pw_hash = generate_password_hash(password)
    cur = db.execute(
        "INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)",
        (username, pw_hash, is_admin),
    )
    user_id = cur.lastrowid
    db.execute("INSERT INTO portfolios (user_id, cash) VALUES (?, ?)", (user_id, STARTING_CASH))
    db.commit()

    session["user_id"]  = user_id
    session["username"] = username
    session["is_admin"] = bool(is_admin)
    session["is_banned"] = False
    return jsonify({"message": f"Welcome, {username}!", "is_admin": bool(is_admin)})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    user = db.execute(
        "SELECT id, password, is_admin, is_banned FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not user or not check_password_hash(user["password"], password):
        return jsonify({"error": "Invalid username or password"}), 401
    if user["is_banned"]:
        return jsonify({"error": "Your account has been banned"}), 403

    session["user_id"]  = user["id"]
    session["username"] = username
    session["is_admin"] = bool(user["is_admin"])
    session["is_banned"] = False
    return jsonify({"message": f"Welcome back, {username}!", "is_admin": bool(user["is_admin"])})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})


@app.route("/api/account/username", methods=["POST"])
@login_required
def change_username():
    uid  = current_user_id()
    data = request.get_json()
    new_username = (data.get("username") or "").strip()

    if len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400

    db = get_db()
    existing = db.execute(
        "SELECT id FROM users WHERE username = ? AND id != ?", (new_username, uid)
    ).fetchone()
    if existing:
        return jsonify({"error": "Username already taken"}), 400

    db.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, uid))
    db.commit()
    session["username"] = new_username
    return jsonify({"message": "Username updated successfully", "username": new_username})


@app.route("/api/account/password", methods=["POST"])
@login_required
def change_password():
    uid  = current_user_id()
    data = request.get_json()
    current_pw  = data.get("current_password") or ""
    new_pw      = data.get("new_password") or ""

    if len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400

    db   = get_db()
    user = db.execute("SELECT password FROM users WHERE id = ?", (uid,)).fetchone()
    if not check_password_hash(user["password"], current_pw):
        return jsonify({"error": "Current password is incorrect"}), 401

    db.execute(
        "UPDATE users SET password = ? WHERE id = ?",
        (generate_password_hash(new_pw), uid),
    )
    db.commit()
    return jsonify({"message": "Password changed successfully"})


# ── Market API ────────────────────────────────────────────────────────────────

WATCHLIST = [
    "^GSPC",
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA",
    "JPM","V","WMT","XOM","JNJ","BRK-B","NFLX","AMD",
    "DIS","PYPL","INTC","BA","GS",
]

# Small set of cryptocurrencies (yfinance tickers).
CRYPTO_WATCHLIST = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD",
    "ADA-USD", "BNB-USD", "LTC-USD",
]

@app.route("/market")
@login_required
def market_page():
    return render_template(
        "market.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/api/market/stocks")
@login_required
def market_stocks():
    results = []
    for ticker in WATCHLIST:
        try:
            t    = yf.Ticker(ticker)
            fi   = t.fast_info
            price     = round(float(fi.last_price), 2)
            prev      = round(float(fi.previous_close), 2)
            change    = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2) if prev else 0
            results.append({
                "ticker":     ticker,
                "name":       t.info.get("shortName", ticker),
                "price":      price,
                "prev_close": prev,
                "change":     change,
                "change_pct": change_pct,
            })
        except Exception:
            pass
    return jsonify(results)


@app.route("/api/market/crypto")
@login_required
def market_crypto():
    results = []
    for ticker in CRYPTO_WATCHLIST:
        try:
            t    = yf.Ticker(ticker)
            fi   = t.fast_info
            price      = round(float(fi.last_price), 2)
            prev       = round(float(fi.previous_close), 2)
            change     = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2) if prev else 0
            results.append({
                "ticker":     ticker,
                "name":       ticker.replace("-USD", ""),
                "price":      price,
                "prev_close": prev,
                "change":     change,
                "change_pct": change_pct,
            })
        except Exception:
            pass
    return jsonify(results)


@app.route("/api/market/chart/<ticker>")
@login_required
def market_chart(ticker):
    try:
        hist = yf.Ticker(ticker.upper()).history(period="1mo", interval="1d")
        if hist.empty:
            return jsonify({"error": "No data"}), 404
        data = [
            {"date": str(idx.date()), "close": round(float(row["Close"]), 2)}
            for idx, row in hist.iterrows()
        ]
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/info/<ticker>")
@login_required
def market_info(ticker):
    try:
        t    = yf.Ticker(ticker.upper())
        info = t.info
        fi   = t.fast_info
        price      = round(float(fi.last_price), 2)
        prev       = round(float(fi.previous_close), 2)
        change     = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0
        return jsonify({
            "ticker":       ticker.upper(),
            "name":         info.get("shortName", ticker),
            "price":        price,
            "change":       change,
            "change_pct":   change_pct,
            "open":         round(float(fi.open), 2) if fi.open else None,
            "high":         round(float(fi.day_high), 2) if fi.day_high else None,
            "low":          round(float(fi.day_low), 2) if fi.day_low else None,
            "volume":       int(fi.last_volume) if fi.last_volume else None,
            "market_cap":   info.get("marketCap"),
            "pe_ratio":     info.get("trailingPE"),
            "week52_high":  info.get("fiftyTwoWeekHigh"),
            "week52_low":   info.get("fiftyTwoWeekLow"),
            "sector":       info.get("sector"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Portfolio API ─────────────────────────────────────────────────────────────

@app.route("/api/portfolio")
@login_required
def portfolio():
    uid = current_user_id()
    db  = get_db()
    row = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()
    cash = row["cash"] if row else 0.0

    holdings_rows = db.execute(
        "SELECT ticker, shares FROM holdings WHERE user_id = ?", (uid,)
    ).fetchall()

    holdings    = []
    total_value = cash
    for r in holdings_rows:
        price = get_price(r["ticker"]) or 0.0
        value = round(r["shares"] * price, 2)
        total_value += value
        holdings.append({"ticker": r["ticker"], "shares": r["shares"], "price": price, "value": value})

    return jsonify({
        "cash": round(cash, 2),
        "holdings": holdings,
        "total_value": round(total_value, 2),
        "pnl": round(total_value, 2),   # starts at 0, so P&L = total value gained
    })


@app.route("/api/quote/<ticker>")
@login_required
def quote(ticker):
    price = get_price(ticker.upper())
    if price is None:
        return jsonify({"error": "Ticker not found or no data"}), 404
    return jsonify({"ticker": ticker.upper(), "price": price})


@app.route("/api/buy", methods=["POST"])
@login_required
def buy():
    uid  = current_user_id()
    data = request.get_json()
    ticker = (data.get("ticker") or "").upper().strip()
    try:
        shares = float(data.get("shares", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares"}), 400

    if not ticker or shares <= 0:
        return jsonify({"error": "Invalid ticker or shares"}), 400

    price = get_price(ticker)
    if price is None:
        return jsonify({"error": "Ticker not found"}), 404

    total = round(shares * price, 2)
    db    = get_db()
    cash  = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()["cash"]

    if total > cash:
        return jsonify({"error": f"Insufficient funds. Need ${total:.2f}, have ${cash:.2f}"}), 400

    new_cash = round(cash - total, 2)
    db.execute("UPDATE portfolios SET cash = ? WHERE user_id = ?", (new_cash, uid))

    existing = db.execute(
        "SELECT shares FROM holdings WHERE user_id = ? AND ticker = ?", (uid, ticker)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE holdings SET shares = ? WHERE user_id = ? AND ticker = ?",
            (existing["shares"] + shares, uid, ticker),
        )
    else:
        db.execute(
            "INSERT INTO holdings (user_id, ticker, shares) VALUES (?, ?, ?)",
            (uid, ticker, shares),
        )

    db.execute(
        "INSERT INTO trades (user_id, ticker, action, shares, price, total) VALUES (?, ?, 'BUY', ?, ?, ?)",
        (uid, ticker, shares, price, total),
    )
    db.commit()
    return jsonify({"message": f"Bought {shares} shares of {ticker} at ${price:.2f}", "cash_remaining": new_cash})


@app.route("/api/sell", methods=["POST"])
@login_required
def sell():
    uid  = current_user_id()
    data = request.get_json()
    ticker = (data.get("ticker") or "").upper().strip()
    try:
        shares = float(data.get("shares", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares"}), 400

    if not ticker or shares <= 0:
        return jsonify({"error": "Invalid ticker or shares"}), 400

    db       = get_db()
    existing = db.execute(
        "SELECT shares FROM holdings WHERE user_id = ? AND ticker = ?", (uid, ticker)
    ).fetchone()
    if not existing or existing["shares"] < shares:
        held = existing["shares"] if existing else 0
        return jsonify({"error": f"Not enough shares. Holding {held:.4f} of {ticker}"}), 400

    price = get_price(ticker)
    if price is None:
        return jsonify({"error": "Ticker not found"}), 404

    total    = round(shares * price, 2)
    cash     = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()["cash"]
    new_cash = round(cash + total, 2)

    db.execute("UPDATE portfolios SET cash = ? WHERE user_id = ?", (new_cash, uid))

    new_shares = existing["shares"] - shares
    if new_shares < 1e-9:
        db.execute("DELETE FROM holdings WHERE user_id = ? AND ticker = ?", (uid, ticker))
    else:
        db.execute(
            "UPDATE holdings SET shares = ? WHERE user_id = ? AND ticker = ?",
            (new_shares, uid, ticker),
        )

    db.execute(
        "INSERT INTO trades (user_id, ticker, action, shares, price, total) VALUES (?, ?, 'SELL', ?, ?, ?)",
        (uid, ticker, shares, price, total),
    )
    db.commit()
    return jsonify({"message": f"Sold {shares} shares of {ticker} at ${price:.2f}", "cash_remaining": new_cash})


@app.route("/api/trades")
@login_required
def trades():
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        "SELECT ticker, action, shares, price, total, timestamp FROM trades "
        "WHERE user_id = ? ORDER BY id DESC LIMIT 50",
        (uid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Ticker search ─────────────────────────────────────────────────────────────

@app.route("/api/search")
@login_required
def search_ticker():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify([])
    try:
        resp = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 8, "newsCount": 0, "enableFuzzyQuery": "true"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=4,
        )
        data   = resp.json()
        quotes = data.get("quotes", [])
        results = [
            {"symbol": x["symbol"], "name": x.get("shortname") or x.get("longname") or ""}
            for x in quotes
            if x.get("quoteType") in ("EQUITY", "ETF", "INDEX", "MUTUALFUND")
        ]
        return jsonify(results[:7])
    except Exception:
        return jsonify([])


# ── Captcha (anti-bot) ────────────────────────────────────────────────────────

CAPTCHA_OPS = [
    ("+", lambda a, b: a + b),
    ("−", lambda a, b: a - b),
    ("×", lambda a, b: a * b),
]

def make_captcha():
    op_sym, op_fn = random.choice(CAPTCHA_OPS)
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    if op_sym == "−" and b > a:
        a, b = b, a
    return f"{a} {op_sym} {b}", op_fn(a, b)


@app.route("/api/captcha")
def captcha():
    question, answer = make_captcha()
    session["captcha_answer"] = answer
    return jsonify({"question": question})


# ── Pages: Items / Chat / Profile ─────────────────────────────────────────────

@app.route("/items")
@login_required
def items_page():
    return render_template(
        "items.html",
        username=session.get("username"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/chat")
@login_required
def chat_page():
    return render_template(
        "chat.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/profile/<int:user_id>")
@login_required
def profile_page(user_id):
    db   = get_db()
    user = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return render_template(
            "profile.html",
            not_found=True,
            target_id=user_id,
            target_username=None,
            username=session.get("username"),
            is_admin=session.get("is_admin", False),
            current_user_id=session.get("user_id"),
        )
    return render_template(
        "profile.html",
        not_found=False,
        target_id=user["id"],
        target_username=user["username"],
        username=session.get("username"),
        is_admin=session.get("is_admin", False),
        current_user_id=session.get("user_id"),
    )


# ── Items API ─────────────────────────────────────────────────────────────────

def _serialize_item(row):
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "emoji": row["emoji"], "rarity": row["rarity"],
    }


@app.route("/api/items")
@login_required
def api_items():
    db   = get_db()
    rows = db.execute("SELECT * FROM items ORDER BY id").fetchall()
    return jsonify([_serialize_item(r) for r in rows])


@app.route("/api/inventory")
@login_required
def api_inventory():
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        """SELECT ui.item_id, ui.quantity, i.name, i.description, i.emoji, i.rarity
           FROM user_items ui JOIN items i ON i.id = ui.item_id
           WHERE ui.user_id = ? AND ui.quantity > 0
           ORDER BY i.name""",
        (uid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/inventory/<int:user_id>")
@login_required
def api_user_inventory(user_id):
    db   = get_db()
    rows = db.execute(
        """SELECT ui.item_id, ui.quantity, i.name, i.emoji, i.rarity
           FROM user_items ui JOIN items i ON i.id = ui.item_id
           WHERE ui.user_id = ? AND ui.quantity > 0
           ORDER BY i.name""",
        (user_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/listings")
@login_required
def api_listings():
    db   = get_db()
    rows = db.execute(
        """SELECT l.id, l.seller_id, u.username AS seller_name, l.item_id, l.quantity,
                  l.price, l.accepts_items, l.created_at,
                  i.name AS item_name, i.emoji AS item_emoji, i.rarity AS item_rarity
           FROM item_listings l
           JOIN users u ON u.id = l.seller_id
           JOIN items i ON i.id = l.item_id
           WHERE l.status = 'OPEN'
           ORDER BY l.id DESC LIMIT 200"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["accepts_items"] = json.loads(d["accepts_items"]) if d["accepts_items"] else []
        out.append(d)
    return jsonify(out)


@app.route("/api/listings/mine")
@login_required
def api_my_listings():
    uid  = current_user_id()
    db   = get_db()
    rows = db.execute(
        """SELECT l.id, l.item_id, l.quantity, l.price, l.accepts_items, l.status, l.created_at,
                  i.name AS item_name, i.emoji AS item_emoji
           FROM item_listings l
           JOIN items i ON i.id = l.item_id
           WHERE l.seller_id = ? ORDER BY l.id DESC LIMIT 100""",
        (uid,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["accepts_items"] = json.loads(d["accepts_items"]) if d["accepts_items"] else []
        out.append(d)
    return jsonify(out)


@app.route("/api/listings/create", methods=["POST"])
@login_required
def create_listing():
    uid  = current_user_id()
    data = request.get_json()
    try:
        item_id  = int(data.get("item_id"))
        quantity = int(data.get("quantity"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid item or quantity"}), 400

    price = data.get("price")
    if price in ("", None):
        price = None
    else:
        try:
            price = float(price)
            if price <= 0:
                return jsonify({"error": "Price must be positive"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid price"}), 400

    accepts_raw = data.get("accepts_items") or []
    accepts = []
    for opt in accepts_raw:
        try:
            iid = int(opt.get("item_id"))
            qty = int(opt.get("quantity"))
            if iid <= 0 or qty <= 0: continue
            accepts.append({"item_id": iid, "quantity": qty})
        except (TypeError, ValueError, AttributeError):
            continue

    if price is None and not accepts:
        return jsonify({"error": "Must accept either cash or at least one item offer"}), 400
    if quantity <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400

    db = get_db()
    own = db.execute(
        "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?", (uid, item_id)
    ).fetchone()
    if not own or own["quantity"] < quantity:
        return jsonify({"error": "You do not own enough of this item"}), 400

    # Lock the items by removing from inventory (returned on cancel)
    db.execute(
        "UPDATE user_items SET quantity = quantity - ? WHERE user_id = ? AND item_id = ?",
        (quantity, uid, item_id),
    )
    db.execute(
        """INSERT INTO item_listings (seller_id, item_id, quantity, price, accepts_items)
           VALUES (?, ?, ?, ?, ?)""",
        (uid, item_id, quantity, price, json.dumps(accepts) if accepts else None),
    )
    db.commit()
    return jsonify({"message": "Listing created"})


@app.route("/api/listings/<int:listing_id>/cancel", methods=["POST"])
@login_required
def cancel_listing(listing_id):
    uid  = current_user_id()
    db   = get_db()
    row  = db.execute("SELECT * FROM item_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["seller_id"] != uid:
        return jsonify({"error": "Not your listing"}), 403
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing is not open"}), 400

    # Return items to seller
    _add_user_item(db, uid, row["item_id"], row["quantity"])
    db.execute("UPDATE item_listings SET status = 'CANCELLED' WHERE id = ?", (listing_id,))
    db.commit()
    return jsonify({"message": "Listing cancelled, items returned"})


def _add_user_item(db, user_id, item_id, quantity):
    existing = db.execute(
        "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?", (user_id, item_id)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE user_items SET quantity = quantity + ? WHERE user_id = ? AND item_id = ?",
            (quantity, user_id, item_id),
        )
    else:
        db.execute(
            "INSERT INTO user_items (user_id, item_id, quantity) VALUES (?, ?, ?)",
            (user_id, item_id, quantity),
        )


@app.route("/api/listings/<int:listing_id>/buy", methods=["POST"])
@login_required
def buy_listing(listing_id):
    uid = current_user_id()
    db  = get_db()
    row = db.execute("SELECT * FROM item_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing no longer available"}), 400
    if row["seller_id"] == uid:
        return jsonify({"error": "Cannot buy your own listing"}), 400
    if row["price"] is None:
        return jsonify({"error": "This listing does not accept cash"}), 400

    price = row["price"]
    cash  = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()["cash"]
    if cash < price:
        return jsonify({"error": f"Insufficient funds. Need ${price:.2f}, have ${cash:.2f}"}), 400

    # Transfer cash
    db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (price, uid))
    db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id = ?", (price, row["seller_id"]))
    # Transfer items
    _add_user_item(db, uid, row["item_id"], row["quantity"])
    # Close listing
    db.execute("UPDATE item_listings SET status = 'SOLD' WHERE id = ?", (listing_id,))
    # History
    db.execute(
        """INSERT INTO item_trades (listing_id, seller_id, buyer_id, item_id, quantity,
                                    payment_type, cash_amount)
           VALUES (?, ?, ?, ?, ?, 'CASH', ?)""",
        (listing_id, row["seller_id"], uid, row["item_id"], row["quantity"], price),
    )
    db.commit()
    return jsonify({"message": f"Bought item for ${price:.2f}"})


@app.route("/api/listings/<int:listing_id>/trade", methods=["POST"])
@login_required
def trade_listing(listing_id):
    uid  = current_user_id()
    data = request.get_json()
    try:
        offer_index = int(data.get("offer_index"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid offer"}), 400

    db  = get_db()
    row = db.execute("SELECT * FROM item_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing no longer available"}), 400
    if row["seller_id"] == uid:
        return jsonify({"error": "Cannot trade with your own listing"}), 400

    accepts = json.loads(row["accepts_items"]) if row["accepts_items"] else []
    if not accepts:
        return jsonify({"error": "This listing does not accept item trades"}), 400
    if offer_index < 0 or offer_index >= len(accepts):
        return jsonify({"error": "Invalid offer choice"}), 400

    offer = accepts[offer_index]
    paid_item_id  = int(offer["item_id"])
    paid_item_qty = int(offer["quantity"])

    own = db.execute(
        "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?", (uid, paid_item_id)
    ).fetchone()
    if not own or own["quantity"] < paid_item_qty:
        return jsonify({"error": "You do not own enough of the offered item"}), 400

    # Take buyer's offered items, give to seller
    db.execute(
        "UPDATE user_items SET quantity = quantity - ? WHERE user_id = ? AND item_id = ?",
        (paid_item_qty, uid, paid_item_id),
    )
    _add_user_item(db, row["seller_id"], paid_item_id, paid_item_qty)
    # Give buyer the listed items
    _add_user_item(db, uid, row["item_id"], row["quantity"])
    db.execute("UPDATE item_listings SET status = 'SOLD' WHERE id = ?", (listing_id,))
    db.execute(
        """INSERT INTO item_trades (listing_id, seller_id, buyer_id, item_id, quantity,
                                    payment_type, paid_item_id, paid_item_qty)
           VALUES (?, ?, ?, ?, ?, 'ITEM', ?, ?)""",
        (listing_id, row["seller_id"], uid, row["item_id"], row["quantity"],
         paid_item_id, paid_item_qty),
    )
    db.commit()
    return jsonify({"message": "Trade complete!"})


# ── Messages API ──────────────────────────────────────────────────────────────

@app.route("/api/users/search")
@login_required
def search_users():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    db = get_db()
    rows = db.execute(
        "SELECT id, username FROM users WHERE username LIKE ? AND id != ? LIMIT 8",
        (f"%{q}%", current_user_id()),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/messages/conversations")
@login_required
def conversations():
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        """SELECT
            CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END AS other_id,
            MAX(id) AS last_id
           FROM messages
           WHERE sender_id = ? OR recipient_id = ?
           GROUP BY other_id
           ORDER BY last_id DESC""",
        (uid, uid, uid),
    ).fetchall()
    out = []
    for r in rows:
        last = db.execute(
            "SELECT sender_id, content, timestamp FROM messages WHERE id = ?",
            (r["last_id"],),
        ).fetchone()
        user = db.execute(
            "SELECT id, username FROM users WHERE id = ?", (r["other_id"],),
        ).fetchone()
        unread = db.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE sender_id = ? AND recipient_id = ? AND is_read = 0""",
            (r["other_id"], uid),
        ).fetchone()["n"]
        if user:
            out.append({
                "user_id":   user["id"],
                "username":  user["username"],
                "preview":   last["content"][:60],
                "from_me":   last["sender_id"] == uid,
                "timestamp": last["timestamp"],
                "unread":    unread,
            })
    return jsonify(out)


@app.route("/api/messages/<int:other_id>")
@login_required
def messages_with(other_id):
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        """SELECT id, sender_id, recipient_id, content, timestamp, is_read
           FROM messages
           WHERE (sender_id = ? AND recipient_id = ?)
              OR (sender_id = ? AND recipient_id = ?)
           ORDER BY id ASC LIMIT 500""",
        (uid, other_id, other_id, uid),
    ).fetchall()
    db.execute(
        "UPDATE messages SET is_read = 1 WHERE sender_id = ? AND recipient_id = ?",
        (other_id, uid),
    )
    db.commit()
    return jsonify([dict(r) for r in rows])


@app.route("/api/messages/unread-count")
@login_required
def unread_count():
    uid = current_user_id()
    db  = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE recipient_id = ? AND is_read = 0",
        (uid,),
    ).fetchone()
    return jsonify({"count": row["n"]})


@app.route("/api/messages/send", methods=["POST"])
@login_required
def send_message():
    uid  = current_user_id()
    data = request.get_json()
    try:
        recipient_id = int(data.get("recipient_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid recipient"}), 400
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Message cannot be empty"}), 400
    if len(content) > 1000:
        return jsonify({"error": "Message too long (max 1000 chars)"}), 400
    if recipient_id == uid:
        return jsonify({"error": "Cannot message yourself"}), 400

    db = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404

    db.execute(
        "INSERT INTO messages (sender_id, recipient_id, content) VALUES (?, ?, ?)",
        (uid, recipient_id, content),
    )
    db.commit()
    return jsonify({"message": "Sent"})


# ── Profile API ───────────────────────────────────────────────────────────────

@app.route("/api/profile/<int:user_id>")
@login_required
def api_profile(user_id):
    db   = get_db()
    user = db.execute(
        "SELECT id, username, is_admin, is_banned, created FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not user:
        return jsonify({"error": "Not found"}), 404

    trade_count = db.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]
    item_trade_count = db.execute(
        "SELECT COUNT(*) AS n FROM item_trades WHERE buyer_id = ? OR seller_id = ?",
        (user_id, user_id),
    ).fetchone()["n"]
    item_count = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS n FROM user_items WHERE user_id = ?",
        (user_id,),
    ).fetchone()["n"]
    holdings_count = db.execute(
        "SELECT COUNT(*) AS n FROM holdings WHERE user_id = ?", (user_id,)
    ).fetchone()["n"]

    return jsonify({
        "id":       user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "is_banned": bool(user["is_banned"]),
        "created":  user["created"],
        "trade_count":      trade_count,
        "item_trade_count": item_trade_count,
        "item_count":       item_count,
        "holdings_count":   holdings_count,
    })


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.route("/api/admin/items", methods=["POST"])
@admin_required
def admin_create_item():
    data        = request.get_json()
    name        = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    emoji       = (data.get("emoji") or "📦").strip()[:4] or "📦"
    rarity      = (data.get("rarity") or "common").strip().lower()
    if rarity not in ("common", "uncommon", "rare", "epic", "legendary"):
        rarity = "common"
    if len(name) < 2:
        return jsonify({"error": "Name must be at least 2 characters"}), 400

    db = get_db()
    if db.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone():
        return jsonify({"error": "Item with that name already exists"}), 400
    db.execute(
        "INSERT INTO items (name, description, emoji, rarity) VALUES (?, ?, ?, ?)",
        (name, description, emoji, rarity),
    )
    db.commit()
    return jsonify({"message": f"Item '{name}' created"})


@app.route("/api/admin/items/<int:item_id>", methods=["DELETE"])
@admin_required
def admin_delete_item(item_id):
    db = get_db()
    if not db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone():
        return jsonify({"error": "Item not found"}), 404
    # Cancel all open listings of this item, return to sellers
    listings = db.execute(
        "SELECT id, seller_id, quantity FROM item_listings WHERE item_id = ? AND status = 'OPEN'",
        (item_id,),
    ).fetchall()
    for l in listings:
        _add_user_item(db, l["seller_id"], item_id, l["quantity"])
        db.execute("UPDATE item_listings SET status = 'CANCELLED' WHERE id = ?", (l["id"],))
    db.execute("DELETE FROM user_items WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify({"message": "Item deleted"})


@app.route("/api/admin/items/grant", methods=["POST"])
@admin_required
def admin_grant_item():
    data = request.get_json()
    try:
        user_id  = int(data.get("user_id"))
        item_id  = int(data.get("item_id"))
        quantity = int(data.get("quantity"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid input"}), 400
    if quantity == 0:
        return jsonify({"error": "Quantity cannot be zero"}), 400

    db = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404
    if not db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone():
        return jsonify({"error": "Item not found"}), 404

    if quantity > 0:
        _add_user_item(db, user_id, item_id, quantity)
    else:
        existing = db.execute(
            "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        ).fetchone()
        held = existing["quantity"] if existing else 0
        new_qty = max(0, held + quantity)
        if existing:
            db.execute(
                "UPDATE user_items SET quantity = ? WHERE user_id = ? AND item_id = ?",
                (new_qty, user_id, item_id),
            )
    db.commit()
    return jsonify({"message": f"Granted {quantity}× to user"})


@app.route("/api/admin/trades")
@admin_required
def admin_trades():
    db   = get_db()
    rows = db.execute(
        """SELECT t.id, u.username, t.ticker, t.action, t.shares, t.price, t.total, t.timestamp
           FROM trades t JOIN users u ON u.id = t.user_id
           ORDER BY t.id DESC LIMIT 500"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users")
@admin_required
def admin_users():
    db   = get_db()
    rows = db.execute(
        """SELECT u.id, u.username, u.is_admin, u.is_banned, u.created,
                  COALESCE(p.cash, 0) as cash
           FROM users u
           LEFT JOIN portfolios p ON p.user_id = u.id
           ORDER BY u.id""",
    ).fetchall()

    result = []
    for r in rows:
        holdings = db.execute(
            "SELECT ticker, shares FROM holdings WHERE user_id = ?", (r["id"],)
        ).fetchall()
        result.append({
            "id":       r["id"],
            "username": r["username"],
            "is_admin": bool(r["is_admin"]),
            "is_banned": bool(r["is_banned"]),
            "created":  r["created"],
            "cash":     round(r["cash"], 2),
            "holdings": [{"ticker": h["ticker"], "shares": h["shares"]} for h in holdings],
        })
    return jsonify(result)


@app.route("/api/admin/balance", methods=["POST"])
@admin_required
def admin_balance():
    data    = request.get_json()
    user_id = data.get("user_id")
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400

    db  = get_db()
    row = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404

    new_cash = round(row["cash"] + amount, 2)
    if new_cash < 0:
        new_cash = 0.0

    db.execute("UPDATE portfolios SET cash = ? WHERE user_id = ?", (new_cash, user_id))
    db.commit()
    action = f"Added ${amount:.2f}" if amount >= 0 else f"Removed ${abs(amount):.2f}"
    return jsonify({"message": f"{action}. New balance: ${new_cash:.2f}", "new_cash": new_cash})


@app.route("/api/admin/set-balance", methods=["POST"])
@admin_required
def admin_set_balance():
    data    = request.get_json()
    user_id = data.get("user_id")
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount < 0:
        return jsonify({"error": "Balance cannot be negative"}), 400

    db = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404

    db.execute("UPDATE portfolios SET cash = ? WHERE user_id = ?", (round(amount, 2), user_id))
    db.commit()
    return jsonify({"message": f"Balance set to ${amount:.2f}", "new_cash": round(amount, 2)})


@app.route("/api/admin/clear-assets", methods=["POST"])
@admin_required
def admin_clear_assets():
    data    = request.get_json()
    user_id = data.get("user_id")
    db      = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404

    db.execute("DELETE FROM holdings WHERE user_id = ?", (user_id,))
    db.commit()
    return jsonify({"message": "All holdings cleared"})


@app.route("/api/admin/ban", methods=["POST"])
@admin_required
def admin_ban():
    data    = request.get_json()
    user_id = data.get("user_id")
    banned  = 1 if data.get("banned") else 0

    db  = get_db()
    row = db.execute("SELECT id, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if row["is_admin"]:
        return jsonify({"error": "Cannot ban an admin"}), 400

    db.execute("UPDATE users SET is_banned = ? WHERE id = ?", (banned, user_id))
    db.commit()
    status = "banned" if banned else "unbanned"
    return jsonify({"message": f"User {status} successfully"})


@app.route("/api/admin/delete-user", methods=["POST"])
@admin_required
def admin_delete_user():
    data    = request.get_json()
    user_id = data.get("user_id")
    db      = get_db()
    row     = db.execute("SELECT id, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if row["is_admin"]:
        return jsonify({"error": "Cannot delete an admin account"}), 400

    db.execute("DELETE FROM trades    WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM holdings  WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM portfolios WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users     WHERE id = ?",      (user_id,))
    db.commit()
    return jsonify({"message": "User deleted"})


# ── Trade sessions (negotiated 1-on-1 trade) ──────────────────────────────────

from datetime import datetime, timedelta

REVIEW_SECONDS = 30


def _add_holding(db, user_id, ticker, shares):
    existing = db.execute(
        "SELECT shares FROM holdings WHERE user_id = ? AND ticker = ?", (user_id, ticker)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE holdings SET shares = shares + ? WHERE user_id = ? AND ticker = ?",
            (shares, user_id, ticker),
        )
    else:
        db.execute(
            "INSERT INTO holdings (user_id, ticker, shares) VALUES (?, ?, ?)",
            (user_id, ticker, shares),
        )


def _remove_holding(db, user_id, ticker, shares):
    existing = db.execute(
        "SELECT shares FROM holdings WHERE user_id = ? AND ticker = ?", (user_id, ticker)
    ).fetchone()
    if not existing or existing["shares"] + 1e-9 < shares:
        return False
    new_shares = existing["shares"] - shares
    if new_shares < 1e-9:
        db.execute("DELETE FROM holdings WHERE user_id = ? AND ticker = ?", (user_id, ticker))
    else:
        db.execute(
            "UPDATE holdings SET shares = ? WHERE user_id = ? AND ticker = ?",
            (new_shares, user_id, ticker),
        )
    return True


def _trade_get(db, trade_id):
    return db.execute(
        "SELECT * FROM trade_sessions WHERE id = ?", (trade_id,)
    ).fetchone()


def _trade_side(row, uid):
    if uid == row["user_a"]: return "A"
    if uid == row["user_b"]: return "B"
    return None


def _refund_trade_offers(db, trade_id):
    row = _trade_get(db, trade_id)
    offers = db.execute(
        "SELECT * FROM trade_offers WHERE trade_id = ?", (trade_id,)
    ).fetchall()
    for o in offers:
        owner = row["user_a"] if o["side"] == "A" else row["user_b"]
        if o["kind"] == "cash":
            db.execute(
                "UPDATE portfolios SET cash = cash + ? WHERE user_id = ?",
                (o["qty"], owner),
            )
        elif o["kind"] == "item":
            _add_user_item(db, owner, int(o["ref"]), int(o["qty"]))
        elif o["kind"] == "stock":
            _add_holding(db, owner, o["ref"], o["qty"])
    db.execute("DELETE FROM trade_offers WHERE trade_id = ?", (trade_id,))


def _execute_trade(db, trade_id):
    row = _trade_get(db, trade_id)
    a, b = row["user_a"], row["user_b"]
    offers = db.execute(
        "SELECT * FROM trade_offers WHERE trade_id = ?", (trade_id,)
    ).fetchall()
    for o in offers:
        recipient = b if o["side"] == "A" else a
        if o["kind"] == "cash":
            db.execute(
                "UPDATE portfolios SET cash = cash + ? WHERE user_id = ?",
                (o["qty"], recipient),
            )
        elif o["kind"] == "item":
            _add_user_item(db, recipient, int(o["ref"]), int(o["qty"]))
        elif o["kind"] == "stock":
            _add_holding(db, recipient, o["ref"], o["qty"])
    db.execute("DELETE FROM trade_offers WHERE trade_id = ?", (trade_id,))
    db.execute(
        "UPDATE trade_sessions SET status = 'COMPLETED', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (trade_id,),
    )


def _check_review_expiry(db, row):
    """If REVIEW window past 30s, auto-cancel + refund. Returns updated row."""
    if row["status"] == "REVIEW" and row["review_started_at"]:
        try:
            started = datetime.fromisoformat(row["review_started_at"].replace(" ", "T"))
        except ValueError:
            return row
        if datetime.utcnow() - started > timedelta(seconds=REVIEW_SECONDS):
            _refund_trade_offers(db, row["id"])
            db.execute(
                "UPDATE trade_sessions SET status = 'CANCELLED' WHERE id = ?",
                (row["id"],),
            )
            db.commit()
            row = _trade_get(db, row["id"])
    return row


def _reset_acceptance(db, trade_id):
    """Any change to offers resets accepts/confirms and clears REVIEW."""
    db.execute(
        """UPDATE trade_sessions
           SET accept_a = 0, accept_b = 0, confirm_a = 0, confirm_b = 0,
               review_started_at = NULL,
               status = CASE WHEN status IN ('REVIEW','OPEN') THEN 'OPEN' ELSE status END
           WHERE id = ?""",
        (trade_id,),
    )


@app.route("/trades")
@login_required
def trades_hub_page():
    return render_template(
        "trades_hub.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/trade/<int:trade_id>")
@login_required
def trade_page(trade_id):
    db  = get_db()
    row = _trade_get(db, trade_id)
    if not row:
        return "Trade not found", 404
    uid = current_user_id()
    if uid not in (row["user_a"], row["user_b"]):
        return "Not your trade", 403
    other_id = row["user_b"] if uid == row["user_a"] else row["user_a"]
    other = db.execute("SELECT username FROM users WHERE id = ?", (other_id,)).fetchone()
    return render_template(
        "trade.html",
        trade_id=trade_id,
        username=session.get("username"),
        user_id=uid,
        other_id=other_id,
        other_username=(other["username"] if other else "?"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/api/trades/start", methods=["POST"])
@login_required
def trade_start():
    uid  = current_user_id()
    data = request.get_json() or {}
    try:
        other_id = int(data.get("other_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400
    if other_id == uid:
        return jsonify({"error": "Cannot trade with yourself"}), 400

    db = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (other_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404

    # Re-use existing OPEN/REVIEW trade with this user (either order)
    existing = db.execute(
        """SELECT id FROM trade_sessions
           WHERE status IN ('OPEN','REVIEW')
             AND ((user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?))
           ORDER BY id DESC LIMIT 1""",
        (uid, other_id, other_id, uid),
    ).fetchone()
    if existing:
        return jsonify({"trade_id": existing["id"], "reused": True})

    cur = db.execute(
        "INSERT INTO trade_sessions (user_a, user_b) VALUES (?, ?)",
        (uid, other_id),
    )
    db.commit()
    return jsonify({"trade_id": cur.lastrowid, "reused": False})


@app.route("/api/trades/active")
@login_required
def trade_active_list():
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        """SELECT t.*, ua.username AS user_a_name, ub.username AS user_b_name
           FROM trade_sessions t
           JOIN users ua ON ua.id = t.user_a
           JOIN users ub ON ub.id = t.user_b
           WHERE (t.user_a = ? OR t.user_b = ?)
             AND t.status IN ('OPEN','REVIEW')
           ORDER BY t.id DESC""",
        (uid, uid),
    ).fetchall()
    out = []
    for r in rows:
        other_id   = r["user_b"] if uid == r["user_a"] else r["user_a"]
        other_name = r["user_b_name"] if uid == r["user_a"] else r["user_a_name"]
        out.append({
            "id": r["id"], "status": r["status"],
            "other_id": other_id, "other_username": other_name,
            "created_at": r["created_at"],
        })
    return jsonify(out)


def _serialize_offer(db, o):
    d = {"id": o["id"], "side": o["side"], "kind": o["kind"], "qty": o["qty"], "ref": o["ref"]}
    if o["kind"] == "item":
        item = db.execute(
            "SELECT name, emoji, rarity FROM items WHERE id = ?", (int(o["ref"]),)
        ).fetchone()
        if item:
            d["item_name"] = item["name"]
            d["item_emoji"] = item["emoji"]
            d["item_rarity"] = item["rarity"]
    return d


@app.route("/api/trades/<int:trade_id>")
@login_required
def trade_state(trade_id):
    uid = current_user_id()
    db  = get_db()
    row = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    if uid not in (row["user_a"], row["user_b"]):
        return jsonify({"error": "Not your trade"}), 403

    row = _check_review_expiry(db, row)

    offers = db.execute(
        "SELECT * FROM trade_offers WHERE trade_id = ? ORDER BY id", (trade_id,)
    ).fetchall()
    a_offers = [_serialize_offer(db, o) for o in offers if o["side"] == "A"]
    b_offers = [_serialize_offer(db, o) for o in offers if o["side"] == "B"]

    user_a = db.execute("SELECT id, username FROM users WHERE id = ?", (row["user_a"],)).fetchone()
    user_b = db.execute("SELECT id, username FROM users WHERE id = ?", (row["user_b"],)).fetchone()

    review_remaining = None
    if row["status"] == "REVIEW" and row["review_started_at"]:
        try:
            started = datetime.fromisoformat(row["review_started_at"].replace(" ", "T"))
            elapsed = (datetime.utcnow() - started).total_seconds()
            review_remaining = max(0, REVIEW_SECONDS - int(elapsed))
        except ValueError:
            pass

    return jsonify({
        "id": row["id"],
        "status": row["status"],
        "user_a": dict(user_a) if user_a else None,
        "user_b": dict(user_b) if user_b else None,
        "accept_a": bool(row["accept_a"]),
        "accept_b": bool(row["accept_b"]),
        "confirm_a": bool(row["confirm_a"]),
        "confirm_b": bool(row["confirm_b"]),
        "review_remaining": review_remaining,
        "your_side": _trade_side(row, uid),
        "a_offers": a_offers,
        "b_offers": b_offers,
        "your_id": uid,
    })


@app.route("/api/trades/<int:trade_id>/add", methods=["POST"])
@login_required
def trade_add(trade_id):
    uid  = current_user_id()
    data = request.get_json() or {}
    db   = get_db()
    row  = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    side = _trade_side(row, uid)
    if not side:
        return jsonify({"error": "Not your trade"}), 403
    if row["status"] not in ("OPEN", "REVIEW"):
        return jsonify({"error": "Trade is closed"}), 400

    kind = (data.get("kind") or "").lower()
    if kind not in ("cash", "item", "stock"):
        return jsonify({"error": "Invalid kind"}), 400

    try:
        qty = float(data.get("qty"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid quantity"}), 400
    if qty <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400

    if kind == "cash":
        cash = db.execute(
            "SELECT cash FROM portfolios WHERE user_id = ?", (uid,)
        ).fetchone()["cash"]
        if cash + 1e-9 < qty:
            return jsonify({"error": f"Insufficient funds. Have ${cash:.2f}"}), 400
        db.execute(
            "UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (qty, uid),
        )
        db.execute(
            "INSERT INTO trade_offers (trade_id, side, kind, qty) VALUES (?, ?, 'cash', ?)",
            (trade_id, side, qty),
        )

    elif kind == "item":
        try:
            item_id = int(data.get("ref"))
            qty_i   = int(qty)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid item"}), 400
        if qty_i <= 0:
            return jsonify({"error": "Quantity must be positive"}), 400
        own = db.execute(
            "SELECT quantity FROM user_items WHERE user_id = ? AND item_id = ?",
            (uid, item_id),
        ).fetchone()
        if not own or own["quantity"] < qty_i:
            return jsonify({"error": "You don't own enough of that item"}), 400
        db.execute(
            "UPDATE user_items SET quantity = quantity - ? WHERE user_id = ? AND item_id = ?",
            (qty_i, uid, item_id),
        )
        db.execute(
            "INSERT INTO trade_offers (trade_id, side, kind, ref, qty) VALUES (?, ?, 'item', ?, ?)",
            (trade_id, side, str(item_id), qty_i),
        )

    elif kind == "stock":
        ticker = (data.get("ref") or "").upper().strip()
        if not ticker:
            return jsonify({"error": "Invalid ticker"}), 400
        if not _remove_holding(db, uid, ticker, qty):
            return jsonify({"error": f"You don't own enough shares of {ticker}"}), 400
        db.execute(
            "INSERT INTO trade_offers (trade_id, side, kind, ref, qty) VALUES (?, ?, 'stock', ?, ?)",
            (trade_id, side, ticker, qty),
        )

    _reset_acceptance(db, trade_id)
    db.commit()
    return jsonify({"message": "Added"})


@app.route("/api/trades/<int:trade_id>/remove", methods=["POST"])
@login_required
def trade_remove(trade_id):
    uid  = current_user_id()
    data = request.get_json() or {}
    db   = get_db()
    row  = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    side = _trade_side(row, uid)
    if not side:
        return jsonify({"error": "Not your trade"}), 403
    if row["status"] not in ("OPEN", "REVIEW"):
        return jsonify({"error": "Trade is closed"}), 400

    try:
        offer_id = int(data.get("offer_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid offer"}), 400

    o = db.execute(
        "SELECT * FROM trade_offers WHERE id = ? AND trade_id = ?", (offer_id, trade_id),
    ).fetchone()
    if not o:
        return jsonify({"error": "Offer not found"}), 404
    if o["side"] != side:
        return jsonify({"error": "That isn't your offer"}), 403

    # Refund this single offer
    if o["kind"] == "cash":
        db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id = ?", (o["qty"], uid))
    elif o["kind"] == "item":
        _add_user_item(db, uid, int(o["ref"]), int(o["qty"]))
    elif o["kind"] == "stock":
        _add_holding(db, uid, o["ref"], o["qty"])

    db.execute("DELETE FROM trade_offers WHERE id = ?", (offer_id,))
    _reset_acceptance(db, trade_id)
    db.commit()
    return jsonify({"message": "Removed"})


@app.route("/api/trades/<int:trade_id>/accept", methods=["POST"])
@login_required
def trade_accept(trade_id):
    uid = current_user_id()
    db  = get_db()
    row = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    side = _trade_side(row, uid)
    if not side:
        return jsonify({"error": "Not your trade"}), 403
    row = _check_review_expiry(db, row)
    if row["status"] != "OPEN":
        return jsonify({"error": "Trade is not open for acceptance"}), 400

    has_offers = db.execute(
        "SELECT COUNT(*) AS n FROM trade_offers WHERE trade_id = ?", (trade_id,),
    ).fetchone()["n"]
    if has_offers == 0:
        return jsonify({"error": "Add at least one item before accepting"}), 400

    col = "accept_a" if side == "A" else "accept_b"
    db.execute(f"UPDATE trade_sessions SET {col} = 1 WHERE id = ?", (trade_id,))

    # Re-fetch and check both
    row = _trade_get(db, trade_id)
    if row["accept_a"] and row["accept_b"]:
        db.execute(
            "UPDATE trade_sessions SET status = 'REVIEW', review_started_at = CURRENT_TIMESTAMP, "
            "confirm_a = 0, confirm_b = 0 WHERE id = ?",
            (trade_id,),
        )
    db.commit()
    return jsonify({"message": "Accepted"})


@app.route("/api/trades/<int:trade_id>/confirm", methods=["POST"])
@login_required
def trade_confirm(trade_id):
    uid = current_user_id()
    db  = get_db()
    row = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    side = _trade_side(row, uid)
    if not side:
        return jsonify({"error": "Not your trade"}), 403
    row = _check_review_expiry(db, row)
    if row["status"] != "REVIEW":
        return jsonify({"error": "Trade is not in review"}), 400

    col = "confirm_a" if side == "A" else "confirm_b"
    db.execute(f"UPDATE trade_sessions SET {col} = 1 WHERE id = ?", (trade_id,))
    row = _trade_get(db, trade_id)
    if row["confirm_a"] and row["confirm_b"]:
        _execute_trade(db, trade_id)
        db.commit()
        return jsonify({"message": "Trade complete!", "completed": True})
    db.commit()
    return jsonify({"message": "Confirmed — waiting for the other side"})


@app.route("/api/trades/<int:trade_id>/cancel", methods=["POST"])
@login_required
def trade_cancel(trade_id):
    uid = current_user_id()
    db  = get_db()
    row = _trade_get(db, trade_id)
    if not row:
        return jsonify({"error": "Trade not found"}), 404
    side = _trade_side(row, uid)
    if not side:
        return jsonify({"error": "Not your trade"}), 403
    if row["status"] not in ("OPEN", "REVIEW"):
        return jsonify({"error": "Trade is already closed"}), 400

    _refund_trade_offers(db, trade_id)
    db.execute("UPDATE trade_sessions SET status = 'CANCELLED' WHERE id = ?", (trade_id,))
    db.commit()
    return jsonify({"message": "Trade cancelled, items returned"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
