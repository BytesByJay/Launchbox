# Launchbox — Architecture & Developer Reference

This is the deep reference: every module, every data structure, every request path, and
why each piece is built the way it is. If you're new to this codebase (human or AI
agent), start here — the [README](../README.md) is the pitch, this is the manual.

Verified against the code on 2026-08-09. When in doubt, the source is the source of
truth; this document should be kept in sync with it, not the other way around.

## Table of contents

1. [What Launchbox is](#1-what-launchbox-is)
2. [Repository layout](#2-repository-layout)
3. [Runtime dependencies](#3-runtime-dependencies)
4. [Module reference](#4-module-reference)
5. [Data model — SQLite state store](#5-data-model--sqlite-state-store)
6. [Configuration reference — `launchbox.yaml`](#6-configuration-reference--launchboxyaml)
7. [The deployment lifecycle, end to end](#7-the-deployment-lifecycle-end-to-end)
8. [The health-gated swap (`_promote`)](#8-the-health-gated-swap-_promote)
9. [Other lifecycles: rollback, env update, database, remove](#9-other-lifecycles-rollback-env-update-database-remove)
10. [Database credential model](#10-database-credential-model)
11. [Networking & routing](#11-networking--routing)
12. [Health supervision](#12-health-supervision)
13. [CLI reference](#13-cli-reference)
14. [Dashboard — HTTP API reference](#14-dashboard--http-api-reference)
15. [Dashboard — frontend](#15-dashboard--frontend)
16. [Testing](#16-testing)
17. [Known limitations & security notes](#17-known-limitations--security-notes)
18. [Extension points — "if you want to add X, start here"](#18-extension-points)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. What Launchbox is

A self-hosted PaaS: `git push` to a bare repository on your own machine, and Launchbox
builds a Docker image, health-checks the new container before it receives any traffic,
routes `<app>.localhost` to it through Traefik, and (optionally) provisions a database
with generated per-app credentials. No cloud account, no vendor lock-in.

**The one invariant that matters more than any other**: nothing that destroys the
currently-running version of an application happens until its replacement has been
created, started, and verified healthy. Every mutating action in this codebase — a
forward deploy, a rollback, a dashboard-triggered environment edit, provisioning a
database — funnels through one shared routine that enforces this
(`deploy._promote`, see [§8](#8-the-health-gated-swap-_promote)). There is one safe way
to change what's running, not four.

---

## 2. Repository layout

```
Launchbox/
├── launchbox/                  # The application package
│   ├── __main__.py             # CLI entry point: `python -m launchbox <command>`
│   ├── config.py               # BASE_DIR/APPS_DIR/REPOS_DIR, app-name validation
│   ├── config_parser.py        # LaunchboxConfig — parses/merges launchbox.yaml
│   ├── logger.py               # setup_logger(), exception hierarchy
│   ├── builder.py               # docker build, image tagging by commit SHA
│   ├── runner.py                # container lifecycle primitives (no policy)
│   ├── router.py                # writes Traefik's dynamic route files
│   ├── database_manager.py     # provisions Postgres/MySQL/MongoDB containers
│   ├── deploy.py                # THE ORCHESTRATOR — deploy/rollback/env/db/remove
│   ├── supervisor.py            # background health-poll + restart loop
│   ├── state.py                 # SQLite persistence (StateStore)
│   ├── init.py                   # registers an app: bare repo + git hook
│   ├── ssl_manager.py           # local HTTPS certs via mkcert
│   ├── dashboard.py              # Flask web UI + JSON API
│   └── templates/               # Jinja2 templates for the dashboard
│       ├── base.html            # shell: nav, theme toggle, toasts, shared JS
│       ├── dashboard.html       # app list (dense table) + activity feed
│       └── app_detail.html      # per-app tabs: overview/env/database/deployments
├── tests/                        # pytest suite, one file per module (mostly)
├── docs/
│   ├── ARCHITECTURE.md           # this file
│   └── superpowers/              # planning artifacts from the Chapter 6 hardening pass
├── apps/                          # local application source (Dockerfile-based apps)
│   └── <app_name>/                #   only exists for apps deployed from a local dir,
│                                   #   not for git-push-only apps (see §7)
├── repos/                          # bare git repos apps push to; <app>.git/hooks/post-receive
├── traefik/
│   ├── traefik.yml                # static Traefik config (entrypoint :80, docker provider)
│   └── dynamic/                   # ROUTE FILES — one <app>.yml per deployed app, written
│                                   #   by router.py, watched by Traefik's file provider
├── state/                          # launchbox.db (SQLite) — chmod 700, gitignored
├── certs/, letsencrypt/            # mkcert / ACME certificate material, gitignored
├── logs/                            # per-module log files from logger.py, gitignored
├── docker-compose.yml               # the Traefik container definition
├── setup.sh                          # first-time directory scaffolding + permissions
├── deploy-production.sh              # opinionated systemd-based production install
├── requirements.txt                  # pyyaml, docker, flask + pytest, pytest-mock
└── pytest.ini
```

**Directories that don't exist until something creates them**: `apps/<name>/` only
exists for an application whose source lives on the Launchbox host directly. An
application deployed purely via `git push` never gets one — the git hook builds from a
temporary worktree that is deleted immediately after the build (see [§7](#7-the-deployment-lifecycle-end-to-end)).
This single fact is the reason several pieces of code exist: `LaunchboxConfig.from_mapping`
([§6](#6-configuration-reference--launchboxyaml)), the `config_json`/`env_json` replay
mechanism ([§9](#9-other-lifecycles-rollback-env-update-database-remove)), and the
apps/-directory-union logic in `dashboard.get_app_list` ([§15](#15-dashboard--frontend)).

---

## 3. Runtime dependencies

| Dependency | Role | Notes |
|---|---|---|
| **Docker Engine** | Runs every app container, database container, and (via `docker-compose.yml`) Traefik itself | Accessed via the `docker` CLI (subprocess, in `runner.py`/`router.py`) **and** the `docker` Python SDK (in `dashboard.py`/`database_manager.py`) — two different access paths to the same daemon, see [§17](#17-known-limitations--security-notes) |
| **Traefik v2.11** | Reverse proxy; routes `*.localhost` to the right container | Pinned to v2.11 in `docker-compose.yml` — v2.10 and earlier vendor a Docker SDK hardcoded to API 1.24, which Docker Engine 29+ (minimum API 1.40) rejects outright; there is no working `DOCKER_API_VERSION` env-var workaround on the older line |
| **SQLite** (via Python's stdlib `sqlite3`) | All deployment/app/database state | One file, `state/launchbox.db`; no server process |
| **Flask** | Dashboard web UI + JSON API + CLI's `dashboard` subcommand | Runs on the built-in dev server (`debug=True`), not a production WSGI server — see [§17](#17-known-limitations--security-notes) |
| **mkcert** (optional) | Locally-trusted HTTPS certificates | Only touched if an app sets `https.enabled: true`; `ssl_manager.py` will attempt to install it via `apt`/`brew` if absent |
| **PyYAML** | Parses `launchbox.yaml` and writes Traefik's dynamic route documents | |
| **pytest + pytest-mock** | Test suite | `mocker` fixture used throughout to fake the Docker daemon — see [§16](#16-testing) |

Python 3.8+. Install with `pip install -r requirements.txt`, or use the checked-in
`.venv/`.

---

## 4. Module reference

Ordered roughly by dependency direction — later modules import earlier ones.

### `config.py` (25 lines)
- `BASE_DIR`, `APPS_DIR`, `REPOS_DIR` — the three filesystem roots, derived from this
  file's own location (`BASE_DIR = <repo root>`).
- `APP_NAME_RE = ^[a-zA-Z0-9][a-zA-Z0-9_-]*$`, checked with `.fullmatch()` (not
  `.match()` — a trailing-newline bug with `.match()` + `$` was found and fixed here).
- `validate_app_name(app_name) -> str` — raises `ValueError` on anything outside that
  character set. Called at the top of every public entry point that takes an app name
  (`deploy`, `rollback`, `remove_app`, `init`, `router.route_file_path`, …) because app
  names are interpolated into filesystem paths, Docker image tags, and (via the
  generated git hook) a shell script.

### `logger.py` (56 lines)
- `setup_logger(name, level="INFO") -> Logger` — console (INFO) + per-module file
  handler (DEBUG) writing to `logs/<name>.log`. Idempotent (returns the existing logger
  if handlers are already attached, so re-importing a module doesn't double-log).
- Exception hierarchy: `LaunchboxError` (base) → `ConfigurationError`, `BuildError`,
  `DeploymentError`. `dashboard.py` and `__main__.py` both catch `LaunchboxError`
  specifically to turn it into a clean error response/exit code instead of a traceback.

### `config_parser.py` (206 lines) — `LaunchboxConfig`
Parses and merges `launchbox.yaml` over a built-in default (see full schema in
[§6](#6-configuration-reference--launchboxyaml)).

- `LaunchboxConfig(app_path)` — normal constructor; reads `<app_path>/launchbox.yaml` if
  present, deep-merges over defaults, also loads `<app_path>/.env` into
  `get_environment_vars()`.
- `LaunchboxConfig.from_mapping(mapping)` — **alternate constructor with no filesystem
  access at all**. `app_path`/`config_path` are `None`. This is how a rollback
  reconstructs the exact configuration a past deployment ran with, from the JSON stored
  on its `deployments` row, without ever touching `apps/<app>/` (which for a git-pushed
  app doesn't exist). Critically, `get_environment_vars()` on a `from_mapping` instance
  does **not** fall back to reading a relative `.env` file, because that would pick up
  whatever happens to be in the process's current working directory.
- Getters: `get_port()`, `get_dockerfile()`, `get_build_context()`,
  `get_environment_vars()`, `get_resource_limits()`, `get_health_check()`,
  `is_database_enabled()`, `get_database_config()`, `is_https_enabled()`,
  `should_redirect_http()`.

### `builder.py` (92 lines)
- `image_name(app_name) -> str` — `"launchbox-<app_name>"`.
- `build(app_name, source_dir=None, tag=None) -> str` — runs `docker build`, returns the
  primary image reference. **Tags by commit SHA** (`launchbox-<app>:<sha7>`), and
  additionally moves `launchbox-<app>:latest` to point at the same build (unless the tag
  *is* `latest`). This dual-tagging is what makes rollback possible without a rebuild —
  see [§8](#8-the-health-gated-swap-_promote) for why `:latest` alone isn't enough.
- Raises `BuildError` (with captured stdout/stderr) on a non-zero exit from `docker build`.

### `runner.py` (357 lines) — container primitives, no policy
Everything here is a fact-checker or an imperative action; nothing here decides *when*
to act — that's `deploy.py`'s job.

- **Duration parsing**: `parse_duration("30s") -> 30.0`, understands `ms|s|m|h`.
  `compute_health_timeout(health_check)` derives a probe's total wait budget as
  `interval × max(retries, 1) + probe_timeout + 15s grace`, overridable wholesale via the
  `LAUNCHBOX_HEALTH_TIMEOUT` env var (used by the test suite to keep runs fast).
- **Docker facts** (each a thin `subprocess.run(["docker", ...])` wrapper):
  `ensure_network()`, `container_exists()`, `find_app_containers(app_name)` (label-based,
  filtered on **both** `launchbox.app=<name>` **and** `launchbox.service=app` — the
  second filter exists because `database_manager.py` stamps `launchbox.app` on database
  containers too, and without it a reaping pass would delete an app's database),
  `container_is_running()`, `image_exists()`, `image_id()` (the immutable `sha256:...`
  an image reference resolves to *right now* — recorded at deploy time because a tag can
  move later), `list_images()`, `remove_image()`, `docker_health_status()` (returns one
  of `healthy | unhealthy | starting | none | missing`), `container_logs()`,
  `stop_and_remove()`, `restart_container()`.
- **Lifecycle**:
  - `build_health_command(port, path)` — the actual `HEALTHCHECK` command injected into
    containers: a one-line `python3 -c "urllib.request.urlopen(...)"`, because `curl` is
    absent from most slim base images.
  - `create_container(...)` — `docker run -d`, labelled `launchbox.app=<name>` and
    `launchbox.service=app` (plus `launchbox.commit=<sha>` if given). **Deliberately
    creates no Traefik label** — the container is unreachable until `router.py` writes
    a route for it.
  - `wait_for_health(container_name, health_check, timeout=None, ...) -> bool` — with a
    configured probe, polls `docker_health_status` until `healthy`/`unhealthy`/timeout.
    **Without one**, falls back to a weaker gate: must be running, and still running
    after a 5-second settle period — catches "starts then immediately exits" but not "runs
    fine but is actually broken."
  - `run(app_name) -> bool` — legacy shim kept for backwards compatibility; delegates to
    `deploy.deploy()`.

### `router.py` (138 lines) — Traefik route files
Writes Traefik's *dynamic* (file-provider) configuration — deliberately not container
labels. See [§11](#11-networking--routing) for the full rationale.

- `route_file_path(app_name, dynamic_dir) -> str` — `traefik/dynamic/<app>.yml`, after
  `validate_app_name` (an unvalidated app name here would be an arbitrary-file-write/
  delete primitive).
- `build_route_document(app_name, backend_host, port, https=False, redirect_http=True)`
  — the actual YAML structure: an HTTP router with rule `` Host(`<app>.localhost`) ``, a
  service load-balancing to `http://<backend_host>:<port>`, and — if `https` — a second
  `<app>-secure` router on the `websecure` entrypoint plus (if `redirect_http`) a
  `redirectScheme` middleware on the plain-HTTP router.
- `write_route(...)` — writes to `<target>.tmp` then `os.replace()`s it over the real
  target. **Atomic**, so Traefik's file-watcher never observes a half-written document.
- `read_route(...)`, `remove_route(...)`.

### `database_manager.py` (508 lines) — `DatabaseManager`
Provisions Postgres/MySQL/MongoDB containers with per-application credentials. Full
credential-resolution rationale in [§10](#10-database-credential-model).

- `DB_USERNAME = 'launchbox'` — **shared** across every app deliberately; isolation
  comes from each app having its own database *container* plus its own password, and a
  per-app username would risk MySQL's 32-character username limit.
- `LEGACY_PASSWORD = 'launchbox123'` — the fixed password every database used before
  per-app credentials existed. Kept as a constant because a pre-existing, unrecorded
  database container's real password *is* this string, baked into its data directory —
  see `_resolve_credentials`.
- `PASSWORD_BYTES = 24` — `secrets.token_urlsafe(24)` for generated passwords;
  URL-safe alphabet only (`[A-Za-z0-9_-]`), so it's always safe to interpolate directly
  into a `DATABASE_URL`.
- `create_database_for_app(app_name, app_path, config=None) -> Optional[Dict[str,str]]`
  — the public entry point. Returns `None` if `database.enabled` is false. `config` is
  injectable so a rollback replay doesn't have this re-read `launchbox.yaml` from a
  directory that may not exist.
- `_resolve_credentials(app_name, engine, container_name) -> (username, password)` — the
  three-case resolver (reuse recorded → adopt legacy if container exists unrecorded →
  generate new). `_container_exists` **raises** on a daemon error rather than treating it
  as "doesn't exist" — an unreachable daemon must never be read as "safe to generate a
  fresh password."
- `_create_postgresql` / `_create_mysql` / `_create_mongodb` — each: reuse a
  running/stopped container of the same name if one exists, else `docker run` the
  official image with generated credentials, then block in `_wait_for_*` until a
  readiness probe succeeds (`pg_isready`, `mysqladmin ping` — **with the real generated
  username/password**, not a hardcoded pair — or a `mongo --eval` ping).
  `POSTGRES_HOST_AUTH_METHOD=trust` was previously set here, which disabled password
  checking entirely; removed. MySQL's root password is a separate, unrecorded
  `secrets.token_urlsafe()` value — nothing in Launchbox ever connects as root.
- `_record_state(...)` — writes the connection info into `StateStore.record_database`;
  wrapped in try/except so a bookkeeping failure never fails provisioning itself.
  `volume_name` is always recorded as `""`: **no engine mounts a named volume today**, so
  a database container's data does not survive `docker rm` — see [§17](#17-known-limitations--security-notes).
- `remove_database_for_app(app_name) -> bool`, `list_databases() -> List[Dict]`
  (label-filtered on `launchbox.service=database`).

### `deploy.py` (849 lines) — **the orchestrator**
The only module that makes decisions; everything above it is a primitive it calls. Full
walkthroughs in [§7](#7-the-deployment-lifecycle-end-to-end)–[§9](#9-other-lifecycles-rollback-env-update-database-remove).

Public functions: `deploy()`, `rollback()`, `update_env()`, `configure_database()`,
`detach_database()`, `remove_app()`. Shared private machinery: `_promote` (the swap
itself), `_run_promotion` (uniform failure-recording wrapper), `_find_previous_container`
(state-store lookup with a label-based fallback), `_replayed_config`/
`_replayed_environment` (rollback/env/database-change replay), `_resolve_rollback_image`
(refuses to resolve a moving `:latest` tag), `_reap_stray_containers`,
`_redeploy_with_database_change` (shared by `configure_database`/`detach_database`).

`RESERVED_ENV_KEYS = {DATABASE_URL, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
DB_TYPE}` — these seven cannot be set/unset through `update_env`; they're re-resolved
live from the database container on every deploy, so a stray manual edit could never
desynchronise a container from the database it's actually pointed at.

`DATABASE_ENGINES = {postgresql: '13', mysql: '8', mongodb: '7'}` — the engines the
dashboard/CLI can provision, with their default versions.

`ROUTE_PROPAGATION_SECONDS = 3.0` (env override `LAUNCHBOX_ROUTE_PROPAGATION`) — how long
`_promote` waits after writing a route before touching the old container. Traefik's file
provider debounces reloads by `providersThrottleDuration` (default 2s); skipping this
wait produced reproducible 502s during hardening.

### `supervisor.py` (133 lines)
- `supervise_once(store, health_fn=runner.docker_health_status,
  restart_fn=runner.restart_container, max_attempts=3) -> List[dict]` — one pass over
  every known app. `healthy`/`none` → resets restart counter. `starting` → no-op.
  `missing` → logs a warning (container is gone; does not restart, does not mark
  failed — a human needs to redeploy). `unhealthy` → restart, increment counter; at
  `max_attempts` (default 3, `LAUNCHBOX_MAX_RESTARTS`), **mark failed and stop touching
  it** instead of restarting again. Apps already `marked_failed` are skipped outright.
  Returns the list of actions taken — this is what makes the decision logic unit-testable
  without a Docker daemon, and what the dashboard could surface as an activity log.
- `run_forever(interval=None)` — the loop `python -m launchbox supervisor` runs. Any
  exception from a single pass is caught and logged; the loop itself never dies.
  `POLL_INTERVAL` default 30s (`LAUNCHBOX_SUPERVISOR_INTERVAL`).
- `start_background_thread(interval=None)` — daemon thread variant, for running the
  supervisor alongside another long-lived process. **Not currently wired into the
  dashboard process** — running the supervisor is a separate, manual step
  (`python -m launchbox supervisor`).

### `init.py` (167 lines)
- `_validate_branch_name(branch)` — `^[A-Za-z0-9][A-Za-z0-9._/-]*$` via `.fullmatch()`,
  plus an explicit `..` rejection. The branch name is spliced **literally** into the
  generated hook's shell script; this is a command-injection guard, not just input
  validation.
- `HOOK_TEMPLATE` / `render_hook(app_name, base_dir, default_branch="main") -> str` — the
  post-receive hook installed into every registered app's bare repo. Properties that
  matter: `set -euo pipefail` (a failing deploy fails the push visibly instead of being
  swallowed); filters on `refs/heads/<default_branch>` (a push to a feature branch does
  nothing); checks out the pushed commit into a `mktemp -d` worktree and runs the deploy
  against *that*, not against whatever happens to be checked out in a working tree
  (there usually isn't one — bare repos have no working tree at all).
- `init(app_name, default_branch="main") -> str` — creates the bare repo (`git init
  --bare`) if absent, (re)writes and `chmod 755`s the hook unconditionally (so an app
  registered under an older Launchbox picks up hook fixes on next `init`), and calls
  `StateStore().register(app_name)` so the app is visible in the dashboard/CLI
  immediately as "not deployed," before any push has happened.

### `ssl_manager.py` (165 lines) — `SSLManager`
mkcert wrapper: `is_mkcert_installed()`, `install_mkcert()` (best-effort via `brew`/`apt`;
prints manual instructions for `yum` or anything else), `setup_ca()` (`mkcert -install`),
`create_certificate(domains)`, `get_certificate_for_app(app_name)` (reuses an existing
cert/key pair if present), `update_traefik_config_for_https()` (regenerates
`traefik/dynamic.yml` listing every `.pem`/`-key.pem` pair in `certs/`). This module is
the least integrated into the rest of the pipeline — HTTPS routing itself is handled by
`router.py` writing a `websecure` entrypoint router; this module's job is only producing
the certificate material Traefik's TLS store references.

### `dashboard.py` (725 lines) — full reference in [§14](#14-dashboard--http-api-reference)/[§15](#15-dashboard--frontend).

### `__main__.py` (246 lines) — full reference in [§13](#13-cli-reference).

---

## 5. Data model — SQLite state store

`state/launchbox.db`, opened via `StateStore` (`state.py`, 422 lines). One connection per
`StateStore` instance; `sqlite3.Row` row factory; `PRAGMA foreign_keys = ON`.

### `apps`
| Column | Type | Notes |
|---|---|---|
| `app_name` | TEXT PRIMARY KEY | |
| `current_container` | TEXT | The container name currently serving this app |
| `current_image` | TEXT | The image tag it's running |
| `current_deployment` | INTEGER | FK (informal) → `deployments.id` |
| `health_state` | TEXT | `healthy \| unhealthy \| starting \| unknown \| failed` — set by the supervisor |
| `restart_attempts` | INTEGER, default 0 | Reset on every successful promotion |
| `marked_failed` | INTEGER (bool), default 0 | Set once `restart_attempts` hits the ceiling |

### `deployments`
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `app_name` | TEXT | |
| `commit_sha` | TEXT | |
| `image_tag` | TEXT | e.g. `launchbox-myapp:a1b2c3d` |
| `image_id` | TEXT | **Immutable** `sha256:...` — resolved at deploy time; this, not `image_tag`, is what rollback actually uses (see [§8](#8-the-health-gated-swap-_promote)) |
| `container_name` | TEXT | |
| `status` | TEXT | `in_progress \| success \| failed` |
| `started_at` / `finished_at` | TEXT (ISO 8601, UTC) | |
| `error` | TEXT | Failure message, if any |
| `logs` | TEXT | Container logs captured on a health-probe failure |
| `config_json` | TEXT | The **fully resolved** `LaunchboxConfig.config` mapping this deployment ran with, JSON-serialized. This is what rollback replays instead of re-reading `launchbox.yaml` |
| `env_json` | TEXT | The **fully resolved** environment (including database credentials) this deployment ran with. Also what rollback replays — ⚠️ **stored in plaintext**, see [§17](#17-known-limitations--security-notes) |
| `kind` | TEXT | `deploy \| rollback \| env \| database` — what triggered this row. NULL on rows predating this column, which the reading side treats identically to `"deploy"` |

Index: `idx_deployments_app ON deployments (app_name, id DESC)`.

### `databases`
| Column | Type | Notes |
|---|---|---|
| `app_name` | TEXT PRIMARY KEY | One database per app, by design |
| `engine` | TEXT | `postgresql \| mysql \| mongodb` |
| `container_name` | TEXT | e.g. `myapp_postgres` |
| `volume_name` | TEXT | Always `""` today — no engine mounts a named volume |
| `db_name`, `username`, `password` | TEXT | ⚠️ password in plaintext |
| `created_at` | TEXT | |

### Migration mechanism
`_ADDED_DEPLOYMENT_COLUMNS` is a tuple of `(column, sql_type)` pairs added to
`deployments` after the schema's first release (currently: `config_json`, `env_json`,
`image_id`, `kind`). `StateStore._migrate()` reads `PRAGMA table_info(deployments)` and
`ALTER TABLE ... ADD COLUMN`s anything missing — idempotent, runs on every
`StateStore()` construction, safe against both a brand-new database (schema already has
the columns, so this is a no-op) and an old one (columns get added, no data lost).
**When adding a new column to `deployments`, add it to both `_SCHEMA` and this tuple.**

### Key `StateStore` methods not obvious from the schema
- `register(app_name)` — creates an `apps` row with no deployment yet, so a freshly
  `init`'d app is visible as "not deployed" instead of invisible.
- `last_successful_deployment(app_name)` — most recent `success` row that is **not**
  the currently-running one (excludes `current_deployment` explicitly).
- `find_successful_deployment_by_sha(app_name, commit_sha)` — matches on a SHA prefix via
  `LIKE '<sha>%'`; the input is run through `_escape_like()` first because it can come
  from a raw CLI argument, and an unescaped `%`/`_` there is a wildcard that could match
  an unintended deployment.
- `list_recent_deployments(limit=15)` — cross-application feed (dashboard's homepage
  activity list); `list_deployments(app_name, ...)` is the per-app version.

---

## 6. Configuration reference — `launchbox.yaml`

Lives at `apps/<app_name>/launchbox.yaml` (optional — every key has a default).
Full schema, with defaults from `config_parser.LaunchboxConfig._default_config()`:

```yaml
app:
  port: 3000                    # what the app listens on inside the container
  health_check: null            # null = no probe; see below for the object form
  build:
    dockerfile: "Dockerfile"    # relative to app_path
    context: "."                # relative to app_path

resources:
  memory: null                  # e.g. "512m", "1g" -- passed as `docker run --memory`
  cpu: null                     # e.g. 0.5 -- passed as `docker run --cpus`

environment:
  KEY: value                    # merged with apps/<app>/.env (if present); .env wins on conflict

database:
  enabled: false
  type: postgresql              # postgresql | mysql | mongodb
  version: "13"
  name: null                    # defaults to "<app_name>_db"

https:
  enabled: false
  redirect_http: true           # if enabled, plain HTTP redirects to HTTPS
```

**`health_check` object form**, when present:
```yaml
health_check:
  path: "/health"     # default "/health"
  interval: "30s"      # default "30s" -- accepts ms|s|m|h suffixes (runner.parse_duration)
  timeout: "10s"        # default "10s"
  retries: 3             # default 3
```
This is passed almost verbatim to `docker run --health-cmd/--health-interval/
--health-timeout/--health-retries` (`runner.create_container`), with `--health-cmd` built
as a one-line `urllib.request.urlopen` call against `http://localhost:<port><path>`
(`runner.build_health_command`) — chosen specifically because `curl` isn't present in
most slim base images.

`.env` files (`KEY=value` per line, `#` comments, blank lines ignored) are merged into
`environment` by `LaunchboxConfig.get_environment_vars()` — **only when `app_path` is
set**; a config reconstructed via `from_mapping` (i.e. every rollback/env-update/
database-change replay) never touches a `.env` file, because there's no directory to
read one from and no guarantee the process's CWD contains a relevant one.

---

## 7. The deployment lifecycle, end to end

```
git push launchbox main
        │
        ▼
repos/<app>.git/hooks/post-receive   (bash, generated by init.render_hook)
        │  filters: only refs/heads/<default_branch>
        │  checks out the pushed commit into a `mktemp -d` worktree
        ▼
python -m launchbox deploy <app> --source <worktree> --commit <sha>
        │
        ▼
deploy.deploy(app_name, source_dir, commit_sha)          [deploy.py]
    1. validate_app_name
    2. store.record_start(..., kind="deploy")             → deployments row, status=in_progress
    3. LaunchboxConfig(build_source)                       → reads launchbox.yaml + .env
    4. builder.build(...)                                  → docker build, tagged by commit SHA
    5. _collect_environment(...)                            → config env + database env (provisions
                                                                the DB container here if enabled)
    6. store.record_config(...)                             → persists resolved config_json/env_json
    7. runner.image_id(image_ref)                            → resolve the immutable image ID NOW,
                                                                 while the tag still points at it
    8. _find_previous_container(...)                         → what's currently serving, if anything
    9. _run_promotion(... _promote(...) ...)                  → see §8
```

The worktree Git checked the commit into is deleted (`rm -rf "$WORKDIR"`, via a `trap`)
immediately after `deploy` returns — **this is why `apps/<app_name>/` never exists for a
git-push-only application**: the build source was always temporary. Everything that
matters about *this* deployment (its config, its resolved environment, its image ID) has
already been written to the `deployments` row by step 6–7, before the worktree vanishes.

---

## 8. The health-gated swap (`_promote`)

`deploy._promote()` is the single routine every mutating action funnels through. Nine
steps, in this exact order:

1. **Clear the name.** If a stale container already occupies the target container name
   (only possible if a previous attempt died between creation and cleanup), remove it —
   unless it *is* the container being reused as `previous_container`.
2. **Start the new container, unreachable.** `runner.create_container(...)` — no Traefik
   label, no route file. The container exists; nothing can reach it yet.
3. **Wait for health.** `runner.wait_for_health(...)` — polls Docker's own
   `HEALTHCHECK` status up to a timeout derived from the probe's own
   `interval × retries + timeout + 15s grace` (or the weaker running-after-settle
   fallback if no probe is configured).
4. **Branch: unhealthy.** Capture logs (`runner.container_logs`), remove the new
   container, `store.record_failure(...)`, raise `DeploymentError`. **The previous
   container was never touched** — the app keeps serving its last good version. This is
   the whole point: a failed deploy fails loudly with zero downtime.
5. **Branch: healthy → write the route.** `router.write_route(...)` — atomic temp-file +
   `os.replace()`.
6. **Wait for route propagation.** `_await_route_propagation()` sleeps
   `ROUTE_PROPAGATION_SECONDS` (default 3s, env-overridable). Traefik's file provider
   debounces reloads (`providersThrottleDuration`, default 2s); skipping this step
   produced reproducible 502s during development, because the old container could be
   destroyed before Traefik had actually reloaded the rewritten route.
7. **Retire the old container.** Only now — after traffic has somewhere new to land —
   is `previous_container` stopped and removed.
8. **Reap strays.** `_reap_stray_containers(app_name, keep=container_name)` — removes any
   other containers labelled for this app (orphans from earlier interrupted attempts),
   filtered on **both** `launchbox.app` and `launchbox.service=app` so a sibling database
   container is never touched.
9. **Record success.** `store.record_success(...)`, reset restart-attempt counter, clear
   `marked_failed`.

**What this deliberately is not**: a true blue-green deploy with instant traffic-shifting
or percentage-based canary rollout. It's a sequential create → verify → swap, which is
the right amount of machinery for one container per app on one host, not for a fleet.

### Why `image_id`, not `image_tag`, drives rollback
`builder.build()` tags every image with the commit SHA **and** moves `:latest` to point
at it. A rollback needs to run a **past** image without rebuilding — but if it resolved
`launchbox-<app>:latest`, that tag has been re-pointed by every build since, landing on a
*newer* image than the one being rolled back to (reported as a successful rollback that
actually deployed the thing being escaped). `deploy._resolve_rollback_image()` therefore
prefers the recorded immutable `image_id` (a `sha256:...` digest that cannot move) and
**refuses outright** to resolve a stored `image_tag` ending in `:latest` with no
`image_id` — that row can only be rolled back to by redeploying the commit.

---

## 9. Other lifecycles: rollback, env update, database, remove

All four share `_replayed_config`/`_replayed_environment` and go through the same
`_promote` swap as a forward deploy.

### `rollback(app_name, commit_sha=None)`
Finds the target deployment row (`find_successful_deployment_by_sha` if `commit_sha`
given, else `last_successful_deployment`), resolves its image
(`_resolve_rollback_image`), **reconstructs its exact configuration**
(`_replayed_config` → `LaunchboxConfig.from_mapping(json.loads(row.config_json))` —
refuses rows that predate config capture, since falling back to reading
`apps/<app>/launchbox.yaml` silently produces defaults for a git-push app), and
**reconstructs its environment** (`_replayed_environment` — strips the seven
`RESERVED_ENV_KEYS` from the stored snapshot and re-provisions database credentials
live, since a stored DB password could be stale). Then runs `_promote` exactly as
`deploy()` does. `kind="rollback"`.

### `update_env(app_name, set_vars=None, unset_vars=None)`
Rejects any key in `RESERVED_ENV_KEYS` outright (`ValueError`). Otherwise: replays the
**current** deployment's config/env (not a past one), applies the requested
sets/unsets on top, and redeploys the **current image** — no rebuild. `kind="env"`.
Nothing is written to `launchbox.yaml`/`.env`; the next real deploy supersedes this
entirely.

### `configure_database(app_name, engine, version=None, db_name=None)` / `detach_database(app_name)`
Both go through `_redeploy_with_database_change`, which replays the current
configuration, calls a `mutate_config(config)` closure to flip the `database` section
(`enabled: true` + engine/version/name, or `enabled: false`), then redeploys — which is
what actually triggers `DatabaseManager.create_database_for_app` to run (provisioning
happens as a side effect of `_collect_environment`/`_database_environment` during the
swap, not as a separate step). `detach_database` additionally calls
`store.delete_database(app_name)` — **but only after the redeploy has actually
succeeded** (a real bug this fixed: doing it unconditionally left the dashboard showing
a stale "Detach" panel after a failed detach). The database container and its data are
**never destroyed** by detach; only the credential injection stops. Both `kind="database"`.

### `remove_app(app_name, remove_database=False)`
Order matters and is the point: **route file first** (nothing gets routed at a container
about to disappear), then every container Launchbox can find for this app (state-store
record, label lookup, and the pre-swap bare-app-name fallback — deduplicated), then this
app's images (all `launchbox-<app>:*` tags), then — only if `remove_database=True` — the
database container and its state row, then finally `store.forget_app(app_name)`. Doing
these steps in any other order either leaves Traefik routing to a deleted backend, or
leaves the app listed with nothing behind it. **Databases are kept by default** —
removing an app should not silently destroy its data.

---

## 10. Database credential model

Three-case resolution in `DatabaseManager._resolve_credentials`, evaluated on **every**
deploy that has `database.enabled: true` (provisioning is meant to be idempotent):

1. **Already recorded for this exact container** (`databases.app_name` row matches
   engine + container name, and has a password) → reuse it verbatim.
2. **No record, but the container already exists** → it predates per-app credentials.
   **Its real password is `launchbox123`** (`LEGACY_PASSWORD`), fixed into its data
   directory the first time it booted — passing a different `POSTGRES_PASSWORD`/etc. now
   would do nothing to the actual database. Adopt the legacy pair and record it.
3. **Nothing exists yet** → generate a fresh `secrets.token_urlsafe(24)` password.

This exists because of one hard constraint from the official Postgres/MySQL/MongoDB
Docker images: **credential-bootstrap environment variables are only honored on a
container's first boot**, against an empty data directory. There is no way to rotate a
password by changing an env var on a container that has already initialized — you'd have
to destroy its volume and re-bootstrap it, losing the data.

The username (`launchbox`) is shared across every application deliberately — each app
already has its own database *container*, so a shared username grants no cross-app
access; the actual isolation boundary is the per-app password plus the fact that each
database lives in its own container. A per-app *username* was also rejected because
MySQL caps usernames at 32 characters.

Credentials are surfaced to the dashboard **on demand only**
(`GET /api/apps/<app>/database/credentials`) — never rendered into the page itself, so a
password never ends up in page source, browser cache, or a screenshot before someone
explicitly clicks reveal.

---

## 11. Networking & routing

All app and database containers join one Docker network: `traefik_default`
(`runner.NETWORK`, created if absent by `runner.ensure_network()`; also the network name
in `docker-compose.yml`'s `networks.traefik`). Containers address each other by container
name over this network (e.g. `postgresql://launchbox:...@myapp_postgres:5432/myapp_db`).

**Traefik is configured with two providers simultaneously**:
- `providers.docker` (`exposedByDefault: false`) — for the Traefik container's own
  dashboard route (labelled directly in `docker-compose.yml`), not for app containers.
- `providers.file` (watching `traefik/dynamic/`) — **this is what routes every deployed
  app.** `router.py` writes one `<app>.yml` per app here.

**Why file-based routing instead of labelling app containers directly** (the far more
common pattern): a label takes effect the instant the labelled container exists — there
is no way to create a container, verify it's healthy, and *only then* expose it, because
the label already exposed it. Writing the route as a separate file lets `_promote`
sequence "container exists" and "container receives traffic" as two distinct, ordered
steps. This is the mechanism that makes the health gate in [§8](#8-the-health-gated-swap-_promote)
possible at all.

**HTTPS**: if `https.enabled`, `router.build_route_document` adds a second router on the
`websecure` entrypoint with `tls: {}`, and (if `redirect_http`) a `redirectScheme`
middleware on the plain-HTTP router. The actual certificate material comes from
`ssl_manager.py` (mkcert-generated, referenced from `traefik/dynamic.yml`) — this half is
less automated than the rest of the pipeline; provisioning a cert for a new HTTPS app is
currently a manual `SSLManager.get_certificate_for_app()` call, not wired into `deploy()`.

---

## 12. Health supervision

`supervisor.py` is a **separate, manually-started process** (`python -m launchbox
supervisor`) — it is not spawned automatically by the dashboard or by a deploy. It polls
every app in `state.apps` once per interval (default 30s) and, per app:

- `healthy` / no probe configured (`none`) → mark healthy, reset restart counter.
- `starting` → leave alone.
- `missing` (container gone) → log a warning; **does not restart or mark failed** — this
  is a state a human needs to look at (the container may have been removed on purpose).
- `unhealthy` → `docker restart`, increment the counter. At `MAX_RESTART_ATTEMPTS`
  (default 3, `LAUNCHBOX_MAX_RESTARTS`) → **mark the app failed and stop touching it**,
  rather than restarting forever. This is the platform's only defense against a
  crash-loop consuming the host indefinitely. An app already `marked_failed` is skipped
  on every subsequent pass until a human redeploys it (which clears the flag via
  `_promote`'s `store.clear_failed()`).

---

## 13. CLI reference

`python -m launchbox <command> [options]` (`__main__.py`). All commands print to
stdout/stderr and return a process exit code; `LaunchboxError`/`ValueError` become a
one-line logged error + exit code 1, not a traceback.

| Command | Args | What it does |
|---|---|---|
| `init <app_name>` | `--branch main` | Register an app: bare repo + git hook (`init.init`) |
| `build <app_name>` | `--source`, `--tag` | Build an image without deploying (`builder.build`) |
| `deploy <app_name>` | `--source`, `--commit` | Full deploy pipeline (`deploy.deploy`) |
| `rollback <app_name>` | `--commit` (default: last good) | `deploy.rollback` |
| `env <app_name>` | `--set KEY=VALUE` (repeatable), `--unset KEY` (repeatable) | `deploy.update_env` |
| `db <app_name>` | `--engine {postgresql,mysql,mongodb} \| --detach` (mutually exclusive, required), `--version`, `--name` | `deploy.configure_database` / `deploy.detach_database` |
| `remove <app_name>` | `--with-database` | `deploy.remove_app`; prints a summary line |
| `history <app_name>` | `--limit 20` | Tabular deployment history from the state store |
| `list` | | Every known app, its current container, health, restart count |
| `logs <app_name>` | `--tail 200` | `runner.container_logs`, resolving the container name via the state store (falling back to the bare app name) |
| `dashboard` | | Starts the Flask dashboard (blocking) |
| `supervisor` | `--interval` | Starts the health-poll loop (blocking) |

---

## 14. Dashboard — HTTP API reference

All under `app = Flask(__name__)` in `dashboard.py`. JSON in/out except the two HTML page
routes. Error responses are `{"error": "<message>"}` with a 4xx/5xx status.

| Method & path | Purpose | Notes |
|---|---|---|
| `GET /` | Renders `dashboard.html` | app list + Docker info + recent activity (last 12 across all apps) |
| `GET /app/<app_name>` | Renders `app_detail.html` | 404s (via flash + redirect) if the app is unknown to both `apps/` and the state store |
| `GET /api/apps` | `jsonify(get_app_list())` | Polled every 5s by the dashboard's auto-refresh |
| `POST /api/apps` | `{name, branch}` → registers a new app | Wraps `init.init`; returns `push_commands` (the exact `git remote add`/`git push` lines) |
| `POST /api/apps/<app>/deploy` | Triggers `deploy.deploy(app)` | |
| `POST /api/apps/<app>/rollback` | `{commit_sha?}` → `deploy.rollback` | |
| `POST /api/apps/<app>/env` | `{set: {...}, unset: [...]}` → `deploy.update_env` | |
| `POST /api/apps/<app>/database` | `{engine, version?, name?}` → `deploy.configure_database` | 201 on success |
| `GET /api/apps/<app>/database/credentials` | Returns password + full `DATABASE_URL` + the complete injected `env` dict | Fetched **on demand only** — see [§10](#10-database-credential-model) |
| `DELETE /api/apps/<app>/database` | `deploy.detach_database` | Does not destroy the database |
| `POST /api/apps/<app>/stop` / `/start` | Direct `container.stop()`/`.start()` via the Docker SDK | Not routed through `_promote` — these don't change *what* is running, just whether it's running |
| `DELETE /api/apps/<app>/remove` | `deploy.remove_app` | Returns the removal summary |
| `GET /api/apps/<app>/logs` | Last 100 lines via the Docker SDK | |

### `get_app_list()` — how the app list is actually assembled
Unions two sources: iterating `apps/` on disk (catches Dockerfile-based apps with a local
directory) **and** `store.list_apps()` (catches every app ever registered or deployed,
directory or not — this is the only way a git-push-only app is discoverable at all). For
each app, layers on: container status/health via the Docker SDK
(`_apply_container_info`, using `resolve_container_name` — which reads the state store's
`current_container`, because containers are named `<app>-<sha7>`, not `<app>`, and a
lookup by the bare name found nothing), state-row fields (`_apply_state_row` — health
state, restart count, and — reading through to the current deployment row — when it was
last deployed and on what commit), and `has_database` (a `store.get_database()` lookup
per app, added to drive the homepage's stat strip).

---

## 15. Dashboard — frontend

Three templates, Tailwind via CDN (`darkMode: 'class'`), Font Awesome icons, no build
step.

- **`base.html`** — nav shell, dark-mode pre-paint script (avoids flash-of-wrong-theme by
  reading `localStorage` before first paint), shared JS: `showToast`, `showLoading`/
  `hideLoading` (button spinner state), `relativeTime(isoString)` (client-side
  "3m ago" formatting — carefully handles both bare-`Z` and explicit-`+00:00` timezone
  suffixes, since Python's `isoformat()` produces the latter and a naive
  `.endsWith('Z')` check missed it), `copyText`, and the shared `openModal`/`closeModal`
  pair (scale+fade transition, used by every modal in the app).
- **`dashboard.html`** — a dense, table-based app list (status/name/URL/last-deploy/
  commit/restarts/actions as columns — deliberately not a card-per-app layout, to keep
  many apps scannable at once), a compact stat strip (apps/running/unhealthy/restarting/
  databases), a recent-activity table (labelled by `kind`: 🚀 deploy / ⟲ rollback / ⚙ env
  update / 🗄 database), and the "New application"/"Logs" modals. Polls `/api/apps` every
  5s and reloads the page only if a fingerprint (name+status+health+container_id+
  marked_failed per app) actually changed — never while a modal is open.
- **`app_detail.html`** — four tabs (Overview / Environment / Database / Deployments),
  active tab persisted in the URL hash so a reload (which every mutating action here
  triggers) returns to where you were. The Environment tab separates
  **database-managed** variables (read-only, reveal-on-demand) from **custom** variables
  (freely editable, redeployed via `saveEnv` → `POST .../env`). The Database tab shows
  connection details with the password/connection-string redacted until revealed. The
  Deployments tab is a real history table with a "Roll back" action on every successful
  row except the currently-live one.

---

## 16. Testing

```
.venv/bin/pytest -q
```

400 tests across 12 files (~5,700 lines), one file roughly per module
(`test_builder.py`, `test_config.py`, `test_dashboard.py`, `test_database_manager.py`,
`test_deploy.py`, `test_env_update.py`, `test_hook.py` [tests `init.py`],
`test_rollback.py`, `test_router.py`, `test_runner.py`, `test_scaffolding.py` [basic
import sanity], `test_state.py`, `test_supervisor.py`).

**Docker is never actually invoked in the default run.** Every test that would touch the
daemon mocks it out — either via `pytest-mock`'s `mocker` fixture patching
`subprocess.run`/`docker.from_env`, or by constructing a manager with
`DatabaseManager.__new__(DatabaseManager)` and assigning a mock `.client` directly. A few
integration-style tests are marked `@pytest.mark.docker` and are auto-skipped when no
daemon is reachable (`conftest.py`'s `pytest_runtest_setup`, which shells out to
`docker info`).

Key `conftest.py` fixtures:
- `_no_route_propagation_wait` (autouse) — sets `LAUNCHBOX_ROUTE_PROPAGATION=0` for the
  whole suite, so tests don't pay `deploy.py`'s real 3-second route-propagation sleep.
- `tmp_state_db` / `tmp_dynamic_dir` — isolated temp paths so tests never touch the real
  `state/launchbox.db` or `traefik/dynamic/`.

`pytest.ini` treats any `DeprecationWarning` from `launchbox.*` as an error
(`filterwarnings = error::DeprecationWarning:launchbox.*`) — deprecated stdlib/dependency
usage inside this package fails the suite rather than silently accumulating.

---

## 17. Known limitations & security notes

Named here deliberately, not discovered by accident — treat this section as current, not
aspirational.

- **Secrets are stored in plaintext.** `deployments.config_json`/`env_json` and
  `databases.password` are unencrypted columns in `state/launchbox.db`. A resolved
  database password ends up in `env_json` on every deployment row. Unresolved; the
  honest fix is encrypting at rest or excluding secret keys before persisting.
- **`state/` permissions drift.** `setup.sh` `chmod 700`s it, but `StateStore.__init__`
  creates the directory at runtime via plain `os.makedirs`, under whatever the process
  umask happens to be — in practice looser than intended if the directory didn't already
  exist when `setup.sh` ran.
- **No database volumes.** `volume_name` is always recorded empty — a database
  container's data does not survive `docker rm` (or `launchbox remove --with-database`,
  by design, but also not a plain container crash-and-recreate outside Launchbox's
  control).
- **Single SQLite writer.** Fine for one host; would not survive multiple Launchbox
  instances sharing state, which was never a goal.
- **Dashboard runs on Flask's dev server** (`debug=True`) — not hardened for anything
  beyond local/demo use; a real deployment needs a production WSGI server in front.
- **Traefik's API/dashboard is insecure by default** (`--api.insecure=true` in
  `docker-compose.yml`) — acceptable only because it's bound to localhost by convention
  here.
- **Two independent Docker access paths.** `runner.py`/`router.py`/`builder.py` shell out
  to the `docker` CLI via `subprocess`; `dashboard.py`/`database_manager.py` use the
  `docker` Python SDK (`docker.from_env()`). Both reach the same daemon, but this means
  Docker-availability handling is duplicated and can drift.
- **No horizontal scaling / replicas.** One container per app, by design — an explicit
  roadmap item, not an oversight.
- **Single Docker host only.** No remote/multi-server deployment target.
- **Weaker health fallback without a configured probe** — "still running after a 5s
  settle" catches an app that crashes immediately, not one that starts fine but is
  actually broken.
- **HTTPS provisioning is a manual step.** `ssl_manager.SSLManager.get_certificate_for_app`
  is not called automatically by `deploy()` when `https.enabled: true` — a cert has to
  already exist (or be created out-of-band) for the `websecure` route to actually work.

---

## 18. Extension points

- **Add a config option** → `config_parser.LaunchboxConfig._default_config()` (add the
  default) + a getter method. Remember: anything read here must also work when the
  instance came from `from_mapping` (no filesystem), so don't read from disk inside a
  getter — do it in `_load_config`/constructor only.
- **Add a new deployment "kind"** (beyond deploy/rollback/env/database) → add the string
  literal wherever `store.record_start(..., kind=...)` is called for that path, and add
  a branch in both `dashboard.html`'s activity feed and `app_detail.html`'s deployments
  table (`{% if dep.kind == '...' %}`) to label it.
- **Add a new database engine** → `deploy.DATABASE_ENGINES` (default version) +
  `database_manager._create_<engine>`/`_get_<engine>_connection_info`/
  `_wait_for_<engine>` following the existing three engines' shape, + the container-name
  convention `f"{app_name}_{engine_short}"`, + `runner.find_app_containers`'s label
  filter already generalizes (no engine-specific change needed there).
- **Add a new dashboard-triggered mutation that changes running config** → follow
  `configure_database`'s shape: write a `mutate(config)` closure and call
  `_redeploy_with_database_change` (rename it if it stops being database-specific) —
  don't hand-roll a new redeploy path; every existing one funnels through `_promote` for
  the health gate.
- **Add a new CLI subcommand** → `__main__.build_parser()` (the `argparse` subparser) +
  a branch in `__main__.main()`. Keep it a thin wrapper over an existing `deploy.py`/
  other-module function — don't put orchestration logic in `__main__.py` itself.
- **Add a new dashboard API endpoint** → follow the existing pattern: thin Flask route,
  delegate immediately to a `deploy.py`/`state.py` function, catch `ValueError` → 400,
  `LaunchboxError` → 500 (with the message), bare `Exception` → log + 500.

---

## 19. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `localhost:8000` refuses to connect | The dashboard's Flask process isn't running — it does **not** start automatically with `docker compose up` (that only starts Traefik) | `python -m launchbox dashboard`; check `ps aux \| grep "launchbox dashboard"` |
| App shows "not deployed" right after a successful-looking push | The hook ran against the wrong branch, or the deploy failed after the hook printed success but before the health probe passed | `logs/deploy.log`; `launchbox history <app>` |
| Traefik logs `client version 1.24 is too old` | Docker Engine ≥29 rejects Traefik <2.11's hardcoded old API version; `DOCKER_API_VERSION` env var does **not** fix this on the old image | Bump `image: traefik:v2.11` (or newer) in `docker-compose.yml` |
| 502 immediately after a deploy | `ROUTE_PROPAGATION_SECONDS` too short for this Traefik's actual `providersThrottleDuration`, or route-propagation wait was disabled | `LAUNCHBOX_ROUTE_PROPAGATION` env var |
| Dashboard "Database" panel shows the wrong/old password | Container predates per-app credentials — it's on the legacy shared password (`launchbox123`) and will stay there until its data volume is destroyed and recreated | [§10](#10-database-credential-model) |
| A container keeps restarting and then stops entirely | Supervisor's crash-loop ceiling (`marked_failed`) kicked in after 3 failed restarts | `launchbox history <app>`, container logs, then redeploy after fixing the underlying issue — a redeploy's successful `_promote` clears `marked_failed` |
| `git push` hangs or errors with a shell-looking message | The generated hook failed `set -euo pipefail` partway through — check that Python/venv is actually at the path the hook expects | `repos/<app>.git/hooks/post-receive`; re-run `launchbox init <app>` to regenerate it |
