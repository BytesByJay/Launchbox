# Launchbox Completion — Design Spec

**Date:** 2026-08-07
**Status:** Approved, ready for planning
**Context:** Closes the gaps recorded in Chapter 4.12 / Table 5.2 / Section 6.3 of the
submitted dissertation, implementing the future work of Section 6.4.

---

## 1. Purpose

The delivered Launchbox package implements a working git-push-to-deploy pipeline but
omits every safety mechanism the Chapter 3 design specified. The dissertation records
this honestly. This spec defines the work that closes those gaps so the code matches
the design it was written against.

In scope (from Section 6.4):

| Ref | Item |
|-----|------|
| 6.4.1 | Health-gated deployment swap with rollback on probe failure |
| 6.4.2 | Persistent deployment history, commit-versioned images, rollback command |
| 6.4.3 | Restart supervision with bounded retries |
| 6.4-1 | Per-application generated database credentials |
| 6.4-2 | Verification of the MySQL, MongoDB and HTTPS paths |
| 6.4-4 | Automated pytest suite replacing manual verification |
| 6.4-5 | Dashboard hardening: auth, debug mode off, loopback bind |

Out of scope, deliberately:

- 6.4-6 buildpack-style detection for applications without a Dockerfile
- 6.4-7 public deployment mode (real domain + public CA in place of `*.localhost`)
- 6.4-3 validation of `deploy-production.sh` against a real remote server

Two defects found during design review are also in scope, because they block items
above and because the dissertation claims behaviour the code does not deliver:

- **Database containers mount no volume.** Section 1.4 of the report states data is
  "persisted on a named Docker volume". No volume is created, so removing a database
  container destroys its data.
- **`_wait_for_mongodb` runs `mongo --eval`.** The `mongo` shell was removed from the
  official MongoDB image at 6.0. The probe can only time out, which is the most likely
  reason the MongoDB path was never successfully exercised.

A third, smaller defect: `dashboard.py` renders `app_detail.html`, which does not exist
in `launchbox/templates/`. `GET /app/<name>` returns HTTP 500 today.

## 2. Environment constraints

Verified on the development host, 2026-08-07:

- Docker 29.4.2 and Compose v5.1.3 present and running.
- Ports **8000 and 8080 are occupied** by unrelated processes. Traefik's dashboard moves
  to 8090; the Launchbox dashboard moves to 8100. Both configurable by environment
  variable. Ports 80 and 443 are free.
- `mkcert` is **not installed**. `setup.sh` gains an install step, required to verify
  the HTTPS path.
- Python 3.12.3. Project dependencies are not installed globally; work proceeds in a
  virtualenv.

## 3. Architecture

### 3.1 The routing change

Docker labels are immutable after container creation. Because `runner.py` attaches the
Traefik route as a label on the application container, a container is either routable
from the moment it exists or never routable. A health gate is therefore impossible
without changing how routes are registered.

Routing moves to **Traefik's file provider**, which is what Chapter 3 (Listing 3.4)
specified and Chapter 4.8 records as not implemented. `docker-compose.yml` already
enables it (`--providers.file.directory`, `--providers.file.watch=true`).

Consequences:

- The compose mount changes from `./traefik:/etc/traefik/dynamic:ro` to
  `./traefik/dynamic:/etc/traefik/dynamic:ro`. `traefik/traefik.yml` is a *static*
  config file that currently sits inside the watched directory; the file provider
  would attempt to parse it as dynamic configuration and log errors.
- Traefik's Docker provider remains enabled, for Traefik's own dashboard router.
- Application containers are named `<app>-<short-sha>` rather than `<app>`, so the
  outgoing and incoming versions coexist during the probe. Traefik reaches the backend
  by container name over Docker's embedded DNS on `traefik_default`.
- Route files are written to `traefik/dynamic/<app>.yml` by temp-file-plus-rename, so
  Traefik never observes a partially written document.

### 3.2 New modules

| Module | Responsibility |
|--------|----------------|
| `launchbox/deploy.py` | Deployment orchestrator. Owns step ordering, the health gate, promotion, rollback on failure, and history recording. Calls `builder` and `runner` as libraries. |
| `launchbox/state.py` | SQLite metadata store at `state/launchbox.db`. Deployment history, current-container pointers, per-app database credentials. |
| `launchbox/supervisor.py` | Background health poller. Restarts unhealthy containers within a bounded retry count, then marks the application failed. |
| `launchbox/router.py` | Writes and removes Traefik dynamic route files, including the TLS router when HTTPS is enabled. |
| `launchbox/__main__.py` | Unified CLI entry point. |

### 3.3 Modified modules

