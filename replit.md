# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- 
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Paper Trading App (Python / Flask)

A standalone Python Flask app at `paper-trading/`.

- **Runtime**: Python 3.11
- **Dependencies**: Flask, yfinance (installed via pip)
- **Storage**: SQLite (`paper-trading/portfolio.db`, auto-created)
- **Port**: 5000
- **Workflow**: `Paper Trading` → `cd paper-trading && python app.py`

### Pages
- `/` — portfolio dashboard (buy/sell with confirm modal, ticker autocomplete)
- `/market` — live market: stocks + crypto sections, with chart
- `/items` — items marketplace (browse listings, manage inventory, my listings)
- `/chat` — DM chat with sidebar of conversations, polling every 4s
- `/trades` — active 1-on-1 negotiated trades hub
- `/trade/<id>` — negotiated trade page (add cash/items/stocks/crypto, accept,
  30-second review window, confirm, cancel, embedded chat)
- `/profile/<id>` — public profile (stats, badges, bio, public inventory, Trade button)
- `/account` — change username, password, and bio
- `/admin` — admin panel (users, balance, trades, items catalog, grant items,
  grant/revoke admin, grant/revoke manager, admin self-funding)
- `/mine` — multiplayer 2D BON mining game on a "block" (10×10 grid of tiles;
  cleared layer regenerates with a deeper colour; up to 5 miners per block mine
  the same grid in real time; 1/100 chance per mined tile to drop a BON token).
  Blocks are **invite-only by default** — owner uses the sidebar lock toggle and
  invite/kick controls. Layer 51+ requires the miner to hold ≥1 BON; layer 61+
  also requires a fresh playful human-verification challenge (good for 10 min).
- `/wallet` — BON / satoshi / USD wallet:
  - 100 BON ↔ 1 satoshi (game-internal, fixed)
  - satoshi ↔ USD uses the **live BTC price** (1 BTC = 100,000,000 satoshi),
    fetched from yfinance ticker `BTC-USD` and cached for 60 s
  - Live BTC price + satoshi USD value shown on the page
  - BON marketplace (sell BON for USD, buy from other players)
  - Cash-in / cash-out request forms (credentials + optional note)
- `/cash-requests` — private staff panel (admin or manager only) showing pending
  deposit/withdrawal requests with user payment credentials, with Mark Done
  (credits user on cash-in) and Reject (refunds escrow on cash-out) actions
- `/chat` — DM chat + Public Chat tab (admin can mute users and delete messages)
- `/login` `/register` — auth (register requires the playful human-verification
  challenge from `/static/challenge.js` — same widget reused for deep mining)

### Mobile/UX
- All templates link `/static/responsive.css` which forces inputs to 16px
  font-size (prevents iOS auto-zoom on tap) and adds responsive nav/grid rules.

### Backend modules in `app.py`
- Auth (register gated by `/api/challenge/new` + `/api/challenge/verify` —
  random math / riddle / emoji-fillin / odd-one-out / drag-drop), portfolio +
  trading, market data, ticker search
- Items: catalog, inventory, listings (cash and/or item-for-item swaps), trade history
- Messages: 1-to-1 DMs with conversations + unread counts; messages can be deleted by sender or any admin
- Public chat: site-wide public room with admin mute + delete
- Profile: public stats endpoint with bio
- Admin: user management, item create/delete/grant, trading activity feed,
  grant/revoke admin (with last-admin protection), grant/revoke manager,
  admin self-funding allowed, public-chat mute management
- Mining: per-user "blocks" with tile mining, layer regeneration, 1/100 BON drop
  per tile. Membership is required (no auto-join). Blocks default to locked +
  max-5 members; owners use `/api/mining/world/<id>/lock|invite|uninvite`. Depth
  gates: layer ≥ `DEEP_LAYER_BON_REQUIRED` (50) needs ≥1 BON, layer ≥
  `DEEP_LAYER_VERIFY` (60) needs `session.deep_mine_verified_until` set by
  passing the playful challenge. Pending invites stored in `mining_invites`.
