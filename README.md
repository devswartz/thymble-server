# Thymble Server

The open-source, self-hostable server behind **Thymble**, a native iOS cooking app: import and store recipes, plan meals, and keep a smart shopping list that syncs with the lists you already use.

> **Status: early development.** Accounts, households, recipe import, the meal-plan-driven shopping list with stores and trips, the Home Assistant list connector and offline sync all run end to end; there is no public release yet. Watch the repo to follow along.

## What it does
- **Accounts and households:** Sign in with Apple or an email magic link; households are shared with invite codes
- **Recipe import** from the web page you're viewing, pasted text, or a photo of a recipe card, with every uncertain line marked for review
- **Meal planning** for a household, shared across devices
- **A structured shopping list** that merges quantities, remembers which store each item comes from, handles urgent items and pickup orders, and works offline
- **List connector** that mirrors the list to Home Assistant (and through it, Alexa), including voice entries like "urgent toilet paper from costco"
- **Bring your own LLM:** a local model through any OpenAI-compatible endpoint (e.g. Ollama), Anthropic's Claude, or both, routed per task

Planned: retailer connectors that send the list to grocery carts, and learning from corrections across households (opt-in).

## Stack
Python / FastAPI · PostgreSQL · PowerSync (offline sync to the app) · Redis + arq (background jobs) · Docker Compose

## Self-hosting
Thymble is designed to run on a home server. Docker Compose files and setup docs will land here as the server takes shape.

## Development

