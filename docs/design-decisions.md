# Design decisions

What was chosen, what was rejected, and what would reverse it, decision by decision.
[README](../README.md) indexes the same list one line each.

## asyncio with one client and N workers

`asyncio.TaskGroup` runs N worker tasks over one `httpx.AsyncClient`.

- **Why:** a crawl waits on sockets, so tasks beat threads and share one keep-alive pool.
- **Why:** one thread owns the state, so the seen set and the counters need no lock.
- **Rejected:** a thread pool (a lock or a pool per thread, no gain on IO waits) and a process pool
  (interprocess dedup for a problem that has none).
- **Reverses if:** parsing dominates the profile: a `ProcessPoolExecutor` behind `extract_links`.

## The worker count is the only limit

`httpx.Limits(max_connections=N, max_keepalive_connections=N)` matches the worker count.

- **Why:** no second semaphore, no rps cap. Browsers cap HTTP/1.1 at 6 per host, so 10 is assertive.
- **Why the queue is unbounded:** consumers are producers, so a bound deadlocks once every worker
  blocks on enqueue. The seen set, `--max-pages` and `--max-bytes` bound memory instead.
- **Rejected:** an rps token bucket and AIMD off the error rate. A 429 is per request today:
  `Retry-After` honoured, request retried, no feedback into concurrency.
- **Reverses if:** real targets return repeated 429s. Narrowing concurrency on sustained 429s comes
  first; AIMD belongs in [the multi-host service](extending.md).

## Redirects are never followed by the client

The client runs `follow_redirects=False`, so a 3xx becomes a page whose only link is its `Location`.

- **Why:** `follow_redirects=True` hits an off-host target before any code here can check scope.
- **What it buys:** the target passes through resolve, normalize, scope and dedup like any link, so
  an off-host target prints unrequested, chains show hop by hop, and cycles need no special case.
- **Exception:** the seed's chain is walked eagerly, up to 5 hops, to anchor scope on the real host.
- **Rejected:** following redirects and filtering after, which leaks requests off-host.
- **Reverses if:** scope becomes a multi-host allowlist, where an in-allowlist redirect is safe.

## Exact-host scope, re-anchored after a seed redirect

Case-insensitive `(host, port)` equality, anchored to the **final** host of the seed's chain.

- **Why equality:** `blog.example.com`, `www.example.com`, `notexample.com`, `example.com.evil.tld`
  and `example.com:8080` are all out of scope for a seed of `https://example.com`.
- **Why not suffix:** suffix matching is how a crawler wanders onto `example.com.evil.tld`, and
  registrable domains (`tldextract`) pull in the subdomains the brief excludes.
- **Why re-anchor:** sites redirect apex to `www`, and the typed host would fetch one redirect and
  stop. The re-anchor is logged, and exactly one host is crawled either way.
- **Cost:** the crawled host may be a subdomain of the typed host, an alias the site declared.
- **Rejected:** a wide scope of `{typed, final}` (two hosts, better apex coverage) and no re-anchor
  (stops after one hop on many real sites).
- **Reverses if:** a target has pages reachable only on the apex; the wide scope is two lines.

## One normal form, and a stricter key for dedup

`normalize()` builds the URL requested and printed. `canonical_key()` is the dedup identity, never
printed.

- **normalize:** lowercases scheme and host, drops a default port and the fragment, empty path to
  `/`, resolves dot segments, removes userinfo, rejects over 2048 characters.
- **Why strip userinfo:** credentials identify the caller, not the resource, and stripping keeps a
  password out of the frontier, the output and every log line.
- **Why reject control bytes:** `urlsplit` drops a tab or newline silently, so one href parses two
  ways, and a printed ESC or BEL drives the reader's terminal.
- **canonical_key:** folds percent-escapes, so `/café` and `/caf%C3%A9` are one page.
- **canonical_key:** sorts query names stably, so `?b=1&a=2` folds into `?a=2&b=1` but `?a=2&a=1`
  and `?a=1&a=2` stay two.
