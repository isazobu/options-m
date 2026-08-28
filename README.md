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

## Deploying to Northflank

The free Sandbox tier allows 2 services + 2 cron jobs + 1 addon, with
always-on compute (no sleeping). This app fits in **1 service + 1 addon**.

1. **Create the Postgres addon** first. Copy its connection string.
2. **Create a Combined (build + deploy) service** from this repo.
   - Build type: **Dockerfile**, path `/Dockerfile`.
   - Port: **8080**, protocol HTTP, and publish it so the dashboard is
     reachable.
3. **Set environment variables** on the service:
   - `DATABASE_URL` — from the addon (link it as a secret rather than pasting
     the DSN, so credential rotation propagates).
   - `LOG_FORMAT=json`, `LOG_LEVEL=INFO`.
   - Leave `PORT` alone unless you change the exposed port.
4. **Point the health check at `/health`** (not `/ready`) — readiness failures
   should not trigger restarts.

Notes:

- Northflank requires a payment method on file even for the free tier, and
  documents the free tier as *not for production* — expect restarts and do not
  store anything there you cannot lose.
- The free tier gives **one** addon, so there is no Redis alongside Postgres.
  If you need a work queue, build it on Postgres with
  `SELECT ... FOR UPDATE SKIP LOCKED` rather than adding a broker.
- If you later split the agents and the dashboard into two services, both can
  run the same image with different env vars — the second service still fits in
  the free tier's 2-service budget.

## Adding dependencies

Add them to `[project].dependencies` in `pyproject.toml` (dev-only tools go
under `[project.optional-dependencies].dev`), then re-run
`pip install -e ".[dev]"`.
