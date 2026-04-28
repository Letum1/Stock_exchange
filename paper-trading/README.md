# The Exchange Hub



A practice-trading playground built with Flask. Users get virtual cash, buy
real stocks and crypto at live prices, mine for tokens, chat, swap items,
post short videos — all without risking real money.

This README is the **single source of truth for new developers** (or Replit
itself) joining this codebase. Read this once and you should know where
everything lives.

---

## 1. What this app actually is

| Thing                        | Reality                                                |
|------------------------------|--------------------------------------------------------|
| Stock & crypto **prices**    | Real, live (yfinance)                                  |
| **Cash** in the app          | Virtual. Has no real-world value                       |
| **BON** tokens               | In-game currency, mined for free                       |
| **Satoshi** balance          | In-game accounting unit (100 BON ↔ 1 sat)              |
| **Cash In** / **Cash Out**   | Manual, admin-handled. Users chat the admin to top up  |
| **Real money**               | Never touched by this app                              |

Nobody gets virtual cash automatically. New accounts start at **$0**. To
get cash, a user opens **Cash In** and asks the admin in the support chat.
The only thing that's truly free is mining **BON** in the Mine page, which
can later be sold for cash.

---

## 2. Quick start

```bash
# From the repo root (parent of paper-trading/)
gunicorn --bind 0.0.0.0:5000 --reuse-port --reload \
         --chdir paper-trading main:app
```

That's also what the `Start application` workflow runs.