- **Rejected:** folding the key into the printed URL (rewrites a URL the site chose) and sorting
  whole `name=value` pairs (merges two orderings of a repeated parameter).
- **Reverses if:** a session id shows up in a query, where the fix is dropping known parameters.

## Robots on by default, and unreadable means blocked

robots.txt is fetched once for the final seed host, parsed here, and checked before a URL enters the
frontier. The seed is the one URL fetched first, since its chain decides whose robots.txt applies.

- **Why fail closed:** a 503 is the one moment a site cannot say what it allows; RFC 9309 says no.
- **Why parse it here:** `urllib.robotparser` is not used at all. It ignores wildcards, picks a
  group by substring rather than longest prefix, and reads internals Python 3.13 rearranged.
- **Crawl-delay:** honoured by serializing a sleep across workers, with one warning naming the delay
  and the pages per hour it implies, so a throttled crawl does not look hung.
- **nofollow:** followed and printed. It is a hint about link equity, not access control.
- **Rejected:** failing open (kinder to the crawler, wrong for the site), robots off by default, and
  honouring `nofollow`, which would hide pages the site never asked to protect.
- **Reverses if:** a flaky robots.txt makes exit 3 usual: retry the fetch rather than fail open.

<details><summary>Detail: the matching rules and what each fetch outcome means</summary>

Matching follows RFC 9309 section 2.2, a behaviour change rather than a tidy-up: a site writing
`Disallow: /*.pdf$` used to be ignored on that line and is now obeyed.

- `*` matches any run of characters, and a trailing `$` anchors the end of the path.
- The longest matching rule wins, whatever order the rules appear in the file.
- `Allow` breaks a tie against a `Disallow` of the same length.
- An empty `Disallow:` allows everything, which is what the standard says it means.
- Percent-escapes are folded on rule and URL alike, so `/caf%C3%A9` and `/café` match.

| Fetch outcome | Policy |
| --- | --- |
| 200 | Parse the body and apply it. |
| 3xx with a `Location` | Followed up to five hops, to another host if the header says so, because a site may serve the file from a CDN. |
| Any other status below 500, a 404 above all | The site has no robots.txt, so every path is allowed. Logged at INFO. |
| 5xx, a connection or transport error, a redirect with no `Location`, over five hops, or a body that is not UTF-8 | Complete disallow, per RFC 9309 section 2.3.1. |

A complete disallow ends the run with exit code 3 and a message naming `--ignore-robots`, and so
does a readable robots.txt that disallows the seed. `load_robots` takes an optional guard, awaited
on the robots URL and every redirect target; a refusal takes the same complete-disallow path.

</details>

## Retry classification by leaf type

`retry_delay()` is a pure function of status, exception, attempt and headers, so the whole policy is
a table test that runs in microseconds.

- **Retryable:** statuses 408, 425, 429, 500, 502, 503, 504; exceptions `httpx.TimeoutException`,
  `httpx.NetworkError`, `httpx.RemoteProtocolError`.
- **Why leaves:** `isinstance(exc, httpx.TransportError)` also catches `UnsupportedProtocol` and
  `LocalProtocolError`, caller bugs no retry can fix.
- **Budget:** three attempts, full jitter `uniform(0, min(8, 0.5 * 2**attempt))`, `Retry-After` on
  429 and 503 in delta-seconds or HTTP-date form, capped at 30 seconds.
- **Rejected:** Tenacity (a dependency, a decorator, and `Retry-After` still needs custom code) and
  retrying by exception base class.
- **Reverses if:** retry has to differ per host, where a policy object beats a function.

<details><summary>Detail: DecodingError, and what bounds memory</summary>

- `httpx.DecodingError` is deliberately not retryable: the same bytes decode the same way twice. The
  fetcher catches it by name, so broken gzip counts a `protocol` failure rather than `internal`.