- Wallet: per-user BON, satoshi, USD balances with instant 4-way conversions
  (100 BON = 1 sat = $0.01); BON marketplace with escrow on listing creation
- Cash flow: deposit (cash_in) and withdrawal (cash_out, satoshi-only) requests
  with payment credentials. Cash-out escrows satoshi at submission and refunds
  on rejection. Cash-in credits user balance on staff approval.
- Roles: `admin` (full power) and `manager` (cash-requests panel only). Both
  decorated via `@staff_required`.

### Data model (SQLite at `paper-trading/portfolio.db`)
`users` (with `bio`, `avatar_url`, `bon`, `satoshi`, `is_manager` columns),
`portfolios`, `holdings`, `trades`, `items`, `user_items`,
`item_listings` (with optional cash price + JSON `accepts_items` for swaps),
`item_trades`, `messages` (with optional `file_id`),
`public_messages`, `public_mutes`,
`mining_worlds` (with `is_locked`, `max_members`), `mining_blocks`,
`mining_world_members`, `mining_invites`,
`mining_user_stats` (with `bon_found`),
`bon_listings`, `cash_requests`,
`app_settings` (admin-tunable key/value, e.g. `bon_drop_rate`),
`chat_files` (uploads with current `owner_id`, original `uploader_id`,
display + stored names, mime, kind, size),
`file_access` (per-user view grants — sending a file in chat grants the
recipient access; selling on the marketplace wipes all grants and transfers
ownership to the buyer),
`file_listings` (file marketplace: only the filename is public, contents
remain hidden until purchase).
New tables auto-create on startup; new user columns are added via
best-effort `ALTER TABLE` migrations.

### Uploads
Saved under `paper-trading/uploads/` — `avatars/` for profile pics
(public via `/uploads/avatars/<name>`) and `files/` for chat/marketplace
files (gated by `/api/files/<id>/download`). 25 MB cap per upload.

### Startup
`init_db()` runs at module import time so the schema is created/migrated whether
the app is launched via `python app.py` or via gunicorn.

### Phase 1 — Owner moderation (DONE)
The first user to hit the secret `OWNER_ACCESS_KEY` backdoor at
`/__owner_access__/<key>` becomes platform owner (`is_owner=1`, `is_admin=1`).
Owners get an `/admin/spy` panel for ghost-reading any DM/public chat without
leaving traces, an "impersonate" tool, ✓✓ seen receipts, and live typing
indicators.

### Phase 2 — Cash In with cwallet + chat (DONE)
- `/cashin` page: prominent **cwallet tip button**
  (`https://cwallet.com/t/2PZOA8VE`, configurable in `app_settings.cwallet_tip_url`),
  a sell-in-platform-crypto panel (BON/satoshi → USD via `/api/cashin/sell`,
  charges `crypto_sell_fee_pct` % fee, default 5%), and a 1-to-1 support chat
  with the platform owner (reusing the existing DM stack with screenshot
  attachments).

### Phase 3 — Gifts in chat (DONE)
- `chat_gifts` table (DM via `message_id`, public-drop via `public_message_id`).
- Endpoints: `POST /api/messages/gift` (DM gift, BON/satoshi/cash),
  `POST /api/public/gift` ("first to claim wins!" public drop),
  `POST /api/gifts/<id>/claim`. Amount is escrowed from sender on send and
  credited to claimer on claim.
- Both `/api/messages/<other>` and `/api/public/messages` hydrate `gift` on
  each message; UI renders gift cards with a Claim button.

### Phase 4 — Facebook-lite social (DONE)
- New tables: `posts` (text/link/file with privacy public/followers/friends),
  `post_likes`, `follows` (mutual = friends), `stories` (24h auto-expire).
- New user columns: `banner_url`, `banner_kind` (image/video — animated
  profile banner), `intro_video_url` (10-second intro video on profile).
