# Self-hosting Thymble Server

This guide runs the production stack on one Linux host (a VM, a Proxmox LXC, a small VPS)
with Docker Compose, reachable from the internet through a Cloudflare Tunnel, with nightly
database backups. Everything here is in the repository: `docker-compose.yml` (the stack),
`docker-compose.prod.yml` (the production overlay), `deploy/` (Postgres init, PowerSync
config, the backup image) and `.env.example` (every setting).

> Thymble is in early development. Expect breaking changes between versions until the
> first release, and keep backups.

## What runs

| Service | What it does | Reachable from |
|---|---|---|
| `migrate` | One-shot: `alembic upgrade head`, then exits. Everything else waits for it. | — |
| `api` | FastAPI on port 8000: auth, households, imports, shopping, sync uploads | compose network only |
| `worker` | arq jobs: recipe imports, trip reminders, connector sync | — |
| `powersync` | PowerSync service on port 8080: streams each user's data to their devices | compose network only |
| `postgres` | PostgreSQL 17 with `wal_level=logical` (PowerSync replicates from it) | compose network only |
| `redis` | Job queue and rate-limit counters (append-only file on disk) | compose network only |
| `cloudflared` | Cloudflare Tunnel connector: outbound connection to Cloudflare, forwards public requests to `api` and `powersync` | — (outbound only) |
| `backup` | Nightly `pg_dump`, optional age encryption, optional off-site copy | — |

No service publishes a host port. Public traffic goes **client → Cloudflare (TLS) →
tunnel → cloudflared → api / powersync** over the private compose network.

**Why no Caddy or other reverse proxy?** Cloudflare terminates TLS at its edge, and the
tunnel routes each public hostname straight to a container, so a proxy in the middle would
add a hop and a certificate to manage without doing anything. The host opens no inbound
ports at all. If you don't use Cloudflare, put your own TLS proxy (Caddy, Traefik, nginx) on
the compose network in place of `cloudflared`, route the two hostnames the same way, and
set `CLIENT_IP_HEADER` to whatever header that proxy uses for the client address (or blank,
since uvicorn already honours `X-Forwarded-For` from it).

## Requirements

- Linux with Docker Engine and the Compose plugin **v2.24 or newer** (the overlay uses
  `!reset`). In a Proxmox LXC: an unprivileged container with the `nesting` and `keyctl`
  features turned on.
- About 2 vCPU / 4 GB RAM / 20 GB disk for a household or two; more if you run a local
  LLM elsewhere and import lots of photos. The image builds on the host, so the first
  build needs internet access.
- A domain on Cloudflare (free plan is fine) for the tunnel.

## Install

```bash
# Layout: code in /opt/thymble/server, data next to it in /opt/thymble/{postgres,redis,backups,...}
sudo mkdir -p /opt/thymble && cd /opt/thymble
sudo git clone https://github.com/devswartz/thymble-server.git server
cd server

# Configuration: start from the example and fill in every change-me
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

1. Generate each `change-me` secret with `openssl rand -base64 32 | tr '+/' '-_' | tr -d '='`
   (or `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Use a different
   value for each one: `POSTGRES_PASSWORD`, `POWERSYNC_DB_PASSWORD`, `POWERSYNC_JWT_SECRET`,
   `POWERSYNC_API_TOKEN`, `CONNECTOR_ENCRYPTION_KEY`, `AUTH_JWT_SECRET`.
2. Set `THYMBLE_ENV=production`, `THYMBLE_PUBLIC_URL` (e.g. `https://thymble.example.com`) and
   `POWERSYNC_URL` (e.g. `https://thymble-sync.example.com`; see "Tunnel" below).
3. Sign-in: `APPLE_BUNDLE_ID` for Sign in with Apple, and/or the `SMTP_*` settings for email
   magic links.
