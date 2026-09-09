# Testing

The test layers, how to run each of them, the fake site they share, and what CI runs.

Six layers plus the tests that keep the fixtures honest, all deterministic, with no external network
in the default run. `make test` collects 676 tests; 80 of them need a Postgres and skip without one,
and a single network smoke test is deselected unless you ask for it. With a Postgres, all 676 pass.

## Commands

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

## Layers

| Layer | Where | What it covers |
| --- | --- | --- |
| Unit, pure | `tests/unit/test_urls.py`, `test_scope.py`, `test_retry.py`, `test_parser.py`, `test_frontier.py`, `test_reporting.py`, `test_config.py`, `test_cli_args.py`, `test_progress.py`, `test_http.py`, `test_settings.py`, `test_schemas.py`, `test_models.py`, `test_service_models.py`, `test_db_reporter.py`, `test_hostcheck.py`, `test_service_entrypoints.py` | Parametrized tables for normalization (userinfo, dot segments, percent-encoding folding, query ordering), scope near-misses, retry classification, `Retry-After`, jitter bounds with a seeded rng, extraction from saved HTML fixtures, dedup, golden output, progress and banner formatting, the client factory, service settings and request validation, `DbReporter` batching and retrying against a fake repository, the seed host guard against a fake resolver, and both service entry points reporting a missing `service` extra |
| Test infrastructure | `tests/unit/test_fakesite.py`, `test_bench_smoke.py` | The fixtures themselves: the fake site's HTML root, its 500-then-200 flaky route, 404, PDF content type and redirect `Location`, query strings ignored for routing, off-host requests recorded as absolute URLs, the `EXPECTED_CRAWLED` and `NEVER_REQUESTED` sets kept consistent, the loopback server answering real GETs over one keep-alive connection, and the benchmark harness returning one row per concurrency level |
| HTTP layer, mocked transport | `tests/unit/test_fetcher.py`, `test_robots.py` | `httpx.MockTransport` handlers: 500 then 200 with an asserted call count, 404 with no retry, 429 with `Retry-After`, three timeouts, a decoding error reported as `protocol` without a retry, the request budget expiring, PDF rejected without reading the body, oversize by header and mid-stream, 3xx returning `Location`, the RFC 9309 group selection (the longest matching agent token, consecutive `User-agent` lines sharing one group, the `*` fallback) and matching table (wildcards, a trailing `$`, longest-match precedence, `Allow` ties, an empty `Disallow:`, percent-escapes), robots.txt allowing everything on a 404, following its redirects, a guard refusing a redirect hop, and denying everything on a 5xx, a connect error or an undecodable body. The compression cases run against a loopback HTTP server instead, so chunk boundaries are real socket reads: a gzip bomb rejected without inflating past the cap, a body larger than a lying `Content-Length`, gzip and both deflate framings decoded under the cap, a corrupt and an unsupported `Content-Encoding` reported as `protocol`, a gzipped robots.txt, and the client advertising only the encodings it can inflate |
| Integration, in-process | `tests/integration/test_crawl.py` | The crawler against an ASGI fake site through `httpx.ASGITransport`: the exact set of crawled paths, exactly-once fetching, subdomain and external links printed but never requested, redirect chain, redirect cycle, off-host redirect, 404, 500-then-200, `<base href>`, malformed HTML, worker exception isolated, `--max-pages` drain, fuse trip, robots-blocked path, seed re-anchoring, a cyclic seed redirect raising instead of looping, the `Crawl-delay` warning logged once rather than per page, its sleep serialised across workers, and concurrency capping the requests in flight |
| Subprocess, real sockets | `tests/integration/test_cli.py` | The installed CLI against a loopback `ThreadingHTTPServer`: exit codes, stdout purity under `-vv`, JSONL parses and ends with a summary, seed without a scheme, unreachable seed, SIGINT flushing a complete page and exiting 130, closed stdout exiting 0, the failure fuse exiting 4 and naming its reason on stderr |
| Service, real Postgres | `tests/service/` | 84 tests, all marked `postgres`: `SKIP LOCKED` giving two concurrent claimers different crawls, a claim wiping the previous attempt's pages, a re-sent insert batch ignored rather than rejected, heartbeat rejecting a stale worker, the reaper requeueing then failing at `MAX_ATTEMPTS`, release on shutdown giving the attempt back so repeated handoffs never reach the limit, an insert refused after another worker takes the lease, cancel of a queued, a running, a finished and an unknown crawl, keyset pagination over 250 rows, the API surface including SSE, a 503 healthz and the shutdown hook, the committed migration matching the ORM and surviving a downgrade, a worker that keeps polling while the database refuses connections, one that gives up on a heartbeat outlasting the lease, one that retries the final write and then gives up on it, the fetcher receiving the request budget, a robots.txt redirect into the private network refused, and a crawl posted over the API then run by a real `Worker` against the fake site |
| Smoke, opt-in | `tests/smoke/test_live.py` | One real HTTPS crawl of `crawler-test.com`, capped at 5 pages: exit 0, the seed printed first, no log lines on stdout, the summary on stderr. It passes `--ignore-robots` to keep the one test that touches the internet independent of a file this repository does not control: a third party editing `crawler-test.com/robots.txt` must not turn this red. The flag arrived for a narrower reason, which no longer holds. That site carries a `Disallow: //` line, and the stdlib matcher the crawler used to rely on read it as block-all; the RFC 9309 matcher reads it as blocking only paths that start with two slashes. The comment in `test_live.py` still gives the old reason. Marked `network` and deselected by default |