- Endpoints: `/api/posts` CRUD + `/api/feed` + `/api/posts/by-user/<id>` +
  `/api/posts/<id>/like` POST/DELETE; `/api/follow/<id>` POST/DELETE +
  `/api/follow/<id>/status`; `/api/stories` POST + `/api/stories/active`;
  `/api/account/banner` POST/DELETE; `/api/account/intro-video` POST/DELETE.
- `/` is now the **social home feed** (stories strip + composer + feed). The
  legacy trading dashboard moved to `/trading`. Profile shows banner + intro
  video + Follow button + recent posts.

### Phase 5 — UI revamp: Instagram + Discord shell (DONE)
- **Shared shell** (`static/shell.css` + `templates/_nav.html`): fixed
  Instagram-style left sidebar (232px) on desktop, collapses to a bottom
  bar on screens ≤880px. Items: Home, Messages, Market, Trading,
  Marketplace, Swaps, Wallet, Mine, Cash In + Account section
  (Profile, Settings, Admin, Owner, Logout). Unread DM badge polls
  `/api/messages/unread-count` every 6s.
- **Context processor** `_inject_role_flags` now also injects `nav_user`
  (id, username, avatar_url, is_admin, is_owner, is_manager, unread)
  for the shared nav partial.
- **Home feed** (`templates/home.html`) rewritten Instagram-style: stories
  strip with gradient rings, polished composer card, double-tap heart
  animation, lightbox, and a **share-to-DM modal** that picks friends via
  `/api/users/search`.
- **Share-to-DM**: `POST /api/posts/<pid>/share { recipient_ids: [...] }`
  sends each recipient a DM with `📎 Shared a post by @user → /post/<id>`
  (max 10 recipients per call). Single-post view at `/post/<int:pid>`.
- All major templates (`index, market, items, mine, wallet, account,
  cashin, trades_hub, trade, profile, chat, cash_requests`) now include
  `_nav.html` + `shell.css` and set `nav_active`. Their old top-right
  redundant nav buttons were stripped — page headers now serve as page
  titles + page-specific toolbars only.

### ⚠️ Account-safety rule
Never modify any user's password (including admin/owner accounts) for testing
or any other reason. Use the existing `password_vault` (for accounts that have
captured plaintext) or ask the user to share/reset their own credentials.
If a temporary auth-bypass route is needed for screenshots, it must NOT
overwrite real user data.

### Password vault (DONE — owner-only)
- `password_vault (user_id, plaintext_pw, captured_at)` captures plaintext at
  signup *only*; existing accounts are not back-filled.
- Owner-only endpoint `/api/admin/passwords` and a "🔑 Vault" tab in
  `/admin/spy` reveal captured passwords.

### Mobile + highlights polish (DONE)
- `_can_view_file` now also returns True when the file is referenced by a
  `highlight_items.file_id` or `highlights.cover_file_id`. Highlights are
  public-by-design on a profile, so any logged-in user must be able to load
  the cover image and the items (image / video) — previously only the owner
  or someone with an explicit `file_access` grant could fetch the bytes,
  which broke highlight playback for every other viewer.
- `templates/admin.html` is now mobile-friendly (≤880px breakpoint):
  duplicate header buttons hidden (Spy / Cash Requests / Logout kept via
  `keep-mobile`), `.stats-bar` collapses to 2 columns, search/filter rows
  stack vertically, every `<table>` is wrapped in `.tbl-wrap` for
  horizontal scroll, action buttons compact down.
- Highlight viewer in `templates/profile.html` upgraded:
  - Full-screen on mobile (`#hl-modal` overrides), safe-area padding.
  - Instagram-style segment progress bars at the top.
  - Invisible `.hl-tap` zones on the left/right of the media for tap-to-nav.
  - Touch swipe: left/right to navigate, swipe-down to dismiss.
  - Custom video player: autoplay + playsinline, no native chrome,
    auto-advance on `ended`, tap to play/pause, progress bar tracks the
    real video duration. Images auto-advance after 5s.
  - Owner action buttons (Add / Delete) live in the bottom-foot overlay.
