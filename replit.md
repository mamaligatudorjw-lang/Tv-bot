# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

Russian-language crypto signals bot (Telegram) with a companion React web
preview. The Python Flask bot polls Binance every 5 min and fires alerts on
volume surges, RSI extremes, new listings, weekly/monthly highs, and a
combined "volume-surge + CRSI" setup. Users can also journal their own
trades via `/trade SYMBOL лонг|шорт ENTRY EXIT` **or by forwarding a
Binance Futures Share-card screenshot** — the bot extracts the four fields
via Gemini Vision and runs them through the same analysis pipeline.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Never `git add -f telegram-webhook-bot/alerts.db`. The SQLite file contains
  Telegram chat IDs, alert history, and user feedback. It is gitignored;
  forcing it into a commit would leak that data.
- Bot helpers that talk to Telegram or Gemini must log failures through
  `_safe_err(e)`, not `"%s" % e`. The raw `requests.RequestException` string
  contains the full URL — which holds the bot token (Telegram) or the API
  key (Gemini if ever moved to query-param auth).
- The Gemini base URL is the Replit AI Integrations proxy; it mounts model
  endpoints **without** a `/v1beta` prefix (mirrors the SDK template's
  `apiVersion: ""`). Auth is via the `x-goog-api-key` header.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
