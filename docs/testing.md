# Testing

The test layers, how to run each of them, the fake site they share, and what CI runs.

Six layers plus the tests that keep the fixtures honest, all deterministic, with no external network
in the default run. `make test` runs 485 tests; 69 of them need a Postgres and skip without one, and
a single network smoke test is deselected unless you ask for it.

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
| Unit, pure | `tests/unit/test_urls.py`, `test_scope.py`, `test_retry.py`, `test_parser.py`, `test_frontier.py`, `test_reporting.py`, `test_config.py`, `test_cli_args.py`, `test_progress.py`, `test_http.py`, `test_settings.py`, `test_schemas.py`, `test_models.py`, `test_service_models.py`, `test_db_reporter.py` | Parametrized tables for normalization, scope near-misses, retry classification, `Retry-After`, jitter bounds with a seeded rng, extraction from saved HTML fixtures, dedup, golden output, progress and banner formatting, the client factory, service settings and request validation, and `DbReporter` batching and retrying against a fake repository |
| Test infrastructure | `tests/unit/test_fakesite.py`, `test_bench_smoke.py` | The fixtures themselves: the fake site's HTML root, its 500-then-200 flaky route, 404, PDF content type and redirect `Location`, query strings ignored for routing, off-host requests recorded as absolute URLs, the `EXPECTED_CRAWLED` and `NEVER_REQUESTED` sets kept consistent, the loopback server answering real GETs over one keep-alive connection, and the benchmark harness returning one row per concurrency level |
| HTTP layer, mocked transport | `tests/unit/test_fetcher.py`, `test_robots.py` | `httpx.MockTransport` handlers: 500 then 200 with an asserted call count, 404 with no retry, 429 with `Retry-After`, three timeouts, PDF rejected without reading the body, oversize by header and mid-stream, 3xx returning `Location`, robots.txt failing open on 404, connect error and undecodable body |
| Integration, in-process | `tests/integration/test_crawl.py` | The crawler against an ASGI fake site through `httpx.ASGITransport`: the exact set of crawled paths, exactly-once fetching, subdomain and external links printed but never requested, redirect chain, redirect cycle, off-host redirect, 404, 500-then-200, `<base href>`, malformed HTML, worker exception isolated, `--max-pages` drain, fuse trip, robots-blocked path, seed re-anchoring |
| Subprocess, real sockets | `tests/integration/test_cli.py` | The installed CLI against a loopback `ThreadingHTTPServer`: exit codes, stdout purity under `-vv`, JSONL parses and ends with a summary, seed without a scheme, unreachable seed, SIGINT flushing a complete page and exiting 130, closed stdout exiting 0 |
| Service, real Postgres | `tests/service/` | 69 tests marked `postgres`: `SKIP LOCKED` giving two concurrent claimers different crawls, a claim wiping a previous attempt's pages, heartbeat rejecting a stale worker, the reaper requeueing then failing at `MAX_ATTEMPTS`, release on shutdown, an insert refused after another worker takes the lease, cancel of a queued, a running, a finished and an unknown crawl, keyset pagination over 250 rows, the API surface including SSE, a 503 healthz and the shutdown hook, the committed migration matching the ORM and surviving a downgrade, a worker that keeps polling while the database refuses connections, and a crawl posted over the API then run by a real `Worker` against the fake site |
| Smoke, opt-in | `tests/smoke/test_live.py` | One real HTTPS crawl of `crawler-test.com`, capped at 5 pages: exit 0, the seed printed first, no log lines on stdout, the summary on stderr. It passes `--ignore-robots`, because that site's robots.txt carries a `Disallow: //` line which stdlib `robotparser` reads as block-all. Marked `network` and deselected by default |

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
a mock. The tests are marked `postgres` and skip when `URL_CRAWLER_TEST_DATABASE_URL` is unset, so
`make test` stays offline and dependency-free; `.env.example` sets that variable to the
`crawler_test` DSN, kept separate from `DATABASE_URL` so the compose worker and the test suite never
fight over the same rows. Each test truncates `page` and `crawl` first, so they are
order-independent.

## CI

`.github/workflows/ci.yml` runs six jobs on every push and pull request to `main`.

| Job | What it runs |
| --- | --- |
| `lint` | `make lint`: `ruff check` and `ruff format --check` |
| `types` | `make types`: `mypy --strict` over `src/` and `tests/` |
| `test` | `make cov` on Python 3.12 and 3.13 |
| `service` | the service tests against a Postgres service container |
| `docker` | builds both Docker targets and runs `url-crawler --help` and `alembic heads` |
| `smoke` | `make smoke`, on manual dispatch only |

Every job runs the same commands you can run locally. `pre-commit` runs ruff and mypy before each
commit.

---

[Back to README](../README.md)
