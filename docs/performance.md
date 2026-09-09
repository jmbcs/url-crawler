# Performance

The patterns that make the crawl fast, the measured benchmark behind the numbers, and the caveats
on reading them.

## Patterns used for speed

- **Async worker pool.** N tasks on one event loop, so waiting on sockets costs nothing (`crawler.py`).
- **Pooled keep-alive client.** Every request reuses a connection to the single host (`http.py`).
- **No level barrier.** A worker starts the next URL the moment it is enqueued (`frontier.py`).
- **Dedup before enqueue.** A canonical key ignoring query order gates the queue (`urls.py`, `parser.py`).
- **Redirects as links.** No double fetch of the target, and no request that leaves the host (`crawler.py`).
- **robots.txt fetched once.** One request per run, then in-memory matching per URL (`robots.py`).
- **Streaming with an early exit.** Content type and size are checked before the body fully loads (`fetcher.py`).
- **Bodies dropped after parse.** Only the seen keys and the queue stay resident (`crawler.py`, `frontier.py`).
- **selectolax on the loop.** The lexbor C parser is fast enough to never block the IO it follows (`parser.py`).
- **Per-phase timeouts.** Connect and pool are fixed at 5s; read and write take `--timeout` (`http.py`).
- **Full-jitter backoff and Retry-After.** Retries spread out instead of retrying in lockstep (`retry.py`).
- **Failure fuse.** A dead host ends the run instead of burning 20 timeouts per worker (`crawler.py`).

## Measured

301 pages (300 generated plus the seed's own `/`), 50 links per page, 50ms of server-side delay per
request. Every run reported 301 pages crawled, 301 ok, 0 failed, 0 retries. Run on Python 3.12.3,
Linux-6.6.114.1-microsoft-standard-WSL2-x86_64.

| concurrency | wall seconds | pages per second |
| --- | --- | --- |
| 1 | 30.82 | 9.8 |
| 2 | 15.84 | 19.0 |
| 5 | 6.47 | 46.5 |
| 10 | 3.54 | 85.0 |
| 20 | 1.93 | 156.0 |

Reproduce with `make bench`, or `uv run python scripts/bench.py --pages 300 --delay-ms 50`.

## Method

- `make bench` starts the fake site behind a loopback `ThreadingHTTPServer` with `--delay-ms 50`,
  standing in for round-trip time; loopback with no delay is too fast to show any concurrency gain.
- It then runs the real CLI as a subprocess, once per concurrency level, over the same page set.
  Concurrency 1 is the serial baseline.
- The curve stays close to linear to 20 workers (16x throughput for 20x the workers), matching a
  worker that spends its time idle on a 50ms delay. A concurrency-40 run (outside the default sweep)
  hit 1.45s (208 pages/s), only 1.3x for double the workers, so returns diminish past 20.
- Two consecutive full sweeps agreed within 3% at every level.
- The default of 10 is a politeness choice, not the throughput knee. This fake server has no rate
  limit and absorbs anything; a real host does not, and browsers cap HTTP/1.1 at 6 connections per
  host. `--concurrency 20` is for a target you own or have permission to hammer.

## Caveats

- This is a loopback benchmark with a synthetic delay: it measures the crawler's concurrency, not
  real internet latency, bandwidth, or a server that pushes back.
- It is one machine; treat the shape of the curve as the result, not the third significant figure.

## One real-network data point

From the same machine against `crawler-test.com`, the site `tests/smoke/test_live.py` uses
(`make smoke`). Only the stderr summary is shown; the pages went to stdout:

```
$ uv run url-crawler https://crawler-test.com --max-pages 5 --ignore-robots --timeout 15 > /dev/null
WARNING url_crawler.crawler: stopped at --max-pages 5; 403 URLs left unvisited
Crawled 5 pages (5 ok, 0 failed) and found 415 links in 1.2s (4.3 pages/s); 0 retries, 5 duplicate URLs skipped
```

- 4.3 pages/s here, 4.7 on a repeat run. Five pages is too small to set against the table above, and
  the seed alone carries 411 of the 415 links, so internet latency sets the pace, not the crawler.
- This is evidence the tool works against a real site, not a throughput number. Why the run passes
  `--ignore-robots` is explained in the smoke-test row of [testing.md](testing.md).

---

[Back to README](../README.md)
