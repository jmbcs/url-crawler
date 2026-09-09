# CLI reference

Every flag, every exit code, both output formats, and what the CLI writes to stderr.

## Running it

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

`python -m url_crawler <url>` works the same as the `url-crawler` script.

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
environment variables instead; see [service.md](service.md).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The crawl finished, or stopped cleanly at `--max-pages`. |
| `1` | Unexpected internal error, logged with a traceback. |
| `2` | Usage error: bad flag value, unsupported scheme, or an unparseable seed URL. |
| `3` | The seed could not be fetched, or robots.txt disallows it. |
| `4` | The failure fuse aborted the crawl after 20 consecutive failures. |
| `130` | Interrupted by SIGINT or SIGTERM. Pages already crawled are flushed and the summary is printed. |

## Text output

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

## JSONL output

`--format jsonl` emits one object per page and a final summary object:

```json
{"url": "http://127.0.0.1:41295/missing", "status": 404, "links": [], "error": {"kind": "http_status", "status": 404, "message": "HTTP 404"}}
{"url": "http://127.0.0.1:41295/redirect", "status": 301, "links": ["http://127.0.0.1:41295/redirected"], "error": null}
{"url": "http://127.0.0.1:41295/file.pdf", "status": 200, "links": [], "error": {"kind": "unsupported_content", "status": 200, "message": "application/pdf"}}
{"summary": {"pages_ok": 17, "pages_failed": {"http_status": 2, "unsupported_content": 1}, "pages_without_links": 5, "redirects": 4, "links_found": 26, "duplicates_dropped": 7, "retries": 1, "pages_total": 20, "elapsed_seconds": 1.422}}
```

## Banner and progress line

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

---

[Back to README](../README.md)