| Module | Change |
|--------|--------|
| `init.py` | Hook reads `old new ref` from stdin, filters to the default branch, checks the pushed commit into a temp worktree, invokes `deploy.py`, cleans up. `set -e`. Replaces `os.system` with `subprocess.run`. |
| `builder.py` | Accepts an explicit source directory (the temp worktree) and a commit SHA. Tags `launchbox-<app>:<short-sha>` and moves `launchbox-<app>:latest`. |
| `runner.py` | Becomes container lifecycle primitives used by `deploy.py`: create-without-route, probe health, promote, stop-and-remove. Adds `--restart unless-stopped`. No longer removes the old container unconditionally. |
| `database_manager.py` | Per-app generated credentials read from / written to `state.py`. Named volumes for data persistence. `mongosh` readiness probe with `mongo` fallback. |
| `dashboard.py` | Basic auth, `debug=False`, generated secret key, loopback bind, deployment history and rollback UI, health column, supervisor status. |
| `docker-compose.yml` | Dynamic-config mount path; Traefik dashboard port 8090. |
| `setup.sh` | Creates `state/` and `traefik/dynamic/`, installs `mkcert`, installs test dependencies. |
| `requirements.txt` | Adds `pytest`, `pytest-mock`. Drops `pathlib` (a stdlib module since 3.4; the PyPI package is a dead backport). |

## 4. Core algorithm — health-gated deployment

Implemented in `deploy.py`. This is Listing 3.3 of the dissertation, made real.

```
FUNCTION deploy(app_name, source_dir, commit_sha):
    config       <- LaunchboxConfig(source_dir)
    short        <- commit_sha[:7]
    deployment   <- state.record_start(app_name, commit_sha)

    TRY:
        image <- builder.build(app_name, source_dir, config, tag=short)
            # failure here raises; old container never touched

        IF config.database.enabled:
            db_env <- database_manager.provision(app_name, config)
            # idempotent; reuses stored credentials and existing volume
        ELSE:
            db_env <- {}

        old <- state.current_container(app_name)      # may be legacy name <app>
        new <- runner.create(
                   name    = app_name + '-' + short,
                   image   = image,
                   env     = merge(config.env, dotenv, db_env),
                   network = 'traefik_default',
                   limits  = config.resources,
                   health  = config.health_check,
                   restart = 'unless-stopped')
            # NO route registered at this point

        healthy <- runner.wait_for_health(new, config.health_check, timeout)

        IF NOT healthy:
            state.record_logs(deployment, runner.logs(new))
            runner.stop_and_remove(new)
            state.record_result(deployment, FAILED, 'health probe failed')
            RAISE DeploymentError    # old container still serving, route unchanged

        router.write_route(app_name, new, config)     # atomic; Traefik hot-reloads
        IF old IS NOT NULL:
            runner.stop_and_remove(old)
        state.record_success(deployment, container=new, image=image)

    CATCH error:
        state.record_result(deployment, FAILED, error)
        RAISE
```

**Health probe.** Reads `.State.Health.Status` from `docker inspect`, polling with short
exponential back-off up to a bounded timeout derived from
`interval x retries + timeout + grace`. Applications that declare no `health_check` fall
back to a weaker gate: the container must be running and must still be running after a
settle period. This is weaker than a real probe but strictly stronger than the current
behaviour, which verifies nothing.

**Legacy migration.** If no state row exists for an app but a container named exactly
`<app>` is present, it is adopted as `old` and removed after a successful promotion. The
first deploy after this change migrates cleanly with no manual step.

## 5. State store

SQLite, file `state/launchbox.db`, created on first use. Chosen for requiring no server
process and being backed up by copying one file, per Section 6.4.2 of the report.

```sql
CREATE TABLE deployments (
    id            INTEGER PRIMARY KEY,
    app_name      TEXT    NOT NULL,
    commit_sha    TEXT,
    image_tag     TEXT,
    container_name TEXT,
    status        TEXT    NOT NULL,   -- in_progress | success | failed
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    error         TEXT,
    logs          TEXT
);

CREATE TABLE apps (
    app_name           TEXT PRIMARY KEY,
    current_container  TEXT,
    current_image      TEXT,
    current_deployment INTEGER REFERENCES deployments(id),
    health_state       TEXT,          -- healthy | unhealthy | failed | unknown
    restart_attempts   INTEGER NOT NULL DEFAULT 0,
    marked_failed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE databases (
    app_name       TEXT PRIMARY KEY,
    engine         TEXT NOT NULL,
    container_name TEXT NOT NULL,
    volume_name    TEXT NOT NULL,
    db_name        TEXT NOT NULL,
    username       TEXT NOT NULL,
    password       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
```

