"""
Mining Game Module - BON Currency Earning
Allows users to mine BON tokens through a simple clicker game
"""

import sqlite3
from datetime import datetime, timedelta
from flask import jsonify, request, session
from functools import wraps

MINING_REWARD = 1.0  # BON per click
MINING_ENERGY_MAX = 20
MINING_ENERGY_REGEN = 1  # per second
MINING_BOOST_DURATION = 300  # 5 minutes

def init_mining_db():
    """Add mining tables to database"""
    DB_PATH = "portfolio.db"
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS mining_stats (
                user_id          INTEGER PRIMARY KEY REFERENCES users(id),
                total_bon_earned REAL NOT NULL DEFAULT 0,
                total_clicks     INTEGER NOT NULL DEFAULT 0,
                current_energy   INTEGER NOT NULL DEFAULT 20,
                last_energy_tick DATETIME DEFAULT CURRENT_TIMESTAMP,
                boost_active     INTEGER DEFAULT 0,
                boost_multiplier REAL DEFAULT 1.0,
                boost_expires_at DATETIME,
                last_click_at    DATETIME,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bon_wallet (
                user_id  INTEGER PRIMARY KEY REFERENCES users(id),
                bon      REAL NOT NULL DEFAULT 0,
                locked   REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS mining_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                bon_earned  REAL NOT NULL,
                multiplier  REAL DEFAULT 1.0,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mining_boosts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                description  TEXT,
                emoji        TEXT DEFAULT '⚡',
                cost_bon     REAL NOT NULL,
                duration     INTEGER NOT NULL,
                multiplier   REAL NOT NULL DEFAULT 2.0,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_mining_user ON mining_stats(user_id);
            CREATE INDEX IF NOT EXISTS idx_history_user ON mining_history(user_id);
        """)
        db.commit()

        # Add default mining boosts if not exist
        db.executescript("""
            INSERT OR IGNORE INTO mining_boosts (name, description, emoji, cost_bon, duration, multiplier)
            VALUES
                ('Golden Pickaxe', 'Double your mining speed!', '⛏️', 50.0, 300, 2.0),
                ('Mega Boost', '3x mining for 5 minutes', '🚀', 100.0, 300, 3.0),
                ('Ultra Power', 'Max energy boost!', '💪', 30.0, 180, 1.0);
        """)
        db.commit()


def get_db():
    """Get database connection (Flask context)"""
    from flask import g
    if "db" not in g:
        g.db = sqlite3.connect("portfolio.db")
        g.db.row_factory = sqlite3.Row
    return g.db


def create_user_mining_account(user_id):
    """Initialize mining accounts for new users"""
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO mining_stats (user_id) VALUES (?)",
        (user_id,)
    )
    db.execute(
        "INSERT OR IGNORE INTO bon_wallet (user_id, bon) VALUES (?, ?)",
        (user_id, 0.0)
    )
    db.commit()


def update_energy(user_id):
    """Regenerate mining energy based on time elapsed"""
    db = get_db()
    stats = db.execute(
        "SELECT current_energy, last_energy_tick FROM mining_stats WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not stats:
        return

    try:
        last_tick = datetime.fromisoformat(stats["last_energy_tick"].replace(" ", "T"))
    except (TypeError, ValueError):
        last_tick = datetime.utcnow()

    elapsed = (datetime.utcnow() - last_tick).total_seconds()
    energy_regen = int(elapsed * MINING_ENERGY_REGEN)

    new_energy = min(
        MINING_ENERGY_MAX,
        stats["current_energy"] + energy_regen
    )

    db.execute(
        "UPDATE mining_stats SET current_energy = ?, last_energy_tick = CURRENT_TIMESTAMP WHERE user_id = ?",
        (new_energy, user_id)
    )
    db.commit()


def get_mining_state(user_id):
    """Get current mining state for user"""
    db = get_db()
    update_energy(user_id)

    stats = db.execute(
        "SELECT * FROM mining_stats WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    wallet = db.execute(
        "SELECT bon, locked FROM bon_wallet WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    # Check if boost expired
    if stats and stats["boost_active"]:
        try:
            boost_expires = datetime.fromisoformat(
                stats["boost_expires_at"].replace(" ", "T")
            )
            if datetime.utcnow() > boost_expires:
                db.execute(
                    "UPDATE mining_stats SET boost_active = 0, boost_multiplier = 1.0 WHERE user_id = ?",
                    (user_id,)
                )
                db.commit()
                stats = db.execute(
                    "SELECT * FROM mining_stats WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
        except (TypeError, ValueError):
            pass

    return {
        "energy": stats["current_energy"] if stats else 0,
        "energy_max": MINING_ENERGY_MAX,
        "bon_earned": stats["total_bon_earned"] if stats else 0,
        "clicks": stats["total_clicks"] if stats else 0,
        "bon_balance": round(wallet["bon"], 2) if wallet else 0,
        "bon_locked": round(wallet["locked"], 2) if wallet else 0,
        "boost_active": bool(stats["boost_active"]) if stats else False,
        "boost_multiplier": stats["boost_multiplier"] if stats else 1.0,
    }


def mine_click(user_id):
    """Process a mining click"""
    db = get_db()
    update_energy(user_id)

    stats = db.execute(
        "SELECT * FROM mining_stats WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not stats:
        return {"error": "Mining account not initialized"}, 400

    if stats["current_energy"] < 1:
        return {"error": "Not enough energy!"}, 400

    # Calculate BON earned with multiplier
    multiplier = stats["boost_multiplier"]
    bon_earned = round(MINING_REWARD * multiplier, 2)

    # Update stats
    db.execute(
        """UPDATE mining_stats
           SET current_energy = current_energy - 1,
               total_bon_earned = total_bon_earned + ?,
               total_clicks = total_clicks + 1,
               last_click_at = CURRENT_TIMESTAMP
           WHERE user_id = ?""",
        (bon_earned, user_id)
    )

    # Add to wallet
    db.execute(
        "UPDATE bon_wallet SET bon = bon + ? WHERE user_id = ?",
        (bon_earned, user_id)
    )

    # Log mining history
    db.execute(
        "INSERT INTO mining_history (user_id, bon_earned, multiplier) VALUES (?, ?, ?)",
        (user_id, bon_earned, multiplier)
    )

    db.commit()

    return {
        "message": f"Mined {bon_earned} BON!",
        "bon_earned": bon_earned,
        "multiplier": multiplier,
        "state": get_mining_state(user_id)
    }, 200


def get_mining_boosts(user_id=None):
    """Get available mining boosts"""
    db = get_db()
    boosts = db.execute("SELECT * FROM mining_boosts ORDER BY cost_bon").fetchall()
    return [dict(b) for b in boosts]


def activate_boost(user_id, boost_id):
    """Purchase and activate a mining boost"""
    db = get_db()

    boost = db.execute(
        "SELECT * FROM mining_boosts WHERE id = ?",
        (boost_id,)
    ).fetchone()

    if not boost:
        return {"error": "Boost not found"}, 404

    wallet = db.execute(
        "SELECT bon FROM bon_wallet WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not wallet or wallet["bon"] < boost["cost_bon"]:
        return {
            "error": f"Insufficient BON. Need {boost['cost_bon']}, have {wallet['bon'] if wallet else 0}"
        }, 400

    # Deduct BON
    db.execute(
        "UPDATE bon_wallet SET bon = bon - ? WHERE user_id = ?",
        (boost["cost_bon"], user_id)
    )

    # Activate boost
    expires_at = datetime.utcnow() + timedelta(seconds=boost["duration"])
    db.execute(
        """UPDATE mining_stats
           SET boost_active = 1,
               boost_multiplier = ?,
               boost_expires_at = ?
           WHERE user_id = ?""",
        (boost["multiplier"], expires_at, user_id)
    )

    db.commit()

    return {
        "message": f"Activated {boost['name']}! {boost['multiplier']}x multiplier for {boost['duration']}s",
        "state": get_mining_state(user_id)
    }, 200


def get_mining_leaderboard(limit=10):
    """Get top miners by BON earned"""
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.username, m.total_bon_earned, m.total_clicks
           FROM mining_stats m
           JOIN users u ON u.id = m.user_id
           ORDER BY m.total_bon_earned DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def convert_bon_to_cash(user_id, bon_amount, exchange_rate=0.1):
    """Convert BON to trading cash (admin controlled rate)"""
    db = get_db()
    
    wallet = db.execute(
        "SELECT bon FROM bon_wallet WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if not wallet or wallet["bon"] < bon_amount:
        return {"error": "Insufficient BON"}, 400

    cash_amount = bon_amount * exchange_rate

    # Deduct BON
    db.execute(
        "UPDATE bon_wallet SET bon = bon - ? WHERE user_id = ?",
        (bon_amount, user_id)
    )

    # Add cash to portfolio
    db.execute(
        "UPDATE portfolios SET cash = cash + ? WHERE user_id = ?",
        (cash_amount, user_id)
    )

    db.commit()

    return {
        "message": f"Converted {bon_amount} BON to ${cash_amount:.2f}",
        "cash_added": round(cash_amount, 2),
        "bon_remaining": round(wallet["bon"] - bon_amount, 2)
    }, 200
