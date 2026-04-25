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

            CREATE TABLE IF NOT EXISTS public_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id  INTEGER NOT NULL REFERENCES users(id),
                content    TEXT NOT NULL,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS public_mutes (
                user_id    INTEGER PRIMARY KEY REFERENCES users(id),
                muted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                muted_by   INTEGER REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS mining_worlds (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id    INTEGER NOT NULL REFERENCES users(id),
                name        TEXT NOT NULL,
                width       INTEGER NOT NULL DEFAULT 10,
                height      INTEGER NOT NULL DEFAULT 10,
                layer       INTEGER NOT NULL DEFAULT 0,
                generation  INTEGER NOT NULL DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mining_blocks (
                world_id    INTEGER NOT NULL REFERENCES mining_worlds(id),
                generation  INTEGER NOT NULL,
                x           INTEGER NOT NULL,
                y           INTEGER NOT NULL,
                mined_by    INTEGER REFERENCES users(id),
                mined_at    DATETIME,
                PRIMARY KEY (world_id, generation, x, y)
            );

            CREATE TABLE IF NOT EXISTS mining_world_members (
                world_id    INTEGER NOT NULL REFERENCES mining_worlds(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                joined_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (world_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS mining_user_stats (
                user_id        INTEGER PRIMARY KEY REFERENCES users(id),
                blocks_mined   INTEGER NOT NULL DEFAULT 0,
                layers_cleared INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bon_listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id   INTEGER NOT NULL REFERENCES users(id),
                quantity    INTEGER NOT NULL,
                price       REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'OPEN',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                buyer_id    INTEGER REFERENCES users(id),
                sold_at     DATETIME
            );

            CREATE TABLE IF NOT EXISTS cash_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                kind         TEXT    NOT NULL,
                currency     TEXT    NOT NULL,
                amount       REAL    NOT NULL,
                credentials  TEXT    NOT NULL,
                note         TEXT    DEFAULT '',
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                handled_by   INTEGER REFERENCES users(id),
                handled_at   DATETIME
            );

            CREATE INDEX IF NOT EXISTS idx_msg_pair ON messages(sender_id, recipient_id, id);
            CREATE INDEX IF NOT EXISTS idx_listings_status ON item_listings(status);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trade_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_offers_trade ON trade_offers(trade_id);
            CREATE INDEX IF NOT EXISTS idx_pubmsg_id ON public_messages(id);
            CREATE INDEX IF NOT EXISTS idx_blocks_world ON mining_blocks(world_id, generation);
            CREATE INDEX IF NOT EXISTS idx_bonlist_status ON bon_listings(status);
            CREATE INDEX IF NOT EXISTS idx_cashreq_status ON cash_requests(status);
        """)
        # Best-effort schema migrations on existing DBs
        for ddl in (
            "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN bon INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN satoshi INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE mining_user_stats ADD COLUMN bon_found INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                db.execute(ddl)
            except sqlite3.OperationalError:
                pass
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


def staff_required(f):
    """Allow admins OR managers (used for cash request panel)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not logged in"}), 401
        if not (session.get("is_admin") or session.get("is_manager")):
            return jsonify({"error": "Staff only"}), 403
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def _inject_role_flags():
    """Make is_manager available in every template (is_admin already passed
       explicitly by most pages, but expose both as a fallback)."""
    return {
        "is_manager": session.get("is_manager", False),
    }


# Conversion rates (changeable in one place)
BON_PER_SATOSHI = 100         # 100 BON = 1 satoshi (game-internal)
SATOSHI_PER_BTC = 100_000_000 # 1 BTC = 100,000,000 satoshi (real Bitcoin)

# Live BTC/USD price cache — refreshed via yfinance every BTC_PRICE_TTL seconds.
BTC_PRICE_TTL = 60
_BTC_PRICE_CACHE = {"price": None, "ts": 0.0}


def get_btc_price_usd():
    """Return the current BTC price in USD, cached for BTC_PRICE_TTL seconds.
    Falls back to the last known price if a fresh fetch fails. Returns None if
    no price has ever been successfully fetched."""
    import time as _time
    now = _time.time()
    if _BTC_PRICE_CACHE["price"] and (now - _BTC_PRICE_CACHE["ts"]) < BTC_PRICE_TTL:
        return _BTC_PRICE_CACHE["price"]
    try:
        t = yf.Ticker("BTC-USD")
        price = float(t.fast_info.last_price)
        if price > 0:
            _BTC_PRICE_CACHE["price"] = price
            _BTC_PRICE_CACHE["ts"] = now
            return price
    except Exception as e:
        logging.warning("BTC price fetch failed: %s", e)
    return _BTC_PRICE_CACHE["price"]


BON_DROP_RATE   = 100         # 1 in 100 mined blocks drops a BON


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
    session["is_manager"] = False
    session["is_banned"] = False
    return jsonify({"message": f"Welcome, {username}!", "is_admin": bool(is_admin)})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    user = db.execute(
        "SELECT id, password, is_admin, COALESCE(is_manager,0) AS is_manager, is_banned FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not user or not check_password_hash(user["password"], password):
        return jsonify({"error": "Invalid username or password"}), 401
    if user["is_banned"]:
        return jsonify({"error": "Your account has been banned"}), 403

    session["user_id"]  = user["id"]
    session["username"] = username
    session["is_admin"] = bool(user["is_admin"])
    session["is_manager"] = bool(user["is_manager"])
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
        "SELECT id, username, is_admin, is_banned, created, COALESCE(bio, '') AS bio FROM users WHERE id = ?",
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
        "bio":      user["bio"] or "",
        "trade_count":      trade_count,
        "item_trade_count": item_trade_count,
        "item_count":       item_count,
        "holdings_count":   holdings_count,
    })


@app.route("/api/account/bio", methods=["POST"])
@login_required
def update_bio():
    uid = current_user_id()
    data = request.get_json() or {}
    bio = (data.get("bio") or "").strip()
    if len(bio) > 500:
        return jsonify({"error": "Bio must be 500 characters or fewer"}), 400
    db = get_db()
    db.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, uid))
    db.commit()
    return jsonify({"message": "Bio saved", "bio": bio})


@app.route("/api/account/me")
@login_required
def my_account():
    db = get_db()
    row = db.execute(
        "SELECT id, username, COALESCE(bio, '') AS bio FROM users WHERE id = ?",
        (current_user_id(),),
    ).fetchone()
    return jsonify(dict(row))


# ── Direct message: delete ────────────────────────────────────────────────────

@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
@login_required
def delete_dm(msg_id):
    uid = current_user_id()
    db = get_db()
    row = db.execute(
        "SELECT id, sender_id FROM messages WHERE id = ?", (msg_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Message not found"}), 404
    if row["sender_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not allowed"}), 403
    db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    db.commit()
    return jsonify({"message": "Deleted"})


# ── Public chat ──────────────────────────────────────────────────────────────

PUBLIC_MSG_LIMIT = 100


def _is_muted(db, uid):
    row = db.execute("SELECT user_id FROM public_mutes WHERE user_id = ?", (uid,)).fetchone()
    return row is not None


@app.route("/api/public/messages")
@login_required
def public_messages_list():
    db = get_db()
    rows = db.execute(
        """SELECT m.id, m.sender_id, m.content, m.timestamp, u.username, u.is_admin
           FROM public_messages m
           JOIN users u ON u.id = m.sender_id
           ORDER BY m.id DESC LIMIT ?""",
        (PUBLIC_MSG_LIMIT,),
    ).fetchall()
    rows = list(reversed(rows))
    out = [{
        "id": r["id"], "sender_id": r["sender_id"], "username": r["username"],
        "is_admin": bool(r["is_admin"]),
        "content": r["content"], "timestamp": r["timestamp"],
    } for r in rows]
    muted = _is_muted(db, current_user_id())
    return jsonify({"messages": out, "muted": muted})


@app.route("/api/public/send", methods=["POST"])
@login_required
def public_send():
    uid = current_user_id()
    db = get_db()
    if _is_muted(db, uid):
        return jsonify({"error": "You are muted from public chat"}), 403
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Message cannot be empty"}), 400
    if len(content) > 500:
        return jsonify({"error": "Message too long (max 500)"}), 400
    db.execute(
        "INSERT INTO public_messages (sender_id, content) VALUES (?, ?)", (uid, content)
    )
    db.commit()
    return jsonify({"message": "Sent"})


@app.route("/api/public/<int:msg_id>", methods=["DELETE"])
@login_required
def public_delete(msg_id):
    uid = current_user_id()
    db = get_db()
    row = db.execute(
        "SELECT id, sender_id FROM public_messages WHERE id = ?", (msg_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Message not found"}), 404
    if row["sender_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not allowed"}), 403
    db.execute("DELETE FROM public_messages WHERE id = ?", (msg_id,))
    db.commit()
    return jsonify({"message": "Deleted"})


@app.route("/api/admin/public/mute", methods=["POST"])
@admin_required
def admin_mute():
    data = request.get_json() or {}
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400
    mute = bool(data.get("muted"))
    db = get_db()
    row = db.execute("SELECT id, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if row["is_admin"] and mute:
        return jsonify({"error": "Cannot mute an admin"}), 400
    if mute:
        db.execute(
            "INSERT OR REPLACE INTO public_mutes (user_id, muted_by) VALUES (?, ?)",
            (user_id, current_user_id()),
        )
    else:
        db.execute("DELETE FROM public_mutes WHERE user_id = ?", (user_id,))
    db.commit()
    return jsonify({"message": "Muted" if mute else "Unmuted"})


@app.route("/api/admin/public/mutes")
@admin_required
def admin_mutes_list():
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.username, m.muted_at
           FROM public_mutes m JOIN users u ON u.id = m.user_id
           ORDER BY m.muted_at DESC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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
        """SELECT u.id, u.username, u.is_admin,
                  COALESCE(u.is_manager, 0) AS is_manager,
                  u.is_banned, u.created,
                  COALESCE(p.cash, 0) as cash,
                  COALESCE(u.bon, 0)     AS bon,
                  COALESCE(u.satoshi, 0) AS satoshi
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
            "is_manager": bool(r["is_manager"]),
            "is_banned": bool(r["is_banned"]),
            "created":  r["created"],
            "cash":     round(r["cash"], 2),
            "bon":      int(r["bon"]),
            "satoshi":  int(r["satoshi"]),
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


@app.route("/api/admin/grant-admin", methods=["POST"])
@admin_required
def admin_grant_admin():
    data = request.get_json() or {}
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400
    make_admin = 1 if data.get("admin") else 0

    db = get_db()
    row = db.execute("SELECT id, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if user_id == current_user_id() and not make_admin:
        # Safety: don't let an admin demote themselves if they are the last admin
        admin_count = db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
        if admin_count <= 1:
            return jsonify({"error": "Cannot remove the last admin"}), 400

    db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (make_admin, user_id))
    db.commit()
    if user_id == current_user_id():
        session["is_admin"] = bool(make_admin)
    return jsonify({"message": ("Granted admin" if make_admin else "Revoked admin")})


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


# ── Mining game (BON) ────────────────────────────────────────────────────────

# Layer color palette — cycled when player goes deeper.
LAYER_COLORS = [
    {"name": "Stone",   "color": "#6b7280"},
    {"name": "Dirt",    "color": "#92400e"},
    {"name": "Coal",    "color": "#1f2937"},
    {"name": "Copper",  "color": "#c2410c"},
    {"name": "Iron",    "color": "#cbd5e1"},
    {"name": "Gold",    "color": "#fbbf24"},
    {"name": "Emerald", "color": "#10b981"},
    {"name": "Diamond", "color": "#67e8f9"},
    {"name": "Ruby",    "color": "#ef4444"},
    {"name": "Obsidian","color": "#0f0820"},
]


def _layer_info(layer):
    return LAYER_COLORS[layer % len(LAYER_COLORS)]


def _generate_blocks(db, world_id, generation, width, height):
    rows = [(world_id, generation, x, y) for x in range(width) for y in range(height)]
    db.executemany(
        "INSERT OR IGNORE INTO mining_blocks (world_id, generation, x, y) VALUES (?, ?, ?, ?)",
        rows,
    )


def _ensure_my_world(db, uid):
    """Make sure the user has at least one world; auto-create their personal one."""
    row = db.execute(
        "SELECT id FROM mining_worlds WHERE owner_id = ? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()
    if row:
        return row["id"]
    user = db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    name = f"{user['username'] if user else 'Player'}'s World"
    cur = db.execute(
        "INSERT INTO mining_worlds (owner_id, name, width, height, layer, generation) VALUES (?, ?, 10, 10, 0, 0)",
        (uid, name),
    )
    world_id = cur.lastrowid
    db.execute(
        "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
        (world_id, uid),
    )
    _generate_blocks(db, world_id, 0, 10, 10)
    db.commit()
    return world_id


def _world_state(db, world_id):
    w = db.execute("SELECT * FROM mining_worlds WHERE id = ?", (world_id,)).fetchone()
    if not w:
        return None
    blocks = db.execute(
        """SELECT x, y, mined_by, mined_at FROM mining_blocks
           WHERE world_id = ? AND generation = ?""",
        (world_id, w["generation"]),
    ).fetchall()
    members = db.execute(
        """SELECT u.id, u.username FROM mining_world_members m
           JOIN users u ON u.id = m.user_id
           WHERE m.world_id = ? ORDER BY m.joined_at""",
        (world_id,),
    ).fetchall()
    owner = db.execute(
        "SELECT id, username FROM users WHERE id = ?", (w["owner_id"],)
    ).fetchone()
    info = _layer_info(w["layer"])
    total = w["width"] * w["height"]
    mined_count = sum(1 for b in blocks if b["mined_by"])
    return {
        "id": w["id"],
        "name": w["name"],
        "owner": {"id": owner["id"], "username": owner["username"]} if owner else None,
        "width": w["width"],
        "height": w["height"],
        "layer": w["layer"],
        "generation": w["generation"],
        "layer_name": info["name"],
        "color": info["color"],
        "blocks_total": total,
        "blocks_mined": mined_count,
        "blocks_remaining": total - mined_count,
        "blocks": [
            {"x": b["x"], "y": b["y"], "mined": bool(b["mined_by"])}
            for b in blocks
        ],
        "members": [{"id": m["id"], "username": m["username"]} for m in members],
    }


@app.route("/mine")
@login_required
def mine_page():
    return render_template(
        "mine.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/api/mining/my-world")
@login_required
def mining_my_world():
    uid = current_user_id()
    db = get_db()
    world_id = _ensure_my_world(db, uid)
    return jsonify(_world_state(db, world_id))


@app.route("/api/mining/world/<int:world_id>")
@login_required
def mining_world_get(world_id):
    db = get_db()
    state = _world_state(db, world_id)
    if not state:
        return jsonify({"error": "World not found"}), 404
    # auto-join if requested via ?join=1
    if request.args.get("join") == "1":
        uid = current_user_id()
        db.execute(
            "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
            (world_id, uid),
        )
        db.commit()
        state = _world_state(db, world_id)
    return jsonify(state)


@app.route("/api/mining/worlds")
@login_required
def mining_worlds_list():
    """All worlds the user can join (public list)."""
    db = get_db()
    rows = db.execute(
        """SELECT w.id, w.name, w.layer, w.width, w.height, u.username AS owner_username,
                  (SELECT COUNT(*) FROM mining_world_members mm WHERE mm.world_id = w.id) AS members
           FROM mining_worlds w JOIN users u ON u.id = w.owner_id
           ORDER BY members DESC, w.id DESC LIMIT 50"""
    ).fetchall()
    out = []
    for r in rows:
        info = _layer_info(r["layer"])
        out.append({
            "id": r["id"], "name": r["name"], "layer": r["layer"],
            "layer_name": info["name"], "color": info["color"],
            "owner_username": r["owner_username"], "members": r["members"],
        })
    return jsonify(out)


@app.route("/api/mining/mine", methods=["POST"])
@login_required
def mining_mine():
    uid = current_user_id()
    data = request.get_json() or {}
    try:
        world_id = int(data.get("world_id"))
        x = int(data.get("x"))
        y = int(data.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400

    db = get_db()
    w = db.execute("SELECT * FROM mining_worlds WHERE id = ?", (world_id,)).fetchone()
    if not w:
        return jsonify({"error": "World not found"}), 404
    if not (0 <= x < w["width"] and 0 <= y < w["height"]):
        return jsonify({"error": "Out of bounds"}), 400

    # Auto-join the world (no permissions for now)
    db.execute(
        "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
        (world_id, uid),
    )

    # Try to mine the block (only if not already mined this generation)
    cur = db.execute(
        """UPDATE mining_blocks
           SET mined_by = ?, mined_at = CURRENT_TIMESTAMP
           WHERE world_id = ? AND generation = ? AND x = ? AND y = ? AND mined_by IS NULL""",
        (uid, world_id, w["generation"], x, y),
    )
    if cur.rowcount == 0:
        return jsonify({"error": "Block already mined", "state": _world_state(db, world_id)}), 200

    # Track user stats
    db.execute(
        """INSERT INTO mining_user_stats (user_id, blocks_mined, layers_cleared, bon_found)
           VALUES (?, 1, 0, 0)
           ON CONFLICT(user_id) DO UPDATE SET blocks_mined = blocks_mined + 1""",
        (uid,),
    )

    # 1-in-BON_DROP_RATE chance to drop a BON token
    bon_dropped = (random.randint(1, BON_DROP_RATE) == 1)
    if bon_dropped:
        db.execute("UPDATE users SET bon = COALESCE(bon,0) + 1 WHERE id = ?", (uid,))
        db.execute(
            "UPDATE mining_user_stats SET bon_found = bon_found + 1 WHERE user_id = ?",
            (uid,),
        )

    # Check if layer is fully mined
    remaining = db.execute(
        """SELECT COUNT(*) AS n FROM mining_blocks
           WHERE world_id = ? AND generation = ? AND mined_by IS NULL""",
        (world_id, w["generation"]),
    ).fetchone()["n"]

    layer_cleared = False
    if remaining == 0:
        new_gen = w["generation"] + 1
        new_layer = w["layer"] + 1
        db.execute(
            "UPDATE mining_worlds SET layer = ?, generation = ? WHERE id = ?",
            (new_layer, new_gen, world_id),
        )
        _generate_blocks(db, world_id, new_gen, w["width"], w["height"])
        db.execute(
            "UPDATE mining_user_stats SET layers_cleared = layers_cleared + 1 WHERE user_id = ?",
            (uid,),
        )
        layer_cleared = True

    db.commit()
    return jsonify({
        "ok": True,
        "layer_cleared": layer_cleared,
        "bon_dropped": bon_dropped,
        "state": _world_state(db, world_id),
    })


@app.route("/api/mining/leave", methods=["POST"])
@login_required
def mining_leave():
    uid = current_user_id()
    data = request.get_json() or {}
    try:
        world_id = int(data.get("world_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid world"}), 400
    db = get_db()
    w = db.execute("SELECT owner_id FROM mining_worlds WHERE id = ?", (world_id,)).fetchone()
    if not w:
        return jsonify({"error": "World not found"}), 404
    if w["owner_id"] == uid:
        return jsonify({"error": "Owners cannot leave their own world"}), 400
    db.execute(
        "DELETE FROM mining_world_members WHERE world_id = ? AND user_id = ?",
        (world_id, uid),
    )
    db.commit()
    return jsonify({"message": "Left the world"})


# ── Wallet / BON / Satoshi ────────────────────────────────────────────────────

def _wallet_state(db, uid):
    row = db.execute(
        """SELECT u.username,
                  COALESCE(u.bon, 0)     AS bon,
                  COALESCE(u.satoshi, 0) AS satoshi,
                  COALESCE(p.cash, 0)    AS cash
           FROM users u
           LEFT JOIN portfolios p ON p.user_id = u.id
           WHERE u.id = ?""",
        (uid,),
    ).fetchone()
    btc_price = get_btc_price_usd()
    usd_per_satoshi = (btc_price / SATOSHI_PER_BTC) if btc_price else None
    sat_value_usd = float(row["satoshi"]) * usd_per_satoshi if usd_per_satoshi else None
    return {
        "user_id": uid,
        "username": row["username"],
        "bon": int(row["bon"]),
        "satoshi": int(row["satoshi"]),
        "cash": float(row["cash"]),
        "satoshi_value_usd": sat_value_usd,
        "rates": {
            "bon_per_satoshi": BON_PER_SATOSHI,
            "satoshi_per_btc": SATOSHI_PER_BTC,
            "btc_price_usd": btc_price,
            "usd_per_satoshi": usd_per_satoshi,
        },
    }


@app.route("/api/wallet")
@login_required
def wallet_get():
    return jsonify(_wallet_state(get_db(), current_user_id()))


@app.route("/api/wallet/convert", methods=["POST"])
@login_required
def wallet_convert():
    """Instant conversions:
       bon_to_satoshi: amount in BON  → satoshi (100 BON = 1 sat)
       satoshi_to_bon: amount in sats → BON
       satoshi_to_usd: amount in sats → cash  (100 sat = $1)
       usd_to_satoshi: amount in USD  → satoshi
    """
    uid = current_user_id()
    data = request.get_json() or {}
    direction = (data.get("direction") or "").strip()
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    db = get_db()
    w = db.execute(
        "SELECT COALESCE(bon,0) AS bon, COALESCE(satoshi,0) AS satoshi FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    cash = db.execute(
        "SELECT COALESCE(cash,0) AS cash FROM portfolios WHERE user_id = ?", (uid,)
    ).fetchone()
    bon = int(w["bon"])
    sat = int(w["satoshi"])
    usd = float(cash["cash"]) if cash else 0.0

    if direction == "bon_to_satoshi":
        bon_in = int(amount)
        if bon_in <= 0 or bon_in % BON_PER_SATOSHI != 0:
            return jsonify({"error": f"Must be a positive multiple of {BON_PER_SATOSHI} BON"}), 400
        if bon_in > bon:
            return jsonify({"error": "Not enough BON"}), 400
        gained = bon_in // BON_PER_SATOSHI
        db.execute("UPDATE users SET bon = bon - ?, satoshi = satoshi + ? WHERE id = ?",
                   (bon_in, gained, uid))
        msg = f"Converted {bon_in} BON → {gained} satoshi"

    elif direction == "satoshi_to_bon":
        sat_in = int(amount)
        if sat_in <= 0:
            return jsonify({"error": "Must be a positive number of satoshi"}), 400
        if sat_in > sat:
            return jsonify({"error": "Not enough satoshi"}), 400
        gained = sat_in * BON_PER_SATOSHI
        db.execute("UPDATE users SET satoshi = satoshi - ?, bon = bon + ? WHERE id = ?",
                   (sat_in, gained, uid))
        msg = f"Converted {sat_in} satoshi → {gained} BON"

    elif direction == "satoshi_to_usd":
        sat_in = int(amount)
        if sat_in <= 0:
            return jsonify({"error": "Must be a positive number of satoshi"}), 400
        if sat_in > sat:
            return jsonify({"error": "Not enough satoshi"}), 400
        btc_price = get_btc_price_usd()
        if not btc_price:
            return jsonify({"error": "Live BTC price unavailable, try again shortly"}), 503
        # USD value at current BTC price, rounded down to the nearest cent
        gained_usd = int(sat_in * btc_price * 100 / SATOSHI_PER_BTC) / 100.0
        if gained_usd <= 0:
            min_sat = int(SATOSHI_PER_BTC / (btc_price * 100)) + 1
            return jsonify({"error": f"Too small to be worth $0.01 — need at least {min_sat} sat"}), 400
        db.execute("UPDATE users SET satoshi = satoshi - ? WHERE id = ?", (sat_in, uid))
        if not cash:
            db.execute("INSERT INTO portfolios (user_id, cash) VALUES (?, 0)", (uid,))
        db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id = ?", (gained_usd, uid))
        msg = f"Converted {sat_in:,} satoshi → ${gained_usd:.2f} (BTC ${btc_price:,.2f})"

    elif direction == "usd_to_satoshi":
        usd_in = round(float(amount), 2)
        if usd_in <= 0:
            return jsonify({"error": "Amount must be positive"}), 400
        btc_price = get_btc_price_usd()
        if not btc_price:
            return jsonify({"error": "Live BTC price unavailable, try again shortly"}), 503
        # satoshi at current BTC price, rounded down to whole satoshi
        sats_gained = int(usd_in * SATOSHI_PER_BTC / btc_price)
        if sats_gained <= 0:
            return jsonify({"error": "Amount too small to buy 1 satoshi"}), 400
        if usd_in > usd + 1e-9:
            return jsonify({"error": "Not enough cash"}), 400
        db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (usd_in, uid))
        db.execute("UPDATE users SET satoshi = satoshi + ? WHERE id = ?", (sats_gained, uid))
        msg = f"Converted ${usd_in:.2f} → {sats_gained:,} satoshi (BTC ${btc_price:,.2f})"

    else:
        return jsonify({"error": "Unknown direction"}), 400

    db.commit()
    return jsonify({"message": msg, "wallet": _wallet_state(db, uid)})


# ── BON marketplace (sellers list BON for USD; anyone can buy) ────────────────

@app.route("/api/bon/listings")
@login_required
def bon_listings_list():
    db = get_db()
    rows = db.execute(
        """SELECT bl.id, bl.seller_id, u.username AS seller_username,
                  bl.quantity, bl.price, bl.created_at
           FROM bon_listings bl
           JOIN users u ON u.id = bl.seller_id
           WHERE bl.status = 'OPEN'
           ORDER BY (bl.price / bl.quantity) ASC, bl.id DESC
           LIMIT 100"""
    ).fetchall()
    return jsonify([{
        "id": r["id"],
        "seller_id": r["seller_id"],
        "seller_username": r["seller_username"],
        "quantity": int(r["quantity"]),
        "price": float(r["price"]),
        "price_per_bon": float(r["price"]) / int(r["quantity"]) if r["quantity"] else 0,
        "is_mine": r["seller_id"] == current_user_id(),
        "created_at": r["created_at"],
    } for r in rows])


@app.route("/api/bon/listings/create", methods=["POST"])
@login_required
def bon_listings_create():
    uid = current_user_id()
    data = request.get_json() or {}
    try:
        qty = int(data.get("quantity"))
        price = round(float(data.get("price")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid quantity or price"}), 400
    if qty <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400
    if price <= 0:
        return jsonify({"error": "Price must be positive"}), 400

    db = get_db()
    have = db.execute("SELECT COALESCE(bon,0) AS b FROM users WHERE id = ?", (uid,)).fetchone()
    if int(have["b"]) < qty:
        return jsonify({"error": "Not enough BON"}), 400

    # Escrow: deduct BON now, return on cancel
    db.execute("UPDATE users SET bon = bon - ? WHERE id = ?", (qty, uid))
    db.execute(
        "INSERT INTO bon_listings (seller_id, quantity, price) VALUES (?, ?, ?)",
        (uid, qty, price),
    )
    db.commit()
    return jsonify({"message": f"Listed {qty} BON for ${price:.2f}"})


@app.route("/api/bon/listings/<int:lid>/cancel", methods=["POST"])
@login_required
def bon_listings_cancel(lid):
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM bon_listings WHERE id = ?", (lid,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing is not open"}), 400
    if row["seller_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not your listing"}), 403
    # Refund BON
    db.execute("UPDATE users SET bon = bon + ? WHERE id = ?", (row["quantity"], row["seller_id"]))
    db.execute("UPDATE bon_listings SET status = 'CANCELLED' WHERE id = ?", (lid,))
    db.commit()
    return jsonify({"message": "Listing cancelled and BON refunded"})


@app.route("/api/bon/listings/<int:lid>/buy", methods=["POST"])
@login_required
def bon_listings_buy(lid):
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM bon_listings WHERE id = ?", (lid,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing is no longer available"}), 400
    if row["seller_id"] == uid:
        return jsonify({"error": "Cannot buy your own listing"}), 400
    price = float(row["price"])
    qty = int(row["quantity"])
    cash = db.execute(
        "SELECT COALESCE(cash,0) AS c FROM portfolios WHERE user_id = ?", (uid,)
    ).fetchone()
    have = float(cash["c"]) if cash else 0.0
    if have + 1e-9 < price:
        return jsonify({"error": f"Need ${price:.2f}, you have ${have:.2f}"}), 400
    if not cash:
        db.execute("INSERT INTO portfolios (user_id, cash) VALUES (?, 0)", (uid,))
    # Pay seller, credit buyer
    db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (price, uid))
    db.execute("INSERT OR IGNORE INTO portfolios (user_id, cash) VALUES (?, 0)", (row["seller_id"],))
    db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id = ?", (price, row["seller_id"]))
    db.execute("UPDATE users SET bon = COALESCE(bon,0) + ? WHERE id = ?", (qty, uid))
    db.execute(
        "UPDATE bon_listings SET status='SOLD', buyer_id=?, sold_at=CURRENT_TIMESTAMP WHERE id=?",
        (uid, lid),
    )
    db.commit()
    return jsonify({"message": f"Bought {qty} BON for ${price:.2f}"})


# ── Cash in / Cash out (private requests visible only to admin/manager) ───────

ALLOWED_KINDS = {"cash_in", "cash_out"}
ALLOWED_CURRENCIES = {"satoshi", "usd"}


@app.route("/api/cash/request", methods=["POST"])
@login_required
def cash_request_create():
    uid = current_user_id()
    data = request.get_json() or {}
    kind = (data.get("kind") or "").strip()
    currency = (data.get("currency") or "").strip()
    credentials = (data.get("credentials") or "").strip()
    note = (data.get("note") or "").strip()[:500]
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400

    if kind not in ALLOWED_KINDS:
        return jsonify({"error": "kind must be cash_in or cash_out"}), 400
    if currency not in ALLOWED_CURRENCIES:
        return jsonify({"error": "currency must be satoshi or usd"}), 400
    # Withdrawals must be in satoshi
    if kind == "cash_out" and currency != "satoshi":
        return jsonify({"error": "You can only withdraw satoshi"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400
    if not credentials or len(credentials) < 4:
        return jsonify({"error": "Please provide payout credentials (wallet/bank/etc.)"}), 400
    if len(credentials) > 1000:
        return jsonify({"error": "Credentials too long"}), 400

    db = get_db()

    # Cash-out: escrow user's funds immediately so they can't double-spend
    if kind == "cash_out":
        sat_amount = int(amount)
        if sat_amount <= 0:
            return jsonify({"error": "Withdraw must be a whole number of satoshi"}), 400
        row = db.execute("SELECT COALESCE(satoshi,0) AS s FROM users WHERE id = ?", (uid,)).fetchone()
        if int(row["s"]) < sat_amount:
            return jsonify({"error": "Not enough satoshi"}), 400
        db.execute("UPDATE users SET satoshi = satoshi - ? WHERE id = ?", (sat_amount, uid))
        amount = sat_amount

    db.execute(
        """INSERT INTO cash_requests (user_id, kind, currency, amount, credentials, note)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (uid, kind, currency, amount, credentials, note),
    )
    db.commit()
    return jsonify({"message": "Request submitted. Staff will process it shortly."})


@app.route("/api/cash/my-requests")
@login_required
def cash_my_requests():
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        """SELECT id, kind, currency, amount, status, created_at, handled_at
           FROM cash_requests
           WHERE user_id = ?
           ORDER BY id DESC LIMIT 50""",
        (uid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/cash/requests")
@staff_required
def admin_cash_requests_list():
    db = get_db()
    rows = db.execute(
        """SELECT cr.id, cr.user_id, u.username, cr.kind, cr.currency, cr.amount,
                  cr.credentials, cr.note, cr.status, cr.created_at,
                  cr.handled_by, cr.handled_at,
                  h.username AS handled_by_username
           FROM cash_requests cr
           JOIN users u ON u.id = cr.user_id
           LEFT JOIN users h ON h.id = cr.handled_by
           WHERE cr.status = 'pending'
           ORDER BY cr.id ASC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/cash/requests/<int:rid>/done", methods=["POST"])
@staff_required
def admin_cash_request_done(rid):
    """Mark a request as done. For cash_in requests, credits the user with
       the amount they asked for (admin processed payment externally).
       For cash_out, no balance change (already escrowed at request time)."""
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM cash_requests WHERE id = ?", (rid,)).fetchone()
    if not row:
        return jsonify({"error": "Request not found"}), 404
    if row["status"] != "pending":
        return jsonify({"error": "Request already handled"}), 400

    if row["kind"] == "cash_in":
        if row["currency"] == "satoshi":
            db.execute("UPDATE users SET satoshi = COALESCE(satoshi,0) + ? WHERE id = ?",
                       (int(row["amount"]), row["user_id"]))
        else:
            db.execute(
                "INSERT OR IGNORE INTO portfolios (user_id, cash) VALUES (?, 0)",
                (row["user_id"],),
            )
            db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id = ?",
                       (float(row["amount"]), row["user_id"]))
    db.execute(
        "UPDATE cash_requests SET status='done', handled_by=?, handled_at=CURRENT_TIMESTAMP WHERE id=?",
        (uid, rid),
    )
    db.commit()
    return jsonify({"message": "Marked done"})


@app.route("/api/admin/cash/requests/<int:rid>/reject", methods=["POST"])
@staff_required
def admin_cash_request_reject(rid):
    """Reject a pending request. For cash_out, refunds escrowed satoshi.
       For cash_in, no balance change."""
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM cash_requests WHERE id = ?", (rid,)).fetchone()
    if not row:
        return jsonify({"error": "Request not found"}), 404
    if row["status"] != "pending":
        return jsonify({"error": "Request already handled"}), 400
    if row["kind"] == "cash_out":
        db.execute("UPDATE users SET satoshi = COALESCE(satoshi,0) + ? WHERE id = ?",
                   (int(row["amount"]), row["user_id"]))
    db.execute(
        "UPDATE cash_requests SET status='rejected', handled_by=?, handled_at=CURRENT_TIMESTAMP WHERE id=?",
        (uid, rid),
    )
    db.commit()
    return jsonify({"message": "Request rejected" + (" and refunded" if row["kind"] == "cash_out" else "")})


# ── Manager role grant (admin only) ───────────────────────────────────────────

@app.route("/api/admin/grant-manager", methods=["POST"])
@admin_required
def admin_grant_manager():
    data = request.get_json() or {}
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400
    make = bool(data.get("manager"))
    db = get_db()
    row = db.execute(
        "SELECT id, COALESCE(is_manager,0) AS is_manager FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    db.execute("UPDATE users SET is_manager = ? WHERE id = ?", (1 if make else 0, user_id))
    db.commit()
    if user_id == current_user_id():
        session["is_manager"] = bool(make)
    return jsonify({"message": "Granted manager" if make else "Revoked manager"})


# ── Wallet page ───────────────────────────────────────────────────────────────

@app.route("/wallet")
@login_required
def wallet_page():
    return render_template(
        "wallet.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


@app.route("/cash-requests")
@login_required
def cash_requests_page():
    if not (session.get("is_admin") or session.get("is_manager")):
        return redirect(url_for("index"))
    return render_template(
        "cash_requests.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
        is_manager=session.get("is_manager", False),
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
