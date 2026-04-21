import sqlite3
import yfinance as yf
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)
DB_PATH = "portfolio.db"
STARTING_CASH = 100_000.0


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
        db.execute(
            """CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY,
                cash REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS holdings (
                ticker TEXT PRIMARY KEY,
                shares REAL NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                total REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        row = db.execute("SELECT id FROM portfolio WHERE id = 1").fetchone()
        if row is None:
            db.execute("INSERT INTO portfolio (id, cash) VALUES (1, ?)", (STARTING_CASH,))
        db.commit()


def get_price(ticker):
    t = yf.Ticker(ticker)
    hist = t.history(period="1d")
    if hist.empty:
        return None
    return round(float(hist["Close"].iloc[-1]), 2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/portfolio")
def portfolio():
    db = get_db()
    cash = db.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()["cash"]
    holdings_rows = db.execute("SELECT ticker, shares FROM holdings").fetchall()

    holdings = []
    total_value = cash
    for row in holdings_rows:
        ticker = row["ticker"]
        shares = row["shares"]
        price = get_price(ticker)
        if price is None:
            price = 0.0
        value = round(shares * price, 2)
        total_value += value
        holdings.append({
            "ticker": ticker,
            "shares": shares,
            "price": price,
            "value": value,
        })

    return jsonify({
        "cash": round(cash, 2),
        "holdings": holdings,
        "total_value": round(total_value, 2),
        "pnl": round(total_value - STARTING_CASH, 2),
    })


@app.route("/api/quote/<ticker>")
def quote(ticker):
    price = get_price(ticker.upper())
    if price is None:
        return jsonify({"error": "Ticker not found or no data"}), 404
    return jsonify({"ticker": ticker.upper(), "price": price})


@app.route("/api/buy", methods=["POST"])
def buy():
    data = request.get_json()
    ticker = data.get("ticker", "").upper().strip()
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
    db = get_db()
    cash = db.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()["cash"]

    if total > cash:
        return jsonify({"error": f"Insufficient funds. Need ${total:.2f}, have ${cash:.2f}"}), 400

    new_cash = round(cash - total, 2)
    db.execute("UPDATE portfolio SET cash = ? WHERE id = 1", (new_cash,))

    existing = db.execute("SELECT shares FROM holdings WHERE ticker = ?", (ticker,)).fetchone()
    if existing:
        new_shares = existing["shares"] + shares
        db.execute("UPDATE holdings SET shares = ? WHERE ticker = ?", (new_shares, ticker))
    else:
        db.execute("INSERT INTO holdings (ticker, shares) VALUES (?, ?)", (ticker, shares))

    db.execute(
        "INSERT INTO trades (ticker, action, shares, price, total) VALUES (?, 'BUY', ?, ?, ?)",
        (ticker, shares, price, total),
    )
    db.commit()

    return jsonify({
        "message": f"Bought {shares} shares of {ticker} at ${price:.2f}",
        "total_spent": total,
        "cash_remaining": new_cash,
    })


@app.route("/api/sell", methods=["POST"])
def sell():
    data = request.get_json()
    ticker = data.get("ticker", "").upper().strip()
    try:
        shares = float(data.get("shares", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid shares"}), 400

    if not ticker or shares <= 0:
        return jsonify({"error": "Invalid ticker or shares"}), 400

    db = get_db()
    existing = db.execute("SELECT shares FROM holdings WHERE ticker = ?", (ticker,)).fetchone()
    if not existing or existing["shares"] < shares:
        held = existing["shares"] if existing else 0
        return jsonify({"error": f"Not enough shares. Holding {held:.4f} shares of {ticker}"}), 400

    price = get_price(ticker)
    if price is None:
        return jsonify({"error": "Ticker not found"}), 404

    total = round(shares * price, 2)
    cash = db.execute("SELECT cash FROM portfolio WHERE id = 1").fetchone()["cash"]
    new_cash = round(cash + total, 2)

    db.execute("UPDATE portfolio SET cash = ? WHERE id = 1", (new_cash,))

    new_shares = existing["shares"] - shares
    if new_shares < 1e-9:
        db.execute("DELETE FROM holdings WHERE ticker = ?", (ticker,))
    else:
        db.execute("UPDATE holdings SET shares = ? WHERE ticker = ?", (new_shares, ticker))

    db.execute(
        "INSERT INTO trades (ticker, action, shares, price, total) VALUES (?, 'SELL', ?, ?, ?)",
        (ticker, shares, price, total),
    )
    db.commit()

    return jsonify({
        "message": f"Sold {shares} shares of {ticker} at ${price:.2f}",
        "total_received": total,
        "cash_remaining": new_cash,
    })


@app.route("/api/trades")
def trades():
    db = get_db()
    rows = db.execute(
        "SELECT ticker, action, shares, price, total, timestamp FROM trades ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reset", methods=["POST"])
def reset():
    db = get_db()
    db.execute("UPDATE portfolio SET cash = ? WHERE id = 1", (STARTING_CASH,))
    db.execute("DELETE FROM holdings")
    db.execute("DELETE FROM trades")
    db.commit()
    return jsonify({"message": "Portfolio reset to $100,000"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
