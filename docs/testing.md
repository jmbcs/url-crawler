# Testing

The test layers, how to run each one, the fake site they share, and what CI runs.

- Six layers, all deterministic, no external network in the default run.
- `make test` collects 676 tests; 80 need a Postgres and skip without one, one network smoke test
  is deselected unless requested, and all 676 pass with a Postgres running.

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

| Layer | Where | Covers | Run |
| --- | --- | --- | --- |
| Unit, pure | `tests/unit/test_*.py` (17 files) | Normalization, scope, retry, HTML extraction, dedup, golden output, progress/banner formatting, service settings, `DbReporter`, the seed host guard | `uv run pytest tests/unit -q` |
| Test infrastructure | `test_fakesite.py`, `test_bench_smoke.py` | The fake site's routes and fixed crawl sets; the benchmark harness output | part of unit run |
| HTTP, mocked transport | `test_fetcher.py`, `test_robots.py` | Retry, timeout and error classification via `MockTransport`; RFC 9309 group and rule matching; compression over a real loopback socket | part of unit run |
| Integration, in-process | `tests/integration/test_crawl.py` | Full crawl over `httpx.ASGITransport`: crawled set, redirects, cycles, `<base href>`, worker isolation, the failure fuse | `uv run pytest tests/integration/test_crawl.py -q` |
| Subprocess, real sockets | `tests/integration/test_cli.py` | The installed CLI over a loopback server: exit codes, stdout purity, JSONL, SIGINT flushing, the fuse | `uv run pytest tests/integration/test_cli.py -q` |
| Service, real Postgres | `tests/service/` | 84 tests, all marked `postgres`: `SKIP LOCKED` claiming, heartbeats, the reaper, cancellation, keyset pagination, migration parity | `make db-up && make test-service` |
| Smoke, opt-in | `tests/smoke/test_live.py` | One real HTTPS crawl of `crawler-test.com`, capped at 5 pages | `make smoke` |

## The fake site

`tests/fakesite/` is shared by the integration, subprocess and service layers, and the benchmark.

- One hazard site packs a cycle, a self-link, a subdomain and external link, a 404, a 500-then-200,
  a redirect chain, a redirect cycle, an off-host redirect, a PDF, a zero-link leaf, malformed HTML,
  a `<base href>` page, and a robots-blocked path. It serves over `ASGITransport` in-process or over
  a real socket.
- The crawl contract is two frozen sets, `EXPECTED_CRAWLED` and `NEVER_REQUESTED`, and
  `pytest-timeout` fails any test that hangs, catching a worker-pool termination bug as a red test.

Serve it to try the CLI without touching a real website:

```bash
uv run python -m tests.fakesite.server --port 8765          # the 20-page hazard site
uv run python -m tests.fakesite.server --port 8765 --pages 30   # a generated site instead
```

## Why the service tests use a real Postgres

`SKIP LOCKED` and `RETURNING` are the behaviour worth testing, and neither exists in a mock.

- All 84 service tests are marked `postgres`; 80 skip when `URL_CRAWLER_TEST_DATABASE_URL` is unset,
  so `make test` stays offline.
- The four that still run supply their own broken database: a 503 `healthz`, a missing
  `DATABASE_URL`, the API binding to its configured address, and a worker polling a dead database.
- `.env.example` points that variable at the `crawler_test` DSN, kept separate from `DATABASE_URL`
  so the compose worker and the suite never fight over the same rows; each test truncates `page` and
  `crawl` first, so tests stay order-independent.

## Coverage

`make cov` fails under 75%. Offline it measures 82%, with a real Postgres 97%.

- The gap is not untested code: `api.py`, `repository.py` and `worker.py` are exercised by the
  service layer, which skips with no database, reading as 59%, 38% and 32% offline. The floor sits
  below that so `make cov` runs without Docker; the `service` CI job covers the rest on every push.
- `[tool.coverage.run] patch = ["subprocess"]` follows the subprocess tests into the child process,
  so lines only a real `python -m url_crawler` run reaches are counted, not read as dead.

## CI

`.github/workflows/ci.yml` runs six jobs on every push and pull request to `main`.

| Job | What it runs |
| --- | --- |
| `lint` | `make lint`: `ruff check` and `ruff format --check` |
| `types` | `make types`: `mypy --strict` over `src/` and `tests/` |
| `test` | `make cov` on Python 3.12 and 3.13, failing under 75% coverage |
| `service` | the service tests against a Postgres service container |
| `docker` | builds both Docker targets and runs `url-crawler --help` and `alembic heads` |
| `smoke` | `make smoke`, on manual dispatch only |

Every job runs the same commands you can run locally; `pre-commit` runs ruff and mypy before each
commit.

---

[Back to README](../README.md)