You need Docker (Docker Desktop on Windows/macOS, or Docker Engine with the Compose plugin). On Windows, run the commands from WSL2 or PowerShell; both work. Python and [uv](https://docs.astral.sh/uv/) on the host are optional (only for running tools outside containers).

```bash
# 1. Create a .env with random, development-only secrets (git-ignored; never commit it)
python3 scripts/dev_env.py

# 2. Start the stack: postgres, redis, migrations, api, worker, powersync, mailpit (dev)
docker compose up -d --build

# 3. Check it
curl http://localhost:8000/healthz
curl http://localhost:8000/v1/capabilities
curl http://localhost:8080/probes/liveness      # PowerSync
# Magic-link email lands in Mailpit: http://localhost:8025

# 4. Run the tests and linters inside the api container
docker compose exec api sh -c 'DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB" pytest'
docker compose exec api ruff format --check .
docker compose exec api ruff check .

# 5. Stop everything and delete the dev volumes
docker compose down -v
```

`docker compose up` automatically layers `docker-compose.override.yml` on top of `docker-compose.yml`. The override builds the `dev` image target, bind-mounts the source tree, and runs the api (uvicorn) and worker (arq) with auto-reload, so code changes apply without rebuilding (on Windows bind mounts the reload may miss changes; `docker compose restart api worker` picks them up). It also publishes Postgres on `127.0.0.1:15432` and Redis on `127.0.0.1:16379`. For a production-shaped run, use only the base file: `docker compose -f docker-compose.yml up -d`.

If a host port is already taken, set `THYMBLE_API_PORT`, `POWERSYNC_PORT`, `POSTGRES_PORT`, `REDIS_PORT` or `MAILPIT_PORT` in `.env`.

### Layout

| Path | What it is |
|---|---|
| `src/thymble/` | The Python package: `main.py` (FastAPI app), `config.py` (settings from env vars), `api/` (routes), `auth/` (sign-in, tokens, the current-user and household dependencies), `db/` + `models/` (SQLAlchemy), `sync/` (PowerSync upload: table registry and applier), `importer/` (recipe import pipeline), `llm/` (LLM providers, routing, cache, metering), `shopping/` (list merging, stores, urgency, trips, voice-entry rules), `connectors/` (Home Assistant list connector), `units.py` (the one table of unit codes and conversions, used by the importer and the shopping list), `worker.py` (arq jobs: imports, trip reminders, connector supervisor) |
| `migrations/` | Alembic migrations, one linear chain (`0001` → `auth_0002` → `import_0001` → `shop_0001` → `plan_0001`). Create one with `docker compose exec api alembic revision --autogenerate -m "..."` |
| `deploy/postgres/` | First-boot SQL for PowerSync: replication role, publication, storage database |
| `deploy/powersync/` | PowerSync service config and sync config (what each device downloads, scoped by household) |
| `scripts/dev_env.py` | Writes a development `.env` from `.env.example` |
| `tests/` | pytest suite; database tests need `DATABASE_URL` and create a throwaway database. `tests/accounts.py` creates a signed-in member for API tests |
| `tests/vectors/` | Shared ingredient-line test vectors, also run by the iOS app's Swift package (D-033); format in its README |

### Dependencies

Dependencies are managed with [uv](https://docs.astral.sh/uv/): `pyproject.toml` declares them and `uv.lock` pins exact versions. Add one with `uv add <package>` (or `uv add --dev <package>`), commit both files, and rebuild the image. To work outside Docker: `uv sync`, then `uv run pytest` / `uv run ruff check .`.

### Configuration

Everything is configured through environment variables; `.env.example` lists them all with placeholder values. `GET /v1/capabilities` reflects that configuration, for example `llm` turns on when `ANTHROPIC_API_KEY` or `OPENAI_COMPATIBLE_BASE_URL` is set.

### Recipe import

`POST /v1/imports` starts an import and returns `202` with a job id; `GET /v1/imports/{id}` returns its status (`queued`, `running`, `done`, `failed`), the draft recipe id and the full parse result. The worker (`thymble.importer.jobs.import_recipe`) does the work. The request body is one of:

| Body | Use |
|---|---|
| `{"html": "...", "final_url": "https://..."}` | The page the app already loaded (preferred: many recipe sites block server-side fetches) |
| `{"url": "https://..."}` | Server-side fetch fallback: honest `ThymbleBot` user agent, honours robots.txt and the page charset, public addresses only. Bot checks (Cloudflare, 403/429) are reported, never worked around. `IMPORT_SERVER_FETCH_ENABLED=false` turns it off |
| `{"text": "..."}` | Pasted recipe text |
| multipart field `photo` (JPEG/PNG/WebP/GIF) | Photo of a recipe card or cookbook page; needs an LLM (`photo_import` in `/v1/capabilities`) |

Stages (`src/thymble/importer/`): schema.org JSON-LD and `recipe-scrapers` (generic + site scraper) merged per field, keeping raw `recipeYield`, ISO-8601 durations and section names → LLM page extraction (T1) only when there is no usable markup → ingredient lines through a rules layer (`rules.py`: scraper debris, word numbers, unit aliases, ditto marks, and/or) and `ingredient-parser-nlp` (kept loaded in the worker) → LLM line parse (T3) for lines with confidence below 0.90, "or"/"sub", or a non-English recipe → normalization against the ingredient catalog: compound-product-name dictionary (`importer/data/compound_names.txt`), aliases, then the LLM (T4), whose answer is saved as a household alias and an `alias_learned` learning event → section headings returned as steps become step groups, and step durations become timers. Photos take two LLM passes (literal transcription, then text-only structuring) with ditto marks resolved by code in between.

The result is saved as a recipe with `is_draft = true` for the user to review; the app clears the flag through the sync upload once the user has reviewed it. Names the catalog and dictionary don't know are mapped by the LLM; with no LLM they become household ingredients as written, so every line still reaches the shopping list as a clean item. Every ingredient row keeps its original `raw_text`; the recipe's `source_url` is the `final_url` the page actually came from. Anything the pipeline could not settle (no LLM configured, a line it could not parse) is listed in `review_reasons` instead of failing the import.

Like every household-scoped route, the import endpoints need a signed-in member and the household in `X-Thymble-Household` (see "Accounts and auth").

### LLM providers and routing

All model calls go through `thymble.llm` (`LLMClient.run`), which enforces a strict JSON schema (validated, one retry on invalid output), caches answers in `llm_cache` keyed by the normalized prompt, records every call or cache hit per household in `llm_usage`, and falls back to the other provider when one fails or times out. Providers:

- **Anthropic** (`ANTHROPIC_API_KEY`): `ANTHROPIC_MODEL_SMALL` (default `claude-haiku-5-5`) for small tasks, `ANTHROPIC_MODEL_LARGE` (default `claude-sonnet-5-5`) for page and photo extraction.
- **Any OpenAI-compatible endpoint** (`OPENAI_COMPATIBLE_BASE_URL`, e.g. `http://ollama:11434/v1`, with `LOCAL_MODEL` and optionally `LOCAL_VISION_MODEL`). Local calls run one at a time per worker, and `LLM_LOCAL_TIMEOUT_SECONDS` covers the wait, so a busy GPU falls back instead of queuing.

Tasks T1–T8 (see the architecture doc) route by default to Claude for T1/T2 and to the local model for T3–T8; override per task with `LLM_ROUTE_T<n>=anthropic|local|none`. `LLM_HOUSEHOLD_MONTHLY_TOKEN_LIMIT` caps uncached tokens per household. With no provider configured, LLM steps return "needs review" and the import keeps whatever the rules produced.

### Shopping list, stores and trips

Architecture §6, D-027 to D-031. All under `/v1`, household-scoped:

- **Lists and items:** `/shopping/lists`, `/shopping/lists/{id}/items`, `/shopping/items/{id}` (+ `/check`, `/uncheck`). Adding an item merges it into an open item of the same ingredient. An item takes `{"name", "quantity", "unit"}` or free `{"text"}` parsed like a voice entry: "urgent"/"asap"/"need"/"out of" mark it urgent, a store name ("costco paper towels", "bread from aldi") assigns it to that household store, and non-food items are recognised by whole word. These rules are pinned by `tests/vectors/`.
- **From the plan:** `POST /shopping/lists/{id}/meal-plan` adds the planned recipes for a date range, scaled to the planned servings; `POST /shopping/lists/{id}/recipes` adds one recipe. Each share is kept as a `shopping_item_source`, so removing a plan entry or recipe subtracts it again.
- **Stores:** `/stores`, `/store-groups` (one can be the household default for stop-gaps), `/item-prefs` (home store or group per item, also-OK stores, preferred brand and size). The first explicit store for an item becomes its remembered home.
- **Urgency:** an urgent item whose home is a bulk store gets a linked stop-gap item ("(small)") in the default group; buying the stop-gap clears only the urgency.
- **Trips:** `/trips` (in-store, pickup, delivery). `GET /trips/{id}` shows *get here*, *needed now* and *could get here*; `GET /trips/{id}/suggested` and `POST /trips/{id}/export` put items on order; `POST /trips/{id}/confirm` marks them bought or returns missing ones to the list. The worker reminds about unconfirmed orders.
- **Connectors:** `/connectors` links a Home Assistant to-do list (`CONNECTORS_LISTS=home_assistant`). Credentials are encrypted with `CONNECTOR_ENCRYPTION_KEY` and never returned. The worker's supervisor runs one sync engine per account, both ways: adds, check-offs, deletes, voice entries, and routing bulk-store items to a store's own list.

### PowerSync

PowerSync reads Postgres through logical replication (`wal_level=logical`) as `powersync_role`, and keeps its bucket storage in a separate `powersync_storage` database on the same server; `deploy/postgres/init-powersync.sh` creates both the first time the data volume is initialized. Devices authenticate to PowerSync with short-lived HS256 JWTs (`aud: thymble-powersync`, `sub`: the user id) signed with `POWERSYNC_JWT_SECRET`; the app gets them from `GET /v1/sync/token`. Client writes go back through the api (`POST /v1/sync/upload`), never straight to Postgres; see below.

`deploy/powersync/sync-config.yaml` decides what devices download: households and members, recipes with their lines, steps and tags, the meal plan, the household's and the global ingredient catalog, the shopping list with item sources and trips, and stores, groups and preferences, all limited to the user's households. Every query filters `deleted_at IS NULL`, so a delete (always a soft delete) removes the row from devices. Server-only tables never sync: `auth_*`, `household_invite`, `import_job`, `llm_usage`, `llm_cache`, `connector_account`, `connector_item_link`, `learning_event`. `tests/test_sync_config.py` checks all three rules and fails when a new table isn't classified either way.

### Accounts and auth

Sign-in methods (architecture §9, D-015):

- **Sign in with Apple:** the app sends Apple's identity token and the raw nonce to `POST /v1/auth/apple`. The api checks the signature against Apple's published keys (fetched from `appleid.apple.com` and cached), the issuer, the audience (`APPLE_BUNDLE_ID`), expiry, and that the token's nonce is SHA-256 of the raw nonce. Leave `APPLE_BUNDLE_ID` blank to turn it off.
- **Email magic link:** `POST /v1/auth/email/request` emails a link (`MAGIC_LINK_URL?token=...`, a universal link into the app); the app then posts the token to `POST /v1/auth/email/verify`. Links are single-use and expire after `MAGIC_LINK_TTL_MINUTES`. The request endpoint answers the same way whether or not an account exists, and is rate-limited per address and per client. Without SMTP settings, email sign-in is off and `/v1/capabilities` reports `email_sign_in: false`. In development, mail goes to the Mailpit container: open `http://localhost:8025` (or `MAILPIT_PORT`) to read it.

Both return the api's own tokens: a short-lived access token (HS256 JWT signed with `AUTH_JWT_SECRET`, sent as `Authorization: Bearer ...`) and a refresh token. Refresh tokens are opaque, stored only as hashes, and rotate on every `POST /v1/auth/refresh`; presenting an already-used refresh token revokes that whole sign-in (theft detection). `POST /v1/auth/logout` revokes one device, `POST /v1/auth/logout-all` every device. `GET /v1/me` returns the user and their households.

**Households** are the data boundary. A user gets a household on first sign-in and can create more (`POST /v1/households`). Owners invite people with single-use codes (`POST /v1/households/{id}/invites`), rename the household, change roles and remove members; anyone can `POST /v1/households/join` with a code and `POST /v1/households/{id}/leave`. Every household route, and every synced write, checks membership; non-members get 404.

**Household-scoped routes** (imports, shopping, stores, trips, connectors) take the access token and name the household explicitly in an `X-Thymble-Household: <household id>` header; the server checks that the user is a member (`thymble.auth.deps.get_current_household`). A missing header is `400`, a household the user isn't in is `404`. There is no default household and no development shortcut.

**Sync writes.** `POST /v1/sync/upload` takes the PowerSync upload queue (`{"batch": [{"op": "PUT"|"PATCH"|"DELETE", "table", "id", "opData"}]}`) and applies it in one transaction: each row must belong to one of the user's households, rows can't move between households, foreign keys must point into the same household, and only registered tables and columns are accepted (`GET /v1/sync/tables` lists them). Writable: recipes (lines, steps, tags), the meal plan, shopping lists, items and item sources, trips, stores, store groups and item preferences. Columns with server logic behind them stay with the REST endpoints: trip status and order export (`trip_id`), stop-gap links (`bridge_of`), a store's external list, connector provenance. DELETE is a soft delete. A 4xx names the failing entry and nothing is written; the client should report and drop that batch rather than retry it. New features make their tables writable with `thymble.sync.register(SyncTable(Model, ...))`; see `src/thymble/sync/registry.py`.

Alexa account linking will need an OAuth2 authorization-code endpoint; tokens already carry a `client_id` so it can reuse the same token machinery (see `src/thymble/auth/__init__.py`).

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are signed off under the [Developer Certificate of Origin](https://developercertificate.org/).

## Security
Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License
[GNU Affero General Public License v3.0](LICENSE). If you run a modified version of this server as a network service, you must make your modified source available to its users.
