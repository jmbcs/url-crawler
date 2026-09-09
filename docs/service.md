# Crawl service

An optional API and worker that turn a crawl into a background job: how to run it, the endpoints,
the settings, and what it does not do yet.

`POST /crawls` queues a crawl, a worker claims it from Postgres and runs the same `Crawler` the CLI
runs, and the pages are readable by cursor while the crawl is still going. The API never crawls and
the worker never serves HTTP, so Postgres is the only thing they share. Both live in
`src/url_crawler_service/`, behind the optional `service` dependency group.

## Run it

```bash
docker compose up --build           # postgres, alembic upgrade, api on :8000, one worker
```

```bash
curl -sS -X POST localhost:8000/crawls \
  -H 'content-type: application/json' \
  -d '{"seed": "https://example.com"}'
# {"id":"7c1f...","seed":"https://example.com/","state":"queued", ...}

curl -sS localhost:8000/crawls/7c1f.../pages | jq '.items[] | {url, status, links}'
curl -sN localhost:8000/crawls/7c1f.../events      # server-sent progress until the crawl ends
```

Scale the workers with `docker compose up --build --scale worker=3`. Nothing coordinates them: each
one claims its own crawl.

Without Docker, run the three pieces yourself:

```bash
make db-up        # postgres on localhost:55432, plus the database the tests use
make migrate      # alembic upgrade head
make api          # url-crawler-api  on :8000
make worker       # url-crawler-worker, in another shell
```

## API

| Method | Path | Behaviour |
| --- | --- | --- |
| `POST` | `/crawls` | Queue a crawl. 202 with the crawl and a `Location` header. 422 when the seed is not a crawlable http(s) URL, or a field is out of range or unknown. |
| `GET` | `/crawls` | Newest first. `state` filters, `limit` is 1 to 200, default 50. |
| `GET` | `/crawls/{id}` | State, timestamps, attempts, the stats snapshot and the error. 404 when unknown. |
| `GET` | `/crawls/{id}/pages` | Keyset page list: `after` is the last `seq` you saw, `limit` is 1 to 500, default 100. `next_after` is null on the last page. |
| `GET` | `/crawls/{id}/events` | `text/event-stream`. An `event: stats` frame every two seconds carrying the same body as `GET /crawls/{id}`, then one `event: end`. 404 when unknown. |
| `DELETE` | `/crawls/{id}` | Request cancellation. 202 with the resulting state. 404 when unknown. |
| `GET` | `/healthz` | 200 after a `SELECT 1`, 503 when the database is unreachable. |

The request body is validated by pydantic and rejects unknown fields: `seed` is required,
`concurrency` is 1 to 50, `timeout` is above 0 and at most 120 seconds, `max_pages` and `max_bytes`
are at least 1, `respect_robots` defaults to true. The defaults come from `CrawlConfig()`, so they
are the CLI defaults, and a test asserts it. The seed goes through the same `prepare_seed` and
`normalize` the CLI uses, both in `urls.py`, so `{"seed": "example.com"}` is stored as
`https://example.com/`. OpenAPI is at `/docs`.

The pages endpoint pages by keyset, not `OFFSET`. A keyset cursor is the last row you saw rather
than a row count to skip. Rows keep arriving while a crawl runs, so an offset shifts every later
page; a cursor over `(crawl_id, seq)` does not. The links of one page stay inside one item, so a
consumer never sees half a page.

Cancel is a request, not a kill; [architecture.md](architecture.md) has the state-by-state rules.

## Configuration

Every setting is an environment variable named after its field. A missing `DATABASE_URL` or an
unparsable value exits 2 with a message naming the variable.

- `cp .env.example .env` to start: every variable is listed there with its default and a one-line
  comment on what it does.
- `docker compose up` loads `.env` on its own (`env_file`), but always builds the containers'
  `DATABASE_URL` itself against `postgres:5432`, so a host-side value in `.env` never leaks in.
- Outside Docker, `make migrate`, `make api`, `make worker` and `make test-service` run through
  `uv run --env-file .env` when `.env` exists, falling back to their built-in defaults otherwise.
- Two databases: `crawler` for `migrate`/`api`/`worker`, `crawler_test` for `test-service`, so a
  compose worker never claims a crawl the test suite queued. `make db-reset` recreates the local
  Postgres volume, which `docker/postgres-init.sql` needs to create `crawler_test` on first boot.

| Variable | Default | Read by |
| --- | --- | --- |
| `DATABASE_URL` | required | api, worker, alembic |
| `API_HOST` | `0.0.0.0` | api |
| `API_PORT` | `8000` | api |
| `WORKER_POLL_SECONDS` | `1.0` | worker, when the queue is empty or the database is unreachable |
| `HEARTBEAT_SECONDS` | `5.0` | worker lease refresh and cancel check |
| `LEASE_SECONDS` | `30.0` | reaper: how long silence is tolerated. The worker also gives up its own lease after this many seconds of failed heartbeats |
| `MAX_ATTEMPTS` | `3` | reaper: requeue below this, fail at it |
| `PAGE_BATCH_SIZE` | `100` | `DbReporter` size trigger |
| `PAGE_FLUSH_SECONDS` | `0.2` | `DbReporter` time trigger |

`DATABASE_URL` is a SQLAlchemy async URL, for example
`postgresql+asyncpg://crawler:crawler@localhost:55432/crawler`.

`.env.example` also carries the `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`
and `API_PORT` values `docker-compose.yml` reads, and the `URL_CRAWLER_TEST_DATABASE_URL` the
service tests read.

## Testing the service

```bash
make db-up          # postgres on :55432 plus the crawler_test database
make test-service   # 69 tests against it; the suite migrates that database itself
```

[testing.md](testing.md) covers what those tests assert and why they need a real Postgres.

## What the service does not do yet

- **No frontier resume.** A requeued crawl restarts from the seed and its earlier pages are deleted.
  Resuming needs the frontier in the database, which is the multi-host design sketched in
  [extending.md](extending.md).
- **No cross-worker politeness.** Two workers handed two crawls of the same host will both crawl it.
  Per-host leases are the fix, and they are the reason that section exists.
- **No authentication, quotas or tenancy.** Anyone who can reach the API can queue a crawl.
- **No UI.** `curl` and `jq` are the client, plus `/docs` for the schema.
- **One crawl per worker process.** Scale by running more workers. A worker that ran several crawls
  at once would need per-crawl connection accounting for no gain a second container does not give.

---

[Back to README](../README.md)