Query supporting rollback: the most recent `deployments` row for an app with
`status = 'success'` whose `id` differs from `apps.current_deployment`.

## 6. Rollback

`python3 -m launchbox rollback <app> [<sha>]`

Resolves the target deployment (explicit SHA, or the last successful one that is not
current), asserts its image still exists locally, and re-runs the promotion path of
Section 4 from the container-creation step. No rebuild occurs. If the rolled-back
container fails its own health probe, the currently running container is left in place
and the rollback reports failure.

## 7. Database provisioning

**Credentials.** Username `lb_<app_name>`, password `secrets.token_urlsafe(24)`,
generated once and persisted in `databases`. Every later deployment reads the stored
pair, so provisioning stays idempotent and the application's connection string is
stable.

**Backwards compatibility.** If a database container for an app exists but has no row in
`databases`, the legacy fixed pair (`launchbox` / `launchbox123`, root `rootpass123`) is
recorded for that app rather than rotated. Existing deployments keep working; only newly
provisioned databases get generated secrets.

**Volumes.** Each engine gets a named volume `<app>_<engine>_data` mounted at the
engine's data directory (`/var/lib/postgresql/data`, `/var/lib/mysql`, `/data/db`).
This is the persistence Section 1.4 of the report claims and the code does not provide.

**Readiness.** PostgreSQL `pg_isready`; MySQL `mysqladmin ping`; MongoDB `mongosh
--eval 'db.runCommand("ping").ok'` falling back to `mongo --eval` for images older
than 6.0. Polled to a 60-second bound.

`POSTGRES_HOST_AUTH_METHOD=trust` is removed. It disables password authentication
entirely, which makes generated credentials pointless.

## 8. Restart supervision

`launchbox/supervisor.py`, runnable as a thread inside the dashboard process or
standalone via `python3 -m launchbox supervisor`.

```
LOOP every POLL_INTERVAL seconds:
    FOR EACH app WITH a current container IN state:
        IF state.marked_failed(app): CONTINUE
        status <- docker_health(container)

        IF status == healthy OR status == none:
            state.set_health(app, healthy); state.reset_restart_attempts(app)
        ELSE IF status == unhealthy:
            attempts <- state.restart_attempts(app)
            IF attempts < MAX_RESTART_ATTEMPTS:
                docker_restart(container)
                state.increment_restart_attempts(app)
            ELSE:
                state.mark_failed(app)          # surfaced on the dashboard
        ELSE IF container is gone:
            state.set_health(app, unknown)
```

`MAX_RESTART_ATTEMPTS` defaults to 3 and is configurable. Marking failed rather than
restarting indefinitely is the property that stops a crash loop consuming the host.

Containers additionally receive `--restart unless-stopped`, so Docker itself recovers a
container that exits, which the current code does not configure at all.

## 9. Reverse proxy and HTTPS

`router.py` writes `traefik/dynamic/<app>.yml`:

```yaml
http:
  routers:
    <app>:
      rule: "Host(`<app>.localhost`)"
      service: <app>-svc
      entryPoints: [web]
  services:
    <app>-svc:
      loadBalancer:
        servers:
          - url: "http://<app>-<sha>:<port>"
```

When `https.enabled` is true, a second router on `websecure` with `tls: {}` is added,
and when `redirect_http` is true the plain router gains a `redirectscheme` middleware.
Certificate material continues to be issued by `ssl_manager.py` via mkcert, unchanged in
mechanism, but its output path moves with the mount: it writes the TLS section to
`traefik/dynamic/tls.yml` rather than `traefik/dynamic.yml`. Certificates themselves stay
in `certs/`, mounted at `/certs`.

Route removal (on app removal) deletes the file; Traefik drops the route on the next
watch event.

## 10. Dashboard

**Hardening**
- `debug=False` unconditionally.
- Secret key generated once into `state/secret_key`, replacing the hardcoded literal.
- HTTP Basic auth on every route. Credentials from `LAUNCHBOX_USER` / `LAUNCHBOX_PASS`,
  or generated into `state/.launchbox_auth` on first run and printed once.
- Binds `127.0.0.1` by default; `LAUNCHBOX_BIND` overrides.
- Port 8100 by default, `LAUNCHBOX_PORT` overrides.

**New surface**
- Health column per application, sourced from `apps.health_state`.
- Deployment history table per application: SHA, image tag, status, timestamps, error.
- Rollback action per historical successful deployment.
- Supervisor status indicator, including applications marked failed.
- `POST /api/apps/<name>/deploy` routes through `deploy.py` so the dashboard and the git
  hook share one code path.
- The missing `app_detail.html` template is written.

## 11. Verifying the unverified paths