- **Port**: 5000 (must stay 5000 for Replit's preview proxy)
- **Python**: 3.11
- **DB**: SQLite, auto-created at `paper-trading/portfolio.db`
- **Schema**: `init_db()` runs at module import — no manual migration step
- **Hot reload**: gunicorn `--reload` picks up `.py` changes; refresh the
  browser for template changes

### Required environment variables

| Variable             | Purpose                                                  |
|----------------------|----------------------------------------------------------|
| `SESSION_SECRET`     | Flask session cookie key                                 |
| `OWNER_ACCESS_KEY`   | Secret key for the one-time `/__owner_access__/<key>` URL that promotes the first visitor to platform owner |

Optional: `DATABASE_URL` is **not** used — this app is SQLite only.

---

## 3. Tech stack

- **Web**: Flask + Jinja2 templates (server-rendered, mostly no SPA)
- **DB**: SQLite via the stdlib `sqlite3` module (no ORM)
- **Market data**: `yfinance` (cached in-memory ~60s)
- **Crypto**: `cryptography` (used by the owner-only password vault and
  signed-cookie helpers)
- **Server**: gunicorn in dev and prod
- **Frontend**:
  - `static/shell.css` + `templates/_nav.html` — shared shell (Instagram
    sidebar on desktop, bottom bar on mobile ≤880px)
  - `static/responsive.css` — global rules (forces inputs to 16px so iOS
    doesn't auto-zoom on tap)
  - `static/tour.js` — onboarding tour (auto-injected after_request)
  - `static/challenge.js` — playful human-verification widget reused for
    register and deep mining
  - `static/swipe-nav.js` — left/right swipe between top-nav pages

There is **no JS build step**. Everything is plain HTML/CSS/JS. Polling
(every 4–6s) is preferred over websockets for simplicity.

---

## 4. Project layout

```
paper-trading/
├── app.py                 # ~6.7k lines — every route, every helper, every table
├── main.py                # `from app import app` — gunicorn entry point
├── mining_game.py         # tile-grid + BON drop logic for the Mine page
├── requirements.txt
├── portfolio.db           # SQLite, auto-created
├── vault_public_key.pem   # owner-only password-vault encryption key
├── uploads/
│   ├── avatars/           # public via /uploads/avatars/<name>
│   ├── ads/               # public via /uploads/ads/<name>
│   └── files/             # gated via /api/files/<id>/download
├── static/                # css + js (no build)
└── templates/             # Jinja templates (one per page)
```

`app.py` is intentionally a single file. If you need to find something,
ripgrep is your friend:

```bash
rg -n "def cash_request_create" paper-trading/app.py
rg -n "@app.route" paper-trading/app.py
rg -n "CREATE TABLE IF NOT EXISTS" paper-trading/app.py
```

---

## 5. Pages (templates)

| URL                | Template              | What it does                                 |
|--------------------|-----------------------|----------------------------------------------|
| `/`                | `home.html`           | Social feed (stories + posts + composer)     |
| `/trading`         | `index.html`          | Portfolio dashboard (buy/sell)               |
| `/market`          | `market.html`         | Live stocks + crypto with charts             |
| `/wallet`          | `wallet.html`         | BON / sat / USD balances + BON marketplace   |
| `/mine`            | `mine.html`           | Multiplayer 2D BON mining                    |
| `/items`           | `items.html`          | Item shop / inventory / listings             |
| `/trades`          | `trades_hub.html`     | Active 1-on-1 negotiated trades              |
| `/trade/<id>`      | `trade.html`          | Trade negotiation page                       |
| `/chat`            | `chat.html`           | DMs + Public Chat                            |
| `/cashin`          | `cashin.html`         | Sell BON/sat for cash + admin support chat   |
| `/cash-requests`   | `cash_requests.html`  | Staff panel: approve/reject cash requests    |
| `/profile/<id>`    | `profile.html`        | Public profile (banner, intro vid, posts)    |
| `/account`         | `account.html`        | Settings (username, password, bio, replay tour) |
| `/about`           | `about.html`          | What the app is + how to get cash            |
| `/admin`           | `admin.html`          | User management, items, fees, ads            |
| `/admin/spy`       | `admin_spy.html`      | Owner-only ghost view + 🔑 password vault    |
| `/login` `/register` | `auth.html`         | Auth (register passes a captcha challenge)   |
| `/notifications`   | `notifications.html`  | Notification feed                            |
| `/reels`           | `reels.html`          | Short-video feed                             |

`_nav.html` is the shared shell included in every page. Set
`{% set nav_active = "wallet" %}` etc. before the include so the right
nav item is highlighted.

---

## 6. Currencies & conversions

| Pair              | Rate                                          |
|-------------------|-----------------------------------------------|
| BON ↔ satoshi     | **100 BON = 1 sat** (fixed, in-game)          |
| satoshi ↔ USD     | Live BTC price (yfinance `BTC-USD`, cached 60s) — `1 BTC = 100,000,000 sat` |
| Cash              | USD, virtual, 2-decimal                       |
| Item prices       | Set by sellers in cash, optional swaps        |

Conversions live in `_btc_usd_price()` and helpers around `/api/wallet/*`
in `app.py`.

---

## 7. Roles & permissions

| Role        | Set by                              | Can do                                          |
|-------------|-------------------------------------|-------------------------------------------------|
| user        | default                             | trade, chat, mine, post                         |
| `is_admin`  | granted by another admin            | full admin panel                                |
| `is_manager`| granted by admin                    | only `/cash-requests` panel                     |
| `is_owner`  | first visitor to `/__owner_access__/<OWNER_ACCESS_KEY>` | everything admin + ghost spy + password vault + impersonate |

`@staff_required` = admin OR manager. `@admin_required` = admin only.
`@owner_required` = owner only. The current owner id is `_owner_id()`
(lowest user with `is_owner=1`).

⚠️ **Account-safety rule**: never modify any user's password (admin or
otherwise) for testing. Use the password vault if you need a real
plaintext, or ask the user to reset it themselves.

---

## 8. Database tables (SQLite)

All tables auto-create on import via `init_db()`. New columns are added
via best-effort `ALTER TABLE` migrations in the same function.

**Identity & money**
`users`, `password_vault`, `portfolios`, `holdings`, `trades`,
`platform_ledger`, `cash_requests`

**Items & swaps**
`items`, `user_items`, `item_listings`, `item_trades`,
`bon_listings`

**Messaging**
`messages`, `public_messages`, `public_mutes`, `chat_files`,
`file_access`, `file_listings`, `chat_gifts`

**Mining**
`mining_worlds`, `mining_blocks`, `mining_world_members`,
`mining_user_stats`, `mining_invites`

**Social**
`posts`, `post_likes`, `post_comments`, `post_views`,
`follows`, `follow_requests`, `stories`,
`highlights`, `highlight_items`, `notifications`

**Misc**
`app_settings` (admin-tunable key/value, e.g. `bon_drop_rate`,
`crypto_sell_fee_pct`, `cwallet_tip_url`),
`ad_banners` (auto-injected ad strip)

---

## 9. Big-picture features

- **Auth**: register requires a playful human-verification challenge
  (math / riddle / emoji-fillin / odd-one-out / drag-drop). Same widget
  is reused for deep-layer mining.
- **Trading**: live yfinance prices, buy/sell with confirm modal, ticker
  autocomplete, fees configurable in `app_settings`.
- **Mining**: 10×10 tile grid per "block". Up to 5 miners per block mine
  the same grid in real time. Layer 51+ requires holding ≥1 BON. Layer
  61+ also requires a fresh challenge (good for 10 min).
- **Wallet**: BON / sat / USD with instant 4-way conversions. BON
  marketplace escrows the seller's BON when they list.
- **Cash flow**: `/cashin` lets users sell BON/sat for cash (charges
  `crypto_sell_fee_pct`, default 5%) and chat the owner. `/cash-requests`
  is the staff panel for top-ups and withdrawals (escrows on submit,
  refunds on reject).
- **Messaging**: 1-to-1 DMs with conversations + unread counts + ✓✓ seen
  receipts + typing indicators. Public chat tab with mute + delete. Gifts
  in chat (DM + public-drop "first to claim wins"). Sender or any admin
  can delete a message.
- **Owner spy** (`/admin/spy`): ghost-read any DM or public chat without
  marking anything as seen. 🔑 Vault tab reveals captured plaintext
  passwords (encrypted with the public key in `vault_public_key.pem` —
  the server can't decrypt them, only the owner off-server can).
- **Social**: posts (text/link/file with public/followers/friends
  privacy), follows (mutual = friends), stories (24h auto-expire),
  banners (image or video), 10-second intro video on profile, share-to-DM,
  highlights on profile.
- **Ads**: rotating ad strip auto-injected at the bottom of every HTML
  response by `_inject_ad_banner` (an `after_request` handler).

---

## 10. Mobile / UX rules

- Every template links **`responsive.css`** which forces inputs to
  `font-size:16px` to prevent iOS auto-zoom.
- The shell collapses from a 232px left sidebar to a bottom nav at
  `≤880px`, with a CSS variable `--bottom-h` (62px) you can use to keep
  fixed elements (toasts, FABs) above the bar.
- Heavily-mobile pages have additional `≤480px` rules for tiny phones —
  see `admin.html`, `admin_spy.html`, `cashin.html`, `account.html`.
- The onboarding tour (`tour.js`) is auto-injected on every page. Replay
  it from **Account → Replay tour**.

---

## 11. How to add a new feature (cheat sheet)

1. **New page**:
   - Add a route in `app.py` returning `render_template("foo.html", ...)`.
   - Create `templates/foo.html`. Start with `<link rel="stylesheet" href="/static/responsive.css"/>` and `<link rel="stylesheet" href="/static/shell.css"/>`, then `{% set nav_active = "foo" %}` and `{% include "_nav.html" %}`.
   - If it should appear in the sidebar/bottom-nav, add it inside `templates/_nav.html`.

2. **New table**:
   - Add a `CREATE TABLE IF NOT EXISTS …` block inside `init_db()` in `app.py`.
   - For new **columns** on existing tables, add a guarded `ALTER TABLE` in the same function (wrap in `try/except sqlite3.OperationalError`).

3. **New API**:
   - Add `@app.route("/api/foo")` in `app.py`. Use `@login_required`, `@admin_required`, `@staff_required`, or `@owner_required` as appropriate.
   - Return `jsonify(...)`. Keep payloads small — prefer polling.

4. **New currency/fee knob**:
   - Don't hard-code. Add it to `app_settings` and read with `_get_setting(key, default)`. Surface the toggle in `/admin`.

---

## 12. Things to be careful about

- **`app.py` is one file by design.** Splitting it is a big refactor —
  don't do it casually. Use ripgrep to navigate.
- **No PostgreSQL.** Don't use `DATABASE_URL` here. The Flask snippet in
  the project guidelines is for *other* Flask apps in this workspace.
- **Don't touch user passwords.** Even for testing. See the safety rule
  in section 7.
- **Polling, not websockets.** Keep it boring; the deploy target is a
  single gunicorn process.
- **Templates are big** and contain inline `<style>` + `<script>` blocks.
  This is intentional for now — each page is self-contained.
- **`_inject_ad_banner` rewrites every HTML response.** If you see weird
  HTML appended near `</body>`, that's why.

---

## 13. Where things live (most-asked questions)

| "Where is …?"                    | File / line                              |
|----------------------------------|------------------------------------------|
| The list of routes               | `rg -n "@app.route" paper-trading/app.py`|
| All `CREATE TABLE` statements    | `init_db()` in `app.py` (~line 142+)     |
| Owner id resolution              | `_owner_id()` in `app.py`                |
| Cash-in flow                     | `/cashin` route + `cashin.html`          |
| Cash-request approval            | `/cash-requests` + `admin_cash_request_done` / `_reject` |
| Mining tile logic                | `mining_game.py`                         |
| Onboarding tour                  | `static/tour.js` (auto-injected)         |
| Shared nav                       | `templates/_nav.html` + `static/shell.css` |
| Mobile rules                     | `static/responsive.css` + per-template `@media` blocks |
| Terms of Service text + version  | `templates/_tos_text.html` + `TOS_VERSION` in `app.py` |
| Compliance audit log             | `admin_audit_logs` table + `log_admin_action()` helper |
| Compliance Viewer (formerly Spy) | `/admin/compliance` (alias `/admin/spy`) → `admin_spy.html` |

---

That's it. If you can find what you need with ripgrep + this file, the
codebase is in good shape. If you can't, please add the answer here.