- `--max-bytes` bounds memory, not only download size: the body is read undecoded and inflated
  incrementally under the cap, so a 4 KB gzip expanding to a gigabyte dies mid-inflation.
- The client pins `accept-encoding: gzip, deflate`, so brotli or zstd cannot appear if either
  library becomes importable. An unhandled encoding or a corrupt body is a `protocol` error.
- `sleep` and `random.Random` are injected, so the retry tests do not sleep. The two request clocks
  are in [cli.md](cli.md): `--timeout` per chunk gap, `--request-budget` for the whole request.

</details>

## No circuit breaker, but a failure fuse

20 consecutive failures that look like the host is down abort the run with exit code 4.

- **Why:** a breaker protects a long-lived caller from a flapping dependency and probes for
  recovery. This is one bounded batch against one host, so "open" only means "stop the crawl".
- **What counts:** timeouts, connection errors, protocol errors, 5xx. Any success resets it, and
  4xx never counts: a wall of 404s is dead links, not an outage.
- **Rejected:** a full breaker with half-open probing, and per-host AIMD decay.
- **Reverses if:** it becomes a long-running service over many hosts, where a per-host breaker pays.

## A page with no links is a leaf

HTML with no anchors prints an empty link list and counts as a success.

- **Why:** a link-less page is valid, not an error, and an empty or undecodable body raises nothing.
- **Not the same as:** non-HTML, rejected on `Content-Type` as `unsupported_content`, body unread.
- **Aggregate check:** 50+ pages with over 90% link-less logs one JavaScript-rendering warning.
- **Rejected:** treating a link-less page as an error, and rendering JavaScript, which the exercise
  bans; that warning is what this tool offers in its place.
- **Reverses if:** JavaScript-rendered targets become the norm, where the fix is a rendering service
  behind the fetcher interface, not a change to the crawl loop.

## Postgres in the service, and no Celery or Redis

The service keeps crawls, pages and its queue in Postgres and nothing else. The CLI stores nothing.

- **Why nothing in the CLI:** its frontier is an `asyncio.Queue` plus a `set`, and nothing reads
  crawl data back. The durable artifact is `--format jsonl`, and `jq` is the query interface.
- **One task type:** Celery buys routing, chords and a result backend for one task, "run this
  crawl". What is needed is a claim, a lease and a retry count: three columns.
- **Crawls arrive per hour, not per second:** `SELECT ... FOR UPDATE SKIP LOCKED` over tens of rows
  costs nothing, and a one-second poll is a fine scheduler at that rate.
- **A frontier is a set, not a queue:** in the database the primary key is the dedup check and
  `ON CONFLICT DO NOTHING` is the insert. A broker offers no dedup, no scan, no scheduling.
- **One store beats two:** the worker writes its last page batch before the terminal state, so a
  failed flush leaves the crawl `failed` rather than `finished` with pages missing.
- **Rejected:** Celery with Redis or RabbitMQ, and a Redis-only job store.
- **Reverses if:** replicas need a shared cache or rate limiter, or a worker runs several crawls and
  wants a per-host lease that `SETNX` with a TTL expresses better than a row. Sub-second dispatch
  would too, where `LISTEN`/`NOTIFY` comes before a broker.

<details><summary>Detail: the at-least-once cost of a heartbeat lease</summary>

A worker that crawls a site and then cannot write the result retries that write for a lease; if it
still fails, the reaper requeues the crawl and another worker crawls the site again. Exactly-once
would need the crawl and its terminal write in one transaction, which no HTTP crawl shares with a
database.

