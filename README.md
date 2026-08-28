# options-m

A long-running Python service: several agent loops running concurrently
alongside an admin HTTP interface, in a single asyncio process, shutting down
cleanly on SIGTERM. No business logic yet — this is the base to build on.

## Requirements

- Python 3.12+
- Docker (optional)
- Postgres (optional locally; required in deployment)

## Project layout

```
options-m/
├── src/options_m/
│   ├── __main__.py          # entry point: starts agents + HTTP together
│   ├── agents.py            # agent protocol, supervision, retry/backoff
│   ├── api.py               # /health, /ready, admin dashboard
│   ├── config.py            # env-driven settings
│   ├── db.py                # Postgres connection pool
│   ├── healthcheck.py       # container probe (no dependencies)
│   ├── lifecycle.py         # signal handling, interruptible sleep
│   ├── logging_config.py    # logging setup (JSON / text)
│   └── server.py            # uvicorn wired to the shutdown signal
├── tests/
├── pyproject.toml           # deps, build, ruff, mypy, pytest config
├── Dockerfile
└── .env.example
```

Source lives under `src/` so tests always run against the installed package,
never against a stray copy on the current working directory.

## Architecture

One process, one event loop, two concerns:

- **Agents** (`agents.py`) — each agent runs its own independent loop. Every
  iteration is isolated: an exception is logged and retried with exponential
  backoff, never propagated. A failing agent cannot take down its siblings or
  the process, because a crash-restart cycle is far more expensive than a
  retry.
- **HTTP** (`api.py`, `server.py`) — the admin dashboard and health probes.

Both are started by `__main__.run()` in an `asyncio.TaskGroup` and share a
single `asyncio.Event` for shutdown. On SIGTERM the event is set, agents finish
their current iteration and stop, uvicorn drains in-flight requests, the
database pool closes, and the process exits 0.

Uvicorn's own signal handlers are deliberately disabled (`_ManagedServer`);
otherwise it would catch SIGTERM and stop only the HTTP server, leaving the
agent loops running until the platform force-killed them.

### Adding an agent

Implement the `Agent` protocol (a `name` property and an `async step()`), then
register it in `build_agents()`:

```python
class MyAgent:
    @property
    def name(self) -> str:
        return "my-agent"

    async def step(self) -> None:
        # One iteration. Raising is safe — it is logged and retried.
        await do_the_work()
```

Keep `step()` a single iteration — the supervisor owns the looping, pacing and
error handling.

## Getting started

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Run it:

```bash
cp .env.example .env
python -m options_m          # or: options-m
```

The admin dashboard is then on <http://localhost:8080>. `DATABASE_URL` may be
left unset locally — the app boots without a database and `/ready` reports it.

## Endpoints

| Path      | Purpose                                                        |
| --------- | -------------------------------------------------------------- |
| `/`       | Admin dashboard                                                  |
| `/health` | Liveness. Cheap, touches no dependencies. Drives container restarts |
| `/ready`  | Readiness. Checks Postgres; returns 503 when unreachable          |

`/health` must never check dependencies: if it did, a brief database blip would
make the platform kill an otherwise healthy process. That distinction is what
`/ready` is for.

## Development

```bash
ruff check .          # lint
ruff format .         # format
mypy                  # type check (strict)
pytest                # tests
pytest --cov          # tests with coverage
```

## Logging

Call `setup_logging()` **once**, at the process entry point. Everywhere else,
just grab a module logger:

```python
import logging

logger = logging.getLogger(__name__)
logger.info("order placed", extra={"order_id": 42, "symbol": "SPY"})
```

Anything passed via `extra=` becomes a top-level field in the JSON record:

```json
{"timestamp":"2026-08-29T10:00:00+00:00","level":"INFO","logger":"options_m.orders","message":"order placed","order_id":42,"symbol":"SPY"}
```

Guidelines:

- Never use `print()` — logs go to stdout through the logging handler so
  containers and log collectors pick them up unchanged.
- Prefer `extra={...}` over f-strings for variable data; it keeps records
  queryable.
- Use `logger.exception(...)` inside `except` blocks to capture the traceback.
- Never log secrets or personal data.

## Configuration

Every setting is read from the environment (see `.env.example`), so the same
image runs unchanged everywhere.

