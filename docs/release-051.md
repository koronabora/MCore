# MCore 0.5.1 — durable video catalog, independent live availability

- Cached movie cards no longer await tracker searches or descriptions. Freshness is
  a separate in-memory status (five-minute TTL); failed requests never prove zero
  peers. Restarted servers expose unknown availability until fresh data arrives.
- Search uses a persistent Unicode FTS5 index with fuzzy fallback; provider searches
  run in the background and clients can poll while `refreshing` is true.
- All enabled releases discovered by search/archive are scheduled for file-manifest
  indexing. Startup backfills the existing catalog. Eight bounded workers, open-card
  priority, exponential retries from five minutes up to one day, no three-attempt cutoff.
- Metadata-only torrent handles are released after each indexing job and not eagerly
  restored on startup. Manifests and file mappings remain on disk; video downloads
  are not started by these jobs.
- Archive roots: Kinozal all series, films, animation, shows/concerts/sport and clips;
  RuTracker cinema/video/TV, series, documentaries/humor, sport, educational videos
  and music videos. Root categories were checked against live trackers.
- All forward pagination links are queued. Initial unvisited pages take priority over
  repeat visits. Catalog work alternates archive/archive/detail/search with the existing
  three-second inter-job pause; ordinary refresh interval is six hours, detail daily.
- `/v1/index-status` exposes provider counts, per-section discovered/visited pages,
  retry counts/errors, last success and files/source queues. Coverage is explicitly
  relative to discovered pages, not a promise of complete tracker coverage.
- `/sources?since=VERSION` returns only availability/status if file mappings, release
  metadata, season totals and watch progress are unchanged.

The full tracker crawl is ongoing work, not completed by installing this release.

Verification: full suite 105 passed; after extending the roots regression, the five
index051 tests passed. On an isolated current database backup, migration took 10.73 s,
indexed searches 0.012–0.025 s, cached Breaking Bad/GOT card assembly 0.026–0.088 s.

Deployed after explicit approval with one restart. Database and source backup:
`/var/backups/mcore/20261102-223805-update-0.5.1`.
Live authenticated API: Breaking Bad 0.126 s / 880061 bytes; unchanged 0.008 s /
9272 bytes. GOT 0.188 s / 1489976 bytes; unchanged 0.016 s / 21227 bytes. Local
search 0.025 s. These timings exclude the phone/mobile route. At deployment,
82870 source jobs existed (380 completed, 82490 ready); 83221 historical releases
include 337 disabled rutor rows retained for compatibility. Temporary test session removed.
