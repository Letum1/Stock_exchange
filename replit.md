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
- `/market` — live market with watchlist + chart
- `/items` — items marketplace (browse listings, manage inventory, my listings)
- `/chat` — DM chat with sidebar of conversations, polling every 4s
- `/profile/<id>` — public profile (stats, badges, public inventory)
- `/account` — change username & password
- `/admin` — admin panel (users, balance, trades, items catalog, grant items)
- `/login` `/register` — auth (register requires anti-bot math captcha)

### Backend modules in `app.py`
- Auth (with math captcha for register), portfolio + trading, market data, ticker search
- Items: catalog, inventory, listings (cash and/or item-for-item swaps), trade history
- Messages: 1-to-1 DMs with conversations summary + unread counts
- Profile: public stats endpoint
- Admin: user management, item create/delete/grant, trading activity feed

### Data model (SQLite at `paper-trading/portfolio.db`)
`users`, `portfolios`, `holdings`, `trades`, `items`, `user_items`,
`item_listings` (with optional cash price + JSON `accepts_items` for swaps),
`item_trades`, `messages`. New tables auto-create on startup.
