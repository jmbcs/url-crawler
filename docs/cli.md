# CLI reference

Every flag, every exit code, both output formats, and what the CLI writes to stderr.

Getting started is the [CLI walkthrough](../README.md#the-cli-what-the-exercise-asked-for) in the
README: `uv sync`, then `uv run url-crawler <url>`. This page is the reference behind it and does
not repeat it.

## Running it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

- **Docker**: `docker build --target cli -t url-crawler .` then `docker run --rm url-crawler https://example.com`.
- **Without uv**: `pip install .` then `url-crawler https://example.com`, or `python -m url_crawler <url>`.
- **The service extra**: `pip install '.[service]'` adds the crawl service, covered in [service.md](service.md).
- **Windows**: no asyncio signal handlers, `KeyboardInterrupt` handles Ctrl-C, same exit code 130.

## Flags

| Flag | Default | What it does |
| --- | --- | --- |
| `url` | required | Seed URL. A missing scheme defaults to `https://`; anything but http(s) is a usage error. |
| `--concurrency N` | `10` | Worker tasks, and the only bound on requests in flight. |
| `--timeout SECONDS` | `10.0` | Read and write timeout, so it bounds the gap between chunks. Connect and pool timeouts are fixed at 5s. |
| `--max-pages N` | unlimited | Stop after N pages and log how many URLs were left unvisited. |
| `--max-bytes BYTES` | `5000000` | Skip a page whose body exceeds this, by `Content-Length`, by the compressed stream, or by the decoded one. It bounds memory, not just download size: a small gzip that expands to a gigabyte is rejected while it inflates. |
| `--request-budget SECONDS` | `60.0` | Total time for one request including the body. Exceeding it counts as a timeout and is retried like one. Must be above 0 and at least `--timeout`. |
| `--format {text,jsonl}` | `text` | JSONL emits one object per page plus a final summary object. |
| `--ignore-robots` | off | Crawl paths robots.txt disallows. |
| `--quiet` | off | Suppress the start banner and the progress line. The summary still prints. |
| `-v`, `-vv` | quiet | `-v` logs at INFO, `-vv` at DEBUG (including every link printed but not followed). |
| `--version` | | Print the version and exit. |

No env vars, no config file: flags are the whole configuration surface. The service is configured
differently; see [service.md](service.md).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The crawl finished, or stopped cleanly at `--max-pages`. |
| `1` | Unexpected internal error, logged with a traceback. |
| `2` | Usage error: bad flag value, unsupported scheme, or an unparseable seed URL. |
| `3` | The seed could not be fetched, its redirect chain loops or runs past five hops, robots.txt disallows it, or robots.txt could not be read (see [design decisions](design-decisions.md#robots-on-by-default-and-unreadable-means-blocked)). |
| `4` | The failure fuse aborted the crawl after 20 consecutive failures. |
| `130` | Interrupted by SIGINT or SIGTERM. Pages already crawled are flushed and the summary is printed. |

## Text output

A real run against the 20-page fake site the test suite uses, packing every hazard. Page URLs sit
at column zero, links indented two spaces, and a blank line closes each page.

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

- External and subdomain links (`external.test`, `sub.site.test`) print but are never fetched; a
  robots-blocked path prints but is not followed.
- `/missing` keeps its status, `/redirect` and `/off-site-redirect` show their target, `/file.pdf`
  is rejected on content type before its body loads, and `/leaf` has no links but is not an error.
- Non-http(s) anchors (`mailto:`, `tel:`, `javascript:`, `data:`) and control bytes never appear,
  dropped by normalization.

The summary goes to stderr, so it never pollutes a pipe:

```
Crawled 20 pages (17 ok, 3 failed) and found 26 links in 0.9s (22.1 pages/s); 1 retries, 7 duplicate URLs skipped
```

## JSONL output

`--format jsonl` emits one object per page and a final summary object:

```json
{"url": "http://127.0.0.1:41295/missing", "status": 404, "links": [], "error": {"kind": "http_status", "status": 404, "message": "HTTP 404"}}
{"url": "http://127.0.0.1:41295/redirect", "status": 301, "links": ["http://127.0.0.1:41295/redirected"], "error": null}
{"url": "http://127.0.0.1:41295/file.pdf", "status": 200, "links": [], "error": {"kind": "unsupported_content", "status": 200, "message": "application/pdf"}}
{"summary": {"pages_ok": 17, "pages_failed": {"http_status": 2, "unsupported_content": 1}, "pages_without_links": 5, "redirects": 4, "links_found": 26, "duplicates_dropped": 7, "retries": 1, "pages_total": 20, "elapsed_seconds": 1.422}}
```

## Banner and progress line

- Two things print to stderr in a terminal: a three-line start banner, and a one-line progress
  counter redrawing in place about three times a second. `--quiet` turns both off.
- `-v` also prints the banner in a pipe, for context; the progress line never does, so a redirect or
  a CI log stays free of `\r`. Stdout carries only page results, in every case.

```
url-crawler 0.2.0: crawling https://example.com with 10 workers (robots.txt on, text output)
Results stream to stdout as pages complete. Ctrl-C stops and keeps what was crawled.
Long or unattended crawl? Run it as a job with url-crawler-api and url-crawler-worker; see README, "Crawl service".
pages 143 (2 failed) | queued 512 | links 3,904 | 48.1 pages/s | 3.0s
```

---

[Back to README](../README.md)
