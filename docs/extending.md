# Extending

Where this design runs out, what the multi-domain version looks like, and what was left out on
purpose.

## Why a CLI stops fitting

Four axes, and any one of them is enough:

- **Lifetime.** A six-hour crawl bound to a terminal session dies with the SSH connection, and
  `--resume` is a workaround for the absence of a job.
- **Cross-process politeness.** Ten laptops each running a polite crawler are collectively a denial
  of service, and no amount of per-process courtesy fixes it. This axis alone forces a central service.
- **Machine consumption.** stdout is a poor API. A consumer wants the pages of crawl 47 since cursor
  X, not a re-run and a re-parse.
- **Multi-tenancy.** Quotas, authentication and an audit trail have nowhere to live in a process
  with no identity.

So the [crawl service](service.md) is job-shaped, not stream-shaped: it wraps the same `Crawler`
class the CLI runs with a queue, a lease and a store, changing nothing inside it.

## Extending to multiple domains

- **The unit of parallelism becomes the host, not the URL.** Politeness is per-host, so a worker
  leasing a host owns its rate limit with an in-process token bucket and zero coordination (the
  Mercator design, Heydon and Najork 1999).
- `HostScope` becomes an allowlist scope, the frontier becomes one queue per host with round-robin
  service so a 100k-page site cannot starve a 10-page one, and robots.txt is cached per host with a
  TTL. The fetcher, parser and reporter never knew how many hosts there were, so they survive unchanged.
- **Beyond one process, the frontier is the hard part.** A frontier is a deduplicating set with a
  scheduling policy and durable retry state, and message brokers give the opposite of all three. The
  right primitive is a table, not RabbitMQ.
- In PostgreSQL, `PRIMARY KEY (crawl_id, url_hash)` with `ON CONFLICT DO NOTHING` makes the insert
  itself the "have I seen this?" check, and `SKIP LOCKED` lets N workers claim disjoint batches with
  no queue and no lock convoy:

```sql
UPDATE frontier SET state = 'claimed', claimed_at = now()
WHERE url_hash IN (
    SELECT url_hash FROM frontier
    WHERE crawl_id = $1 AND state = 'queued' AND host = $2
    ORDER BY priority, discovered_at
    FOR UPDATE SKIP LOCKED LIMIT $3
) RETURNING url;
```

- A `claimed_at` timestamp plus a reaper handles a worker that dies mid-page. That is the same
  pattern the crawl service already uses one level up, claiming a whole crawl rather than a batch of
  URLs, so the step from here to there is a third table and a per-host lease, not a rewrite.
- Delivery is at-least-once, so page writes are idempotent upserts keyed on `(crawl_id, url_hash)`.
- The ceiling on this design is not CPU or database throughput: it is the reputation of your egress
  IPs, which is why a real multi-domain crawler ends up caring about proxy pools long before sharding.

## Future work, ranked by value per line of code

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
   so enabling it would be a claim without numbers.
6. **`--include-assets`.** Reporting `img`, `script` and `link` targets alongside anchors, as a
   separate list, for anyone auditing subresources.
7. **Content dedup by body hash, and `If-Modified-Since` or `ETag` conditional requests.** Both pay
   off on repeat crawls, and the service is the first place there are repeat crawls to pay off on.
8. **Authentication, quotas and an audit trail on the API.** The multi-tenancy axis above is
   described and not built.

## Deliberately not implemented

With the reasoning in [design-decisions.md](design-decisions.md): a web UI, Celery, RabbitMQ, Redis,
a circuit breaker, AIMD rate control, site-specific parsers, Strategy, Factory, a pipeline
framework, a separate scheduler, Prometheus, Sentry, structlog, Typer, uvloop, IDNA handling for
non-ASCII hostnames, a CHANGELOG and issue templates.

---

[Back to README](../README.md)
