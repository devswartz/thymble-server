# Thymble Server

The open-source, self-hostable server behind **Thymble**, a native iOS cooking app: import and store recipes, plan meals, and keep a smart shopping list that syncs with the lists you already use.

> **Status: early development.** The server skeleton runs (health, capability report, database schema, sync service), but there are no user-facing features yet. Watch the repo to follow along.

## What it will do
- **Recipe import** from the web page you're viewing, pasted text, or a photo of a recipe card
- **Meal planning** for a household, shared across devices
- **A structured shopping list** that merges quantities, groups by store aisle, and works offline
- **List connectors** that mirror the list to Home Assistant (and through it, Alexa)
- **Retailer connectors** that send the list to grocery carts
- **Learning** from your corrections and choices, so it gets better with use
- **Bring your own LLM:** a local model through any OpenAI-compatible endpoint (e.g. Ollama), Anthropic's Claude, or both, routed per task

## Planned stack
Python / FastAPI · PostgreSQL · PowerSync (offline sync to the app) · Redis · Docker Compose

## Self-hosting
Thymble is designed to run on a home server. Docker Compose files and setup docs will land here as the server takes shape.

## Development

You need Docker (Docker Desktop on Windows/macOS, or Docker Engine with the Compose plugin). On Windows, run the commands from WSL2 or PowerShell; both work. Python and [uv](https://docs.astral.sh/uv/) on the host are optional (only for running tools outside containers).

```bash
# 1. Create a .env with random, development-only secrets (git-ignored; never commit it)
python3 scripts/dev_env.py

# 2. Start the stack: postgres, redis, migrations, api, worker, powersync
docker compose up -d --build

# 3. Check it
curl http://localhost:8000/healthz
curl http://localhost:8000/v1/capabilities
curl http://localhost:8080/probes/liveness      # PowerSync

# 4. Run the tests and linters inside the api container
docker compose exec api sh -c 'DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/$POSTGRES_DB" pytest'
docker compose exec api ruff format --check .
docker compose exec api ruff check .

# 5. Stop everything and delete the dev volumes
docker compose down -v
```

`docker compose up` automatically layers `docker-compose.override.yml` on top of `docker-compose.yml`. The override builds the `dev` image target, bind-mounts the source tree, and runs the api (uvicorn) and worker (arq) with auto-reload, so code changes apply without rebuilding. It also publishes Postgres on `127.0.0.1:15432` and Redis on `127.0.0.1:16379`. For a production-shaped run, use only the base file: `docker compose -f docker-compose.yml up -d`.

If a host port is already taken, set `THYMBLE_API_PORT`, `POWERSYNC_PORT`, `POSTGRES_PORT` or `REDIS_PORT` in `.env`.

### Layout

| Path | What it is |
|---|---|
| `src/thymble/` | The Python package: `main.py` (FastAPI app), `config.py` (settings from env vars), `api/` (routes), `db/` + `models/` (SQLAlchemy), `worker.py` (arq jobs), `importer/` (recipe import pipeline), `llm/` (LLM providers, routing, cache, metering) |
| `migrations/` | Alembic migrations. Create one with `docker compose exec api alembic revision --autogenerate -m "..."` |
| `deploy/postgres/` | First-boot SQL for PowerSync: replication role, publication, storage database |
| `deploy/powersync/` | PowerSync service config and sync config (what each device downloads, scoped by household) |
| `scripts/dev_env.py` | Writes a development `.env` from `.env.example` |
| `tests/` | pytest suite; database tests need `DATABASE_URL` and create a throwaway database |

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

The result is saved as a recipe with `is_draft = true` for the user to review. Every ingredient row keeps its original `raw_text`; the recipe's `source_url` is the `final_url` the page actually came from. Anything the pipeline could not settle (no LLM configured, a line it could not parse) is listed in `review_reasons` instead of failing the import.

Until sign-in lands, the import endpoints take the household from an `X-Household-Id` header in development and test; in production they answer `501`.

### LLM providers and routing

All model calls go through `thymble.llm` (`LLMClient.run`), which enforces a strict JSON schema (validated, one retry on invalid output), caches answers in `llm_cache` keyed by the normalized prompt, records every call or cache hit per household in `llm_usage`, and falls back to the other provider when one fails or times out. Providers:

- **Anthropic** (`ANTHROPIC_API_KEY`): `ANTHROPIC_MODEL_SMALL` (default `claude-haiku-5-5`) for small tasks, `ANTHROPIC_MODEL_LARGE` (default `claude-sonnet-5-5`) for page and photo extraction.
- **Any OpenAI-compatible endpoint** (`OPENAI_COMPATIBLE_BASE_URL`, e.g. `http://ollama:11434/v1`, with `LOCAL_MODEL` and optionally `LOCAL_VISION_MODEL`). Local calls run one at a time per worker, and `LLM_LOCAL_TIMEOUT_SECONDS` covers the wait, so a busy GPU falls back instead of queuing.

Tasks T1–T8 (see the architecture doc) route by default to Claude for T1/T2 and to the local model for T3–T8; override per task with `LLM_ROUTE_T<n>=anthropic|local|none`. `LLM_HOUSEHOLD_MONTHLY_TOKEN_LIMIT` caps uncached tokens per household. With no provider configured, LLM steps return "needs review" and the import keeps whatever the rules produced.

### PowerSync

PowerSync reads Postgres through logical replication (`wal_level=logical`) as `powersync_role`, and keeps its bucket storage in a separate `powersync_storage` database on the same server; `deploy/postgres/init-powersync.sh` creates both the first time the data volume is initialized. Devices authenticate to PowerSync with short-lived HS256 JWTs (`aud: thymble-powersync`, `sub`: the user id) signed with `POWERSYNC_JWT_SECRET`; the api will mint these once auth lands.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are signed off under the [Developer Certificate of Origin](https://developercertificate.org/).

## Security
Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License
[GNU Affero General Public License v3.0](LICENSE). If you run a modified version of this server as a network service, you must make your modified source available to its users.
