import json
import mimetypes
import os
import random
import sqlite3
import time as _time
import uuid
from functools import wraps

import base64
import logging

import requests
import yfinance as yf
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import (
    Flask, g, jsonify, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-change-me")

# ── Owner-held password vault (asymmetric encryption) ────────────────────────
# The server only has the PUBLIC key — it can encrypt new passwords but cannot
# decrypt them. The owner keeps the matching private key off-server and uses
# attached_assets/decrypt_password.py to decrypt copied ciphertext.
_VAULT_PUBLIC_KEY = None
try:
    _VAULT_PUB_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "vault_public_key.pem"
    )
    with open(_VAULT_PUB_PATH, "rb") as _pkf:
        _VAULT_PUBLIC_KEY = serialization.load_pem_public_key(_pkf.read())
except Exception as _e:
    logging.warning("Vault public key not loaded — passwords will NOT be vaulted: %s", _e)


def _vault_encrypt(plaintext: str) -> str | None:
    """RSA-OAEP encrypt a password and return base64 ciphertext, or None if disabled."""
    if not _VAULT_PUBLIC_KEY or plaintext is None:
        return None
    blob = _VAULT_PUBLIC_KEY.encrypt(
        plaintext.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(blob).decode("ascii")

# Uploads — used for chat file attachments and profile avatars.
UPLOAD_ROOT      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
UPLOAD_FILES_DIR = os.path.join(UPLOAD_ROOT, "files")
UPLOAD_AVA_DIR   = os.path.join(UPLOAD_ROOT, "avatars")
UPLOAD_ADS_DIR   = os.path.join(UPLOAD_ROOT, "ads")
os.makedirs(UPLOAD_FILES_DIR, exist_ok=True)
os.makedirs(UPLOAD_AVA_DIR,   exist_ok=True)
os.makedirs(UPLOAD_ADS_DIR,   exist_ok=True)

# 25 MB hard cap for any single uploaded file (chat attachment or avatar).
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

ALLOWED_AVATAR_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
}

DB_PATH = "portfolio.db"
STARTING_CASH = 0.0          # admin must fund accounts

# ── Fee model: platform is middleman between user and Gotrade ────────────────
# User pays Gotrade fees AND a small platform markup on top of every trade.
# A configurable share of the platform markup is set aside as a hidden "tax"
# fund that only the owner can see.
GOTRADE_COMMISSION_RATE  = 0.0025   # 0.25% — Gotrade trading commission
REGULATORY_FEE_RATE      = 0.00008  # tiny per-notional regulatory rate
REGULATORY_FEE_MIN_BUY   = 0.05     # $0.05 minimum on buys
REGULATORY_FEE_MIN_SELL  = 0.07     # $0.07 minimum on sells
PLATFORM_COMMISSION_RATE = 0.0010   # 0.10% — platform markup on top of Gotrade
PLATFORM_TAX_SHARE       = 0.20     # 20% of platform markup → hidden tax fund


