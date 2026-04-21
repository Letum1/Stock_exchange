import os
import sqlite3
from functools import wraps

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
    return render_template("admin.html", username=session.get("username"))


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

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


# ── Admin API ─────────────────────────────────────────────────────────────────

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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
