# Crawl service

An optional API and worker that turn a crawl into a background job. For operators running it, not
readers of the crawler's core code.

Getting started is the [service walkthrough](../README.md#the-crawl-service) in the README, six
steps from `docker compose up --build -d` to `docker compose down`. This page is the reference
behind it and does not repeat it.

`POST /crawls` queues a crawl, a worker claims it from Postgres and runs the same `Crawler` the CLI
runs, and pages are readable by cursor while the crawl is still going. The API never crawls and the
worker never serves HTTP, so Postgres is the only thing they share. Both live in
`src/url_crawler_service/`, behind the optional `service` pip extra.

## Run it

- **With Docker:** `docker compose up --build -d` starts Postgres, runs `alembic upgrade head` to
  completion, then the API on `:8000` and one worker. `--scale worker=3` runs more workers; nothing
  coordinates them, each claims its own crawl.
- **Without Docker:** `uv sync` installs the `service` extra (`pip install 'url-crawler[service]'`
  otherwise). Missing it, both console scripts print an error and exit 2 instead of crashing.

```bash
make db-up          # postgres on :55432, plus the crawler_test database
make migrate        # alembic upgrade head
make api            # url-crawler-api on :8000
make worker         # url-crawler-worker, in another shell
make test-service   # 84 tests against crawler_test; the suite migrates it itself
```

[testing.md](testing.md) covers what those tests assert.

## API

| Method | Path | Behaviour |
| --- | --- | --- |
| `POST` | `/crawls` | Queue a crawl. 202 with the crawl and a `Location` header. 422 when the seed is not a crawlable http(s) URL, its host is private or unresolvable, or a field is out of range or unknown. |
| `GET` | `/crawls` | Newest first. `state` filters, `limit` is 1 to 200, default 50. |
| `GET` | `/crawls/{id}` | State, timestamps, attempts, the stats snapshot and the error. 404 `{"detail": "crawl not found"}` when unknown. |
| `GET` | `/crawls/{id}/pages` | Keyset page list: `after` is the last `seq` seen, `limit` is 1 to 500, default 100. `next_after` is null once you have read everything written so far. |
| `GET` | `/crawls/{id}/events` | `text/event-stream`. An `event: stats` frame every two seconds carrying the `GET /crawls/{id}` body, then one `event: end` at a terminal state. 404 when unknown. |
| `DELETE` | `/crawls/{id}` | Request cancellation, not a kill. 202 with the resulting state: `queued` aborts outright, `running` stops at its worker's next heartbeat with partial pages kept, a terminal crawl comes back unchanged. 404 when unknown. |
| `GET` | `/healthz` | 200 `{"status": "ok"}` after a `SELECT 1`, 503 `{"detail": "database unavailable"}` when the database is unreachable. |

- Pydantic rejects unknown fields: `concurrency` 1-50, `timeout` above 0 up to 60s (the request
  budget; a longer value is invalid, so an over-budget request is a 422 at submission, not a later
  worker failure), `max_pages` at least 1, `max_bytes` 1-100,000,000, `respect_robots` true by
  default, all matching `CrawlConfig()` and so the CLI.
- The seed goes through the CLI's own `prepare_seed` and `normalize`, so `{"seed": "example.com"}`
  is stored as `https://example.com/`, and a control byte in a posted seed is a 422, not a 500.
- `next_after: null` means every page so far is read, not that the crawl ended; `GET /crawls/{id}`
  says whether it finished. Paging is by keyset, not `OFFSET`, so rows arriving mid-crawl never
  shift a page you already read.
- Swagger UI (`/docs`), ReDoc (`/redoc`) and `/openapi.json` all answer once the API is up.
- [`.postman/url_crawler.postman_collection.json`](../.postman/url_crawler.postman_collection.json)
  imports every endpoint into Postman, with a saved example response for each one captured from a
  real crawl. Run "Submit a crawl" first: it stores the new id in a `crawlId` variable that the
  other requests use, so nothing needs copying by hand.

## Configuration

Every setting is an environment variable named after its field. A missing `DATABASE_URL` or an
unparsable value exits 2 naming the variable.

| Variable | Default | Read by |
| --- | --- | --- |
| `DATABASE_URL` | required | api, worker, alembic |
| `API_HOST` | `0.0.0.0` | api |
| `API_PORT` | `8000` | api |
| `WORKER_POLL_SECONDS` | `1.0` | worker, when the queue is empty or the database is unreachable |
| `HEARTBEAT_SECONDS` | `5.0` | worker lease refresh and cancel check |
| `LEASE_SECONDS` | `30.0` | reaper: how long silence is tolerated; also the worker's own give-up span and final-write retry span |
| `MAX_ATTEMPTS` | `3` | reaper: requeue below this, fail at it |
| `PAGE_BATCH_SIZE` | `100` | `DbReporter` size trigger |
| `PAGE_FLUSH_SECONDS` | `0.2` | `DbReporter` time trigger |

- `cp .env.example .env` lists every variable with its default. `docker compose up` loads it but
  always builds `DATABASE_URL` against `postgres:5432` and sets `API_HOST`/`API_PORT` itself, so a
  host-side value never leaks in; `.env` is also `.dockerignore`d.
- Two databases: `crawler` for `migrate`/`api`/`worker`, `crawler_test` for `test-service`, so a
  compose worker never claims a crawl the test suite queued.
- `LEASE_SECONDS` below `HEARTBEAT_SECONDS` is rejected at startup, naming both values: the worker
  would otherwise give up its lease before ever refreshing it.
- The worker container carries a 1 GB memory limit and a 20-second `stop_grace_period`; Docker's
  10-second default would kill it mid-flush, since the final page flush is allowed 15.

## Claim, heartbeat, reaper

<details>
<summary>Detail: the lease mechanics</summary>

- **Claim:** `SELECT ... FOR UPDATE SKIP LOCKED` takes the oldest queued crawl, so concurrent
  claims land on different rows. It deletes the crawl's existing pages unconditionally, so `seq`
  restarts at 1 and a retry never returns a mix of two attempts.
- **Heartbeat:** every `HEARTBEAT_SECONDS`, matched on `worker_id` too, so a lost lease gets no row
  back. It gives up on real elapsed time since the last successful write, not a count of intervals.
  The asyncpg driver carries a 5-second connect and a 10-second command timeout, so claim, finish,
  release and heartbeat fail fast against a black-holed connection instead of hanging forever.
- **Finish and release:** each reports whether its `UPDATE` matched a row, so a worker that matched
  nothing logs a warning instead of claiming a state it never wrote. A graceful `release` (on
  `SIGTERM`) also returns the attempt it took, toward a floor of zero; only the reaper's requeue
  leaves the increment standing.
- **Reaper:** settles any stale `running` crawl, once at startup and at most once per
  `LEASE_SECONDS`: cancelled becomes `aborted`, below `MAX_ATTEMPTS` goes back to `queued`, the
  rest become `failed` with `error = "worker lost"`.
- **Outages:** the claim loop catches every exception, not only SQLAlchemy's, so a dead Postgres
  costs one `WORKER_POLL_SECONDS` poll instead of the worker process.

</details>

## Crawls run at least once

A crawl can run twice. The pages you read never mix two runs.

<details>
<summary>Detail: why, and what it costs</summary>

The worker crawls, flushes its pages, then writes the terminal state; a worker whose lease was
reaped raises `LeaseLostError` and leaves the row to its new owner instead of mixing pages into
another attempt. If the final write fails, it retries for up to `LEASE_SECONDS`, sleeping
`HEARTBEAT_SECONDS` between tries, then gives up; the stale row is reaped, requeued, and crawled
again from an empty page set.

- **A crawl runs at least once, possibly more.** Budget for a site being fetched twice after a
  database outage or a hard worker kill.
- **The stored pages always come from one lease.** Every claim wipes the crawl's existing pages,
  and every insert is fenced by the lease, so a reader never sees two attempts interleaved.
- **A re-sent page batch is not an error.** Inserts are idempotent on `(crawl_id, seq)`, so a batch
  whose commit acknowledgement was lost lands as a no-op instead of failing a finished crawl.

</details>

## The seed host guard

The service refuses a seed that points inside its own network, at submission and again by the
worker before every fetch.

<details>
<summary>Detail: what counts as private, and where the check runs</summary>

`POST /crawls` resolves the seed host and refuses it with 422 and
`{"detail": {"seed": "<reason>"}}`. Refused:

- `localhost`, any `*.localhost`, `*.local` or `*.internal` name.
- A private IP literal, or a host resolving to one: loopback, private (RFC 1918 IPv4, or IPv6
  unique-local), link-local (`169.254.169.254` included), multicast, reserved, unspecified, or
  carrier-grade NAT (RFC 6598, `100.64.0.0/10`) and anything else not globally routable.
- A host that does not resolve at all.

- The worker repeats the check before the first seed fetch and before every redirect hop, so DNS
  that changed since the crawl was queued is caught too. `load_robots` awaits the same guard on the
  robots URL and its redirects, so a robots.txt redirecting into the private network is refused
  like a seed redirect. A refused seed ends the crawl `failed` with `seed rejected: <reason>`.
- The CLI passes no guard: crawling `http://localhost:8000` from a terminal is normal, and the CLI
  reaches nothing its user cannot already reach.
  [design-decisions.md](design-decisions.md#the-service-guards-its-seed-host-the-cli-does-not) has
  the full argument.

</details>

## What the service does not do yet

<details>
<summary>Detail: known gaps and the reasoning behind each</summary>

- **No frontier resume.** A requeued crawl restarts from the seed; its earlier pages are deleted.
  Resuming needs the frontier in the database, the multi-host design in [extending.md](extending.md).
- **No cross-worker politeness.** Two workers handed two crawls of the same host will both crawl
  it. Per-host leases are the fix.
- **No authentication.** Anyone who can reach the API can queue, cancel, and read every crawl on
  it. No quotas, no tenancy. Deploy it somewhere only you can reach.
- **The seed guard resolves, it does not pin.** The check and the fetch each resolve the host, so a
  name whose DNS answer changes between them is not covered. Pinning the fetch's address is the fix.
- **No UI.** `curl` and `jq` are the client, plus Swagger UI at `/docs` for poking at the schema.
- **One crawl per worker process.** Scale by running more workers; per-crawl connection accounting
  for a second crawl in the same process would buy nothing a second container doesn't already give.

</details>

---

[Back to README](../README.md)