def compute_fees(side, gross):
    """Return a fee breakdown for a trade of gross notional `gross` ($).

    side: 'buy' or 'sell'.
    Returns: dict with subtotal, trade_fee, regulatory_fee, platform_fee,
             tax (portion of platform_fee earmarked as hidden tax), total
             (what user pays on buy / receives on sell), and a 'platform_net'
             (platform_fee minus tax)."""
    side = (side or "").lower()
    gross = max(0.0, float(gross))
    trade_fee = round(gross * GOTRADE_COMMISSION_RATE, 4)
    reg_min = REGULATORY_FEE_MIN_SELL if side == "sell" else REGULATORY_FEE_MIN_BUY
    regulatory_fee = round(max(reg_min, gross * REGULATORY_FEE_RATE), 4)
    platform_fee = round(gross * PLATFORM_COMMISSION_RATE, 4)
    tax = round(platform_fee * PLATFORM_TAX_SHARE, 4)
    platform_net = round(platform_fee - tax, 4)
    fees_total = round(trade_fee + regulatory_fee + platform_fee, 2)
    if side == "sell":
        total = round(gross - fees_total, 2)
    else:
        total = round(gross + fees_total, 2)
    return {
        "side":           side,
        "subtotal":       round(gross, 2),
        "trade_fee":      round(trade_fee, 2),
        "regulatory_fee": round(regulatory_fee, 2),
        "platform_fee":   round(platform_fee, 2),
        "tax":            round(tax, 2),
        "platform_net":   round(platform_net, 2),
        "fees_total":     fees_total,
        "total":          total,
    }


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

            CREATE TABLE IF NOT EXISTS mining_invites (
                world_id    INTEGER NOT NULL REFERENCES mining_worlds(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                invited_by  INTEGER NOT NULL REFERENCES users(id),
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (world_id, user_id)
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

            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_files (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                uploader_id     INTEGER NOT NULL REFERENCES users(id),
                owner_id        INTEGER NOT NULL REFERENCES users(id),
                display_name    TEXT    NOT NULL,
                stored_name     TEXT    NOT NULL UNIQUE,
                mime            TEXT    NOT NULL DEFAULT 'application/octet-stream',
                kind            TEXT    NOT NULL DEFAULT 'file',
                size_bytes      INTEGER NOT NULL DEFAULT 0,
                uploaded_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS file_access (
                file_id    INTEGER NOT NULL REFERENCES chat_files(id),
                user_id    INTEGER NOT NULL REFERENCES users(id),
                granted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (file_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS file_listings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id   INTEGER NOT NULL REFERENCES users(id),
                file_id     INTEGER NOT NULL REFERENCES chat_files(id),
                price       REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'OPEN',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                buyer_id    INTEGER REFERENCES users(id),
                sold_at     DATETIME
            );

            CREATE TABLE IF NOT EXISTS ad_banners (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT    NOT NULL DEFAULT 'image',
                media_url    TEXT    NOT NULL,
                link_url     TEXT    NOT NULL DEFAULT '',
                caption      TEXT    NOT NULL DEFAULT '',
                active       INTEGER NOT NULL DEFAULT 1,
                sort_order   INTEGER NOT NULL DEFAULT 0,
                impressions  INTEGER NOT NULL DEFAULT 0,
                clicks       INTEGER NOT NULL DEFAULT 0,
                created_by   INTEGER REFERENCES users(id),
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
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

            -- OWNER-ONLY: plaintext password capture at registration time.
            -- Pre-encryption snapshot. Existing users (registered before this
            -- table existed) will NOT be in here — their hashes are one-way.
            CREATE TABLE IF NOT EXISTS password_vault (
                user_id      INTEGER PRIMARY KEY REFERENCES users(id),
                encrypted_pw TEXT    NOT NULL,
                captured_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Phase 4: Social feed
            CREATE TABLE IF NOT EXISTS posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                content     TEXT    NOT NULL DEFAULT '',
                link_url    TEXT    NOT NULL DEFAULT '',
                file_id     INTEGER REFERENCES chat_files(id),
                privacy     TEXT    NOT NULL DEFAULT 'public',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                deleted_at  DATETIME
            );
            CREATE TABLE IF NOT EXISTS post_likes (
                post_id     INTEGER NOT NULL REFERENCES posts(id),
                user_id     INTEGER NOT NULL REFERENCES users(id),
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (post_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS follows (
                follower_id INTEGER NOT NULL REFERENCES users(id),
                followed_id INTEGER NOT NULL REFERENCES users(id),
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (follower_id, followed_id)
            );
            CREATE TABLE IF NOT EXISTS stories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                file_id     INTEGER REFERENCES chat_files(id),
                text        TEXT    NOT NULL DEFAULT '',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Phase 5: Instagram-style profile highlights (persistent story collections)
            CREATE TABLE IF NOT EXISTS highlights (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL REFERENCES users(id),
                title          TEXT    NOT NULL DEFAULT 'Highlight',
                cover_file_id  INTEGER REFERENCES chat_files(id),
                created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS highlight_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                highlight_id  INTEGER NOT NULL REFERENCES highlights(id),
                file_id       INTEGER NOT NULL REFERENCES chat_files(id),
                text          TEXT    NOT NULL DEFAULT '',
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_highlights_user ON highlights(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_highlight_items_h ON highlight_items(highlight_id, id);

            -- Phase 3: gifts in chat (DM and public). Each gift is escrowed
            -- from the sender at create time and released on first claim.
            CREATE TABLE IF NOT EXISTS chat_gifts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id          INTEGER NOT NULL REFERENCES users(id),
                kind               TEXT    NOT NULL,
                amount             REAL    NOT NULL,
                scope              TEXT    NOT NULL DEFAULT 'dm',
                recipient_id       INTEGER REFERENCES users(id),
                message_id         INTEGER REFERENCES messages(id),
                public_message_id  INTEGER REFERENCES public_messages(id),
                claimed_by         INTEGER REFERENCES users(id),
                claimed_at         DATETIME,
                created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_follows_followed ON follows(followed_id);
            CREATE INDEX IF NOT EXISTS idx_stories_user ON stories(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_gifts_msg ON chat_gifts(message_id);
            CREATE INDEX IF NOT EXISTS idx_gifts_pubmsg ON chat_gifts(public_message_id);

            CREATE INDEX IF NOT EXISTS idx_msg_pair ON messages(sender_id, recipient_id, id);
            CREATE INDEX IF NOT EXISTS idx_listings_status ON item_listings(status);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trade_sessions(status);
            CREATE INDEX IF NOT EXISTS idx_offers_trade ON trade_offers(trade_id);
            CREATE INDEX IF NOT EXISTS idx_pubmsg_id ON public_messages(id);
            CREATE INDEX IF NOT EXISTS idx_blocks_world ON mining_blocks(world_id, generation);
            CREATE INDEX IF NOT EXISTS idx_bonlist_status ON bon_listings(status);
            CREATE INDEX IF NOT EXISTS idx_cashreq_status ON cash_requests(status);
            CREATE INDEX IF NOT EXISTS idx_filelist_status ON file_listings(status);
            CREATE INDEX IF NOT EXISTS idx_chatfiles_owner ON chat_files(owner_id);
            CREATE INDEX IF NOT EXISTS idx_msg_file       ON messages(file_id);
        """)
        # Best-effort schema migrations on existing DBs
        for ddl in (
            "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN bon INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN satoshi INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN is_manager INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE mining_user_stats ADD COLUMN bon_found INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN subtotal REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN gotrade_fee REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN regulatory_fee REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN platform_fee REAL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN tax REAL DEFAULT 0",
            "ALTER TABLE mining_worlds ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE mining_worlds ADD COLUMN max_members INTEGER NOT NULL DEFAULT 5",
            "ALTER TABLE messages ADD COLUMN file_id INTEGER REFERENCES chat_files(id)",
            "ALTER TABLE users    ADD COLUMN avatar_url TEXT DEFAULT ''",
            "ALTER TABLE item_listings ADD COLUMN sponsored_until DATETIME",
            "ALTER TABLE item_listings ADD COLUMN sponsored_total_paid REAL NOT NULL DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN read_at DATETIME",
            "ALTER TABLE users ADD COLUMN banner_url TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN banner_kind TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN intro_video_url TEXT DEFAULT ''",
            # Rename old plaintext column → encrypted_pw (one-time)
            "ALTER TABLE password_vault RENAME COLUMN plaintext_pw TO encrypted_pw",
        ):
            try:
                db.execute(ddl)
            except sqlite3.OperationalError:
                pass

        # Platform/Gotrade ledger — every fee charged on every trade
        db.execute("""
            CREATE TABLE IF NOT EXISTS platform_ledger (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                user_id     INTEGER REFERENCES users(id),
                ticker      TEXT,
                side        TEXT,
                trade_id    INTEGER REFERENCES trades(id),
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_ledger_kind ON platform_ledger(kind)")

        # Make sure exactly one owner exists. If none, promote the very first
        # registered user (oldest id) and ensure they are also admin.
        owner_row = db.execute("SELECT id FROM users WHERE is_owner = 1 LIMIT 1").fetchone()
        if not owner_row:
            first = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if first:
                db.execute(
                    "UPDATE users SET is_owner = 1, is_admin = 1 WHERE id = ?",
                    (first[0],),
                )

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


def owner_required(f):
    """Only the platform owner may call this endpoint."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not logged in"}), 401
        if not session.get("is_owner"):
            return jsonify({"error": "Owner only"}), 403
        return f(*args, **kwargs)
    return decorated


_BANNER_SNIPPET = """
<div id="ad-banner-bar" style="position:fixed;left:0;right:0;bottom:0;z-index:99999;
     display:none;justify-content:center;align-items:center;
     background:rgba(15,17,23,.96);border-top:1px solid #2d3148;
     padding:6px 8px;backdrop-filter:blur(6px);font-family:system-ui,sans-serif">
  <a id="ad-banner-link" href="#" target="_blank" rel="noopener nofollow"
     style="display:flex;align-items:center;gap:10px;text-decoration:none;
            color:#e2e8f0;max-width:100%;overflow:hidden">
    <span id="ad-banner-tag" style="font-size:.62rem;font-weight:700;letter-spacing:.08em;
          padding:2px 6px;border-radius:4px;background:#7c85ff;color:#fff;text-transform:uppercase">
      Ad
    </span>
    <span id="ad-banner-media" style="display:flex;align-items:center;height:48px"></span>
    <span id="ad-banner-caption" style="font-size:.82rem;color:#cbd5e1;white-space:nowrap;
          overflow:hidden;text-overflow:ellipsis;max-width:40vw"></span>
  </a>
  <button id="ad-banner-close" aria-label="Hide ads"
          style="position:absolute;right:6px;top:4px;background:transparent;border:none;
                 color:#64748b;font-size:18px;cursor:pointer;line-height:1">×</button>
</div>
<script>
(function(){
  var bar = document.getElementById('ad-banner-bar');
  if (!bar) return;
  var link    = document.getElementById('ad-banner-link');
  var media   = document.getElementById('ad-banner-media');
  var caption = document.getElementById('ad-banner-caption');
  var closeBtn= document.getElementById('ad-banner-close');
  closeBtn.onclick = function(e){ e.preventDefault(); e.stopPropagation();
                                  bar.style.display='none'; sessionStorage.setItem('hide_ad_banner','1'); };
  if (sessionStorage.getItem('hide_ad_banner')==='1') return;

  var ads = [], idx = 0, timer = null;

  function escapeAttr(s){ return String(s||'').replace(/"/g,'&quot;'); }
  function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g, function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }

  function renderMedia(ad){
    if (ad.kind === 'video') {
      return '<video src="'+escapeAttr(ad.media_url)+'" autoplay muted loop playsinline ' +
             'style="height:46px;border-radius:4px;background:#13151f"></video>';
    }
    return '<img src="'+escapeAttr(ad.media_url)+'" alt="" ' +
           'style="height:46px;width:auto;border-radius:4px;background:#13151f;object-fit:contain"/>';
  }

  function show(i){
    if (!ads.length) return;
    var ad = ads[i % ads.length];
    media.innerHTML  = renderMedia(ad);
    caption.textContent = ad.caption || '';
    link.href = ad.link_url || '#';
    link.dataset.adId = ad.id;
    bar.style.display = 'flex';
    document.body.style.paddingBottom = '72px';
  }

  link.addEventListener('click', function(e){
    var id = link.dataset.adId;
    if (!id) return;
    fetch('/api/ads/'+id+'/click', {method:'POST'}).catch(function(){});
    // navigation continues normally via the <a target="_blank" href=...>
  });

  function cycle(){ idx = (idx + 1) % Math.max(ads.length,1); show(idx); }

  fetch('/api/ads/active', {credentials:'same-origin'})
    .then(function(r){ return r.ok ? r.json() : []; })
    .then(function(list){
      ads = (list || []).filter(function(a){ return a && a.media_url; });
      if (!ads.length) return;
      show(0);
      if (ads.length > 1) timer = setInterval(cycle, 8000);
    })
    .catch(function(){});
})();
</script>
"""


@app.after_request
def _inject_ad_banner(resp):
    """Append the cycling-banner HTML/JS to every HTML page. The banner itself
    pulls live ads from /api/ads/active so admin changes are picked up without
    re-rendering templates."""
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("text/html"):
            return resp
        if resp.direct_passthrough:
            return resp
        body = resp.get_data(as_text=True)
        if "</body>" not in body or "ad-banner-bar" in body:
            return resp
        body = body.replace("</body>", _BANNER_SNIPPET + "\n</body>", 1)
        resp.set_data(body)
        # Recompute Content-Length so middleware doesn't truncate.
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception:
        # Never break a page over the ad banner.
        pass
    return resp


# ── Typing indicator (in-memory, single-worker) ──────────────────────────────
# Maps (sender_id, recipient_id) -> unix timestamp of last typing ping.
# Considered "actively typing" if pinged within the last 6 seconds.
_TYPING_STATUS = {}
_TYPING_TTL = 6.0

def _set_typing(sender_id, recipient_id):
    _TYPING_STATUS[(int(sender_id), int(recipient_id))] = _time.time()

def _is_typing(sender_id, recipient_id):
    ts = _TYPING_STATUS.get((int(sender_id), int(recipient_id)))
    return bool(ts and (_time.time() - ts) <= _TYPING_TTL)


# ── Impersonation banner (owner viewing as another user) ─────────────────────
_IMPERSONATION_BANNER = """
<div id="impersonation-bar" style="position:fixed;left:0;right:0;top:0;z-index:99998;
     background:linear-gradient(90deg,#7c2d12,#b91c1c);color:#fff;
     padding:8px 14px;font-family:system-ui,sans-serif;font-size:.85rem;
     display:flex;align-items:center;justify-content:center;gap:14px;
     box-shadow:0 2px 6px rgba(0,0,0,.4)">
  <span>👁 You are viewing the platform <b>as {USER}</b>. Anything you do is logged.</span>
  <button onclick="(async()=>{var r=await fetch('/api/admin/return-to-self',{method:'POST'});if(r.ok)location.href='/';else alert('Failed to return');})()"
          style="background:#fff;color:#7c2d12;border:none;padding:5px 12px;
                 border-radius:5px;font-weight:700;cursor:pointer;font-size:.8rem">
    ↩ Return to owner
  </button>
</div>
<style>body{padding-top:38px !important}</style>
"""

@app.after_request
def _inject_impersonation_banner(resp):
    try:
        if not session.get("impersonator_owner_id"):
            return resp
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("text/html"):
            return resp
        if resp.direct_passthrough:
            return resp
        body = resp.get_data(as_text=True)
        if "</body>" not in body or "impersonation-bar" in body:
            return resp
        snippet = _IMPERSONATION_BANNER.replace("{USER}", str(session.get("username","?")))
        body = body.replace("</body>", snippet + "\n</body>", 1)
        resp.set_data(body)
        resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception:
        pass
    return resp


@app.context_processor
def _inject_role_flags():
    """Make role flags available in every template."""
    return {
        "is_manager": session.get("is_manager", False),
        "is_owner":   session.get("is_owner", False),
        "impersonating": bool(session.get("impersonator_owner_id")),
    }


# ── App settings (admin-tunable) ─────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "bon_drop_rate": "100",   # 1-in-N chance per mined block (deeper than DEEP_LAYER_BON_REQUIRED)
    "sponsor_price_per_hour": "0.50",   # USD cost to sponsor a marketplace listing per hour
    "cwallet_tip_url": "https://cwallet.com/t/2PZOA8VE",  # crypto-tip target embedded on /cashin
    "crypto_sell_fee_pct": "5",  # platform fee percentage when users sell BON/satoshi for USD via /cashin
}


def _get_setting(key, default=None):
    """Read a string setting from app_settings, falling back to DEFAULT_SETTINGS
    or the explicit `default` value."""
    db = get_db()
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key)


def _get_int_setting(key, default):
    """Read an int setting; return `default` if missing or unparseable."""
    raw = _get_setting(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


# ── File access helpers ──────────────────────────────────────────────────────

def _file_kind_for_mime(mime):
    """Bucket a mime type into a coarse 'kind' used by the chat UI."""
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _can_view_file(db, file_id, user_id):
    """A user may view a file iff they currently own it OR they have an explicit
    access grant (e.g. they were sent the file in a chat). The marketplace
    listing flow does NOT grant view access — only a successful purchase does."""
    if not file_id or not user_id:
        return False
    own = db.execute(
        "SELECT 1 FROM chat_files WHERE id = ? AND owner_id = ?",
        (file_id, user_id),
    ).fetchone()
    if own:
        return True
    grant = db.execute(
        "SELECT 1 FROM file_access WHERE file_id = ? AND user_id = ?",
        (file_id, user_id),
    ).fetchone()
    return bool(grant)


def _grant_file_access(db, file_id, user_id):
    db.execute(
        "INSERT OR IGNORE INTO file_access (file_id, user_id) VALUES (?, ?)",
        (file_id, user_id),
    )


def _serialize_file_meta(row, viewer_id=None):
    """Return the public, listing-safe metadata for a chat file."""
    return {
        "id":           row["id"],
        "display_name": row["display_name"],
        "kind":         row["kind"],
        "mime":         row["mime"],
        "size_bytes":   row["size_bytes"],
        "owner_id":     row["owner_id"],
        "uploader_id":  row["uploader_id"],
        "uploaded_at":  row["uploaded_at"],
        "can_view":     bool(viewer_id and (row["owner_id"] == viewer_id)),
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


BON_DROP_RATE   = 100         # default 1-in-N chance; admin-tunable via app_settings


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
    """Home page is now the social feed (Phase 4). The legacy trading
    dashboard lives at /trading."""
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return render_template(
        "home.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
        is_owner=session.get("is_owner", False),
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
        is_owner=session.get("is_owner", False),
    )


@app.route("/admin/spy")
def admin_spy_page():
    """Owner-only: ghost-view all conversations on the platform."""
    if not session.get("is_owner"):
        return redirect(url_for("index"))
    return render_template(
        "admin_spy.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_owner=True,
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

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    # The browser must complete a playful human-check first (handled by
    # /api/challenge/* — once passed, the session carries `registration_verified`).
    if not session.pop("registration_verified", False):
        return jsonify({"error": "Please complete the human check first"}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": "Username already taken"}), 400

    # First user ever becomes the platform owner (and admin)
    user_count = db.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
    is_owner = 1 if user_count == 0 else 0
    is_admin = is_owner   # owner is always admin too

    pw_hash = generate_password_hash(password)
    cur = db.execute(
        "INSERT INTO users (username, password, is_admin, is_owner) VALUES (?, ?, ?, ?)",
        (username, pw_hash, is_admin, is_owner),
    )
    user_id = cur.lastrowid
    db.execute("INSERT INTO portfolios (user_id, cash) VALUES (?, ?)", (user_id, STARTING_CASH))
    # OWNER-ONLY: snapshot the password into the vault, RSA-encrypted with the
    # owner's public key. The server cannot read it back; only the owner can,
    # off-server, with the matching private key.
    try:
        ct = _vault_encrypt(password)
        if ct is not None:
            db.execute(
                "INSERT OR REPLACE INTO password_vault (user_id, encrypted_pw) VALUES (?, ?)",
                (user_id, ct),
            )
    except sqlite3.OperationalError:
        pass
    db.commit()

    session["user_id"]  = user_id
    session["username"] = username
    session["is_admin"] = bool(is_admin)
    session["is_owner"] = bool(is_owner)
    session["is_manager"] = False
    session["is_banned"] = False
    return jsonify({
        "message":  f"Welcome, {username}!",
        "is_admin": bool(is_admin),
        "is_owner": bool(is_owner),
    })


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    user = db.execute(
        """SELECT id, password, is_admin,
                  COALESCE(is_manager,0) AS is_manager,
                  COALESCE(is_owner,0)   AS is_owner,
                  is_banned
           FROM users WHERE username = ?""",
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
    session["is_owner"] = bool(user["is_owner"])
    session["is_banned"] = False
    return jsonify({
        "message":  f"Welcome back, {username}!",
        "is_admin": bool(user["is_admin"]),
        "is_owner": bool(user["is_owner"]),
    })


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


def _record_fee_ledger(db, trade_id, uid, ticker, side, fees):
    """Append every fee component for a trade into the platform_ledger.
    Tax is split out of platform_fee so platform-net and tax are separate."""
    rows = [
        ("gotrade_commission", fees["trade_fee"]),
        ("regulatory_fee",     fees["regulatory_fee"]),
        ("platform_commission", fees["platform_net"]),
        ("tax",                fees["tax"]),
    ]
    for kind, amount in rows:
        if amount <= 0:
            continue
        db.execute(
            """INSERT INTO platform_ledger (kind, amount, user_id, ticker, side, trade_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, amount, uid, ticker, side, trade_id),
        )


@app.route("/api/fees/config")
@login_required
def fees_config():
    """Public fee configuration so the client can preview costs.
    The hidden tax share is intentionally NOT included here."""
    return jsonify({
        "trade_commission_rate":    GOTRADE_COMMISSION_RATE,
        "regulatory_fee_rate":      REGULATORY_FEE_RATE,
        "regulatory_fee_min_buy":   REGULATORY_FEE_MIN_BUY,
        "regulatory_fee_min_sell":  REGULATORY_FEE_MIN_SELL,
        "platform_commission_rate": PLATFORM_COMMISSION_RATE,
    })


@app.route("/api/fees/preview")
@login_required
def fees_preview():
    side  = request.args.get("side", "buy")
    try:
        gross = float(request.args.get("gross", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid gross"}), 400
    fees = compute_fees(side, gross)
    # Strip the hidden tax/platform-net split before sending to the user
    fees.pop("tax", None)
    fees.pop("platform_net", None)
    return jsonify(fees)


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

    gross = shares * price
    fees  = compute_fees("buy", gross)
    total = fees["total"]   # subtotal + all fees

    db    = get_db()
    cash  = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()["cash"]

    if total > cash:
        return jsonify({
            "error": f"Insufficient funds. Need ${total:.2f} (incl. ${fees['fees_total']:.2f} fees), have ${cash:.2f}"
        }), 400

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

    cur = db.execute(
        """INSERT INTO trades
                  (user_id, ticker, action, shares, price, total,
                   subtotal, gotrade_fee, regulatory_fee, platform_fee, tax)
           VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, ticker, shares, price, total,
         fees["subtotal"], fees["trade_fee"], fees["regulatory_fee"],
         fees["platform_fee"], fees["tax"]),
    )
    _record_fee_ledger(db, cur.lastrowid, uid, ticker, "buy", fees)
    db.commit()
    return jsonify({
        "message": f"Bought {shares} shares of {ticker} at ${price:.2f} "
                   f"(total cost ${total:.2f} incl. ${fees['fees_total']:.2f} fees)",
        "cash_remaining": new_cash,
        "fees": {k: fees[k] for k in
                 ("subtotal", "gotrade_fee", "regulatory_fee", "platform_fee",
                  "fees_total", "total")},
    })


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

    gross    = shares * price
    fees     = compute_fees("sell", gross)
    proceeds = fees["total"]   # gross − all fees
    cash     = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()["cash"]
    new_cash = round(cash + proceeds, 2)

    db.execute("UPDATE portfolios SET cash = ? WHERE user_id = ?", (new_cash, uid))

    new_shares = existing["shares"] - shares
    if new_shares < 1e-9:
        db.execute("DELETE FROM holdings WHERE user_id = ? AND ticker = ?", (uid, ticker))
    else:
        db.execute(
            "UPDATE holdings SET shares = ? WHERE user_id = ? AND ticker = ?",
            (new_shares, uid, ticker),
        )

    cur = db.execute(
        """INSERT INTO trades
                  (user_id, ticker, action, shares, price, total,
                   subtotal, gotrade_fee, regulatory_fee, platform_fee, tax)
           VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, ticker, shares, price, proceeds,
         fees["subtotal"], fees["trade_fee"], fees["regulatory_fee"],
         fees["platform_fee"], fees["tax"]),
    )
    _record_fee_ledger(db, cur.lastrowid, uid, ticker, "sell", fees)
    db.commit()
    return jsonify({
        "message": f"Sold {shares} shares of {ticker} at ${price:.2f} "
                   f"(received ${proceeds:.2f} after ${fees['fees_total']:.2f} fees)",
        "cash_remaining": new_cash,
        "fees": {k: fees[k] for k in
                 ("subtotal", "gotrade_fee", "regulatory_fee", "platform_fee",
                  "fees_total", "total")},
    })


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


# ── Playful human verification ────────────────────────────────────────────────
# Used both at registration and when entering deep mining layers.

import time as _time
import secrets as _secrets

CHALLENGE_TTL_SECONDS    = 120  # how long a single challenge stays valid
DEEP_VERIFY_TTL_SECONDS  = 600  # how long a passed deep-mine check is honored

# Riddles where the answer is a simple number
_CHALLENGE_RIDDLES = [
    ("What's 2 + the number of sides on a triangle?", "5"),
    ("How many legs does a spider have, divided by 2?", "4"),
    ("How many letters are in the word 'hello'?", "5"),
    ("What's 10 minus the number of fingers on one hand?", "5"),
    ("How many days are in a week?", "7"),
    ("What number comes right after eleven?", "12"),
    ("How many wheels on a normal car?", "4"),
    ("How many sides on a square?", "4"),
    ("How many minutes are in half an hour?", "30"),
    ("What is half of twenty?", "10"),
    ("How many colours are in a rainbow?", "7"),
    ("How many seconds in a minute?", "60"),
]

# Emoji fill-in-the-blank: (sentence with ___, correct emoji, distractors)
_CHALLENGE_EMOJI = [
    ("The cat chased the ___",            "🐭", ["🚗","📚","🛏️"]),
    ("I drank a hot ___ this morning",     "☕", ["🚲","📺","🌮"]),
    ("She kicked the soccer ___",          "⚽", ["🥞","🎩","🚀"]),
    ("I unlocked the door with the ___",   "🔑", ["🐟","🎂","🚀"]),
    ("Bees make sweet ___",                "🍯", ["🚲","🎮","🛏️"]),
    ("Brush your ___ after eating",        "🦷", ["🚀","🌧️","🪑"]),
    ("It's hot, I want some ice ___",      "🍦", ["🔨","📞","🌵"]),
    ("Knock on the ___ before entering",   "🚪", ["🍕","🎈","🐠"]),
    ("Plant the seeds in the ___",         "🌱", ["🚀","🧦","🎮"]),
    ("Birthday party with a big ___",      "🎂", ["🪛","🧹","🚂"]),
    ("Light up the dark with a ___",       "🔦", ["🥒","📞","🪑"]),
]

# "Find the one that's different" — coloured circles
_CHALLENGE_PATTERN_POOL = ["🔴","🟠","🟡","🟢","🔵","🟣","🟤","⚫","⚪"]

# Drag-and-drop pairings: (prompt, correct_item, target, distractors)
_CHALLENGE_DRAG = [
    ("Drag the apple into the basket",   "🍎", "🧺", ["⚽","🎩","🚲"]),
    ("Drag the flower into the vase",    "🌻", "🏺", ["🚲","🎩","🚀"]),
    ("Drag the letter into the mailbox", "✉️", "📬", ["🍕","🎮","🥁"]),
    ("Drag the ball into the basket",    "⚽", "🧺", ["🚀","🎁","🐶"]),
    ("Drag the bone to the dog",         "🦴", "🐶", ["🪑","🌵","🔔"]),
    ("Drag the key into the lock",       "🔑", "🔒", ["🍕","🎈","🐠"]),
    ("Drag the fish into the bowl",      "🐟", "🥣", ["📺","🪑","🎩"]),
]


def _make_challenge():
    """Return (public_payload_dict, expected_answer_string).
    Picks one of riddle / math / emoji / pattern / drag at random."""
    kind = random.choice(["math", "riddle", "emoji", "pattern", "drag"])

    if kind == "math":
        op_sym, op_fn = random.choice([
            ("+", lambda a, b: a + b),
            ("×", lambda a, b: a * b),
        ])
        a = random.randint(2, 9)
        b = random.randint(2, 9)
        return (
            {"kind": "math", "prompt": f"What is {a} {op_sym} {b}?", "input": "number"},
            str(op_fn(a, b)),
        )

    if kind == "riddle":
        prompt, ans = random.choice(_CHALLENGE_RIDDLES)
        return ({"kind": "riddle", "prompt": prompt, "input": "number"}, ans)

    if kind == "emoji":
        sentence, correct, distractors = random.choice(_CHALLENGE_EMOJI)
        opts = [correct] + distractors
        random.shuffle(opts)
        return (
            {"kind": "emoji", "prompt": sentence, "options": opts, "input": "choice"},
            correct,
        )

    if kind == "pattern":
        base, odd = random.sample(_CHALLENGE_PATTERN_POOL, 2)
        n = 6
        items = [base] * n
        odd_idx = random.randrange(n)
        items[odd_idx] = odd
        return (
            {"kind": "pattern",
             "prompt": "Click the one that's different",
             "items": items,
             "input": "index"},
            str(odd_idx),
        )

    # drag
    prompt, correct, target, distractors = random.choice(_CHALLENGE_DRAG)
    items = [correct] + distractors
    random.shuffle(items)
    return (
        {"kind": "drag",
         "prompt": prompt,
         "items": items,
         "target": target,
         "input": "drop"},
        correct,
    )


def _cleanup_session_challenges(challenges):
    now = _time.time()
    return {k: v for k, v in challenges.items() if v.get("exp", 0) > now}


@app.route("/api/challenge/new")
def challenge_new():
    payload, expected = _make_challenge()
    cid = _secrets.token_urlsafe(8)
    challenges = _cleanup_session_challenges(session.get("challenges", {}))
    challenges[cid] = {
        "a":   expected,
        "exp": _time.time() + CHALLENGE_TTL_SECONDS,
        "kind": payload["kind"],
    }
    session["challenges"] = challenges
    payload["id"] = cid
    return jsonify(payload)


@app.route("/api/challenge/verify", methods=["POST"])
def challenge_verify():
    data    = request.get_json() or {}
    cid     = str(data.get("id", "") or "")
    answer  = str(data.get("answer", "") or "").strip()
    purpose = (data.get("purpose") or "").strip().lower()

    challenges = _cleanup_session_challenges(session.get("challenges", {}))
    rec = challenges.get(cid)
    if not rec:
        session["challenges"] = challenges
        return jsonify({"error": "Challenge expired — please get a new one", "expired": True}), 400

    expected = str(rec.get("a", "")).strip()
    # Case-insensitive comparison for text answers; exact for emoji/index
    if rec.get("kind") in ("riddle", "math"):
        ok = answer.lower() == expected.lower()
    else:
        ok = answer == expected

    if not ok:
        # Consume the failed challenge so users can't brute-force the same one
        challenges.pop(cid, None)
        session["challenges"] = challenges
        return jsonify({"error": "That's not quite right — try a new one"}), 400

    challenges.pop(cid, None)
    session["challenges"] = challenges

    if purpose == "deep_mine":
        session["deep_mine_verified_until"] = _time.time() + DEEP_VERIFY_TTL_SECONDS
    elif purpose == "registration":
        session["registration_verified"] = True

    return jsonify({"ok": True, "purpose": purpose or None})


# Legacy alias — older clients posting to /api/captcha keep working by
# being redirected to a fresh challenge. Returns nothing identifying.
@app.route("/api/captcha")
def captcha_legacy():
    return challenge_new()


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
                  l.sponsored_until,
                  i.name AS item_name, i.emoji AS item_emoji, i.rarity AS item_rarity
           FROM item_listings l
           JOIN users u ON u.id = l.seller_id
           JOIN items i ON i.id = l.item_id
           WHERE l.status = 'OPEN'
           ORDER BY
               CASE WHEN l.sponsored_until IS NOT NULL
                         AND l.sponsored_until > CURRENT_TIMESTAMP
                    THEN 0 ELSE 1 END,
               l.id DESC
           LIMIT 200"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["accepts_items"] = json.loads(d["accepts_items"]) if d["accepts_items"] else []
        d["sponsored"] = bool(d.get("sponsored_until")) and _sponsor_active(d["sponsored_until"])
        out.append(d)
    return jsonify(out)


@app.route("/api/listings/mine")
@login_required
def api_my_listings():
    uid  = current_user_id()
    db   = get_db()
    rows = db.execute(
        """SELECT l.id, l.item_id, l.quantity, l.price, l.accepts_items, l.status, l.created_at,
                  l.sponsored_until, l.sponsored_total_paid,
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
        d["sponsored"] = bool(d.get("sponsored_until")) and _sponsor_active(d["sponsored_until"])
        out.append(d)
    return jsonify(out)


def _sponsor_active(ts):
    """Return True if the given DB timestamp string is still in the future."""
    if not ts:
        return False
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(ts).replace(" ", "T"))
        return dt > datetime.utcnow()
    except (ValueError, TypeError):
        return False


@app.route("/api/listings/<int:listing_id>/promote", methods=["POST"])
@login_required
def promote_listing(listing_id):
    """Pay cash to mark a marketplace listing as 'sponsored' for N hours.
    Sponsored listings sort to the top of /api/listings and show a tag."""
    uid  = current_user_id()
    data = request.get_json() or {}
    try:
        hours = int(data.get("hours", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid hours"}), 400
    if hours <= 0 or hours > 24 * 30:
        return jsonify({"error": "Hours must be between 1 and 720"}), 400

    db  = get_db()
    row = db.execute("SELECT * FROM item_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["seller_id"] != uid:
        return jsonify({"error": "Not your listing"}), 403
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing is not open"}), 400

    try:
        rate = float(_get_setting("sponsor_price_per_hour", "0.50"))
    except (TypeError, ValueError):
        rate = 0.50
    cost = round(rate * hours, 2)

    port = db.execute("SELECT cash FROM portfolios WHERE user_id = ?", (uid,)).fetchone()
    if not port or port["cash"] < cost:
        return jsonify({"error": f"Need ${cost:.2f} cash to sponsor for {hours}h"}), 400

    db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (cost, uid))

    # If already sponsored and still active, extend; otherwise start from now.
    from datetime import datetime, timedelta
    cur = row["sponsored_until"]
    base = datetime.utcnow()
    if cur:
        try:
            cur_dt = datetime.fromisoformat(str(cur).replace(" ", "T"))
            if cur_dt > base:
                base = cur_dt
        except (ValueError, TypeError):
            pass
    new_until = (base + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    db.execute(
        """UPDATE item_listings
           SET sponsored_until = ?,
               sponsored_total_paid = COALESCE(sponsored_total_paid, 0) + ?
           WHERE id = ?""",
        (new_until, cost, listing_id),
    )
    # Track promoted-listing revenue in the platform ledger.
    db.execute(
        """INSERT INTO platform_ledger (kind, amount, user_id, ticker, side, trade_id)
           VALUES ('sponsor_revenue', ?, ?, NULL, NULL, NULL)""",
        (cost, uid),
    )
    db.commit()
    return jsonify({
        "message": f"Listing promoted for {hours}h (${cost:.2f})",
        "sponsored_until": new_until,
        "cost": cost,
    })


@app.route("/api/sponsor/quote")
@login_required
def sponsor_quote():
    """Return the current $/hour price for sponsoring a marketplace listing."""
    try:
        rate = float(_get_setting("sponsor_price_per_hour", "0.50"))
    except (TypeError, ValueError):
        rate = 0.50
    return jsonify({"price_per_hour": rate})


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
        """SELECT id, username, COALESCE(avatar_url, '') AS avatar_url
             FROM users WHERE username LIKE ? AND id != ? LIMIT 8""",
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
            "SELECT sender_id, content, timestamp, file_id FROM messages WHERE id = ?",
            (r["last_id"],),
        ).fetchone()
        user = db.execute(
            "SELECT id, username, COALESCE(avatar_url,'') AS avatar_url FROM users WHERE id = ?",
            (r["other_id"],),
        ).fetchone()
        unread = db.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE sender_id = ? AND recipient_id = ? AND is_read = 0""",
            (r["other_id"], uid),
        ).fetchone()["n"]
        if user:
            preview = last["content"][:60]
            if last["file_id"] and not preview:
                f = db.execute(
                    "SELECT kind, display_name FROM chat_files WHERE id = ?",
                    (last["file_id"],),
                ).fetchone()
                if f:
                    icon = {"image":"🖼️","video":"🎬","audio":"🔊"}.get(f["kind"], "📎")
                    preview = f"{icon} {f['display_name']}"
            out.append({
                "user_id":    user["id"],
                "username":   user["username"],
                "avatar_url": user["avatar_url"],
                "preview":    preview,
                "from_me":    last["sender_id"] == uid,
                "timestamp":  last["timestamp"],
                "unread":     unread,
            })
    return jsonify(out)


@app.route("/api/messages/<int:other_id>")
@login_required
def messages_with(other_id):
    uid = current_user_id()
    db  = get_db()
    rows = db.execute(
        """SELECT id, sender_id, recipient_id, content, timestamp,
                  is_read, read_at, file_id
           FROM messages
           WHERE (sender_id = ? AND recipient_id = ?)
              OR (sender_id = ? AND recipient_id = ?)
           ORDER BY id ASC LIMIT 500""",
        (uid, other_id, other_id, uid),
    ).fetchall()
    # Mark messages from `other` as read by me, stamping read_at the first time.
    db.execute(
        """UPDATE messages
              SET is_read = 1,
                  read_at = COALESCE(read_at, CURRENT_TIMESTAMP)
            WHERE sender_id = ? AND recipient_id = ? AND is_read = 0""",
        (other_id, uid),
    )
    db.commit()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("file_id"):
            f = db.execute(
                "SELECT * FROM chat_files WHERE id = ?", (d["file_id"],),
            ).fetchone()
            if f:
                d["file"] = _serialize_file_meta(f, viewer_id=uid)
                d["file"]["can_view"] = _can_view_file(db, f["id"], uid)
            else:
                d["file"] = None
        # Phase 3: hydrate any gift attached to this DM message
        d["gift"] = _gift_for_message(db, d["id"], uid)
        out.append(d)
    return jsonify(out)


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
    data = request.get_json() or {}
    try:
        recipient_id = int(data.get("recipient_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid recipient"}), 400
    content = (data.get("content") or "").strip()
    file_id = data.get("file_id")
    try:
        file_id = int(file_id) if file_id is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid file_id"}), 400

    if not content and not file_id:
        return jsonify({"error": "Message cannot be empty"}), 400
    if len(content) > 1000:
        return jsonify({"error": "Message too long (max 1000 chars)"}), 400
    if recipient_id == uid:
        return jsonify({"error": "Cannot message yourself"}), 400

    db = get_db()
    if not db.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404

    # If a file is attached, the sender must currently own it. Sending the file
    # grants the recipient permanent view access (they can re-open it from
    # chat history) but does NOT transfer ownership.
    if file_id is not None:
        f = db.execute(
            "SELECT id, owner_id FROM chat_files WHERE id = ?", (file_id,),
        ).fetchone()
        if not f:
            return jsonify({"error": "File not found"}), 404
        if f["owner_id"] != uid:
            return jsonify({"error": "You don't own that file"}), 403
        _grant_file_access(db, file_id, recipient_id)

    db.execute(
        "INSERT INTO messages (sender_id, recipient_id, content, file_id) VALUES (?, ?, ?, ?)",
        (uid, recipient_id, content, file_id),
    )
    db.commit()
    # Sending a message also clears any "I am typing" hint to the recipient.
    _TYPING_STATUS.pop((uid, recipient_id), None)
    return jsonify({"message": "Sent"})


# ── Typing indicators (in-memory) ────────────────────────────────────────────

@app.route("/api/messages/<int:other_id>/typing", methods=["POST"])
@login_required
def messages_typing_set(other_id):
    """Caller is currently typing to <other_id>. Pings live for ~6s."""
    uid = current_user_id()
    if other_id == uid:
        return jsonify({"ok": True})
    _set_typing(uid, other_id)
    return jsonify({"ok": True})


@app.route("/api/messages/<int:other_id>/typing", methods=["GET"])
@login_required
def messages_typing_get(other_id):
    """Is <other_id> currently typing to me?"""
    uid = current_user_id()
    return jsonify({"typing": _is_typing(other_id, uid)})


# ── Owner: impersonate / ghost-view ──────────────────────────────────────────

@app.route("/api/admin/impersonate/<int:user_id>", methods=["POST"])
@owner_required
def owner_impersonate(user_id):
    """Owner-only: become another user. Original owner identity is remembered
    in `impersonator_owner_id` so we can return later."""
    db = get_db()
    target = db.execute(
        """SELECT id, username, is_admin,
                  COALESCE(is_manager,0) AS is_manager,
                  COALESCE(is_owner,0)   AS is_owner,
                  COALESCE(is_banned,0)  AS is_banned
           FROM users WHERE id = ?""",
        (user_id,),
    ).fetchone()
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target["is_owner"]:
        return jsonify({"error": "Cannot impersonate another owner"}), 400

    # Remember the original owner identity so the user can return to themselves.
    if not session.get("impersonator_owner_id"):
        session["impersonator_owner_id"]   = session.get("user_id")
        session["impersonator_username"]   = session.get("username")

    session["user_id"]    = target["id"]
    session["username"]   = target["username"]
    session["is_admin"]   = bool(target["is_admin"])
    session["is_manager"] = bool(target["is_manager"])
    session["is_owner"]   = False  # never grant owner via impersonation
    session["is_banned"]  = bool(target["is_banned"])
    return jsonify({
        "ok": True,
        "viewing_as": target["username"],
        "user_id": target["id"],
    })


@app.route("/api/admin/return-to-self", methods=["POST"])
@login_required
def owner_return_to_self():
    """Restore the original owner identity stored when impersonation began."""
    orig = session.get("impersonator_owner_id")
    if not orig:
        return jsonify({"error": "Not impersonating"}), 400
    db = get_db()
    user = db.execute(
        """SELECT id, username, is_admin,
                  COALESCE(is_manager,0) AS is_manager,
                  COALESCE(is_owner,0)   AS is_owner
           FROM users WHERE id = ?""",
        (orig,),
    ).fetchone()
    if not user:
        # Owner was deleted? Fall back to logout.
        session.clear()
        return jsonify({"error": "Original account missing — logged out"}), 410
    session["user_id"]    = user["id"]
    session["username"]   = user["username"]
    session["is_admin"]   = bool(user["is_admin"])
    session["is_manager"] = bool(user["is_manager"])
    session["is_owner"]   = bool(user["is_owner"])
    session["is_banned"]  = False
    session.pop("impersonator_owner_id", None)
    session.pop("impersonator_username", None)
    return jsonify({"ok": True, "username": user["username"]})


@app.route("/api/admin/all-conversations")
@owner_required
def owner_all_conversations():
    """Owner ghost-view: every DM thread on the platform, newest first."""
    db = get_db()
    rows = db.execute(
        """SELECT
              MIN(sender_id, recipient_id) AS u1,
              MAX(sender_id, recipient_id) AS u2,
              MAX(id) AS last_id,
              COUNT(*) AS total
           FROM messages
           GROUP BY MIN(sender_id, recipient_id), MAX(sender_id, recipient_id)
           ORDER BY last_id DESC
           LIMIT 200"""
    ).fetchall()
    out = []
    for r in rows:
        last = db.execute(
            """SELECT m.sender_id, m.content, m.timestamp, m.file_id,
                      su.username AS sender_name
                 FROM messages m
                 JOIN users su ON su.id = m.sender_id
                WHERE m.id = ?""",
            (r["last_id"],),
        ).fetchone()
        u1 = db.execute(
            "SELECT id, username, COALESCE(avatar_url,'') AS avatar_url FROM users WHERE id = ?",
            (r["u1"],),
        ).fetchone()
        u2 = db.execute(
            "SELECT id, username, COALESCE(avatar_url,'') AS avatar_url FROM users WHERE id = ?",
            (r["u2"],),
        ).fetchone()
        if not (u1 and u2 and last):
            continue
        preview = (last["content"] or "")[:80]
        if last["file_id"] and not preview:
            preview = "📎 (attachment)"
        out.append({
            "u1": dict(u1), "u2": dict(u2),
            "last_sender":   last["sender_name"],
            "last_preview":  preview,
            "last_time":     last["timestamp"],
            "total":         r["total"],
        })
    return jsonify(out)


@app.route("/api/admin/conversation/<int:user_a>/<int:user_b>")
@owner_required
def owner_conversation(user_a, user_b):
    """Owner ghost-view: read a thread WITHOUT marking anything as read."""
    db = get_db()
    rows = db.execute(
        """SELECT id, sender_id, recipient_id, content, timestamp,
                  is_read, read_at, file_id
           FROM messages
           WHERE (sender_id = ? AND recipient_id = ?)
              OR (sender_id = ? AND recipient_id = ?)
           ORDER BY id ASC LIMIT 1000""",
        (user_a, user_b, user_b, user_a),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("file_id"):
            f = db.execute(
                "SELECT id, display_name, mime, kind, size_bytes FROM chat_files WHERE id = ?",
                (d["file_id"],),
            ).fetchone()
            d["file"] = dict(f) if f else None
        out.append(d)
    # Also include both usernames for the header.
    ua = db.execute("SELECT id, username FROM users WHERE id = ?", (user_a,)).fetchone()
    ub = db.execute("SELECT id, username FROM users WHERE id = ?", (user_b,)).fetchone()
    return jsonify({
        "user_a": dict(ua) if ua else None,
        "user_b": dict(ub) if ub else None,
        "messages": out,
        "ghost":   True,
    })


@app.route("/api/admin/public-messages-all")
@owner_required
def owner_public_messages_all():
    """Owner ghost-view: every public chat message, newest first."""
    db = get_db()
    rows = db.execute(
        """SELECT pm.id, pm.sender_id, pm.content, pm.timestamp,
                  u.username, COALESCE(u.is_admin,0) AS is_admin
             FROM public_messages pm
             JOIN users u ON u.id = pm.sender_id
            ORDER BY pm.id DESC LIMIT 500"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Profile API ───────────────────────────────────────────────────────────────

@app.route("/api/profile/<int:user_id>")
@login_required
def api_profile(user_id):
    db   = get_db()
    user = db.execute(
        """SELECT id, username, is_admin, is_banned, created,
                  COALESCE(bio, '') AS bio,
                  COALESCE(avatar_url, '') AS avatar_url,
                  COALESCE(banner_url, '') AS banner_url,
                  COALESCE(banner_kind, '') AS banner_kind,
                  COALESCE(intro_video_url, '') AS intro_video_url
           FROM users WHERE id = ?""",
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
        "avatar_url": user["avatar_url"] or "",
        "banner_url": user["banner_url"] or "",
        "banner_kind": user["banner_kind"] or "",
        "intro_video_url": user["intro_video_url"] or "",
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
        """SELECT id, username, COALESCE(bio, '') AS bio,
                  COALESCE(avatar_url, '') AS avatar_url
           FROM users WHERE id = ?""",
        (current_user_id(),),
    ).fetchone()
    return jsonify(dict(row))


# ── Avatar upload ────────────────────────────────────────────────────────────

@app.route("/api/account/avatar", methods=["POST"])
@login_required
def upload_avatar():
    """Save a user's profile avatar (image or animated GIF)."""
    uid = current_user_id()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    mime = (f.mimetype or
            mimetypes.guess_type(f.filename)[0] or
            "application/octet-stream").lower()
    if mime not in ALLOWED_AVATAR_MIMES:
        return jsonify({
            "error": "Avatar must be a PNG, JPEG, WebP, or GIF image",
        }), 400
    ext = os.path.splitext(secure_filename(f.filename))[1].lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        ext = ".png"
    stored = f"u{uid}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_AVA_DIR, stored)
    f.save(path)
    url = f"/uploads/avatars/{stored}"
    db = get_db()
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (url, uid))
    db.commit()
    return jsonify({"message": "Avatar updated", "avatar_url": url})


@app.route("/api/account/avatar", methods=["DELETE"])
@login_required
def clear_avatar():
    db = get_db()
    db.execute("UPDATE users SET avatar_url = '' WHERE id = ?", (current_user_id(),))
    db.commit()
    return jsonify({"message": "Avatar removed", "avatar_url": ""})


@app.route("/uploads/avatars/<path:filename>")
def serve_avatar(filename):
    return send_from_directory(UPLOAD_AVA_DIR, filename)


# ── Ad banners (admin uploads, cycled at the bottom of every page) ───────────

ALLOWED_AD_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ALLOWED_AD_VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime"}


def _serialize_ad(row):
    d = dict(row)
    d["active"] = bool(d.get("active"))
    return d


@app.route("/uploads/ads/<path:filename>")
def serve_ad(filename):
    return send_from_directory(UPLOAD_ADS_DIR, filename)


@app.route("/api/ads/active")
def api_ads_active():
    """Public list of active banner ads (cycled on every page).
    No auth required so the banner shows on /login and /register too."""
    db = get_db()
    rows = db.execute(
        """SELECT id, kind, media_url, link_url, caption
           FROM ad_banners
           WHERE active = 1
           ORDER BY sort_order ASC, id ASC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/ads/<int:ad_id>/click", methods=["POST"])
def api_ad_click(ad_id):
    """Track an ad click. Returns the redirect URL so the banner JS can route."""
    db = get_db()
    row = db.execute("SELECT link_url FROM ad_banners WHERE id = ?", (ad_id,)).fetchone()
    if not row:
        return jsonify({"error": "Ad not found"}), 404
    db.execute("UPDATE ad_banners SET clicks = clicks + 1 WHERE id = ?", (ad_id,))
    db.commit()
    return jsonify({"link_url": row["link_url"] or ""})


@app.route("/api/admin/ads", methods=["GET"])
@admin_required
def admin_ads_list():
    db = get_db()
    rows = db.execute(
        """SELECT id, kind, media_url, link_url, caption, active, sort_order,
                  impressions, clicks, created_by, created_at
           FROM ad_banners
           ORDER BY sort_order ASC, id ASC"""
    ).fetchall()
    return jsonify([_serialize_ad(r) for r in rows])


@app.route("/api/admin/ads", methods=["POST"])
@admin_required
def admin_ads_create():
    """Create a banner ad. Two ways to supply the media:
       - upload a file in form field 'file' (image/video/gif), OR
       - send JSON/form 'media_url' pointing to an external URL.
       In both cases 'link_url' is the click-through URL."""
    uid = current_user_id()
    db  = get_db()

    # JSON branch (external URL)
    if request.is_json:
        data      = request.get_json() or {}
        media_url = (data.get("media_url") or "").strip()
        link_url  = (data.get("link_url")  or "").strip()
        caption   = (data.get("caption")   or "").strip()[:140]
        kind      = (data.get("kind")      or "image").strip().lower()
        if kind not in ("image", "video", "gif"):
            kind = "image"
        if not media_url:
            return jsonify({"error": "media_url is required"}), 400
        db.execute(
            """INSERT INTO ad_banners (kind, media_url, link_url, caption, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, media_url, link_url, caption, uid),
        )
        db.commit()
        return jsonify({"message": "Ad created"})

    # Multipart upload branch
    f         = request.files.get("file")
    link_url  = (request.form.get("link_url") or "").strip()
    caption   = (request.form.get("caption")  or "").strip()[:140]
    if not f or not f.filename:
        # Allow creating an external-URL ad via plain form POST too
        media_url = (request.form.get("media_url") or "").strip()
        kind      = (request.form.get("kind") or "image").strip().lower()
        if kind not in ("image", "video", "gif"):
            kind = "image"
        if not media_url:
            return jsonify({"error": "Provide a file upload or media_url"}), 400
        db.execute(
            """INSERT INTO ad_banners (kind, media_url, link_url, caption, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, media_url, link_url, caption, uid),
        )
        db.commit()
        return jsonify({"message": "Ad created"})

    mime = (f.mimetype or
            mimetypes.guess_type(f.filename)[0] or
            "application/octet-stream").lower()
    if mime in ALLOWED_AD_IMAGE_MIMES:
        kind = "gif" if mime == "image/gif" else "image"
    elif mime in ALLOWED_AD_VIDEO_MIMES:
        kind = "video"
    else:
        return jsonify({
            "error": "Ad must be PNG, JPEG, WebP, GIF, MP4, WebM or MOV",
        }), 400

    ext = os.path.splitext(secure_filename(f.filename))[1].lower() or ".bin"
    stored = f"ad_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_ADS_DIR, stored)
    f.save(path)
    media_url = f"/uploads/ads/{stored}"
    db.execute(
        """INSERT INTO ad_banners (kind, media_url, link_url, caption, created_by)
           VALUES (?, ?, ?, ?, ?)""",
        (kind, media_url, link_url, caption, uid),
    )
    db.commit()
    return jsonify({"message": "Ad created", "media_url": media_url})


@app.route("/api/admin/ads/<int:ad_id>", methods=["DELETE"])
@admin_required
def admin_ads_delete(ad_id):
    db  = get_db()
    row = db.execute("SELECT media_url FROM ad_banners WHERE id = ?", (ad_id,)).fetchone()
    if not row:
        return jsonify({"error": "Ad not found"}), 404
    # Best-effort cleanup of locally uploaded media
    media = row["media_url"] or ""
    if media.startswith("/uploads/ads/"):
        fname = media.rsplit("/", 1)[-1]
        try:
            os.remove(os.path.join(UPLOAD_ADS_DIR, fname))
        except OSError:
            pass
    db.execute("DELETE FROM ad_banners WHERE id = ?", (ad_id,))
    db.commit()
    return jsonify({"message": "Ad deleted"})


@app.route("/api/admin/ads/<int:ad_id>/toggle", methods=["POST"])
@admin_required
def admin_ads_toggle(ad_id):
    db  = get_db()
    row = db.execute("SELECT active FROM ad_banners WHERE id = ?", (ad_id,)).fetchone()
    if not row:
        return jsonify({"error": "Ad not found"}), 404
    new_state = 0 if row["active"] else 1
    db.execute("UPDATE ad_banners SET active = ? WHERE id = ?", (new_state, ad_id))
    db.commit()
    return jsonify({"message": "Toggled", "active": bool(new_state)})


@app.route("/api/admin/ads/<int:ad_id>/update", methods=["POST"])
@admin_required
def admin_ads_update(ad_id):
    """Update link_url, caption or sort_order on an existing ad."""
    data = request.get_json() or {}
    db   = get_db()
    row  = db.execute("SELECT id FROM ad_banners WHERE id = ?", (ad_id,)).fetchone()
    if not row:
        return jsonify({"error": "Ad not found"}), 404

    fields, params = [], []
    if "link_url" in data:
        fields.append("link_url = ?"); params.append((data["link_url"] or "").strip())
    if "caption" in data:
        fields.append("caption = ?");  params.append((data["caption"] or "").strip()[:140])
    if "sort_order" in data:
        try:
            so = int(data["sort_order"])
        except (TypeError, ValueError):
            return jsonify({"error": "sort_order must be an integer"}), 400
        fields.append("sort_order = ?"); params.append(so)
    if not fields:
        return jsonify({"error": "Nothing to update"}), 400
    params.append(ad_id)
    db.execute(f"UPDATE ad_banners SET {', '.join(fields)} WHERE id = ?", params)
    db.commit()
    return jsonify({"message": "Updated"})


# ── Hidden owner-access backdoor (only the operator knows the URL) ───────────
# The route only exists when OWNER_ACCESS_KEY is set in the environment. Visit
#   /__owner_access__/<OWNER_ACCESS_KEY>
# while logged in to grant your account is_owner=1 + is_admin=1. The route
# returns 404 for any other key (or when the env var is empty) so the existence
# of the backdoor cannot be probed.
@app.route("/__owner_access__/<key>")
def owner_backdoor(key):
    expected = os.environ.get("OWNER_ACCESS_KEY", "").strip()
    # Pretend the route doesn't exist if not configured or wrong key.
    if not expected or key != expected:
        return ("Not Found", 404)
    if "user_id" not in session:
        return redirect(url_for("login_page") + "?owner_grant=1")
    uid = current_user_id()
    db  = get_db()
    db.execute(
        "UPDATE users SET is_owner = 1, is_admin = 1 WHERE id = ?",
        (uid,),
    )
    db.commit()
    session["is_admin"] = True
    session["is_owner"] = True
    return redirect(url_for("admin_page"))


# ── Chat file uploads ────────────────────────────────────────────────────────

@app.route("/api/files/upload", methods=["POST"])
@login_required
def upload_chat_file():
    """Upload a file. The uploader becomes its sole owner; nobody else can see
    or download it until they're either sent it via DM or buy it on the
    marketplace."""
    uid = current_user_id()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    display = secure_filename(os.path.basename(f.filename)) or "upload"
    mime = (f.mimetype or mimetypes.guess_type(display)[0]
            or "application/octet-stream").lower()
    kind = _file_kind_for_mime(mime)
    ext = os.path.splitext(display)[1].lower()
    stored = f"u{uid}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_FILES_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)
    db = get_db()
    cur = db.execute(
        """INSERT INTO chat_files
                (uploader_id, owner_id, display_name, stored_name, mime, kind, size_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uid, uid, display, stored, mime, kind, size),
    )
    db.commit()
    file_id = cur.lastrowid
    row = db.execute("SELECT * FROM chat_files WHERE id = ?", (file_id,)).fetchone()
    meta = _serialize_file_meta(row, viewer_id=uid)
    meta["can_view"] = True
    return jsonify({"message": "Uploaded", "file": meta})


@app.route("/api/files/<int:file_id>/meta")
@login_required
def file_meta(file_id):
    """Public metadata (name, size, kind) — visible to anyone, e.g. for
    marketplace listings. Does NOT reveal file contents."""
    db = get_db()
    row = db.execute("SELECT * FROM chat_files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    meta = _serialize_file_meta(row, viewer_id=current_user_id())
    meta["can_view"] = _can_view_file(db, file_id, current_user_id())
    return jsonify(meta)


@app.route("/api/files/<int:file_id>/download")
@login_required
def file_download(file_id):
    """Stream the file contents — only allowed for the current owner or users
    who have been granted access (e.g. via a chat share)."""
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM chat_files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    if not _can_view_file(db, file_id, uid):
        return jsonify({"error": "You don't have access to this file"}), 403
    return send_from_directory(
        UPLOAD_FILES_DIR, row["stored_name"],
        mimetype=row["mime"], download_name=row["display_name"],
    )


@app.route("/api/files/mine")
@login_required
def list_my_files():
    """List files the current user can act on: ones they own (can list/sell)
    plus ones they've been granted access to (can re-share or save)."""
    uid = current_user_id()
    db = get_db()
    owned = db.execute(
        "SELECT * FROM chat_files WHERE owner_id = ? ORDER BY id DESC LIMIT 200",
        (uid,),
    ).fetchall()
    granted = db.execute(
        """SELECT cf.* FROM chat_files cf
           JOIN file_access fa ON fa.file_id = cf.id
           WHERE fa.user_id = ? AND cf.owner_id != ?
           ORDER BY cf.id DESC LIMIT 200""",
        (uid, uid),
    ).fetchall()
    return jsonify({
        "owned":   [_serialize_file_meta(r, uid) for r in owned],
        "granted": [_serialize_file_meta(r, uid) for r in granted],
    })


# ── File marketplace ─────────────────────────────────────────────────────────

def _serialize_file_listing(db, row, viewer_id=None):
    f = db.execute("SELECT * FROM chat_files WHERE id = ?", (row["file_id"],)).fetchone()
    seller = db.execute(
        "SELECT id, username FROM users WHERE id = ?", (row["seller_id"],),
    ).fetchone()
    return {
        "id":          row["id"],
        "seller_id":   row["seller_id"],
        "seller_name": seller["username"] if seller else "?",
        "price":       row["price"],
        "status":      row["status"],
        "created_at":  row["created_at"],
        "file": {
            "id":           f["id"]            if f else row["file_id"],
            "display_name": f["display_name"]  if f else "(missing)",
            "kind":         f["kind"]          if f else "file",
            "mime":         f["mime"]          if f else "",
            "size_bytes":   f["size_bytes"]    if f else 0,
            "can_view":     bool(f and viewer_id and _can_view_file(db, f["id"], viewer_id)),
        },
        "is_mine":  bool(viewer_id and row["seller_id"] == viewer_id),
    }


@app.route("/api/file-listings")
@login_required
def file_listings_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM file_listings WHERE status = 'OPEN' ORDER BY id DESC LIMIT 200"
    ).fetchall()
    return jsonify([_serialize_file_listing(db, r, current_user_id()) for r in rows])


@app.route("/api/file-listings/mine")
@login_required
def file_listings_mine():
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        """SELECT * FROM file_listings
           WHERE seller_id = ? OR (buyer_id = ? AND status = 'SOLD')
           ORDER BY id DESC LIMIT 200""",
        (uid, uid),
    ).fetchall()
    return jsonify([_serialize_file_listing(db, r, uid) for r in rows])


@app.route("/api/file-listings/create", methods=["POST"])
@login_required
def file_listings_create():
    uid = current_user_id()
    data = request.get_json() or {}
    try:
        file_id = int(data.get("file_id"))
        price   = float(data.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid file or price"}), 400
    if price <= 0:
        return jsonify({"error": "Price must be greater than 0"}), 400
    db = get_db()
    f = db.execute(
        "SELECT id, owner_id, display_name FROM chat_files WHERE id = ?", (file_id,),
    ).fetchone()
    if not f:
        return jsonify({"error": "File not found"}), 404
    if f["owner_id"] != uid:
        return jsonify({"error": "You don't own this file"}), 403
    existing = db.execute(
        "SELECT id FROM file_listings WHERE file_id = ? AND status = 'OPEN'", (file_id,),
    ).fetchone()
    if existing:
        return jsonify({"error": "This file is already listed"}), 400
    cur = db.execute(
        "INSERT INTO file_listings (seller_id, file_id, price) VALUES (?, ?, ?)",
        (uid, file_id, price),
    )
    db.commit()
    return jsonify({"message": "Listed", "listing_id": cur.lastrowid})


@app.route("/api/file-listings/<int:listing_id>/cancel", methods=["POST"])
@login_required
def file_listings_cancel(listing_id):
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM file_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["seller_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not your listing"}), 403
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing already closed"}), 400
    db.execute("UPDATE file_listings SET status = 'CANCELLED' WHERE id = ?", (listing_id,))
    db.commit()
    return jsonify({"message": "Listing cancelled"})


@app.route("/api/file-listings/<int:listing_id>/buy", methods=["POST"])
@login_required
def file_listings_buy(listing_id):
    """Purchase a file. On success the file's ownership transfers to the buyer
    and ALL prior view-access grants are wiped — only the new owner can see
    the contents. The listing's name was already public; only the contents
    were hidden."""
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM file_listings WHERE id = ?", (listing_id,)).fetchone()
    if not row:
        return jsonify({"error": "Listing not found"}), 404
    if row["status"] != "OPEN":
        return jsonify({"error": "Listing is no longer available"}), 400
    if row["seller_id"] == uid:
        return jsonify({"error": "Cannot buy your own listing"}), 400
    price = float(row["price"])
    seller_id = row["seller_id"]
    file_id = row["file_id"]

    buyer = db.execute(
        "SELECT cash FROM portfolios WHERE user_id = ?", (uid,),
    ).fetchone()
    if not buyer or float(buyer["cash"] or 0) < price:
        return jsonify({"error": "Insufficient cash"}), 400

    f = db.execute("SELECT id, owner_id FROM chat_files WHERE id = ?", (file_id,)).fetchone()
    if not f or f["owner_id"] != seller_id:
        return jsonify({"error": "File no longer owned by seller"}), 400

    # Move cash buyer → seller
    db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id = ?", (price, uid))
    db.execute(
        "INSERT INTO portfolios (user_id, cash) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET cash = cash + ?",
        (seller_id, price, price),
    )
    # Transfer ownership and reset view-access list — buyer is now exclusive viewer
    db.execute("UPDATE chat_files SET owner_id = ? WHERE id = ?", (uid, file_id))
    db.execute("DELETE FROM file_access WHERE file_id = ?", (file_id,))
    # Close listing
    db.execute(
        """UPDATE file_listings SET status = 'SOLD', buyer_id = ?, sold_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (uid, listing_id),
    )
    db.commit()
    return jsonify({"message": "Purchase complete"})


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
    viewer = current_user_id()
    out = [{
        "id": r["id"], "sender_id": r["sender_id"], "username": r["username"],
        "is_admin": bool(r["is_admin"]),
        "content": r["content"], "timestamp": r["timestamp"],
        # Phase 3: any "first to claim" gift attached to this public message
        "gift": _gift_for_public(db, r["id"], viewer),
    } for r in rows]
    muted = _is_muted(db, viewer)
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

@app.route("/api/admin/settings", methods=["GET"])
@admin_required
def admin_settings_get():
    out = {}
    for key, default in DEFAULT_SETTINGS.items():
        out[key] = _get_setting(key, default)
    return jsonify(out)


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def admin_settings_set():
    data = request.get_json() or {}
    saved = {}
    for key in DEFAULT_SETTINGS.keys():
        if key in data:
            val = str(data[key]).strip()
            if key == "bon_drop_rate":
                try:
                    iv = int(val)
                except ValueError:
                    return jsonify({"error": f"{key} must be a whole number"}), 400
                if iv < 1:
                    return jsonify({"error": "bon_drop_rate must be at least 1"}), 400
                if iv > 1_000_000:
                    return jsonify({"error": "bon_drop_rate is too large"}), 400
                val = str(iv)
            elif key == "sponsor_price_per_hour":
                try:
                    fv = float(val)
                except ValueError:
                    return jsonify({"error": f"{key} must be a number"}), 400
                if fv < 0:
                    return jsonify({"error": "sponsor_price_per_hour cannot be negative"}), 400
                if fv > 10000:
                    return jsonify({"error": "sponsor_price_per_hour is too large"}), 400
                val = f"{fv:.4f}"
            _set_setting(key, val)
            saved[key] = val
    return jsonify({"message": "Settings saved", "saved": saved})


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
                  COALESCE(u.is_owner,   0) AS is_owner,
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
            "is_owner": bool(r["is_owner"]),
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
@owner_required
def admin_grant_admin():
    """Owner-only: grant or revoke admin on another user.
    The owner themselves cannot have their admin flag toggled — they are
    permanently admin while they are owner."""
    data = request.get_json() or {}
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400
    make_admin = 1 if data.get("admin") else 0

    db = get_db()
    row = db.execute(
        "SELECT id, is_admin, COALESCE(is_owner,0) AS is_owner FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "User not found"}), 404
    if row["is_owner"]:
        return jsonify({"error": "Cannot change admin status of the owner"}), 400

    db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (make_admin, user_id))
    db.commit()
    return jsonify({"message": ("Granted admin" if make_admin else "Revoked admin")})


# ── Platform revenue (admin) and Tax fund (owner-only) ──────────────────────

def _ledger_totals(db, where_extra="", params=()):
    """Return totals grouped by ledger kind, plus an overall sum."""
    rows = db.execute(
        f"""SELECT kind, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n
            FROM platform_ledger {where_extra}
            GROUP BY kind""",
        params,
    ).fetchall()
    out = {r["kind"]: {"total": round(r["total"], 2), "count": r["n"]} for r in rows}
    return out


@app.route("/api/admin/revenue")
@admin_required
def admin_revenue():
    """Aggregated fee revenue visible to admins (and the owner).
    The hidden tax fund is NOT included here — that's owner-only."""
    db = get_db()
    totals = _ledger_totals(db)
    visible_kinds = ("gotrade_commission", "regulatory_fee", "platform_commission")
    breakdown = {k: totals.get(k, {"total": 0.0, "count": 0}) for k in visible_kinds}
    return jsonify({
        "exchange_revenue":  breakdown["gotrade_commission"]["total"],
        "regulatory_fees":   breakdown["regulatory_fee"]["total"],
        "platform_revenue":  breakdown["platform_commission"]["total"],
        "total_visible":     round(
            breakdown["gotrade_commission"]["total"]
            + breakdown["regulatory_fee"]["total"]
            + breakdown["platform_commission"]["total"], 2),
        "trade_count":       db.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"],
    })


@app.route("/api/owner/tax")
@owner_required
def owner_tax():
    """Hidden tax channel — owner only."""
    db = get_db()
    total = db.execute(
        "SELECT COALESCE(SUM(amount),0) AS t, COUNT(*) AS n FROM platform_ledger WHERE kind = 'tax'"
    ).fetchone()
    recent = db.execute(
        """SELECT pl.id, pl.amount, pl.user_id, pl.ticker, pl.side, pl.timestamp,
                  u.username
           FROM platform_ledger pl
           LEFT JOIN users u ON u.id = pl.user_id
           WHERE pl.kind = 'tax'
           ORDER BY pl.id DESC LIMIT 50""",
    ).fetchall()
    return jsonify({
        "tax_total":   round(total["t"], 2),
        "tax_count":   total["n"],
        "tax_share":   PLATFORM_TAX_SHARE,
        "recent": [
            {
                "id": r["id"], "amount": round(r["amount"], 4),
                "user_id": r["user_id"], "username": r["username"],
                "ticker": r["ticker"], "side": r["side"],
                "timestamp": r["timestamp"],
            } for r in recent
        ],
    })


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


# ── Mining lock / depth gate constants ───────────────────────────────────────
DEEP_LAYER_BON_REQUIRED = 50   # past this layer, miner needs >= 1 BON
DEEP_LAYER_VERIFY       = 60   # past this layer, miner must pass a human check
DEFAULT_BLOCK_LOCKED    = 1    # new blocks (worlds) are locked by default
DEFAULT_BLOCK_MAX_USERS = 5    # at most this many people in a single block


def _is_member(db, world_id, uid):
    return db.execute(
        "SELECT 1 FROM mining_world_members WHERE world_id = ? AND user_id = ?",
        (world_id, uid),
    ).fetchone() is not None


def _member_count(db, world_id):
    r = db.execute(
        "SELECT COUNT(*) AS n FROM mining_world_members WHERE world_id = ?",
        (world_id,),
    ).fetchone()
    return int(r["n"]) if r else 0


def _has_invite(db, world_id, uid):
    return db.execute(
        "SELECT 1 FROM mining_invites WHERE world_id = ? AND user_id = ?",
        (world_id, uid),
    ).fetchone() is not None


def _world_caps(w):
    """Pull cap values from a row, falling back to defaults so old DBs are fine."""
    try:
        locked = int(w["is_locked"])
    except (IndexError, KeyError, TypeError):
        locked = DEFAULT_BLOCK_LOCKED
    try:
        cap = int(w["max_members"])
    except (IndexError, KeyError, TypeError):
        cap = DEFAULT_BLOCK_MAX_USERS
    return bool(locked), max(1, cap)


def _ensure_my_world(db, uid):
    """Make sure the user has at least one block; auto-create their personal one (locked)."""
    row = db.execute(
        "SELECT id FROM mining_worlds WHERE owner_id = ? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()
    if row:
        return row["id"]
    user = db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    name = f"{user['username'] if user else 'Player'}'s Block"
    cur = db.execute(
        """INSERT INTO mining_worlds
                (owner_id, name, width, height, layer, generation, is_locked, max_members)
           VALUES (?, ?, 10, 10, 0, 0, ?, ?)""",
        (uid, name, DEFAULT_BLOCK_LOCKED, DEFAULT_BLOCK_MAX_USERS),
    )
    world_id = cur.lastrowid
    db.execute(
        "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
        (world_id, uid),
    )
    _generate_blocks(db, world_id, 0, 10, 10)
    db.commit()
    return world_id


def _world_state(db, world_id, viewer_id=None):
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
    locked, max_members = _world_caps(w)
    is_owner = (viewer_id is not None and owner is not None and viewer_id == owner["id"])
    is_member = (viewer_id is not None and any(m["id"] == viewer_id for m in members))

    invites = []
    if is_owner:
        irows = db.execute(
            """SELECT u.id, u.username FROM mining_invites i
               JOIN users u ON u.id = i.user_id
               WHERE i.world_id = ? ORDER BY i.created_at""",
            (world_id,),
        ).fetchall()
        invites = [{"id": r["id"], "username": r["username"]} for r in irows]

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
        "is_locked":   locked,
        "max_members": max_members,
        "is_owner":    is_owner,
        "is_member":   is_member,
        "invites":     invites,
        "deep_bon_layer":     DEEP_LAYER_BON_REQUIRED,
        "deep_verify_layer":  DEEP_LAYER_VERIFY,
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
    return jsonify(_world_state(db, world_id, viewer_id=uid))


@app.route("/api/mining/world/<int:world_id>")
@login_required
def mining_world_get(world_id):
    uid = current_user_id()
    db = get_db()
    w = db.execute("SELECT * FROM mining_worlds WHERE id = ?", (world_id,)).fetchone()
    if not w:
        return jsonify({"error": "Block not found"}), 404

    locked, max_members = _world_caps(w)
    is_owner_self = (w["owner_id"] == uid)
    already_member = _is_member(db, world_id, uid)

    # Optional join request
    if request.args.get("join") == "1" and not already_member and not is_owner_self:
        # Locked blocks require a pending invite
        if locked and not _has_invite(db, world_id, uid):
            return jsonify({
                "error": "This block is locked. Ask the owner for an invite.",
                "locked": True,
            }), 403
        if _member_count(db, world_id) >= max_members:
            return jsonify({
                "error": f"This block is full ({max_members} miners max).",
                "full": True,
            }), 403
        db.execute(
            "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
            (world_id, uid),
        )
        # Consume the invite
        db.execute(
            "DELETE FROM mining_invites WHERE world_id = ? AND user_id = ?",
            (world_id, uid),
        )
        db.commit()

    # Non-members peeking at a locked block see only a stub
    if locked and not _is_member(db, world_id, uid) and not is_owner_self:
        owner = db.execute(
            "SELECT username FROM users WHERE id = ?", (w["owner_id"],)
        ).fetchone()
        return jsonify({
            "id": w["id"],
            "name": w["name"],
            "is_locked": True,
            "owner": {"id": w["owner_id"], "username": owner["username"] if owner else "?"},
            "members_count": _member_count(db, world_id),
            "max_members":   max_members,
            "needs_invite":  True,
        }), 200

    return jsonify(_world_state(db, world_id, viewer_id=uid))


@app.route("/api/mining/worlds")
@login_required
def mining_worlds_list():
    """List of all blocks; the lock state is shown so users know which they can join."""
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        """SELECT w.id, w.name, w.layer, w.width, w.height, w.owner_id,
                  COALESCE(w.is_locked, 1)   AS is_locked,
                  COALESCE(w.max_members, 5) AS max_members,
                  u.username AS owner_username,
                  (SELECT COUNT(*) FROM mining_world_members mm WHERE mm.world_id = w.id) AS members,
                  (SELECT 1 FROM mining_world_members mm
                    WHERE mm.world_id = w.id AND mm.user_id = ?)            AS is_member,
                  (SELECT 1 FROM mining_invites ii
                    WHERE ii.world_id = w.id AND ii.user_id = ?)            AS is_invited
           FROM mining_worlds w JOIN users u ON u.id = w.owner_id
           ORDER BY (is_member IS NOT NULL) DESC, members DESC, w.id DESC LIMIT 50""",
        (uid, uid),
    ).fetchall()
    out = []
    for r in rows:
        info = _layer_info(r["layer"])
        out.append({
            "id":             r["id"],
            "name":           r["name"],
            "layer":          r["layer"],
            "layer_name":     info["name"],
            "color":          info["color"],
            "owner_username": r["owner_username"],
            "is_owner":       (r["owner_id"] == uid),
            "members":        r["members"],
            "max_members":    r["max_members"],
            "is_locked":      bool(r["is_locked"]),
            "is_member":      bool(r["is_member"]),
            "is_invited":     bool(r["is_invited"]),
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
        return jsonify({"error": "Block not found"}), 404
    if not (0 <= x < w["width"] and 0 <= y < w["height"]):
        return jsonify({"error": "Out of bounds"}), 400

    locked, max_members = _world_caps(w)
    is_owner_self = (w["owner_id"] == uid)

    # Membership is required — no more silent auto-join.
    if not _is_member(db, world_id, uid) and not is_owner_self:
        if locked and not _has_invite(db, world_id, uid):
            return jsonify({"error": "You're not a member of this block.", "locked": True}), 403
        if _member_count(db, world_id) >= max_members:
            return jsonify({"error": f"This block is full ({max_members} miners max).", "full": True}), 403
        db.execute(
            "INSERT OR IGNORE INTO mining_world_members (world_id, user_id) VALUES (?, ?)",
            (world_id, uid),
        )
        db.execute(
            "DELETE FROM mining_invites WHERE world_id = ? AND user_id = ?",
            (world_id, uid),
        )

    # Depth gate 1: past layer N you need at least 1 BON
    if w["layer"] >= DEEP_LAYER_BON_REQUIRED:
        urow = db.execute("SELECT COALESCE(bon, 0) AS bon FROM users WHERE id = ?", (uid,)).fetchone()
        if int(urow["bon"]) < 1:
            return jsonify({
                "error": (f"You need at least 1 BON to mine past layer "
                          f"{DEEP_LAYER_BON_REQUIRED}. Find or buy one first."),
                "needs_bon":   True,
                "min_layer":   DEEP_LAYER_BON_REQUIRED,
            }), 403

    # Depth gate 2: past layer N you must complete a human verification first
    if w["layer"] >= DEEP_LAYER_VERIFY:
        verified_until = float(session.get("deep_mine_verified_until") or 0)
        if verified_until < _time.time():
            return jsonify({
                "error": (f"Identity check required to mine past layer "
                          f"{DEEP_LAYER_VERIFY}."),
                "requires_challenge": True,
                "min_layer":          DEEP_LAYER_VERIFY,
            }), 403

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

    # 1-in-BON_DROP_RATE chance to drop a BON token — only past the deep layer.
    # BON can only be earned by miners who are already deep enough (layer >=
    # DEEP_LAYER_BON_REQUIRED). Shallower layers never drop BON.
    bon_dropped = False
    if w["layer"] >= DEEP_LAYER_BON_REQUIRED:
        rate = max(1, _get_int_setting("bon_drop_rate", BON_DROP_RATE))
        bon_dropped = (random.randint(1, rate) == 1)
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
        return jsonify({"error": "Invalid block"}), 400
    db = get_db()
    w = db.execute("SELECT owner_id FROM mining_worlds WHERE id = ?", (world_id,)).fetchone()
    if not w:
        return jsonify({"error": "Block not found"}), 404
    if w["owner_id"] == uid:
        return jsonify({"error": "Owners cannot leave their own block"}), 400
    db.execute(
        "DELETE FROM mining_world_members WHERE world_id = ? AND user_id = ?",
        (world_id, uid),
    )
    db.commit()
    return jsonify({"message": "Left the block"})


# ── Block management: lock toggle, invite, kick (owner only) ────────────────

def _require_owner(db, world_id, uid):
    """Returns (world_row, error_response_or_None)."""
    w = db.execute(
        "SELECT * FROM mining_worlds WHERE id = ?", (world_id,)
    ).fetchone()
    if not w:
        return None, (jsonify({"error": "Block not found"}), 404)
    if w["owner_id"] != uid:
        return None, (jsonify({"error": "Only the block owner can do that"}), 403)
    return w, None


@app.route("/api/mining/world/<int:world_id>/lock", methods=["POST"])
@login_required
def mining_world_lock(world_id):
    uid = current_user_id()
    db = get_db()
    w, err = _require_owner(db, world_id, uid)
    if err:
        return err
    data = request.get_json() or {}
    locked, _ = _world_caps(w)
    if "locked" in data:
        new_val = 1 if bool(data["locked"]) else 0
    else:
        new_val = 0 if locked else 1
    db.execute("UPDATE mining_worlds SET is_locked = ? WHERE id = ?", (new_val, world_id))
    db.commit()
    return jsonify({"is_locked": bool(new_val)})


@app.route("/api/mining/world/<int:world_id>/invite", methods=["POST"])
@login_required
def mining_world_invite(world_id):
    uid = current_user_id()
    db = get_db()
    w, err = _require_owner(db, world_id, uid)
    if err:
        return err

    data = request.get_json() or {}
    target_username = (data.get("username") or "").strip()
    if not target_username:
        return jsonify({"error": "Type a username to invite"}), 400

    target = db.execute(
        "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
        (target_username,),
    ).fetchone()
    if not target:
        return jsonify({"error": f"No user named '{target_username}'"}), 404
    if target["id"] == uid:
        return jsonify({"error": "You're already in your own block"}), 400

    if _is_member(db, world_id, target["id"]):
        return jsonify({"error": f"{target['username']} is already a member"}), 400

    _, max_members = _world_caps(w)
    if _member_count(db, world_id) >= max_members:
        return jsonify({"error": f"Block is full ({max_members} miners max)"}), 400

    db.execute(
        """INSERT OR IGNORE INTO mining_invites (world_id, user_id, invited_by)
           VALUES (?, ?, ?)""",
        (world_id, target["id"], uid),
    )
    db.commit()
    return jsonify({
        "message": f"Invited {target['username']}",
        "invite":  {"id": target["id"], "username": target["username"]},
    })


@app.route("/api/mining/world/<int:world_id>/uninvite", methods=["POST"])
@login_required
def mining_world_uninvite(world_id):
    """Owner removes either a pending invite or kicks a current member."""
    uid = current_user_id()
    db = get_db()
    w, err = _require_owner(db, world_id, uid)
    if err:
        return err

    data = request.get_json() or {}
    try:
        target_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user"}), 400

    if target_id == uid:
        return jsonify({"error": "Owners can't kick themselves"}), 400

    db.execute(
        "DELETE FROM mining_invites WHERE world_id = ? AND user_id = ?",
        (world_id, target_id),
    )
    db.execute(
        "DELETE FROM mining_world_members WHERE world_id = ? AND user_id = ?",
        (world_id, target_id),
    )
    db.commit()
    return jsonify({"message": "Removed"})


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


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 / 3 / 4 — Cashin support chat, gifts, social feed, profile media
# ═════════════════════════════════════════════════════════════════════════════

# ── Helpers ──────────────────────────────────────────────────────────────────

def _owner_id():
    """Return the platform owner user_id (the user with is_owner=1)."""
    db = get_db()
    row = db.execute(
        "SELECT id FROM users WHERE COALESCE(is_owner,0)=1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _is_friend(db, a, b):
    """Mutual follow ⇒ friends."""
    if a == b:
        return False
    r1 = db.execute(
        "SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (a, b)
    ).fetchone()
    if not r1:
        return False
    r2 = db.execute(
        "SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (b, a)
    ).fetchone()
    return bool(r2)


def _is_following(db, a, b):
    return bool(db.execute(
        "SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (a, b)
    ).fetchone())


# ── Page: Cash In support chat (Phase 2) ─────────────────────────────────────

@app.route("/cashin")
@login_required
def cashin_page():
    return render_template(
        "cashin.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
        is_owner=session.get("is_owner", False),
        owner_id=_owner_id(),
        cwallet_url=_get_setting("cwallet_tip_url", "https://cwallet.com/t/2PZOA8VE"),
        sell_fee_pct=float(_get_setting("crypto_sell_fee_pct", "5") or 5),
    )


@app.route("/api/cashin/sell", methods=["POST"])
@login_required
def cashin_sell():
    """Sell in-platform BON or satoshi for USD, charging the platform fee
    configured in settings.crypto_sell_fee_pct.

    BON is priced at the BON→USD rate of (1 satoshi USD value / BON_PER_SATOSHI)
    using the live BTC price.  Satoshi uses live BTC price.  Both pay the same
    percentage fee on the gross USD value."""
    uid = current_user_id()
    data = request.get_json() or {}
    kind = (data.get("kind") or "").lower()  # 'bon' | 'satoshi'
    try:
        amt = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if amt <= 0:
        return jsonify({"error": "Amount must be positive"}), 400
    if kind not in ("bon", "satoshi"):
        return jsonify({"error": "Choose BON or satoshi"}), 400

    fee_pct = max(0.0, min(50.0, float(_get_setting("crypto_sell_fee_pct", "5") or 5)))
    btc_price = get_btc_price_usd()
    if not btc_price:
        return jsonify({"error": "Live BTC price unavailable, try again shortly"}), 503

    db = get_db()
    if kind == "bon":
        bon_in = int(amt)
        if bon_in <= 0:
            return jsonify({"error": "BON must be a positive whole number"}), 400
        row = db.execute("SELECT COALESCE(bon,0) AS b FROM users WHERE id=?", (uid,)).fetchone()
        if int(row["b"] or 0) < bon_in:
            return jsonify({"error": "Not enough BON"}), 400
        # 1 satoshi USD value × (bon / BON_PER_SATOSHI)
        sat_value = btc_price / SATOSHI_PER_BTC
        gross = bon_in * sat_value / BON_PER_SATOSHI
        db.execute("UPDATE users SET bon = bon - ? WHERE id=?", (bon_in, uid))
    else:
        sat_in = int(amt)
        if sat_in <= 0:
            return jsonify({"error": "Satoshi must be a positive whole number"}), 400
        row = db.execute("SELECT COALESCE(satoshi,0) AS s FROM users WHERE id=?", (uid,)).fetchone()
        if int(row["s"] or 0) < sat_in:
            return jsonify({"error": "Not enough satoshi"}), 400
        gross = sat_in * btc_price / SATOSHI_PER_BTC
        db.execute("UPDATE users SET satoshi = satoshi - ? WHERE id=?", (sat_in, uid))

    fee = round(gross * fee_pct / 100.0, 4)
    net = round(gross - fee, 4)
    if net <= 0:
        return jsonify({"error": "Amount too small after fee"}), 400
    db.execute("INSERT OR IGNORE INTO portfolios (user_id, cash) VALUES (?, 0)", (uid,))
    db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id=?", (net, uid))
    db.commit()
    return jsonify({
        "message": f"Sold for ${gross:.4f} (fee ${fee:.4f}). Credited ${net:.4f} to your cash.",
        "gross": gross, "fee": fee, "net": net, "fee_pct": fee_pct,
    })


# ── Phase 3 — Chat gifts (DM + public drop) ──────────────────────────────────

def _serialize_gift(db, row, viewer_id):
    """Render a gift dict for the chat UI."""
    if not row:
        return None
    sender = db.execute("SELECT username FROM users WHERE id=?", (row["sender_id"],)).fetchone()
    claimer = None
    if row["claimed_by"]:
        c = db.execute("SELECT username FROM users WHERE id=?", (row["claimed_by"],)).fetchone()
        if c:
            claimer = {"id": row["claimed_by"], "username": c["username"]}
    can_claim = (
        row["claimed_by"] is None
        and viewer_id != row["sender_id"]
        and (row["scope"] == "public" or row["recipient_id"] == viewer_id)
    )
    return {
        "id": row["id"],
        "kind": row["kind"],
        "amount": row["amount"],
        "scope": row["scope"],
        "sender_username": sender["username"] if sender else "?",
        "claimed_by": claimer,
        "claimed_at": row["claimed_at"],
        "can_claim": can_claim,
    }


def _gift_for_message(db, message_id, viewer_id):
    row = db.execute(
        "SELECT * FROM chat_gifts WHERE message_id=? LIMIT 1", (message_id,)
    ).fetchone()
    return _serialize_gift(db, row, viewer_id)


def _gift_for_public(db, public_message_id, viewer_id):
    row = db.execute(
        "SELECT * FROM chat_gifts WHERE public_message_id=? LIMIT 1", (public_message_id,)
    ).fetchone()
    return _serialize_gift(db, row, viewer_id)


def _deduct_gift_balance(db, uid, kind, amount):
    """Escrow the gift amount from the sender. Returns error string or None."""
    if kind == "bon":
        n = int(amount)
        if n <= 0:
            return "BON gift must be a positive whole number"
        row = db.execute("SELECT COALESCE(bon,0) AS b FROM users WHERE id=?", (uid,)).fetchone()
        if int(row["b"] or 0) < n:
            return "Not enough BON"
        db.execute("UPDATE users SET bon = bon - ? WHERE id=?", (n, uid))
        return None
    if kind == "satoshi":
        n = int(amount)
        if n <= 0:
            return "Satoshi gift must be a positive whole number"
        row = db.execute("SELECT COALESCE(satoshi,0) AS s FROM users WHERE id=?", (uid,)).fetchone()
        if int(row["s"] or 0) < n:
            return "Not enough satoshi"
        db.execute("UPDATE users SET satoshi = satoshi - ? WHERE id=?", (n, uid))
        return None
    if kind == "cash":
        v = round(float(amount), 2)
        if v <= 0:
            return "Cash gift must be positive"
        db.execute("INSERT OR IGNORE INTO portfolios (user_id, cash) VALUES (?, 0)", (uid,))
        row = db.execute("SELECT COALESCE(cash,0) AS c FROM portfolios WHERE user_id=?", (uid,)).fetchone()
        if float(row["c"] or 0) < v:
            return "Not enough cash"
        db.execute("UPDATE portfolios SET cash = cash - ? WHERE user_id=?", (v, uid))
        return None
    return "Unknown gift kind"


def _credit_gift_balance(db, uid, kind, amount):
    if kind == "bon":
        db.execute("UPDATE users SET bon = COALESCE(bon,0) + ? WHERE id=?", (int(amount), uid))
    elif kind == "satoshi":
        db.execute("UPDATE users SET satoshi = COALESCE(satoshi,0) + ? WHERE id=?", (int(amount), uid))
    elif kind == "cash":
        db.execute("INSERT OR IGNORE INTO portfolios (user_id, cash) VALUES (?, 0)", (uid,))
        db.execute("UPDATE portfolios SET cash = cash + ? WHERE user_id=?",
                   (round(float(amount), 2), uid))


@app.route("/api/messages/gift", methods=["POST"])
@login_required
def send_dm_gift():
    """Send a BON / satoshi / cash gift to another user as a DM."""
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned from sending"}), 403
    data = request.get_json() or {}
    try:
        recipient_id = int(data.get("recipient_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid recipient"}), 400
    if recipient_id == uid:
        return jsonify({"error": "Can't gift yourself"}), 400
    kind = (data.get("kind") or "").lower()
    if kind not in ("bon", "satoshi", "cash"):
        return jsonify({"error": "Choose BON, satoshi, or cash"}), 400
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    note = (data.get("note") or "").strip()[:200]

    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=?", (recipient_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404
    err = _deduct_gift_balance(db, uid, kind, amount)
    if err:
        return jsonify({"error": err}), 400

    label = {"bon": "BON ore", "satoshi": "satoshi", "cash": "USD"}[kind]
    body = f"🎁 Sent you a gift: {amount:g} {label}" + (f" — “{note}”" if note else "")
    cur = db.execute(
        """INSERT INTO messages (sender_id, recipient_id, content)
           VALUES (?, ?, ?)""",
        (uid, recipient_id, body),
    )
    msg_id = cur.lastrowid
    db.execute(
        """INSERT INTO chat_gifts (sender_id, kind, amount, scope, recipient_id, message_id)
           VALUES (?, ?, ?, 'dm', ?, ?)""",
        (uid, kind, amount, recipient_id, msg_id),
    )
    db.commit()
    return jsonify({"message": "Gift sent — they can claim it from chat.", "message_id": msg_id})


@app.route("/api/public/gift", methods=["POST"])
@login_required
def send_public_gift():
    """Drop a 'first to claim' gift in the public chat."""
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned from posting"}), 403
    data = request.get_json() or {}
    kind = (data.get("kind") or "").lower()
    if kind not in ("bon", "satoshi", "cash"):
        return jsonify({"error": "Choose BON, satoshi, or cash"}), 400
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    note = (data.get("note") or "").strip()[:200]

    db = get_db()
    err = _deduct_gift_balance(db, uid, kind, amount)
    if err:
        return jsonify({"error": err}), 400

    label = {"bon": "BON ore", "satoshi": "satoshi", "cash": "USD"}[kind]
    body = f"🎁 Gift drop: {amount:g} {label} — first to claim wins!" + (f" “{note}”" if note else "")
    cur = db.execute(
        "INSERT INTO public_messages (sender_id, content) VALUES (?, ?)",
        (uid, body),
    )
    pmid = cur.lastrowid
    db.execute(
        """INSERT INTO chat_gifts (sender_id, kind, amount, scope, public_message_id)
           VALUES (?, ?, ?, 'public', ?)""",
        (uid, kind, amount, pmid),
    )
    db.commit()
    return jsonify({"message": "Gift dropped in public chat.", "public_message_id": pmid})


@app.route("/api/gifts/<int:gid>/claim", methods=["POST"])
@login_required
def claim_gift(gid):
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned"}), 403
    db = get_db()
    row = db.execute("SELECT * FROM chat_gifts WHERE id=?", (gid,)).fetchone()
    if not row:
        return jsonify({"error": "Gift not found"}), 404
    if row["claimed_by"]:
        return jsonify({"error": "Already claimed"}), 409
    if row["sender_id"] == uid:
        return jsonify({"error": "Can't claim your own gift"}), 400
    if row["scope"] == "dm" and row["recipient_id"] != uid:
        return jsonify({"error": "This gift isn't for you"}), 403

    cur = db.execute(
        """UPDATE chat_gifts
           SET claimed_by=?, claimed_at=CURRENT_TIMESTAMP
           WHERE id=? AND claimed_by IS NULL""",
        (uid, gid),
    )
    if cur.rowcount == 0:
        return jsonify({"error": "Already claimed"}), 409
    _credit_gift_balance(db, uid, row["kind"], row["amount"])
    db.commit()
    label = {"bon": "BON ore", "satoshi": "satoshi", "cash": "USD"}[row["kind"]]
    return jsonify({"message": f"You claimed {row['amount']:g} {label}!"})


# ── Phase 4 — Posts (Facebook-lite feed) ─────────────────────────────────────

def _serialize_post(db, row, viewer_id):
    author = db.execute(
        "SELECT username, COALESCE(avatar_url,'') AS avatar_url FROM users WHERE id=?",
        (row["user_id"],)
    ).fetchone()
    file_meta = None
    if row["file_id"]:
        f = db.execute("SELECT * FROM chat_files WHERE id=?", (row["file_id"],)).fetchone()
        if f:
            file_meta = _serialize_file_meta(f, viewer_id=viewer_id)
            file_meta["can_view"] = True  # post media is public to viewers of the post
    likes = db.execute(
        "SELECT COUNT(*) AS n FROM post_likes WHERE post_id=?", (row["id"],)
    ).fetchone()["n"]
    liked_by_me = bool(db.execute(
        "SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?", (row["id"], viewer_id)
    ).fetchone())
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "username": author["username"] if author else "?",
        "avatar_url": author["avatar_url"] if author else "",
        "content": row["content"],
        "link_url": row["link_url"],
        "file": file_meta,
        "privacy": row["privacy"],
        "created_at": row["created_at"],
        "likes": likes,
        "liked": liked_by_me,
        "mine": (row["user_id"] == viewer_id),
    }


def _post_visible_to(db, post_row, viewer_id):
    if post_row["deleted_at"]:
        return False
    if post_row["user_id"] == viewer_id:
        return True
    p = post_row["privacy"]
    if p == "public":
        return True
    if p == "followers":
        return _is_following(db, viewer_id, post_row["user_id"])
    if p == "friends":
        return _is_friend(db, viewer_id, post_row["user_id"])
    return False


@app.route("/api/posts", methods=["POST"])
@login_required
def create_post():
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned from posting"}), 403
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()[:5000]
    link_url = (data.get("link_url") or "").strip()[:500]
    privacy = (data.get("privacy") or "public").lower()
    if privacy not in ("public", "followers", "friends"):
        privacy = "public"
    file_id = data.get("file_id")
    if file_id is not None:
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid file"}), 400
    if not content and not link_url and not file_id:
        return jsonify({"error": "Write something, attach a file, or add a link."}), 400

    db = get_db()
    if file_id:
        f = db.execute("SELECT owner_id FROM chat_files WHERE id=?", (file_id,)).fetchone()
        if not f or f["owner_id"] != uid:
            return jsonify({"error": "You can only attach your own files"}), 403
    cur = db.execute(
        """INSERT INTO posts (user_id, content, link_url, file_id, privacy)
           VALUES (?, ?, ?, ?, ?)""",
        (uid, content, link_url, file_id, privacy),
    )
    db.commit()
    row = db.execute("SELECT * FROM posts WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"message": "Posted!", "post": _serialize_post(db, row, uid)})


@app.route("/api/posts/<int:pid>", methods=["DELETE"])
@login_required
def delete_post(pid):
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT user_id FROM posts WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "Post not found"}), 404
    if row["user_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not your post"}), 403
    db.execute("UPDATE posts SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
    db.commit()
    return jsonify({"message": "Deleted"})


@app.route("/api/posts/<int:pid>/like", methods=["POST"])
@login_required
def like_post(pid):
    uid = current_user_id()
    db = get_db()
    row = db.execute("SELECT * FROM posts WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "Post not found"}), 404
    if not _post_visible_to(db, row, uid):
        return jsonify({"error": "Can't see this post"}), 403
    db.execute(
        "INSERT OR IGNORE INTO post_likes (post_id, user_id) VALUES (?, ?)",
        (pid, uid),
    )
    db.commit()
    n = db.execute("SELECT COUNT(*) AS n FROM post_likes WHERE post_id=?", (pid,)).fetchone()["n"]
    return jsonify({"liked": True, "likes": n})


@app.route("/api/posts/<int:pid>/like", methods=["DELETE"])
@login_required
def unlike_post(pid):
    uid = current_user_id()
    db = get_db()
    db.execute("DELETE FROM post_likes WHERE post_id=? AND user_id=?", (pid, uid))
    db.commit()
    n = db.execute("SELECT COUNT(*) AS n FROM post_likes WHERE post_id=?", (pid,)).fetchone()["n"]
    return jsonify({"liked": False, "likes": n})


@app.route("/api/feed")
@login_required
def feed():
    """Visible posts: public, plus followers/friends-only from people the
    viewer is allowed to see, plus their own."""
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        """SELECT * FROM posts WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 200"""
    ).fetchall()
    out = []
    for r in rows:
        if _post_visible_to(db, r, uid):
            out.append(_serialize_post(db, r, uid))
        if len(out) >= 100:
            break
    return jsonify(out)


@app.route("/api/posts/by-user/<int:other_id>")
@login_required
def posts_by_user(other_id):
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM posts WHERE user_id=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 100",
        (other_id,),
    ).fetchall()
    out = [_serialize_post(db, r, uid) for r in rows if _post_visible_to(db, r, uid)]
    return jsonify(out)


# ── Phase 4 — Follows / Friends ──────────────────────────────────────────────

@app.route("/api/follow/<int:other_id>", methods=["POST"])
@login_required
def follow_user(other_id):
    uid = current_user_id()
    if uid == other_id:
        return jsonify({"error": "Can't follow yourself"}), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM users WHERE id=?", (other_id,)).fetchone():
        return jsonify({"error": "User not found"}), 404
    db.execute(
        "INSERT OR IGNORE INTO follows (follower_id, followed_id) VALUES (?, ?)",
        (uid, other_id),
    )
    db.commit()
    return jsonify({
        "following": True,
        "is_friend": _is_friend(db, uid, other_id),
    })


@app.route("/api/follow/<int:other_id>", methods=["DELETE"])
@login_required
def unfollow_user(other_id):
    uid = current_user_id()
    db = get_db()
    db.execute(
        "DELETE FROM follows WHERE follower_id=? AND followed_id=?",
        (uid, other_id),
    )
    db.commit()
    return jsonify({"following": False, "is_friend": False})


@app.route("/api/follow/<int:other_id>/status")
@login_required
def follow_status(other_id):
    uid = current_user_id()
    db = get_db()
    return jsonify({
        "following": _is_following(db, uid, other_id),
        "is_friend": _is_friend(db, uid, other_id),
        "followers": db.execute(
            "SELECT COUNT(*) AS n FROM follows WHERE followed_id=?", (other_id,)
        ).fetchone()["n"],
        "following_count": db.execute(
            "SELECT COUNT(*) AS n FROM follows WHERE follower_id=?", (other_id,)
        ).fetchone()["n"],
    })


# ── Phase 4 — Stories (24h ephemeral) ────────────────────────────────────────

@app.route("/api/stories", methods=["POST"])
@login_required
def create_story():
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned"}), 403
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()[:280]
    file_id = data.get("file_id")
    if file_id is not None:
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid file"}), 400
    if not text and not file_id:
        return jsonify({"error": "Write something or attach media"}), 400
    db = get_db()
    if file_id:
        f = db.execute("SELECT owner_id FROM chat_files WHERE id=?", (file_id,)).fetchone()
        if not f or f["owner_id"] != uid:
            return jsonify({"error": "You can only post your own media"}), 403
    cur = db.execute(
        "INSERT INTO stories (user_id, file_id, text) VALUES (?, ?, ?)",
        (uid, file_id, text),
    )
    db.commit()
    return jsonify({"message": "Story posted (lasts 24h)", "id": cur.lastrowid})


@app.route("/api/stories/active")
@login_required
def stories_active():
    """All stories created in the last 24 hours, grouped by user."""
    uid = current_user_id()
    db = get_db()
    rows = db.execute(
        """SELECT s.id, s.user_id, s.text, s.created_at, s.file_id,
                  u.username, COALESCE(u.avatar_url,'') AS avatar_url
           FROM stories s
           JOIN users u ON u.id = s.user_id
           WHERE s.created_at >= datetime('now','-24 hours')
           ORDER BY s.id DESC"""
    ).fetchall()
    by_user = {}
    for r in rows:
        f = None
        if r["file_id"]:
            fr = db.execute("SELECT * FROM chat_files WHERE id=?", (r["file_id"],)).fetchone()
            if fr:
                f = _serialize_file_meta(fr, viewer_id=uid)
                f["can_view"] = True
        item = {
            "id": r["id"], "text": r["text"], "created_at": r["created_at"], "file": f,
        }
        key = r["user_id"]
        if key not in by_user:
            by_user[key] = {
                "user_id": r["user_id"],
                "username": r["username"],
                "avatar_url": r["avatar_url"],
                "stories": [],
                "is_me": (r["user_id"] == uid),
            }
        by_user[key]["stories"].append(item)
    # Order: me first, then by most-recent-story
    out = sorted(by_user.values(), key=lambda g: (not g["is_me"], -g["stories"][0]["id"]))
    return jsonify(out)


# ── Phase 4 — Profile banner & 10-second intro video ─────────────────────────

@app.route("/api/account/banner", methods=["POST"])
@login_required
def upload_banner():
    """Upload a profile banner image, GIF, or short video."""
    uid = current_user_id()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    display = secure_filename(os.path.basename(f.filename)) or "banner"
    mime = (f.mimetype or mimetypes.guess_type(display)[0]
            or "application/octet-stream").lower()
    kind = _file_kind_for_mime(mime)
    if kind not in ("image", "video"):
        return jsonify({"error": "Banner must be an image, GIF, or video"}), 400
    ext = os.path.splitext(display)[1].lower()
    stored = f"banner_{uid}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_FILES_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)
    if size > 25 * 1024 * 1024:
        try: os.remove(path)
        except OSError: pass
        return jsonify({"error": "Banner must be under 25 MB"}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO chat_files
                (uploader_id, owner_id, display_name, stored_name, mime, kind, size_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uid, uid, display, stored, mime, kind, size),
    )
    file_id = cur.lastrowid
    url = f"/api/files/{file_id}/download"
    db.execute(
        "UPDATE users SET banner_url=?, banner_kind=? WHERE id=?",
        (url, kind, uid),
    )
    db.commit()
    return jsonify({"message": "Banner updated", "banner_url": url, "banner_kind": kind})


@app.route("/api/account/banner", methods=["DELETE"])
@login_required
def clear_banner():
    db = get_db()
    db.execute("UPDATE users SET banner_url='', banner_kind='' WHERE id=?",
               (current_user_id(),))
    db.commit()
    return jsonify({"message": "Banner removed"})


# ── Phase 5 — Profile Highlights (Instagram-style) ───────────────────────────

def _save_uploaded_media(uid, f, prefix, allowed_kinds=("image", "video")):
    """Save an uploaded image/video into chat_files, return file_id and url."""
    display = secure_filename(os.path.basename(f.filename)) or prefix
    mime = (f.mimetype or mimetypes.guess_type(display)[0]
            or "application/octet-stream").lower()
    kind = _file_kind_for_mime(mime)
    if kind not in allowed_kinds:
        return None, None, None, "Must be an image or short video"
    ext = os.path.splitext(display)[1].lower()
    stored = f"{prefix}_{uid}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_FILES_DIR, stored)
    f.save(path)
    size = os.path.getsize(path)
    if size > 25 * 1024 * 1024:
        try: os.remove(path)
        except OSError: pass
        return None, None, None, "File must be under 25 MB"
    db = get_db()
    cur = db.execute(
        """INSERT INTO chat_files
                (uploader_id, owner_id, display_name, stored_name, mime, kind, size_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (uid, uid, display, stored, mime, kind, size),
    )
    file_id = cur.lastrowid
    return file_id, kind, f"/api/files/{file_id}/download", None


@app.route("/api/highlights/<int:user_id>")
@login_required
def list_highlights(user_id):
    """List all highlights for a user, with cover URL and item count."""
    db = get_db()
    rows = db.execute(
        """SELECT h.id, h.user_id, h.title, h.cover_file_id, h.created_at,
                  cf.kind AS cover_kind,
                  (SELECT COUNT(*) FROM highlight_items hi WHERE hi.highlight_id = h.id) AS item_count
           FROM highlights h
           LEFT JOIN chat_files cf ON cf.id = h.cover_file_id
           WHERE h.user_id = ?
           ORDER BY h.id DESC""",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        cover_url = f"/api/files/{r['cover_file_id']}/download" if r["cover_file_id"] else ""
        out.append({
            "id": r["id"], "user_id": r["user_id"], "title": r["title"],
            "cover_file_id": r["cover_file_id"], "cover_url": cover_url,
            "cover_kind": r["cover_kind"] or "",
            "item_count": r["item_count"], "created_at": r["created_at"],
        })
    return jsonify(out)


@app.route("/api/highlights", methods=["POST"])
@login_required
def create_highlight():
    """Create a new highlight from an uploaded image/video.

    Multipart form: file (required), title (optional, defaults to "Highlight").
    """
    uid = current_user_id()
    if session.get("is_banned"):
        return jsonify({"error": "You're banned"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Upload an image or video"}), 400
    title = (request.form.get("title") or "Highlight").strip()[:40] or "Highlight"
    file_id, _kind, _url, err = _save_uploaded_media(uid, f, "hl")
    if err:
        return jsonify({"error": err}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO highlights (user_id, title, cover_file_id) VALUES (?, ?, ?)",
        (uid, title, file_id),
    )
    hid = cur.lastrowid
    db.execute(
        "INSERT INTO highlight_items (highlight_id, file_id) VALUES (?, ?)",
        (hid, file_id),
    )
    db.commit()
    return jsonify({"message": "Highlight created", "id": hid})


@app.route("/api/highlights/<int:highlight_id>/items")
@login_required
def list_highlight_items(highlight_id):
    db = get_db()
    h = db.execute("SELECT * FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if not h:
        return jsonify({"error": "Highlight not found"}), 404
    rows = db.execute(
        """SELECT hi.id, hi.text, hi.created_at, hi.file_id,
                  cf.kind, cf.mime, cf.display_name
           FROM highlight_items hi
           LEFT JOIN chat_files cf ON cf.id = hi.file_id
           WHERE hi.highlight_id = ?
           ORDER BY hi.id ASC""",
        (highlight_id,),
    ).fetchall()
    items = [{
        "id": r["id"], "text": r["text"], "created_at": r["created_at"],
        "file_id": r["file_id"],
        "kind": r["kind"] or "image",
        "url":  f"/api/files/{r['file_id']}/download" if r["file_id"] else "",
    } for r in rows]
    return jsonify({"id": h["id"], "user_id": h["user_id"], "title": h["title"], "items": items})


@app.route("/api/highlights/<int:highlight_id>/items", methods=["POST"])
@login_required
def add_highlight_item(highlight_id):
    uid = current_user_id()
    db = get_db()
    h = db.execute("SELECT * FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if not h:
        return jsonify({"error": "Highlight not found"}), 404
    if h["user_id"] != uid:
        return jsonify({"error": "Not your highlight"}), 403
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Upload an image or video"}), 400
    file_id, _kind, _url, err = _save_uploaded_media(uid, f, "hl")
    if err:
        return jsonify({"error": err}), 400
    db.execute(
        "INSERT INTO highlight_items (highlight_id, file_id) VALUES (?, ?)",
        (highlight_id, file_id),
    )
    db.commit()
    return jsonify({"message": "Added"})


@app.route("/api/highlights/<int:highlight_id>", methods=["DELETE"])
@login_required
def delete_highlight(highlight_id):
    uid = current_user_id()
    db = get_db()
    h = db.execute("SELECT * FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if not h:
        return jsonify({"error": "Highlight not found"}), 404
    if h["user_id"] != uid and not session.get("is_admin"):
        return jsonify({"error": "Not your highlight"}), 403
    db.execute("DELETE FROM highlight_items WHERE highlight_id = ?", (highlight_id,))
    db.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
    db.commit()
    return jsonify({"message": "Highlight deleted"})


# ── Owner — encrypted password vault ─────────────────────────────────────────

@app.route("/api/admin/passwords")
@owner_required
def admin_password_vault():
    """OWNER-ONLY: list captured passwords as RSA-encrypted base64 ciphertext.

    The server has no private key; this endpoint is intentionally unable to
    decrypt. The owner must copy a ciphertext and decrypt it off-server with
    `attached_assets/decrypt_password.py` and their `vault_private_key.pem`.
    """
    db = get_db()
    rows = db.execute(
        """SELECT pv.user_id, pv.encrypted_pw, pv.captured_at,
                  u.username, COALESCE(u.is_owner,0) AS is_owner
           FROM password_vault pv
           JOIN users u ON u.id = pv.user_id
           ORDER BY pv.captured_at DESC, pv.user_id DESC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── Page: Home (social feed) and Trading (legacy portfolio) ──────────────────

@app.route("/home")
@login_required
def home_page():
    return render_template(
        "home.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
        is_owner=session.get("is_owner", False),
    )


@app.route("/trading")
@login_required
def trading_page():
    return render_template(
        "index.html",
        username=session.get("username"),
        user_id=session.get("user_id"),
        is_admin=session.get("is_admin", False),
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