4. Optional: LLM providers (`ANTHROPIC_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, ...), connectors.
5. Production overlay:
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
   ```
   so plain `docker compose ...` commands use the production files. (Otherwise pass
   `-f docker-compose.yml -f docker-compose.prod.yml` to every command. Never let Compose
   pick up `docker-compose.override.yml`; that is the development setup. Setting
   `COMPOSE_FILE` prevents that.)
6. Backups: see "Backups" below. At minimum, decide on `BACKUP_AGE_RECIPIENTS`.

Back up `.env` itself somewhere safe (a password manager): without `CONNECTOR_ENCRYPTION_KEY`
stored connector credentials can't be decrypted, and the other secrets are needed to
bring a restored database back up as-is.

### Tunnel

1. In the Cloudflare dashboard (Zero Trust → Networks → Tunnels) create a tunnel of type
   *Cloudflared* and copy its token. Thymble's tunnel should be its own, not shared with
   other services, so it stays up and down with this stack.
2. Store the token in a file, readable by cloudflared's user (uid 65532) and nobody else:
   ```bash
   sudo install -d -m 0700 /opt/thymble/secrets
   sudo sh -c 'umask 077; printf %s "<token>" > /opt/thymble/secrets/cloudflared-token'
   sudo chown 65532 /opt/thymble/secrets/cloudflared-token
   ```
   (Elsewhere: set `CLOUDFLARED_TOKEN_FILE`. The token is deliberately not in `.env`,
   because the api and worker containers load all of `.env`.)
3. Add two **public hostnames** to the tunnel:

   | Public hostname | Service |
   |---|---|
   | `thymble.example.com` (= `THYMBLE_PUBLIC_URL`) | `http://api:8000` |
   | `thymble-sync.example.com` (= `POWERSYNC_URL`) | `http://powersync:8080` |

   PowerSync gets its own hostname because it serves its routes at the root
   (`/sync/stream`, `/write-checkpoint2.json`, WebSocket upgrades) and can't run under a
   path prefix; a tunnel ingress rule can match a path but doesn't strip it. Keep both
   hostnames one level below your zone (`thymble-sync.example.com`, not
   `sync.thymble.example.com`) if you rely on Cloudflare's free Universal SSL certificate,
   which covers only `example.com` and `*.example.com`.

   A single hostname is possible by adding path rules on it for PowerSync's routes
   (`^/sync/`, `^/write-checkpoint`) ahead of the api, with `POWERSYNC_URL` equal to
   `THYMBLE_PUBLIC_URL`. That depends on PowerSync and its client SDKs never adding a route
   outside those paths, so it isn't the default.

The api trusts `CF-Connecting-IP` (set by Cloudflare) as the client address for rate
limits (`CLIENT_IP_HEADER`); that is safe only because nothing but the tunnel can reach
the api.

## Start

```bash
cd /opt/thymble/server
docker compose up -d --build       # builds the images, runs migrations, starts everything
docker compose ps                   # every service healthy (migrate: exited 0)
docker compose logs -f api worker   # Ctrl-C to stop following
curl https://thymble.example.com/healthz
curl https://thymble.example.com/v1/capabilities
curl https://thymble-sync.example.com/probes/liveness
```

The `backup` service reports *healthy* from the start and turns *unhealthy* if no backup
has succeeded in 26 hours.

## Settings

All settings are environment variables in `.env`; `.env.example` lists every one with a
comment. The ones specific to running in production:

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_FILE` | — | `docker-compose.yml:docker-compose.prod.yml` to use the overlay by default |
| `THYMBLE_DATA_DIR` | `/opt/thymble` | Host directory for `postgres/`, `redis/`, `backups/`, `rclone/` |
| `CLOUDFLARED_TOKEN_FILE` | `/opt/thymble/secrets/cloudflared-token` | File with the tunnel token |
| `THYMBLE_PUBLIC_URL` | — | Public URL of the api (the tunnel hostname) |
| `POWERSYNC_URL` | — | Public URL of PowerSync (its own tunnel hostname) |
| `CLIENT_IP_HEADER` | `CF-Connecting-IP` in the overlay | Header with the real client address |
| `RATE_LIMIT_*` | see `.env.example` | Magic-link and invite-join limits (`N/period`, `0` = off) |
| `REFRESH_REUSE_GRACE_SECONDS` | `30` | Seconds a just-rotated refresh token still returns the same new pair |
| `TZ` | `UTC` | Time zone for `BACKUP_AT` |
| `BACKUP_AT` | `03:30` | Daily backup time (HH:MM) |
| `BACKUP_KEEP_DAYS` | `14` | Days of dumps kept in `$THYMBLE_DATA_DIR/backups` |
| `BACKUP_AGE_RECIPIENTS` | — | age public key(s); dumps are encrypted for them |
| `BACKUP_REQUIRE_ENCRYPTION` | `false` | Fail rather than write an unencrypted dump |
| `BACKUP_RCLONE_REMOTE` | — | rclone destination for an off-site copy, e.g. `offsite:thymble-backups` |
| `BACKUP_OFFSITE_KEEP_DAYS` | `30` | Days kept off-site (`0` = never delete) |
| `BACKUP_NOTIFY_URL` | — | Gets a JSON POST when a backup fails |
| `BACKUP_NOTIFY_ON_SUCCESS` | `false` | ...and after each success |
| `BACKUP_VERIFY` | `false` | Restore each dump into a scratch database before keeping it |

## Backups

The `backup` service dumps the Thymble database (`pg_dump -Fc`) every day at `BACKUP_AT`
into `$THYMBLE_DATA_DIR/backups/thymble-<UTC timestamp>.dump[.age]`, writes a `.sha256`
next to it, deletes local dumps older than `BACKUP_KEEP_DAYS`, and optionally copies the
dump off-site. A failure POSTs `{"status": "failed", "message": ..., "host": ..., "file": ...}`
to `BACKUP_NOTIFY_URL` (a Home Assistant webhook works) and marks the container unhealthy
after 26 hours without a success.

What is and isn't in a dump:
- **In it:** the whole Thymble database: accounts, households, recipes, plans, lists,
  connector settings (credentials still encrypted with `CONNECTOR_ENCRYPTION_KEY`), the
  LLM cache. PowerSync's grants and publication come along.
- **Not in it, on purpose:** PowerSync's bucket storage (`powersync_storage`; rebuilt from
  the main database after a restore), Redis (queued jobs and rate-limit counters only).
- **Not in it:** `.env`. Keep it in a password manager.

Whole-machine snapshots (e.g. Proxmox `vzdump`) are a good extra layer, but a running
Postgres data directory copied mid-write isn't a reliable backup on its own; the dump is.

### Encryption (age)

Encrypt anything that leaves the host. Create a key pair **on another machine** and keep
the private key off the server:

```bash
age-keygen -o thymble-backup.key      # prints "Public key: age1..."
```

Put the public key in `.env` as `BACKUP_AGE_RECIPIENTS=age1...` (several keys, space or
comma separated, all can decrypt) and store `thymble-backup.key` in your password manager.
Lose it and the encrypted dumps are unreadable. Set `BACKUP_REQUIRE_ENCRYPTION=true` to make
sure an empty recipient list fails loudly instead of silently writing plain dumps.

### Off-site copy (rclone)

Any [rclone](https://rclone.org/overview/) backend works: S3-compatible storage,
Backblaze B2, SFTP to another machine, and so on. Write the remote's configuration to
`$THYMBLE_DATA_DIR/rclone/rclone.conf`, for example:

```ini
[offsite]
type = sftp
host = backup-host.example.net
user = thymble
key_file = /config/rclone/id_ed25519
```

(the `rclone/` directory is mounted at `/config/rclone`, so a key file kept there is
referenced by that path), then set `BACKUP_RCLONE_REMOTE=offsite:thymble-backups`. The
service refuses to copy unencrypted dumps off-site unless `BACKUP_OFFSITE_ALLOW_UNENCRYPTED`
is set. Off-site dumps older than `BACKUP_OFFSITE_KEEP_DAYS` are deleted after each upload.
Check the remote from the container: `docker compose run --rm --entrypoint rclone backup lsl offsite:thymble-backups`.

### Running it by hand

```bash
docker compose exec backup thymble-backup run      # take a backup now
docker compose exec backup thymble-backup list     # what's on disk
docker compose logs backup                         # schedule, results, failures
```

## Testing a restore

A backup you haven't restored is a hope. `verify` restores a dump into a throwaway database
on the same Postgres server, prints the migration version and row counts per table, and
drops it again; production is not touched.

```bash
# Newest unencrypted dump
docker compose exec backup thymble-backup verify

# An encrypted dump: mount the private key for this one command only
docker compose run --rm \
  -v /path/to/thymble-backup.key:/run/age.key:ro -e BACKUP_AGE_IDENTITY_FILE=/run/age.key \
  backup verify thymble-20260101T033000Z.dump.age
```

Compare the counts with what you expect (users, recipes, shopping items). Do this after
setting up backups, after changing the encryption key or off-site remote, and every few
months. `BACKUP_VERIFY=true` additionally runs the same check on every nightly dump, before
it is encrypted.

For a full drill, restore onto a second, scratch install (another VM or LXC) following
"Disaster recovery" below and sign in from a client against it.

## Restore

Restoring **replaces** the live database. Take a fresh backup first if the current data is
worth anything.

```bash
cd /opt/thymble/server
docker compose exec backup thymble-backup run                # safety copy of the current state
docker compose stop cloudflared api worker powersync         # nothing writes during the restore
docker compose run --rm \
  -v /path/to/thymble-backup.key:/run/age.key:ro -e BACKUP_AGE_IDENTITY_FILE=/run/age.key \
  backup restore --yes thymble-20260101T033000Z.dump.age
docker compose up -d                                         # migrations run, services start
```

`restore --yes` checks the `.sha256`, decrypts, drops PowerSync's replication slot, recreates
the database from the dump, re-applies PowerSync's grants and publication, and empties
PowerSync's storage database so PowerSync re-replicates from scratch. Devices download their
data again on their next sync; changes a device made after the backup was taken and hadn't
uploaded yet are uploaded as usual, while server-side changes after the backup are gone.

### Disaster recovery (new host)

1. Install Docker, clone the repository to the same layout, restore `.env` and the tunnel
   token file from your password manager.
2. Copy the dump into `$THYMBLE_DATA_DIR/backups/` (from the old disk or from the off-site
   remote with rclone).
3. `docker compose up -d postgres` (first start creates the PowerSync role and storage
   database), then run the `restore --yes` command above, then `docker compose up -d --build`.

## Upgrades

```bash
cd /opt/thymble/server
docker compose exec backup thymble-backup run   # backup before every upgrade
git pull
docker compose up -d --build                    # rebuilds, runs migrations, restarts what changed
docker compose ps
```

Migrations run automatically in the `migrate` service, and the new api, worker and
PowerSync containers start only after it succeeds. Alembic runs migrations inside a
transaction on Postgres, so a failed one leaves the schema as it was; `docker compose up`
reports the failure and `docker compose logs migrate` says why. Migrations are
forward-only in practice: to go back, check out the previous version and restore the
backup taken before the upgrade.

Pinned images (`postgres:17`, `redis:7-alpine`, PowerSync, cloudflared) change only when the
repository updates them. Postgres stays on major version 17: a major-version upgrade means a
dump and restore, which the release notes will spell out.

## Monitoring

- `GET /healthz` on the public URL: the api is up (the tunnel and api both work).
- `docker compose ps`: health of every container. `backup` turns unhealthy after 26 hours
  without a successful backup; `worker` when it stops refreshing its health key in Redis;
  `cloudflared` when the tunnel isn't connected.
- `BACKUP_NOTIFY_URL` for backup failures.

## Security checklist

- `.env` is `chmod 600`, never committed, and backed up in a password manager.
- Every secret is long, random and different; `AUTH_JWT_SECRET` differs from
  `POWERSYNC_JWT_SECRET`.
- No host ports: `docker compose ps` shows no `0.0.0.0:` mappings.
- Backups are encrypted (`BACKUP_REQUIRE_ENCRYPTION=true`) and the private key is not on the
  server.
- PowerSync's admin API (`/api/...` on the sync hostname) is protected by
  `POWERSYNC_API_TOKEN`; keep it strong.
