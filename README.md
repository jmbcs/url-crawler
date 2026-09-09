# url-crawler

A Python CLI that takes one URL, crawls the whole site behind it, and prints every page it visits
together with every link found on that page. It stays on a single host: no other domains, no
subdomains. Output streams to stdout as each page completes, so a large crawl is useful before it
finishes. An optional [crawl service](#crawl-service) runs the same crawl as a background job behind
an HTTP API.

Three readings of the brief were ambiguous, so the choices are stated up front:

- **Scope restricts what is followed, not what is printed.** A link to another domain or to a
  subdomain appears in the output of the page that contained it, and is never requested.
- **"URLs found on a page" means anchor hyperlinks**, `a[href]` and `area[href]`, not subresources
  such as `img`, `script` or `link`. Anchors are the navigable graph the crawl walks.
- **Per-page output is deduplicated in first-occurrence document order.** A navigation menu repeated
  in a header and a footer prints once. Links are not sorted, because document order is already
  deterministic.

Redirects follow from the same model: `follow_redirects=False`, and a 301/302/303/307/308 response
is reported as a page whose single link is its `Location`.
[Design decisions](#design-decisions) covers what that model buys.

The CLI depends on `httpx` and `selectolax` and on nothing else. The frontier, the scope check, the
retry policy, the robots.txt handling and the link extraction are written in this repository rather
than pulled from a crawling framework, which is what makes each of them testable and explainable
below. The crawl service adds `fastapi`, `uvicorn`, `sqlalchemy`, `asyncpg` and `alembic` in an
optional `service` dependency group, so `pip install .` still installs exactly those two direct
dependencies.

## Features

- Recursive crawl of one host with cycle-safe deduplication and a definite end.
- Bounded concurrency: N asyncio workers over one shared `httpx.AsyncClient` connection pool.
- URL normalization, and dedup by a canonical key that ignores query parameter order.
- Exact case-insensitive `(host, port)` scope. `www.example.com` is not `example.com`.
- Redirects modelled as links, so once the seed's chain settles, nothing off-host is contacted.
- robots.txt on by default, `Crawl-delay` honoured, fail open when robots.txt cannot be read.
- Retries with full jitter for retryable statuses and transport errors, `Retry-After` respected.
- Bodies streamed behind a content-type gate and a size cap, so a 4 GB video is never downloaded.
- Text or JSONL output. Results on stdout, logs and the run summary on stderr.
- Exit codes that mean something, partial output flushed on Ctrl-C, and `| head` handled cleanly.
- A failure fuse that aborts a crawl which has stopped producing anything but errors.
- An optional crawl service: `POST /crawls` queues a job, a worker runs it, results page out of
  Postgres by keyset cursor.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                             # install, from the committed uv.lock
uv run url-crawler https://example.com              # crawl and print to stdout
uv run url-crawler example.com --format jsonl > out.jsonl   # scheme defaults to https
uv run url-crawler https://example.com -v --max-pages 200   # progress logs and a page cap
```

With Docker:

```bash
docker build --target cli -t url-crawler .
docker run --rm url-crawler https://example.com
```

Without uv, in any virtual environment:

```bash
pip install .
url-crawler https://example.com
```

`python -m url_crawler <url>` works the same as the `url-crawler` script. To run crawls as
background jobs instead of in a terminal, see [Crawl service](#crawl-service).

## Sample output

Trimmed from a real run against the fake site the test suite uses, which packs every hazard into 20
pages. The site answers on a loopback port but writes its absolute links against its nominal host
`site.test`, so `http://sub.site.test/x` is the subdomain case below and `http://external.test/x`
the foreign-domain one. Page URLs sit at column zero, their links are indented two spaces, and a
blank line closes each page.

```
$ uv run url-crawler http://127.0.0.1:39735 --concurrency 1
http://127.0.0.1:39735/
  http://127.0.0.1:39735/a
  http://127.0.0.1:39735/leaf
  http://127.0.0.1:39735/robots-blocked
  http://external.test/x
  http://sub.site.test/x

http://127.0.0.1:39735/missing  [error: http_status 404]

http://127.0.0.1:39735/redirect  [redirect 301]
  http://127.0.0.1:39735/redirected

http://127.0.0.1:39735/off-site-redirect  [redirect 302]
  http://external.test/landing

http://127.0.0.1:39735/file.pdf  [error: unsupported_content 200]

http://127.0.0.1:39735/leaf

```

Six pages of the twenty are shown, and the seed's link list is cut from seventeen entries to five.
Read them from the top: the seed prints an external link and a subdomain link that are never
requested, and a robots-blocked path that is printed but not followed. `/missing` is reported with
its status rather than dropped. `/redirect` is a page whose one link is its target.
`/off-site-redirect` prints a link to another host and stops there. `/file.pdf` is rejected on its
content type, before its body is read. `/leaf` is a valid page with no links, not an error.

The summary goes to stderr, so it never pollutes a pipe:

```
Crawled 20 pages (17 ok, 3 failed) and found 26 links in 0.9s (22.1 pages/s); 1 retries, 7 duplicate URLs skipped
```

`--format jsonl` emits one object per page and a final summary object:

```json
{"url": "http://127.0.0.1:41295/missing", "status": 404, "links": [], "error": {"kind": "http_status", "status": 404, "message": "HTTP 404"}}
{"url": "http://127.0.0.1:41295/redirect", "status": 301, "links": ["http://127.0.0.1:41295/redirected"], "error": null}
{"url": "http://127.0.0.1:41295/file.pdf", "status": 200, "links": [], "error": {"kind": "unsupported_content", "status": 200, "message": "application/pdf"}}
{"summary": {"pages_ok": 17, "pages_failed": {"http_status": 2, "unsupported_content": 1}, "pages_without_links": 5, "redirects": 4, "links_found": 26, "duplicates_dropped": 7, "retries": 1, "pages_total": 20, "elapsed_seconds": 1.422}}
```

## Architecture

One process, one event loop, one HTTP client. `cli.py` parses flags and wires the objects together,
`crawler.py` owns the crawl, and everything else is a small single-purpose module. Retry lives
inside the fetcher, so the crawler only ever sees a finished `FetchResult` or `FetchError`.

```mermaid
flowchart TD
    CLI["cli.py<br>flags, wiring, signals, summary"] --> Crawler["crawler.py<br>seed redirects, worker pool, scope, fuse"]
    Crawler --> Robots["robots.py<br>robots.txt once, fail open, Crawl-delay"]
    Crawler <--> Frontier["frontier.py<br>asyncio.Queue plus seen keys"]
    Crawler --> Fetcher["fetcher.py<br>streaming GET, content-type and size gates"]
    Fetcher --> Retry["retry.py<br>classify, full-jitter backoff, Retry-After"]
    Fetcher --> Client["http.py<br>httpx.AsyncClient, follow_redirects=False"]
    Crawler --> Parser["parser.py<br>a[href] and area[href], base href, per-page dedup"]
    Parser --> Urls["urls.py<br>normalize, canonical key, HostScope"]
    Frontier --> Urls
    Crawler --> Urls
    Crawler --> Reporter["reporting.py<br>TextReporter or JsonlReporter to stdout"]
```

A worker takes a URL from the frontier, fetches it, parses the body, reports the page, then
enqueues the links that pass scope and robots before calling `task_done()`. The crawl ends when the
queue is empty and every claimed URL is done (`Queue.join()`), so no sentinel values and no timeouts
are involved. A catch-all around the per-URL pipeline turns an unexpected exception into one failed
page instead of a cancelled `TaskGroup`.

| Module | Responsibility |
| --- | --- |
| `cli.py` | argparse flags, object wiring, SIGINT and SIGTERM, exit codes, banner and progress line, stderr summary |
| `config.py` | frozen `CrawlConfig`, validated once in `__post_init__` |
| `crawler.py` | seed redirect chain, scope re-anchoring, worker pool, per-page pipeline, max-pages drain, failure fuse |
| `frontier.py` | `asyncio.Queue` plus a set of canonical keys: dedup, backlog size, termination |
| `fetcher.py` | one streaming GET per attempt, content-type and size gates, retry loop |
| `retry.py` | pure classification, full-jitter backoff, `Retry-After` parsing |
| `parser.py` | link extraction with selectolax, `<base href>`, per-page dedup in document order |
| `urls.py` | `prepare_seed`, `normalize`, `canonical_key`, `resolve_href`, `HostScope` |
| `robots.py` | fetch and parse robots.txt once, `Crawl-delay`, fail open |
| `http.py` | the one `httpx.AsyncClient` factory, shared by the CLI and the service worker |
| `reporting.py` | `Reporter` protocol with a text and a JSONL implementation |
| `models.py` | `FetchResult`, `FetchError`, `FetchErrorKind`, `PageResult`, `CrawlStats`, and the `summary` both the JSONL output and the service store |
| `progress.py` | start banner and the redrawing progress line, both stderr and TTY-only |

`src/url_crawler_service/` holds the optional service and imports the core; the core never imports
it. [Crawl service](#crawl-service) has that module table.

## Flags

| Flag | Default | What it does |
| --- | --- | --- |
| `url` | required | Seed URL. A missing scheme defaults to `https://`; anything but http(s) is a usage error. |
| `--concurrency N` | `10` | Worker tasks, and the only bound on requests in flight. |
| `--timeout SECONDS` | `10.0` | Read and write timeout. Connect and pool timeouts are fixed at 5s. |
| `--max-pages N` | unlimited | Stop after N pages and log how many URLs were left unvisited. |
| `--max-bytes BYTES` | `5000000` | Skip a page whose body exceeds this, by header or while streaming. |
| `--format {text,jsonl}` | `text` | JSONL emits one object per page plus a final summary object. |
| `--ignore-robots` | off | Crawl paths robots.txt disallows. |
| `--quiet` | off | Suppress the start banner and the progress line. The summary still prints. |
| `-v`, `-vv` | quiet | `-v` logs at INFO, `-vv` at DEBUG (including every link printed but not followed). |
| `--version` | | Print the version and exit. |

The CLI reads no environment variables and no config file. Flags are its whole configuration
surface, which keeps a run reproducible from its command line. The service is configured by
environment variables instead, listed under [Crawl service](#crawl-service).

Two things print to stderr when it is a terminal, so a slow seed does not look like a hang: a
three-line start banner before the crawl, and a one-line progress counter that redraws in place
about three times a second. The banner also prints under `-v` in a pipe, because a log-level run
asked for context; the progress line never does, so a redirect or a CI log stays free of `\r`.
`--quiet` turns both off. Stdout carries results and nothing else in every case.

```
url-crawler 0.2.0: crawling https://example.com with 10 workers (robots.txt on, text output)
Results stream to stdout as pages complete. Ctrl-C stops and keeps what was crawled.
Long or unattended crawl? Run it as a job with url-crawler-api and url-crawler-worker; see README, "Crawl service".
pages 143 (2 failed) | queued 512 | links 3,904 | 48.1 pages/s | 3.0s
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The crawl finished, or stopped cleanly at `--max-pages`. |
| `1` | Unexpected internal error, logged with a traceback. |
| `2` | Usage error: bad flag value, unsupported scheme, or an unparseable seed URL. |
| `3` | The seed could not be fetched, or robots.txt disallows it. |
| `4` | The failure fuse aborted the crawl after 20 consecutive failures. |
| `130` | Interrupted by SIGINT or SIGTERM. Pages already crawled are flushed and the summary is printed. |

## Crawl service

An optional API and worker turn a crawl into a job. `POST /crawls` queues one, a worker claims it
from Postgres and runs the same `Crawler` the CLI runs, and the pages are readable by cursor while
the crawl is still going. The API never crawls and the worker never serves HTTP, so Postgres is the
only thing they share. Both live in `src/url_crawler_service/`, behind the optional `service`
dependency group.

### Run it

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

Without Docker, run the three pieces yourself:

```bash
make db-up        # postgres on localhost:55432, plus the database the tests use
make migrate      # alembic upgrade head
make api          # url-crawler-api  on :8000
make worker       # url-crawler-worker, in another shell
```

### API

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

The pages endpoint pages by keyset, not `OFFSET`. Rows keep arriving while a crawl runs, so an
offset shifts every later page; a cursor over `(crawl_id, seq)` does not. The links of one page stay
inside one item, so a consumer never sees half a page.

### Configuration

Every setting is an environment variable named after its field. A missing `DATABASE_URL` or an
unparsable value exits 2 with a message naming the variable.

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

### Testing the service

```bash
make db-up          # postgres on :55432 plus the crawler_test database
make test-service   # 68 tests against it; the suite migrates that database itself
```

They are marked `postgres` and skip when `URL_CRAWLER_TEST_DATABASE_URL` is unset, so `make test`
stays offline and dependency-free. CI runs them in their own job against a Postgres service
container. They use a real database rather than a fake: `SKIP LOCKED` and `RETURNING` are the parts
most worth testing, and neither of them exists in a mock.

<details>
<summary>How it works: why a CLI stops fitting, the claim and lease design, what survives a database
outage, cancel semantics, and what the service does not do yet.</summary>

### Why a CLI stops fitting

Four axes, and any one of them is enough.

*Lifetime.* A six-hour crawl bound to a terminal session dies with the SSH connection, and
`--resume` is a workaround for the absence of a job.

*Cross-process politeness.* Ten laptops each running a polite crawler are collectively a denial of
service, and no amount of per-process courtesy fixes it. That axis alone forces a central service.

*Machine consumption.* stdout is a poor API. A consumer wants the pages of crawl 47 since cursor X,
not a re-run and a re-parse.

*Multi-tenancy.* Quotas, authentication and an audit trail have nowhere to live in a process with no
identity.

So the service is job-shaped rather than stream-shaped. The crawl itself is still the `Crawler`
class the CLI runs: the service adds a queue, a lease and a store around it, and changes nothing
inside it.

### Shape

```mermaid
flowchart LR
    Client["client<br>curl, or any HTTP consumer"] --> Api["url-crawler-api<br>FastAPI: validate, read, cancel"]
    Api --> Db[("postgres<br>crawl and page")]
    Worker["url-crawler-worker<br>claim, heartbeat, reap"] --> Db
    Worker --> Core["url_crawler.Crawler<br>the same crawl the CLI runs"]
    Core --> Reporter["reporter.py<br>DbReporter, batched inserts"]
    Reporter --> Db
```

The API validates a request, writes a row and reads rows back. The worker claims a queued crawl,
runs it, and heartbeats its lease while it does. Nothing coordinates the workers, so N workers are
N processes.

### Data model

Two tables, both created by the Alembic migration in `src/url_crawler_service/alembic/versions/`.

| Table | Columns |
| --- | --- |
| `crawl` | `id` uuid PK, `seed`, `config` jsonb, `state`, `created_at`, `started_at`, `finished_at`, `heartbeat_at`, `worker_id`, `attempts`, `cancel_requested`, `stats` jsonb, `error`. Index on `(state, created_at)`. |
| `page` | PK `(crawl_id, seq)` with `crawl_id` cascading from `crawl`, plus `url`, `status`, `error_kind`, `error_message`, `links` jsonb, `fetched_at`. |

`state` is one of `queued`, `running`, `finished`, `failed`, `aborted`. `seq` is assigned by the
worker, one-based and gap-free, which is what makes it a cursor. One page is one row, so the links
of a page are stored as a JSON array rather than a join table: nothing queries across links, and a
consumer always asks for whole pages.

Migrations are generated, never written by hand: `make migration m="what changed"` runs
`alembic revision --autogenerate`. A service test runs `alembic check` and fails when the committed
migration and the ORM models have drifted apart, and another downgrades to base and upgrades again.

### Claim, heartbeat, reaper

A worker claims the oldest queued crawl in one transaction:

```sql
SELECT crawl.id, crawl.attempts FROM crawl
WHERE crawl.state = 'queued' ORDER BY crawl.created_at
LIMIT 1 FOR UPDATE SKIP LOCKED;

UPDATE crawl SET state = 'running', started_at = now(), heartbeat_at = now(),
                 worker_id = $1, attempts = attempts + 1
WHERE id = $2 RETURNING ...;
```

`SKIP LOCKED` is why no broker is needed. Two workers running that statement at the same instant
take different rows instead of blocking on each other, which a test asserts with two concurrent
claims. A claim whose `attempts` is already above zero deletes that crawl's earlier pages first, so
a retried crawl never returns a mix of two attempts.

While the crawl runs, the worker heartbeats every `HEARTBEAT_SECONDS`: one `UPDATE` that refreshes
`heartbeat_at`, stores the current stats snapshot, and returns `cancel_requested`. The `UPDATE`
matches on `worker_id` too, so a worker that lost its lease gets no row back, learns it no longer
owns the crawl, and stops writing.

Before each claim, the worker also reaps: any crawl still `running` whose `heartbeat_at` is older
than `LEASE_SECONDS` is settled in one pass. A cancelled one becomes `aborted`, one below
`MAX_ATTEMPTS` goes back to `queued`, and the rest become `failed` with `error = "worker lost"`. A
`kill -9` therefore costs one lease period, not a stuck job.

On `SIGTERM` the worker does better than that: it cancels the crawl, writes the pages it has, and
releases the row back to `queued` at once, so no lease period is lost. The attempt counter is left
as it is, and only the reaper consults `MAX_ATTEMPTS`, so a rolling deploy re-runs the crawl instead
of failing it. A cancel request that raced the shutdown wins: the release matches only a row with
`cancel_requested = false`, and when it matches nothing the worker records `aborted` instead.

### Surviving a database outage

The claim loop catches every exception, not only SQLAlchemy's. A Postgres that is down surfaces as
`ConnectionRefusedError` or a DNS failure well before it is a `DBAPIError`, and either one used to
end the worker process. It now logs the traceback, waits `WORKER_POLL_SECONDS` and polls again, so a
database restart costs a poll interval. The compose file restarts the api and worker containers
`unless-stopped`, for the crashes this does not cover.

Pages are written by `DbReporter`, which buffers whatever the crawler reports and inserts it in one
`executemany` when the buffer reaches `PAGE_BATCH_SIZE` or `PAGE_FLUSH_SECONDS` elapses. Its
`page()` method never awaits, so a slow database slows the flusher and not the crawl loop. A failed
insert keeps its rows in the buffer and the flusher retries them on the next tick. The flush after
the crawl ends is the last attempt: if it fails, the crawl is recorded `failed` with that error, and
a crawl that was cancelled keeps its own reason with the write error appended to it.

Every insert is fenced by the lease. It locks the crawl row `FOR SHARE` and writes only while that
row is still `running` under this worker id, in the same transaction. A worker whose lease was
reaped raises `LeaseLost` instead of mixing its pages into the attempt another worker now owns; it
logs a warning, records no terminal state, and leaves the row to its new owner.

### Cancel

`DELETE /crawls/{id}` is a request, not a kill. It answers 202 with the resulting state for any
known crawl, and 404 otherwise.

- A `queued` crawl becomes `aborted` immediately, with `error = "cancelled before start"`, and no
  worker ever claims it.
- A `running` crawl gets `cancel_requested = true`. Its worker sees the flag on the next heartbeat,
  cancels the crawl task, flushes the pages already crawled, and records `aborted` with
  `error = "cancelled by request"`. Partial results stay readable.
- A finished, failed or already aborted crawl is left exactly as it is.

### Modules

| Module | Responsibility |
| --- | --- |
| `settings.py` | `Settings.from_env()`, validation, `SettingsError` naming the bad variable |
| `db.py` | declarative `Base`, async engine, session factory |
| `orm.py` | the `crawl` and `page` tables and `CrawlState` |
| `alembic/` | the migration environment and the versions `alembic upgrade head` applies |
| `models.py` | `PageRow` and the `PageResult` to row conversion |
| `repository.py` | every query, each in its own short transaction: claim, heartbeat, finish, release, reap, cancel, lease-fenced inserts and page reads |
| `reporter.py` | `DbReporter`, the batching `Reporter` the worker hands to the core crawler, retrying whatever an insert rejected |
| `worker.py` | claim loop, heartbeat, cancellation, graceful shutdown, terminal state |
| `api.py` | the FastAPI app, its routes and the lifespan that disposes the engine |
| `schemas.py` | request and response models, and the shared seed validation |

### What the service does not do yet

- **No frontier resume.** A requeued crawl restarts from the seed and its earlier pages are deleted.
  Resuming needs the frontier in the database, which is the multi-host design sketched under
  [Extending to multiple domains](#extending-to-multiple-domains).
- **No cross-worker politeness.** Two workers handed two crawls of the same host will both crawl it.
  Per-host leases are the fix, and they are the reason that section exists.
- **No authentication, quotas or tenancy.** Anyone who can reach the API can queue a crawl.
- **No UI.** `curl` and `jq` are the client, plus `/docs` for the schema.
- **One crawl per worker process.** Scale by running more workers. A worker that ran several crawls
  at once would need per-crawl connection accounting for no gain a second container does not give.

</details>

## Design decisions

Each decision names the option that was rejected and what would reverse it. Open one to read it.

<details>
<summary><strong>asyncio, one client, N workers</strong>: a crawl waits on sockets, so tasks beat
threads and processes.</summary>

A crawl is IO-bound: almost all of the wall time is waiting on sockets. `asyncio.TaskGroup` with N
worker tasks over one `httpx.AsyncClient` reuses connections, keeps keep-alive working, and holds
all shared state in one thread, so the seen set and the counters need no locks.

*Rejected:* a thread pool (one connection pool per thread, or lock-protected sharing, for no gain on
IO waits) and a process pool (interprocess dedup for a problem that has none). *Reverses if:* HTML
parsing starts dominating the profile, at which point the fix is a `ProcessPoolExecutor` behind
`extract_links`, not a different concurrency model for the fetches.

</details>

<details>
<summary><strong>The worker count is the only limit</strong>: no second semaphore, no rps cap, and
an unbounded queue on purpose.</summary>

`httpx.Limits(max_connections=N, max_keepalive_connections=N)` matches the worker count, so there is
no second semaphore and no requests-per-second cap. Browsers cap HTTP/1.1 at 6 connections per host,
so the default of 10 is already assertive against a single site, and a lower number is the honest
answer for a fragile target. The frontier queue is deliberately unbounded: its consumers are also
its producers, so a bounded queue deadlocks as soon as every worker is blocked trying to enqueue
children. Memory is bounded by the seen set, `--max-pages` and `--max-bytes` instead.

*Rejected:* an rps token bucket and an AIMD controller that widens and narrows concurrency from the
error rate. Today a 429 is handled per request: `Retry-After` is honoured and the request is
retried, with no feedback into the concurrency level. *Reverses if:* repeated 429s show up against
real targets, at which point the smallest useful step is narrowing concurrency on sustained 429s,
and full AIMD belongs in the multi-host service described below.

</details>

<details>
<summary><strong>follow_redirects=False, and a redirect is a page with one link</strong>: the client
must never pick the next host.</summary>

With `follow_redirects=True`, httpx contacts the redirect target before any of this code can check
whether it is in scope, which breaks the single-host rule the brief states twice. So redirects are
never followed by the client. A 3xx with a `Location` becomes a page whose only link is that target,
and the target then goes through resolve, normalize, scope and dedup like any other link.

That one decision fixes four things at once: an off-host redirect target is printed but never
requested; a redirect chain is visible in the output hop by hop; a redirect landing on an
already-crawled page is dropped as a duplicate; and an http to https upgrade does not double-fetch.
Redirect cycles need no special case, because the second hop is already in the seen set. The seed is
the one exception: its chain is walked eagerly (up to 5 hops) before the crawl starts, because the
scope has to be anchored to the host that actually serves the site.

*Rejected:* letting httpx follow redirects and filtering afterwards, which leaks requests off-host.
*Reverses if:* the scope model ever becomes a multi-host allowlist, where following a redirect
inside the allowlist is safe.

</details>

<details>
<summary><strong>Exact-host scope, re-anchored to the seed's final host</strong>: equality, not
suffix matching, and apex to www still works.</summary>

Scope is exact case-insensitive `(host, port)` equality, not a suffix match: `blog.example.com`,
`www.example.com`, `notexample.com`, `example.com.evil.tld` and `example.com:8080` are all out of
scope for a seed of `https://example.com`. Suffix matching is how crawlers wander onto
`example.com.evil.tld`, and registrable-domain matching (`tldextract`) would pull in the subdomains
the brief excludes.

Many sites redirect between the apex and `www`. Scoping to the host as typed would fetch exactly one
redirect and stop, so scope is re-anchored to the **final** host of the seed's redirect chain, and
that re-anchor is logged. Exactly one host is ever crawled either way.

The cost lands on the one constraint the brief names twice: when `example.com` redirects to
`www.example.com`, the host actually crawled is a subdomain of the host that was typed. The
single-host rule still holds for the crawl itself, and the typed apex is treated as an alias the
site declared for itself by redirecting.

*Rejected:* a wide scope of `{typed host, final host}`, which crawls two hosts and gets better
coverage of apex-only pages, and a literal scope with no re-anchoring, which stops after one hop on
a large share of real sites. *Reverses if:* a target turns out to have pages reachable only on the
apex, with no link from the `www` host; the wide scope is a two-line change to `HostScope`.

</details>

<details>
<summary><strong>robots.txt on by default, rel="nofollow" followed</strong>: robots.txt is the
access control, nofollow is not.</summary>

robots.txt is fetched once for the final seed host, parsed with `urllib.robotparser`, and applied to
every candidate URL before it enters the frontier. The seed itself is the one URL fetched before
robots.txt is read, because its redirect chain decides which host's robots.txt applies; if that file
then disallows the seed, the run stops there with exit code 3. Fetch failures and undecodable bodies
fail open and log at INFO, because an unreadable robots.txt is not a prohibition. A `Crawl-delay` is
honoured by serializing a sleep across the workers. When robots.txt blocks the seed itself, the run
exits 3 with a message that names `--ignore-robots`.

`rel="nofollow"` links are followed and printed. `nofollow` is a hint to search engines about link
equity, not an access control; robots.txt is the access control, and this tool obeys that one.

*Rejected:* robots off by default (faster to write, wrong for anything pointed at a real site) and
honouring `nofollow` (would silently hide pages the site never asked to protect). *Reverses if:* a
crawl trap turns out to be marked with `nofollow` in practice, which would make it a useful signal
rather than a misapplied one.

</details>

<details>
<summary><strong>Retry classification by leaf type, with Retry-After</strong>: enumerate the
retryable exceptions, never their base class.</summary>

`retry_delay()` is a pure function of status, exception, attempt number and headers, so the whole
policy is a table test that runs in microseconds. Retryable statuses are 408, 425, 429, 500, 502,
503 and 504. Retryable exceptions are enumerated as leaves: `httpx.TimeoutException`,
`httpx.NetworkError` and `httpx.RemoteProtocolError`. The tempting `isinstance(exc,
httpx.TransportError)` is wrong, because `UnsupportedProtocol` and `LocalProtocolError` are also
`TransportError` subclasses and both signal a caller bug that no retry can fix. Three attempts, full
jitter (`uniform(0, min(8, 0.5 * 2**attempt))`), and `Retry-After` on 429 and 503 in both
delta-seconds and HTTP-date form, capped at 30 seconds. `sleep` and `random.Random` are injected, so
the retry tests do not sleep.

*Rejected:* Tenacity (a dependency, a decorator, and `Retry-After` still needs custom code) and
retrying by exception base class. *Reverses if:* retry behaviour needs to differ per host, which is
where a small policy object beats a function.

</details>

<details>
<summary><strong>No circuit breaker, but a failure fuse</strong>: one bounded batch against one host
has nothing to half-open.</summary>

A circuit breaker protects a long-lived caller from a flapping shared dependency and probes for
recovery. This is one bounded batch against one host, so "open" would just mean "stop the crawl".
Instead there is a hardcoded fuse: 20 consecutive failures that look like the host is down
(timeouts, connection errors, protocol errors, 5xx) abort the run with exit code 4 and a message
naming the reason. Any success resets the counter, and 4xx never counts, because a wall of 404s is a
site with dead links, not an outage.

*Rejected:* a full breaker with half-open probing, and per-host AIMD decay. *Reverses if:* the
crawler becomes a long-running service over many hosts, where a breaker (or AIMD) per host earns its
state.

</details>

<details>
<summary><strong>A 200 with no links is a leaf, and 90% of them is a warning</strong>: the answer to
the JavaScript sites this tool cannot render.</summary>

Three cases are distinguished. Non-HTML is skipped before the body is read, on the `Content-Type`
header, and reported as `unsupported_content`. HTML with no anchors is a normal leaf page: it prints
with an empty link list and counts as a success. An empty or undecodable body yields no links and
never raises. On top of that there is an aggregate check: if at least 50 pages succeeded and more
than 90% of them had no links, the run logs one warning that the site probably renders its content
with JavaScript. That warning is what this tool offers in place of the browser the exercise bans.

*Rejected:* treating a link-less page as an error (it is a perfectly valid page) and rendering
JavaScript (Playwright is banned by the exercise). *Reverses if:* JavaScript-rendered targets become
the norm rather than the exception, at which point the fix is a rendering service behind the fetcher
interface, not a change to the crawl loop.

</details>

<details>
<summary><strong>No database in the CLI, Postgres in the service, and no Celery or Redis</strong>:
the job store is a table, not a broker.</summary>

The CLI's frontier is an `asyncio.Queue` plus a `set` of canonical keys. Nothing in the CLI reads
crawl data back, so a database there would add a service dependency, a schema and a migration story
to a tool whose whole value is one command producing one stream. The durable artifact is
`--format jsonl`, and `jq` is the query interface.

The service is where persistence earns its keep, because a job has to survive a deploy and a
consumer has to read results it did not watch arrive. It uses Postgres and nothing else. Celery and
Redis were both considered and both rejected:

- **One task type.** Celery buys routing, chords, chains and a result backend for a system with a
  single task, `run this crawl`. The parts actually needed are a claim, a lease and a retry count,
  which are three columns.
- **Crawls arrive per hour, not per second.** `SELECT ... FOR UPDATE SKIP LOCKED` on a table with
  tens of queued rows costs nothing, and a one-second poll is a perfectly good scheduler at that
  rate. Broker throughput solves a problem this workload does not have.
- **A frontier is a set, not a queue.** The moment the frontier moves into the database, the primary
  key is the dedup check and `ON CONFLICT DO NOTHING` is the insert. A broker gives the opposite:
  no dedup, no scan, no scheduling policy, and at-least-once delivery on top.
- **One store beats two.** Pages, state and the queue live in one database, and the worker writes
  its last page batch before it records the terminal state, so a flush that fails turns the crawl
  into `failed` rather than a `finished` crawl with pages missing.

*Rejected:* Celery with Redis or RabbitMQ, and a Redis-only job store. *Reverses if:* the API grows
replicas that need a shared cache or a rate limiter, or a worker starts running several crawls at
once and needs a per-host lease that a `SETNX` with a TTL expresses better than a row. Sub-second
dispatch latency would also do it: `LISTEN`/`NOTIFY` is the cheaper answer first, and a broker only
after that stops being enough.

</details>

<details>
<summary><strong>An HTTP API, no UI</strong>: keyset pagination is the interesting part, and a UI
would only wrap it.</summary>

The service exposes JSON and server-sent events, and stops there. A UI would be a second client of
the same endpoints, and everything a reviewer would learn from it is already visible in the API.

One note on pagination, since it is the first thing either client needs. The CLI streams and lets
`less` and `jq` do the paging. The API pages server-side with a keyset cursor over
`(crawl_id, seq)`. `OFFSET` is wrong here because rows keep arriving during a crawl and shift every
later page; client-side paging is wrong because shipping the whole result set defeats the point.
The links of one page stay inside one response item, so a consumer never sees half a page.

*Rejected:* a small HTMX UI. *Reverses if:* someone who does not use `curl` has to watch a crawl, at
which point the UI is a static page over the existing `/crawls` and `/events` endpoints and adds no
server-side code.

</details>

<details>
<summary><strong>stdlib logging and a stats dataclass</strong>: stdout stays clean, and the counters
outlive a cancelled task.</summary>

`logging` to stderr, WARNING by default, `-v` for INFO and `-vv` for DEBUG. stdout carries results
only, which is asserted by a test, so `url-crawler site | jq` works with any verbosity.
`CrawlStats` is a plain dataclass owned by `cli.py` and injected into the crawler, which is what
makes the Ctrl-C summary possible: the counters live outside the cancelled task. The service reuses
that: the same dataclass is snapshotted into the `crawl.stats` column on every heartbeat, which is
how progress is visible before a crawl ends.

*Rejected:* structlog, Prometheus and Sentry. *Reverses if:* the service runs somewhere real, where
an aggregator needs structured events and a scrape endpoint.

</details>

<details>
<summary><strong>One generic parser, and no Strategy or Factory</strong>: there is no dispatch key
at design time.</summary>

The seed URL arrives at runtime and is arbitrary, so there is no dispatch key at design time and a
parser registry would ship with zero entries. The extraction target is `a[href]`, which every site
expresses identically. Site-specific parsers exist for structured data (prices, titles), which this
exercise does not ask for. A Factory needs something to select between.

The seams that do exist are earned: `Reporter` is a Protocol with two implementations on day one and
a third (`DbReporter`) once the service arrived, `fetch()` returns `FetchResult | FetchError`
because a 404 is data rather than an exception, and constructor injection (fetcher, reporter,
config, stats, robots loader, extractor, sleep) is what lets the integration tests drive the crawler
with a fake site and a collecting reporter, with no monkeypatching and no `unittest.mock`. That same
seam is what let the service reuse the crawler without editing it. The hazard site runs through
`httpx.ASGITransport`; the failure-mode tests (fuse trip and reset, client errors, unreachable seed,
seed chain cap, re-anchoring) hand in `httpx.MockTransport` handlers instead.

The Repository pattern does appear, in `url_crawler_service/repository.py`, because there is now a
database to keep out of the API and the worker. It is one class of query methods, not a layer.

*Rejected:* a `dict[str, LinkExtractor]` registry keyed by host, with the generic extractor as the
fallback. *Reverses if:* a known host needs different extraction, for instance a sitemap-first
extractor for one large site in a multi-domain service. That is a registry and a lookup, added
behind the current `extract` parameter.

</details>

<details>
<summary><strong>Playwright and Scrapy</strong>: both banned by the exercise, and named here because
a reviewer will wonder.</summary>

Scrapy would have supplied the frontier, the scheduler, the dedup filter, the retry middleware and
robots handling that this repository implements by hand, roughly the whole of `crawler.py`,
`frontier.py` and `retry.py`. Playwright would have rendered JavaScript-built navigation, which is
the one class of site this crawler cannot see; it detects and warns instead.

</details>

## Speed

The brief grades speed, so here are the patterns, then numbers and the method behind them.

### Patterns used for speed

- **Async worker pool.** N tasks on one event loop, so waiting on sockets costs nothing
  (`crawler.py`).
- **One pooled keep-alive client.** Every request reuses a connection to the single host
  (`http.py`).
- **Producer-consumer frontier, no level barrier.** A worker starts the next URL the moment it is
  enqueued, instead of waiting for a depth level to finish (`frontier.py`).
- **Dedup before enqueue.** A canonical key that ignores query order gates the queue, and the parser
  deduplicates within a page first, so the same URL is never fetched twice (`urls.py`,
  `parser.py`).
- **Redirects as links.** No double fetch of the target, and no request that leaves the host
  (`crawler.py`).
- **robots.txt fetched once.** One request per run, then in-memory matching per candidate URL
  (`robots.py`).
- **Streaming with an early exit.** The content-type gate rejects a PDF before its body is read, and
  the size cap aborts mid-stream (`fetcher.py`).
- **Bodies dropped after parse.** Only the seen keys and the queue stay resident, so memory does not
  grow with page size (`crawler.py`, `frontier.py`).
- **selectolax on the loop.** The lexbor C parser is fast enough that parsing never blocks the IO it
  follows (`parser.py`).
- **Per-phase timeouts.** Connect and pool are fixed at 5s, read and write take `--timeout`, so a
  slow connect cannot spend the read budget (`http.py`).
- **Full-jitter backoff and Retry-After.** Retries spread out instead of retrying in lockstep
  (`retry.py`).
- **Failure fuse.** A dead host ends the run instead of burning 20 timeouts per worker
  (`crawler.py`).

### Measured

`make bench` starts the same fake site the tests use, behind a loopback `ThreadingHTTPServer` with
`--delay-ms 50` per request to stand in for network round-trip time (without a delay, loopback is so
fast that it hides the concurrency benefit entirely), then runs the real CLI as a subprocess once
per concurrency level over the same page set. Concurrency 1 is the serial baseline.

301 pages (300 generated pages plus the seed's own `/`), 50 links per page, 50ms of server-side
delay per request. Every run reported 301 pages crawled, 301 ok, 0 failed and 0 retries, so the wall
times compare like with like:

```
pages=300 delay_ms=50 links_per_page=50 python=3.12.3
platform=Linux-6.6.114.1-microsoft-standard-WSL2-x86_64-with-glibc2.39
```

| concurrency | wall seconds | pages per second |
| --- | --- | --- |
| 1 | 30.82 | 9.8 |
| 2 | 15.84 | 19.0 |
| 5 | 6.47 | 46.5 |
| 10 | 3.54 | 85.0 |
| 20 | 1.93 | 156.0 |

Reproduce with `make bench`, or `uv run python scripts/bench.py --pages 300 --delay-ms 50`.

Reading the curve: the speedup stays close to linear all the way to 20 workers, 16x throughput for
20x the workers, which is what you expect when every worker spends its time idle on a 50ms delay.
Diminishing returns start after that. A sixth measurement at concurrency 40, outside the default
sweep, ran in 1.45s (208 pages/s), so doubling the workers past 20 bought 1.3x rather than 1.8x. Two
consecutive full sweeps agreed to within 3% at every level.

So the default of 10 is not the throughput knee. It is a politeness choice. This fake server has no
rate limit and one thread per connection, so it absorbs whatever you throw at it; a real host has
neither property, browsers cap HTTP/1.1 at 6 connections per host, and 10 is already assertive
against one site. `--concurrency 20` is there for a target you own or have permission to hammer, and
it does deliver roughly twice the throughput.

Two caveats, stated rather than hidden. This is a loopback benchmark with a synthetic per-request
delay, so it measures the crawler's concurrency, not real internet latency, bandwidth or a server
that pushes back. And it is one machine; treat the shape of the curve as the result, not the third
significant figure.

One real-network data point, from the same machine against `crawler-test.com`, the site
`tests/smoke/test_live.py` uses (`make smoke`). Only the stderr summary is shown; the pages went to
stdout:

```
$ uv run url-crawler https://crawler-test.com --max-pages 5 --ignore-robots --timeout 15 > /dev/null
WARNING url_crawler.crawler: stopped at --max-pages 5; 403 URLs left unvisited
Crawled 5 pages (5 ok, 0 failed) and found 415 links in 1.2s (4.3 pages/s); 0 retries, 5 duplicate URLs skipped
```

4.3 pages/s at the default concurrency, 4.7 on a repeat run. Five pages is too small a sample to set
against the table above, and the seed alone carries 411 of those 415 links, so internet latency
rather than the crawler sets that pace. It is here as evidence the tool works against a real site,
not as a throughput number. The `--ignore-robots` flag is explained in the testing table.

## Testing

Six layers plus the tests that keep the fixtures honest, all deterministic, with no external network
in the default run. `make test` runs 483 tests; 68 of them need a Postgres and skip without one, and
a single network smoke test is deselected unless you ask for it.

```bash
make test                                  # everything except the network smoke test
make cov                                   # the same with a coverage report
uv run pytest tests/unit -q                # one layer
uv run pytest tests/integration/test_cli.py -q
make db-up && make test-service            # the service layer, against a real Postgres
make smoke                                 # opt-in, hits the network
make lint                                  # ruff check and ruff format --check
make types                                 # mypy --strict over src/ and tests/
make check                                 # lint, types, test
```

<details>
<summary>What each layer covers, and why the fake site is the reason to trust the suite.</summary>

| Layer | Where | What it covers |
| --- | --- | --- |
| Unit, pure | `tests/unit/test_urls.py`, `test_scope.py`, `test_retry.py`, `test_parser.py`, `test_frontier.py`, `test_reporting.py`, `test_config.py`, `test_cli_args.py`, `test_progress.py`, `test_http.py`, `test_settings.py`, `test_schemas.py`, `test_models.py`, `test_service_models.py`, `test_db_reporter.py` | Parametrized tables for normalization, scope near-misses, retry classification, `Retry-After`, jitter bounds with a seeded rng, extraction from saved HTML fixtures, dedup, golden output, progress and banner formatting, the client factory, service settings and request validation, and `DbReporter` batching and retrying against a fake repository |
| Test infrastructure | `tests/unit/test_fakesite.py`, `test_bench_smoke.py` | The fixtures themselves: the fake site's HTML root, its 500-then-200 flaky route, 404, PDF content type and redirect `Location`, query strings ignored for routing, off-host requests recorded as absolute URLs, the `EXPECTED_CRAWLED` and `NEVER_REQUESTED` sets kept consistent, the loopback server answering real GETs over one keep-alive connection, and the benchmark harness returning one row per concurrency level |
| HTTP layer, mocked transport | `tests/unit/test_fetcher.py`, `test_robots.py` | `httpx.MockTransport` handlers: 500 then 200 with an asserted call count, 404 with no retry, 429 with `Retry-After`, three timeouts, PDF rejected without reading the body, oversize by header and mid-stream, 3xx returning `Location`, robots.txt failing open on 404, connect error and undecodable body |
| Integration, in-process | `tests/integration/test_crawl.py` | The crawler against an ASGI fake site through `httpx.ASGITransport`: the exact set of crawled paths, exactly-once fetching, subdomain and external links printed but never requested, redirect chain, redirect cycle, off-host redirect, 404, 500-then-200, `<base href>`, malformed HTML, worker exception isolated, `--max-pages` drain, fuse trip, robots-blocked path, seed re-anchoring |
| Subprocess, real sockets | `tests/integration/test_cli.py` | The installed CLI against a loopback `ThreadingHTTPServer`: exit codes, stdout purity under `-vv`, JSONL parses and ends with a summary, seed without a scheme, unreachable seed, SIGINT flushing a complete page and exiting 130, closed stdout exiting 0 |
| Service, real Postgres | `tests/service/` | 68 tests marked `postgres`: `SKIP LOCKED` giving two concurrent claimers different crawls, a claim wiping a previous attempt's pages, heartbeat rejecting a stale worker, the reaper requeueing then failing at `MAX_ATTEMPTS`, release on shutdown, an insert refused after another worker takes the lease, cancel of a queued, a running, a finished and an unknown crawl, keyset pagination over 250 rows, the API surface including SSE, a 503 healthz and the shutdown hook, the committed migration matching the ORM and surviving a downgrade, a worker that keeps polling while the database refuses connections, and a crawl posted over the API then run by a real `Worker` against the fake site |
| Smoke, opt-in | `tests/smoke/test_live.py` | One real HTTPS crawl of `crawler-test.com`, capped at 5 pages: exit 0, the seed printed first, no log lines on stdout, the summary on stderr. It passes `--ignore-robots`, because that site's robots.txt carries a `Disallow: //` line which stdlib `robotparser` reads as block-all. Marked `network` and deselected by default |

The fake site in `tests/fakesite/` is shared by the integration layer, the subprocess layer, the
service layer and the benchmark, and it is the reason the suite is worth trusting. One hazard site
packs a cycle, a self-link, a subdomain link, an external link, a 404, a 500-then-200, a two-hop
redirect, a redirect cycle, an off-host redirect, a PDF, a zero-link leaf, malformed HTML, a
`<base href>` page, a `rel="nofollow"` link and a robots-blocked path, served either through
`ASGITransport` in-process or over a real socket. The crawl contract is two frozen sets,
`EXPECTED_CRAWLED` and `NEVER_REQUESTED`. `pytest-timeout` fails any test that hangs, which is how a
termination bug in the worker pool shows up as a red test instead of a stuck CI job.

The service tests run against a real Postgres rather than a fake one, because `SKIP LOCKED` and
`RETURNING` are exactly the behaviour worth testing and neither of them exists in a mock. Each test
truncates `page` and `crawl` first, so they are order-independent.

</details>

CI runs lint, types, the suite on 3.12 and 3.13, the service tests against a Postgres service
container, and a Docker build of both targets that executes `url-crawler --help` and
`alembic heads`. Every job runs the same commands you can run locally.

## Extending to multiple domains

<details>
<summary>The unit of parallelism becomes the host, and the frontier becomes a table rather than a
broker.</summary>

**The unit of parallelism becomes the host, not the URL.** Politeness is per-host, so a worker that
leases a host owns that host's rate limit and can enforce it with an in-process token bucket and
zero coordination. That is the Mercator design (Heydon and Najork, 1999) and it is the change that
matters: `HostScope` becomes an allowlist scope, the frontier becomes one queue per host with
round-robin service so a 100k-page site cannot starve a 10-page one, and robots.txt is cached per
host with a TTL instead of fetched once per run. Everything else in this repository survives,
because the fetcher, the parser and the reporter never knew how many hosts there were.

**Beyond one process, the frontier is the hard part.** A frontier is not a queue: it is a
deduplicating set with a scheduling policy and durable retry state, and message brokers give you the
opposite of all three. So the right primitive is a table, not RabbitMQ. In PostgreSQL, `PRIMARY KEY
(crawl_id, url_hash)` with `ON CONFLICT DO NOTHING` makes the insert itself the "have I seen this?"
check, and workers claim work without contending:

```sql
UPDATE frontier SET state = 'claimed', claimed_at = now()
WHERE url_hash IN (
    SELECT url_hash FROM frontier
    WHERE crawl_id = $1 AND state = 'queued' AND host = $2
    ORDER BY priority, discovered_at
    FOR UPDATE SKIP LOCKED LIMIT $3
) RETURNING url;
```

`SKIP LOCKED` lets N workers claim disjoint batches with no queue and no lock convoy, and a
`claimed_at` timestamp plus a reaper handles a worker that dies mid-page. That is the same pattern
the crawl service already uses one level up, where the unit claimed is a whole crawl rather than a
batch of URLs, so the step from here to there is a third table and a per-host lease, not a rewrite.
Delivery is at-least-once, so page writes are idempotent upserts keyed on `(crawl_id, url_hash)`.
The ceiling on this design is not CPU or database throughput: it is the reputation of your egress
IPs, which is why a real multi-domain crawler ends up caring about proxy pools long before it cares
about sharding.

</details>

## Not implemented, and future work

<details>
<summary>Ranked by value per line of code, plus what is deliberately absent.</summary>

1. **Sitemap seeding.** `/sitemap.xml` and the `Sitemap:` lines in robots.txt would find pages that
   no link reaches. Left out to keep this a pure link-graph traversal, which is what the exercise
   asks for.
2. **A durable frontier, for `--resume` and for service-side restart.** Roughly 120 lines behind the
   existing `Frontier` interface: SQLite in WAL mode for the CLI, a third table for the service. It
   is the same feature seen from two ends, and it is what turns a requeued crawl from a restart into
   a resume.
3. **`--max-depth` and per-path caps for crawl traps.** A calendar with infinite `?date=` links is
   bounded today only by `--max-pages`. A depth field on the frontier item is the first extension if
   a trap shows up in practice.
4. **Per-host leases across workers.** Two service workers can be handed two crawls of the same host
   and will both crawl it. A lease row keyed by host, taken at claim time, is the fix.
5. **A measured HTTP/1.1 against HTTP/2 comparison.** `h2` is pure Python and the win is unmeasured,
   so enabling it would be a README claim without numbers.
6. **`--include-assets`.** Reporting `img`, `script` and `link` targets alongside anchors, as a
   separate list, for anyone auditing subresources.
7. **Content dedup by body hash, and `If-Modified-Since` or `ETag` conditional requests.** Both pay
   off on repeat crawls, and the service is the first place there are repeat crawls to pay off on.
8. **Authentication, quotas and an audit trail on the API.** The multi-tenancy axis under
   [Crawl service](#crawl-service) is described and not built.

Deliberately absent, with the reasoning in the decisions above: a web UI, Celery, RabbitMQ, Redis, a
circuit breaker, AIMD rate control, site-specific parsers, Strategy, Factory, a pipeline framework,
a separate scheduler, Prometheus, Sentry, structlog, Typer, uvloop, IDNA handling for non-ASCII
hostnames, a CHANGELOG and issue templates.

</details>

## Tooling and AI disclosure

<details>
<summary>The tooling, the sources consulted, what AI drafted, and how its output was
verified.</summary>

The exercise asks for this in detail, so it is a section rather than a sentence.

### IDE and local tooling

Editor: Visual Studio Code, used as an editor only; every AI interaction ran through Claude Code in
its integrated terminal. Python 3.12 pinned by `.python-version`, `uv` for dependency resolution
with the committed `uv.lock`, `ruff` for lint and format, `mypy --strict` over `src/` and `tests/`,
`pre-commit` running ruff and mypy on every commit, `make check` as the local gate and GitHub
Actions as the remote one. The service adds Docker Compose for a local Postgres and Alembic for the
schema, with migrations autogenerated and never hand-edited.

### Learning the subject

Sources consulted during the design discussion and while implementing, each named with the
question it answered.

- httpx documentation, on transports, redirects and timeouts: how `follow_redirects=False` interacts
  with `Location`, what `Limits` bounds, and why the four timeout phases have to be set separately.
- The httpx exception hierarchy in its source: which exceptions are worth retrying, and the fact
  that `UnsupportedProtocol` and `LocalProtocolError` sit under `TransportError` next to the
  retryable leaves.
- selectolax and lexbor documentation: how a real HTML5 parser recovers from malformed markup, and
  whether adoption-agency reconstruction can duplicate an anchor across a block boundary (the
  `unclosed_anchor.html` fixture exists to pin that behaviour down rather than guess it).
- RFC 9110 section 10.2.3, on `Retry-After`: both accepted forms, delta-seconds and HTTP-date.
- RFC 9309, on robots.txt: matching rules, and what a fetch failure is allowed to mean.
- Heydon and Najork, "Mercator: A Scalable, Extensible Web Crawler" (1999): the per-host frontier
  argument in the multi-domain section.
- PostgreSQL documentation on `FOR UPDATE SKIP LOCKED`: the claim query used by the crawl service
  and the batch claim sketched in the multi-domain section.
- SQLAlchemy 2.0 and Alembic documentation, on async engines and autogenerate: how `run_sync` drives
  a migration on an async driver, and what `alembic check` compares.

### Interactive AI

The interactive AI in this project was Claude Code in a terminal, used conversationally throughout:
design discussion first, then implementation, then this README. It is described in the next section
because it is also where the multi-agent workflow lived.

No editor assistant was used. There was no Copilot and no inline completion; the terminal session
described below was the only AI in the workflow.

### Other AI tools

The code and this README were produced with Claude Code in a multi-agent workflow that I directed:
architect agents proposed candidate architectures, critic agents attacked them, judge agents scored
the comparison, writer agents then implemented one module each against a shared interface contract,
and reviewer agents checked the result. The design document came first and was settled before any
code was written. I reviewed the design document and every decision in the design section above
before any code was written, and I approved the interface contract the writer agents worked from.
The crawl service went through the same loop a second time, with its own design document and its own
contract.

Two places where the process caught itself, which is the part worth reporting. An early pass
proposed classifying retryable errors with `isinstance(exc, httpx.TransportError)`; the critic pass
caught that `UnsupportedProtocol` and `LocalProtocolError` are also `TransportError` subclasses, so
that check would have retried caller bugs. The code enumerates the retryable leaves instead, and
`tests/unit/test_retry.py` pins it. The same panel also wrote concrete dependency pins from memory,
two of which the critic flagged as likely stale; those were thrown out and the pins came from
`uv lock`.

### What AI drafted and what was written by hand

Claude Code drafted every file here: the packages, the tests, the fake site, the benchmark and this
README. My contribution was direction and judgement rather than typing: framing the exercise,
picking one of three candidate architectures, settling the three interpretation calls at the top of
this README, and accepting or rejecting each design decision on its merits. I read the design
document and this README closely and spot-checked the code and tests rather than reading every line;
the test suite, ruff and mypy stood in for a line-by-line review. A reviewer grading this section on
candour should read it as "AI wrote the lines, I chose what the lines should say", not as a claim
that any particular module was typed by hand.

### How AI output was verified

Nothing here is claimed on the strength of a model having said it.

- Behaviour is asserted by the test suite: 478 tests across the layers listed under Testing, of
  which 64 need a Postgres (`make test`, then `make db-up && make test-service`), plus one network
  smoke test deselected by default. The crawl contract, the retry table, the scope near-misses, the
  exit codes and the claim, heartbeat and reaper transitions are all test-enforced rather than
  described.
- Dependency versions come from `uv lock` and are committed, not written from memory.
- The httpx exception hierarchy was checked against the library, which is how the `TransportError`
  mistake was caught.
- The speed table is a measured `make bench` run on the machine named in it, repeated once to check
  it reproduced, with the concurrency-40 data point added by hand to find where the curve bends.
- The database schema is not described from the ORM by hand: `alembic check` runs in the service
  suite and fails if the committed migration and the models disagree.
- `ruff check`, `ruff format --check` and `mypy --strict` run over `src/` and `tests/`, locally
  through pre-commit and once per CI run in the `lint` and `types` jobs. The test suite is the job
  that runs on both 3.12 and 3.13.
- The unclosed-anchor parser fixture exists precisely because the behaviour was an inference: the
  test records what lexbor actually does.

</details>

## License

MIT. See `LICENSE`.
