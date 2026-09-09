# Performance

The patterns that make the crawl fast, the measured benchmark behind the numbers, and the caveats on
reading them.

## Patterns used for speed

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
- **Streaming with an early exit.** The content-type gate rejects a PDF before its body is read, the
  size cap aborts mid-stream, and a compressed body is inflated incrementally under the same cap, so
  memory is bounded by `--max-bytes` rather than by what the server chose to send (`fetcher.py`).
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

## Method

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

## Measured

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

## Caveats

Two, stated rather than hidden. This is a loopback benchmark with a synthetic per-request delay, so
it measures the crawler's concurrency, not real internet latency, bandwidth or a server that pushes
back. And it is one machine; treat the shape of the curve as the result, not the third significant
figure.

## One real-network data point

From the same machine against `crawler-test.com`, the site `tests/smoke/test_live.py` uses
(`make smoke`). Only the stderr summary is shown; the pages went to stdout:

```
$ uv run url-crawler https://crawler-test.com --max-pages 5 --ignore-robots --timeout 15 > /dev/null
WARNING url_crawler.crawler: stopped at --max-pages 5; 403 URLs left unvisited
Crawled 5 pages (5 ok, 0 failed) and found 415 links in 1.2s (4.3 pages/s); 0 retries, 5 duplicate URLs skipped
```

4.3 pages/s at the default concurrency, 4.7 on a repeat run. Five pages is too small a sample to set
against the table above, and the seed alone carries 411 of those 415 links, so internet latency
rather than the crawler sets that pace. It is here as evidence the tool works against a real site,
not as a throughput number. Why the run passes `--ignore-robots` is explained in the smoke-test row
of [testing.md](testing.md).

---

[Back to README](../README.md)
