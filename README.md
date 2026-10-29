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
| `src/thymble/` | The Python package: `main.py` (FastAPI app), `config.py` (settings from env vars), `api/` (routes), `db/` + `models/` (SQLAlchemy), `worker.py` (arq jobs) |
| `migrations/` | Alembic migrations. Create one with `docker compose exec api alembic revision --autogenerate -m "..."` |
| `deploy/postgres/` | First-boot SQL for PowerSync: replication role, publication, storage database |
| `deploy/powersync/` | PowerSync service config and sync config (what each device downloads, scoped by household) |
| `scripts/dev_env.py` | Writes a development `.env` from `.env.example` |
| `tests/` | pytest suite; database tests need `DATABASE_URL` and create a throwaway database |

### Dependencies

Dependencies are managed with [uv](https://docs.astral.sh/uv/): `pyproject.toml` declares them and `uv.lock` pins exact versions. Add one with `uv add <package>` (or `uv add --dev <package>`), commit both files, and rebuild the image. To work outside Docker: `uv sync`, then `uv run pytest` / `uv run ruff check .`.

### Configuration

Everything is configured through environment variables; `.env.example` lists them all with placeholder values. `GET /v1/capabilities` reflects that configuration, for example `llm` turns on when `ANTHROPIC_API_KEY` or `OPENAI_COMPATIBLE_BASE_URL` is set.

### PowerSync

PowerSync reads Postgres through logical replication (`wal_level=logical`) as `powersync_role`, and keeps its bucket storage in a separate `powersync_storage` database on the same server; `deploy/postgres/init-powersync.sh` creates both the first time the data volume is initialized. Devices authenticate to PowerSync with short-lived HS256 JWTs (`aud: thymble-powersync`, `sub`: the user id) signed with `POWERSYNC_JWT_SECRET`; the api will mint these once auth lands.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are signed off under the [Developer Certificate of Origin](https://developercertificate.org/).

## Security
Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License
[GNU Affero General Public License v3.0](LICENSE). If you run a modified version of this server as a network service, you must make your modified source available to its users.
