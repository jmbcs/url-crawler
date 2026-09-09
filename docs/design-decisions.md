# Design decisions

The README table gives one line per decision. Each section below carries the argument behind that
line, the option that was rejected, and what would reverse it.

## asyncio with one client and N workers

A crawl is IO-bound: almost all of the wall time is waiting on sockets. `asyncio.TaskGroup` with N
worker tasks over one `httpx.AsyncClient` reuses connections, keeps keep-alive working, and holds
all shared state in one thread, so the seen set and the counters need no locks.

*Rejected:* a thread pool (one connection pool per thread, or lock-protected sharing, for no gain on
IO waits) and a process pool (interprocess dedup for a problem that has none). *Reverses if:* HTML
parsing starts dominating the profile, at which point the fix is a `ProcessPoolExecutor` behind
`extract_links`, not a different concurrency model for the fetches.

## The worker count is the only limit

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
and full AIMD belongs in the multi-host service described in [extending.md](extending.md).

## Redirects are never followed by the client

With `follow_redirects=True`, httpx contacts the redirect target before any of this code can check
whether it is in scope, which breaks the single-host rule the brief states twice. So the client runs
with `follow_redirects=False`. A 3xx with a `Location` becomes a page whose only link is that
target, and the target then goes through resolve, normalize, scope and dedup like any other link.

That one decision fixes four things at once: an off-host redirect target is printed but never
requested; a redirect chain is visible in the output hop by hop; a redirect landing on an
already-crawled page is dropped as a duplicate; and an http to https upgrade does not double-fetch.
Redirect cycles need no special case, because the second hop is already in the seen set. The seed is
the one exception: its chain is walked eagerly (up to 5 hops) before the crawl starts, because the
scope has to be anchored to the host that actually serves the site.

*Rejected:* letting httpx follow redirects and filtering afterwards, which leaks requests off-host.
*Reverses if:* the scope model ever becomes a multi-host allowlist, where following a redirect
inside the allowlist is safe.

## Exact-host scope, re-anchored after a seed redirect

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

## Robots on by default, and unreadable means blocked

robots.txt is fetched once for the final seed host, parsed with `urllib.robotparser`, and applied to
every candidate URL before it enters the frontier. The seed itself is the one URL fetched before
robots.txt is read, because its redirect chain decides which host's robots.txt applies. A
`Crawl-delay` is honoured by serializing a sleep across the workers.

What the fetch gets back decides the policy:

| Outcome | Policy |
| --- | --- |
| 200 | Parse the body and apply it. |
| 3xx with a `Location` | Followed, up to five hops, to another host if the header says so, because a site is free to serve the file from a CDN. |
| Any other status below 500, a 404 above all | The site has no robots.txt, so every path is allowed. Logged at INFO. |
| 5xx, a connection or transport error, a redirect with no `Location`, more than five hops, or a body that is not UTF-8 | Complete disallow, per RFC 9309 section 2.3.1. |

A complete disallow ends the run there: exit code 3, and a message naming `--ignore-robots`. So does
a robots.txt that reads fine and disallows the seed.

`rel="nofollow"` links are followed and printed. `nofollow` is a hint to search engines about link
equity, not an access control; robots.txt is the access control, and this tool obeys that one.

*Rejected:* failing open on an unreadable robots.txt, which is what an earlier version of this code
did. It is the friendlier answer for whoever is running the crawl and the wrong one for the site: a
503 on robots.txt is the one moment a site cannot say what it allows, and RFC 9309 gives it the
benefit of the doubt. Also rejected: robots off by default (faster to write, wrong for anything
pointed at a real site) and honouring `nofollow` (would silently hide pages the site never asked to
protect). *Reverses if:* a flaky robots.txt makes exit 3 the usual outcome for legitimate crawls, at
which point the fix is retrying the robots fetch rather than going back to fail-open. Honouring
`nofollow` reverses if a crawl trap turns out to be marked with it in practice.

## One normal form, and a stricter key for dedup

`normalize()` produces the form that is requested and printed: scheme and host lowercased, a default
port dropped, an empty path turned into `/`, the fragment dropped, dot segments resolved (`/a/../b`
becomes `/b`, and a trailing slash survives), and userinfo removed. Credentials identify the caller
rather than the resource, so stripping `user:password@` at normalization is what keeps a password
out of the frontier, the output and every log line.

`canonical_key()` is the dedup identity and is never printed. It folds percent-encoding on top of
the normal form: hex digits uppercased, unreserved characters decoded, non-ASCII encoded. So `/café`
and `/caf%C3%A9` are one page, and whichever spelling the site linked to is the one that prints. It
also sorts query parameters by name, with a stable sort, so `?b=1&a=2` and `?a=2&b=1` are the same
page while `?a=2&a=1` and `?a=1&a=2` stay two.

*Rejected:* folding the key back into the printed URL, which would rewrite a URL the site chose to
serve; and sorting whole `name=value` pairs, which would merge two orderings of a repeated parameter
that a server is free to read as two different requests. *Reverses if:* a target puts a session id
or a tracking parameter in the query, where the next step is dropping known parameters from the key
rather than changing how the rest are ordered.

## Retry classification by leaf type

