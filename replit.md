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
  grant/revoke admin, admin self-funding)
- `/mine` — multiplayer 2D BON mining game (10×10 grid; cleared layer
  regenerates with a deeper colour; everyone in a world mines the same grid in real time)
- `/chat` — DM chat + Public Chat tab (admin can mute users and delete messages)
- `/login` `/register` — auth (register requires anti-bot math captcha)

### Mobile/UX
- All templates link `/static/responsive.css` which forces inputs to 16px
  font-size (prevents iOS auto-zoom on tap) and adds responsive nav/grid rules.

### Backend modules in `app.py`
- Auth (with math captcha for register), portfolio + trading, market data, ticker search
- Items: catalog, inventory, listings (cash and/or item-for-item swaps), trade history
- Messages: 1-to-1 DMs with conversations + unread counts; messages can be deleted by sender or any admin
- Public chat: site-wide public room with admin mute + delete
- Profile: public stats endpoint with bio
- Admin: user management, item create/delete/grant, trading activity feed,
  grant/revoke admin (with last-admin protection), admin self-funding allowed,
  public-chat mute management
- Mining: world grid mining (block click, layer regeneration, multi-user worlds)

### Data model (SQLite at `paper-trading/portfolio.db`)
`users` (with `bio` column), `portfolios`, `holdings`, `trades`, `items`, `user_items`,
`item_listings` (with optional cash price + JSON `accepts_items` for swaps),
`item_trades`, `messages`, `public_messages`, `public_mutes`,
`mining_worlds`, `mining_blocks`, `mining_world_members`, `mining_user_stats`.
New tables auto-create on startup; `users.bio` is added via best-effort `ALTER TABLE`.

### Startup
`init_db()` runs at module import time so the schema is created/migrated whether
the app is launched via `python app.py` or via gunicorn.
