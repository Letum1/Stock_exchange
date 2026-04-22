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

### API Endpoints
- `GET /` — HTML frontend
- `GET /api/portfolio` — portfolio snapshot (cash, holdings, P&L)
- `GET /api/quote/<ticker>` — fetch live price via yfinance
- `POST /api/buy` — buy shares `{ ticker, shares }`
- `POST /api/sell` — sell shares `{ ticker, shares }`
- `GET /api/trades` — last 50 trades
- `POST /api/reset` — reset to $100,000