Two new sample applications, each with a `/health` endpoint and a `/db` endpoint that
performs a real query against its engine:

- `apps/mysql_app` — Flask + PyMySQL, `database.type: mysql`, version `8.0`.
- `apps/mongo_app` — Flask + pymongo, `database.type: mongodb`, version `7.0`.

`apps/test_app` gains `https.enabled: true` to exercise the mkcert path end to end.

Together these turn the three "Not exercised" rows of Table 5.1 into verified ones.

## 12. Testing

`tests/`, pytest with `pytest-mock`. Two tiers.

**Unit — no Docker daemon required**

- Config parser: defaults, deep merge, `.env` precedence over `launchbox.yaml`,
  malformed YAML handling.
- State store: schema creation, deployment lifecycle, current-container pointer,
  last-good-deployment query, credential storage and reuse.
- Credential generation: uniqueness, reuse on second call, legacy adoption path.
- Supervisor decision table: healthy resets attempts; unhealthy under the bound
  restarts; unhealthy over the bound marks failed and stops restarting.
- Deploy orchestrator against a faked Docker layer. The critical assertion: **when the
  health probe fails, the old container is not removed and the route file is not
  rewritten.**
- Router: generated YAML shape for HTTP-only, HTTPS, and HTTPS-with-redirect.
- Dashboard routes via Flask's test client, including that auth is enforced.

**Integration — `@pytest.mark.docker`, auto-skipped when no daemon is reachable**

- Build and deploy a minimal application; assert it is reachable through Traefik.
- Redeploy; assert the swap occurs and the old container is gone.
- Deploy a deliberately broken build; assert the previously running container is still
  running and still routed.
- Deploy an app whose health endpoint returns 500; assert the new container is removed
  and the old one still serves.
- Rollback; assert the earlier image is restored without a rebuild.

## 13. Error handling

The exception hierarchy in `logger.py` is retained. `deploy.py` catches every
`LaunchboxError` subclass, records the failure with its message into `deployments.error`,
and exits non-zero. The hook uses `set -e`, so a non-zero exit from `deploy.py` fails the
push visibly in the developer's terminal — which is the behaviour Section 4.11 records
as absent.

Failure semantics, stated as guarantees:

| Failure point | Outcome |
|---------------|---------|
| Non-default branch pushed | Nothing runs; push succeeds with a notice |
| Checkout fails | Nothing built; previous version untouched |
| Build fails | Previous container running and routed; deployment recorded FAILED |
| Database provisioning fails | Previous container running and routed; recorded FAILED |
| New container fails to start | Previous container running and routed; new container removed |
| Health probe fails | Previous container running and routed; new container removed and its logs captured |
| Promotion succeeds | Old container removed; deployment recorded SUCCESS |

## 14. Sequencing

Each stage leaves the repository working and is committed independently.

1. **State store** — `state.py` plus its unit tests. No behaviour change.
2. **Router** — `router.py`, compose mount change, `runner.py` stops emitting labels.
   Routing works exactly as before, through files.
3. **Orchestrator and health gate** — `deploy.py`, `builder.py` SHA tags, `runner.py`
   split into primitives, `init.py` hook rewrite. The core deliverable.
4. **Rollback** — history queries, `rollback` command, CLI entry point.
5. **Supervisor** — `supervisor.py`, restart policy on containers.
6. **Databases** — generated credentials, volumes, `mongosh` fix, legacy adoption.
7. **Dashboard** — hardening, history and rollback UI, `app_detail.html`.
8. **Sample apps and verification** — `mysql_app`, `mongo_app`, HTTPS on `test_app`,
   mkcert install in `setup.sh`, integration tests, end-to-end run.
9. **`IMPROVEMENTS.md`** — gap-by-gap record mapped onto Table 5.2 of the report.

## 15. Documentation deliverable

The dissertation is submitted and states these gaps exist. Rather than leave the code
contradicting the record, `IMPROVEMENTS.md` documents the post-submission work as a
table keyed to Table 5.2: limitation, what was implemented, how it was verified. This
presents the work as completed follow-through rather than as an undocumented divergence.

## 16. Success criteria

- Every row of Table 5.2 is either resolved or explicitly restated as out of scope.
- A failed build, and a failed health probe, each leave the previously running
  application serving traffic — demonstrable on demand.
- A rollback restores a previous version without rebuilding.
- An unhealthy container is restarted automatically, and a crash-looping one is marked
  failed rather than restarted forever.
- Every provisioned database has unique credentials and a persistent volume.
- MySQL, MongoDB and HTTPS are exercised by real sample applications.
- `pytest` passes; integration tests pass against the local Docker daemon.