The design pays on the read side instead: every claim wipes the crawl's pages, every insert is
fenced by the lease, and inserts are idempotent, so stored pages always come from a single attempt.
[service.md](service.md#crawls-run-at-least-once) states the property for API consumers.

</details>

## An HTTP API, no UI

The service exposes JSON and server-sent events, and stops there.

- **Why:** a UI would be a second client of the same endpoints, showing a reviewer nothing new.
- **Pagination:** a keyset cursor over `(crawl_id, seq)`, so the cursor is the last row you saw. One
  page's links stay in one response item, and the CLI streams and lets `jq` page it instead.
- **Why not `OFFSET`:** rows keep arriving during a crawl and shift every later page. Client-side
  paging is worse, since shipping the whole result set defeats the point.
- **Rejected:** a small HTMX UI.
- **Reverses if:** someone who does not use `curl` has to watch a crawl, where the UI is a static
  page over the existing `/crawls` and `/events` endpoints and adds no server-side code.

## The service guards its seed host, the CLI does not

The service resolves the seed host before queueing anything and refuses whatever points inside the
network it runs in; [service.md](service.md#the-seed-host-guard) lists what counts as private.

- **Why guard the API:** anyone who can post to it borrows the network its worker sits in.
  `POST /crawls` answers 422 with `{"detail": {"seed": "<reason>"}}`.
- **Checked again in the worker:** before the first seed fetch, before every hop of the seed's
  chain, and on robots.txt and its redirects, so a public host redirecting to the metadata address
  is caught at the hop. A rejected seed ends the crawl `failed` with `seed rejected: <reason>`.
- **Why not the CLI:** `http://localhost:8000` is a normal target while developing, and the CLI
  reaches nothing the person typing the command cannot already reach.
- **Rejected:** a hostname blocklist with no DNS lookup, which any name pointing at `127.0.0.1`
  walks through, and applying the same guard to the CLI.
- **Reverses if:** the API grows authentication and internal crawls should be allowed for
  authenticated callers, which makes the guard a per-caller policy rather than a flat refusal.

## Standard library logging and a stats dataclass

`logging` to stderr, WARNING by default, `-v` for INFO and `-vv` for DEBUG.

- **Why:** stdout carries results only, asserted by a test, so `url-crawler site | jq` always works.
- **Why a dataclass:** the CLI owns `CrawlStats`, so counters survive a cancelled task and Ctrl-C
  still prints a summary.
- **Reused by the service:** the same dataclass is snapshotted into the `crawl.stats` column on
  every heartbeat, which is how progress is visible before a crawl ends.
- **Rejected:** structlog, Prometheus and Sentry.
- **Reverses if:** the service runs for real, where an aggregator wants events and a scrape target.

## One generic parser, no Strategy or Factory

One `extract_links` over `a[href], area[href]`, honouring any `<base href>`, with no registry.

- **Why:** the seed arrives at runtime and is arbitrary, so there is no dispatch key at design time
  and a registry would ship with zero entries. A Factory needs something to select between.
- **Why:** every site expresses a hyperlink identically. Site-specific parsers exist for structured
  data such as prices and titles, which this exercise does not ask for.
- **Seams that are earned:** `Reporter` is a Protocol with three implementations, `fetch()` returns
  `FetchResult | FetchError` because a 404 is data, and every collaborator is constructor-injected.
- **What that bought:** [the tests](testing.md) drive the crawler with a fake site and a collecting
  reporter, no monkeypatching, and the service reused the crawler without editing it.
- **Repository does appear**, in `url_crawler_service/repository.py`, to keep a database out of the
  API and the worker. It is one class of query methods, not a layer.
- **Rejected:** a `dict[str, LinkExtractor]` registry keyed by host, generic extractor as fallback.
- **Reverses if:** a known host needs different extraction, say a sitemap-first extractor in a
  multi-domain service. That is a registry behind the existing `extract` parameter.

## Playwright and Scrapy

Both are banned by the exercise, and named here because a reviewer will wonder.

- **Scrapy** would have supplied the frontier, scheduler, dedup filter, retry middleware and robots
  handling written by hand here: roughly `crawler.py`, `frontier.py`, `retry.py` and `robots.py`.
- **Playwright** would render JavaScript-built navigation, the one class of site this crawler
  cannot see. It detects the case and warns instead.

---

[Back to README](../README.md)