| Variable                      | Default   | Description                             |
| ----------------------------- | --------- | --------------------------------------- |
| `LOG_LEVEL`                   | `INFO`    | `DEBUG`, `INFO`, `WARNING`, `ERROR`     |
| `LOG_FORMAT`                  | `json`    | `json` for prod, `text` for local       |
| `HOST` / `PORT`               | `0.0.0.0` / `8080` | HTTP bind address              |
| `DATABASE_URL`                | unset     | Postgres DSN; unset disables the pool   |
| `DB_POOL_MIN_SIZE`            | `1`       | Idle connections held; `0` for Neon     |
| `DB_POOL_MAX_SIZE`            | `4`       | Pool ceiling                            |
| `DB_POOL_MAX_IDLE_SECONDS`    | `120`     | Idle connection recycle age             |
| `AGENT_INTERVAL_SECONDS`      | `30`      | Delay between successful iterations     |
| `AGENT_ERROR_BACKOFF_SECONDS` | `5`       | First retry delay after a failure       |
| `AGENT_MAX_BACKOFF_SECONDS`   | `300`     | Backoff ceiling                         |
| `SHUTDOWN_GRACE_SECONDS`      | `20`      | Time to drain requests after SIGTERM    |

## Docker

```bash
docker build -t options-m .
docker run --rm -p 8080:8080 -e LOG_FORMAT=text options-m
```

`-p 8080:8080` is required to reach the dashboard from the host. `EXPOSE` in
the Dockerfile only documents the port; it does not publish it. Without `-p`
the app runs fine but is only reachable inside the container — verify with
`docker exec <name> python -m options_m.healthcheck`.

To run against a local Postgres, pass the DSN (note `host.docker.internal`,
since `localhost` inside the container is the container itself):

```bash
docker run --rm -p 8080:8080 \
  -e DATABASE_URL=postgresql://user:pass@host.docker.internal:5432/options_m \
  options-m
```

The image is multi-stage (build deps stay out of the runtime layer), runs as an
unprivileged `app` user, and ships the dependencies in an isolated virtualenv at
`/opt/venv`. `ENTRYPOINT` uses exec form so python is PID 1 and receives
SIGTERM directly.

## Deploying to Render (free tier)

`render.yaml` deploys this as a **web service** — the agent loops run inside
the same process, and Render's free tier covers web services only, not
background workers.

1. **Create the database on [Neon](https://neon.com)**, not on Render: a free
   Render Postgres is deleted 30 days after creation. Neon's free plan does
   not expire and needs no card.
2. **Render → New → Blueprint**, point it at this repo. It reads `render.yaml`.
3. **Set `DATABASE_URL`** in the Render dashboard (it is `sync: false`, so it
   is never committed). Use Neon's *pooled* connection string.
4. **Add an external pinger** — see below. Without it the service sleeps.

### Keeping it awake

Render sleeps a free web service after **15 minutes without inbound HTTP
traffic**. Busy agent loops do not count — only requests do. So an external
monitor must hit `https://<your-service>.onrender.com/health` every 10 minutes
(UptimeRobot's free plan does this at 5-minute intervals).

Render's own cron jobs are not free, so the pinger has to live elsewhere.
Avoid GitHub Actions for it: scheduled workflows are delayed under load and
are disabled entirely after 60 days of repo inactivity.

Two consequences worth internalising:

- **The agent loops' uptime depends on that pinger.** If it stops, the service
  sleeps and the agents stop with it. It recovers on the next successful ping
  (~1 minute cold start), but the iterations in between simply never ran, and
  nothing alerts you. Monitor the pinger itself.
- **750 free instance hours per month are shared across the workspace.** A
  31-day month is 744 hours, so this must be the *only* always-on free
  service. A second one suspends both before the month ends.

### Neon's compute budget

Neon's free plan allows **100 compute-hours per month** and scales compute to
zero after 5 minutes of inactivity. An always-connected app never lets it
idle, which would exhaust the budget in about four days. `render.yaml`
therefore sets `DB_POOL_MIN_SIZE=0` and a 60-second agent interval — but the
budget still depends on how often your agents actually touch the database.
Batch writes rather than writing on every tick.

The pool is configured with `check=check_connection` for the same reason: when
Neon scales to zero it drops idle connections, and a stale pooled connection
would otherwise fail the next query.

## Adding dependencies

Add them to `[project].dependencies` in `pyproject.toml` (dev-only tools go
under `[project.optional-dependencies].dev`), then re-run
`pip install -e ".[dev]"`.