`retry_delay()` is a pure function of status, exception, attempt number and headers, so the whole
policy is a table test that runs in microseconds. Retryable statuses are 408, 425, 429, 500, 502,
503 and 504. Retryable exceptions are enumerated as leaves: `httpx.TimeoutException`,
`httpx.NetworkError` and `httpx.RemoteProtocolError`. The tempting
`isinstance(exc, httpx.TransportError)` is wrong, because `UnsupportedProtocol` and
`LocalProtocolError` are also `TransportError` subclasses and both signal a caller bug that no retry
can fix. Three attempts, full jitter (`uniform(0, min(8, 0.5 * 2**attempt))`), and `Retry-After` on
429 and 503 in both delta-seconds and HTTP-date form, capped at 30 seconds. `sleep` and
`random.Random` are injected, so the retry tests do not sleep.

`httpx.DecodingError` is deliberately off that list. A body that fails to decompress is reported as
a `protocol` error and not retried, because the same bytes decode the same way on a second attempt.
The fetcher catches it by name, so it comes back as one classified failed page instead of reaching
the crawler's catch-all as an `internal` error, and a host that has started sending broken gzip
counts toward the failure fuse.

Two clocks bound one request. `--timeout` is the read and write timeout, so it caps the gap between
chunks; `--request-budget` (60 seconds by default) caps the whole request including the body,
because a server that trickles one byte at a time never trips a read timeout. Exceeding the budget
is reported as a `timeout` and retried like one. The budget has to be above 0 and at least
`--timeout`, and the CLI exits 2 when it is not.

*Rejected:* Tenacity (a dependency, a decorator, and `Retry-After` still needs custom code) and
retrying by exception base class. *Reverses if:* retry behaviour needs to differ per host, which is
where a small policy object beats a function.

## No circuit breaker, but a failure fuse

A circuit breaker protects a long-lived caller from a flapping shared dependency and probes for
recovery. This is one bounded batch against one host, so "open" would just mean "stop the crawl".
Instead there is a hardcoded fuse: 20 consecutive failures that look like the host is down
(timeouts, connection errors, protocol errors, 5xx) abort the run with exit code 4 and a message
naming the reason. Any success resets the counter, and 4xx never counts, because a wall of 404s is a
site with dead links, not an outage.

*Rejected:* a full breaker with half-open probing, and per-host AIMD decay. *Reverses if:* the
crawler becomes a long-running service over many hosts, where a breaker (or AIMD) per host earns its
state.

## A page with no links is a leaf

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

## Postgres in the service, and no Celery or Redis

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

## An HTTP API, no UI

The service exposes JSON and server-sent events, and stops there. A UI would be a second client of
the same endpoints, and everything a reviewer would learn from it is already visible in the API.

One note on pagination, since it is the first thing either client needs. The CLI streams and lets
`less` and `jq` do the paging. The API pages server-side with a keyset cursor over
`(crawl_id, seq)`, which means the cursor is the last row you saw rather than a row offset. `OFFSET`
is wrong here because rows keep arriving during a crawl and shift every later page; client-side
paging is wrong because shipping the whole result set defeats the point. The links of one page stay
inside one response item, so a consumer never sees half a page.

*Rejected:* a small HTMX UI. *Reverses if:* someone who does not use `curl` has to watch a crawl, at
which point the UI is a static page over the existing `/crawls` and `/events` endpoints and adds no
server-side code.

## The service guards its seed host, the CLI does not

The service resolves the seed host before it queues anything and refuses whatever points inside the
network it runs in: `localhost`, any `*.localhost`, `*.local` or `*.internal` name, a private IP
literal, and any host resolving to a loopback, private, link-local (`169.254.169.254` included),
multicast, reserved or unspecified address, or an IPv6 unique-local one. A host that does not
resolve is refused too. `POST /crawls` answers 422 with `{"detail": {"seed": "<reason>"}}`, and the
worker repeats the check before the first seed fetch and before every hop of the seed's redirect
chain, so a public host that redirects to the metadata address is caught at the hop rather than
fetched. A worker that rejects a seed ends the crawl `failed` with `seed rejected: <reason>`.

The CLI has no such check, on purpose. Pointing it at `http://localhost:8000` is a normal thing to
do while developing, and the CLI reaches nothing the person typing the command cannot already reach.
The API is the opposite: anyone who can post to it borrows the network its worker sits in.

*Rejected:* a hostname blocklist with no DNS lookup, which any name pointing at `127.0.0.1` walks
straight through, and applying the same guard to the CLI. *Reverses if:* the API grows
authentication and an operator wants internal crawls allowed for authenticated callers, which turns
the guard into a per-caller policy rather than a flat refusal.

## Standard library logging and a stats dataclass

`logging` to stderr, WARNING by default, `-v` for INFO and `-vv` for DEBUG. stdout carries results
only, which is asserted by a test, so `url-crawler site | jq` works with any verbosity.
`CrawlStats` is a plain dataclass owned by `cli.py` and injected into the crawler, which is what
makes the Ctrl-C summary possible: the counters live outside the cancelled task. The service reuses
that: the same dataclass is snapshotted into the `crawl.stats` column on every heartbeat, which is
how progress is visible before a crawl ends.

*Rejected:* structlog, Prometheus and Sentry. *Reverses if:* the service runs somewhere real, where
an aggregator needs structured events and a scrape endpoint.

## One generic parser, no Strategy or Factory

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

## Playwright and Scrapy

Both are banned by the exercise, and named here because a reviewer will wonder.

Scrapy would have supplied the frontier, the scheduler, the dedup filter, the retry middleware and
robots handling that this repository implements by hand, roughly the whole of `crawler.py`,
`frontier.py` and `retry.py`. Playwright would have rendered JavaScript-built navigation, which is
the one class of site this crawler cannot see; it detects and warns instead.

---

[Back to README](../README.md)