## The fake site

The fake site in `tests/fakesite/` is shared by the integration layer, the subprocess layer, the
service layer and the benchmark, and it is the reason the suite is worth trusting. One hazard site
packs a cycle, a self-link, a subdomain link, an external link, a 404, a 500-then-200, a two-hop
redirect, a redirect cycle, an off-host redirect, a PDF, a zero-link leaf, malformed HTML, a
`<base href>` page, a `rel="nofollow"` link and a robots-blocked path, served either through
`ASGITransport` in-process or over a real socket. The crawl contract is two frozen sets,
`EXPECTED_CRAWLED` and `NEVER_REQUESTED`. `pytest-timeout` fails any test that hangs, which is how a
termination bug in the worker pool shows up as a red test instead of a stuck CI job.

Serve it yourself to try the CLI without touching a real website:

```bash
uv run python -m tests.fakesite.server --port 8765          # the 20-page hazard site
uv run python -m tests.fakesite.server --port 8765 --pages 30   # a generated site instead
```

## Why the service tests use a real Postgres

`SKIP LOCKED` and `RETURNING` are exactly the behaviour worth testing and neither of them exists in
a mock. All 84 are marked `postgres`, and 80 of them skip when `URL_CRAWLER_TEST_DATABASE_URL` is
unset, so `make test` stays offline. The four that still run supply their own broken database on
purpose: a 503 `healthz`, a missing `DATABASE_URL`, the api binding to its configured address, and a
worker that keeps polling while nothing answers. `.env.example` sets that variable to the
`crawler_test` DSN, kept separate from `DATABASE_URL` so the compose worker and the test suite never
fight over the same rows. Each test truncates `page` and `crawl` first, so they are
order-independent.

## Coverage

`make cov` runs the suite with a report and fails under 75%. Offline it measures 82%, with a real
Postgres 97%. The gap is not untested code: `api.py`, `repository.py` and `worker.py` are exercised
by the service layer, which skips when there is no database, so an offline run reads them as 59%,
38% and 32%. The floor sits below the offline number so `make cov` runs without Docker, and the
`service` CI job runs the same tests against a Postgres container on every push.

Coverage now follows the subprocess tests into the child process. `[tool.coverage.run] patch =
["subprocess"]` makes `coverage` instrument the CLI the subprocess layer launches, so the lines that
only a real `python -m url_crawler` run reaches are counted instead of read as dead.

## CI

`.github/workflows/ci.yml` runs six jobs on every push and pull request to `main`.

| Job | What it runs |
| --- | --- |
| `lint` | `make lint`: `ruff check` and `ruff format --check` |
| `types` | `make types`: `mypy --strict` over `src/` and `tests/` |
| `test` | `make cov` on Python 3.12 and 3.13, which fails the build under 75% coverage |
| `service` | the service tests against a Postgres service container |
| `docker` | builds both Docker targets and runs `url-crawler --help` and `alembic heads` |
| `smoke` | `make smoke`, on manual dispatch only |

Every job runs the same commands you can run locally. `pre-commit` runs ruff and mypy before each
commit.

---

[Back to README](../README.md)
