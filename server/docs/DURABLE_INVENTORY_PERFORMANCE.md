# Durable inventory performance milestone

Date: 2026-08-26

Branch baseline: `a74e1eed2f254b2c2d2f4e197e0ca25454547d9a`

## Request path before the change

```text
GET /api/latest
  +-- InfluxReadRepository.latest() -- one Flux request
  |     +-- environment latest, permanent bucket, -7d
  |     +-- environment inventory, permanent bucket, start 0
  |     +-- air latest, live bucket, -30m
  |     `-- air inventory, permanent air_quality_15m, start 0
  `-- air_quality_context() -------- one parallel Flux request
        +-- current live 15-minute window
        +-- permanent aggregates, -25h
        `-- active event states, start 0

GET /api/nodes
  `-- nodes() -> latest() ----------- same four-stream latest Flux request
```

There was no cache. Each of the two Gunicorn workers owned an Influx client, but
each request repeated its query. `/api/readings` used separate bounded history
queries and did not depend on latest-value state.

## Baseline and alternatives

All Flux measurements used InfluxDB's read-only query API against the same live
data. Times are nine runs unless noted; p95 is nearest-rank and therefore the
maximum for nine samples.

| Operation | First (s) | Median (s) | Min-max (s) | p95 (s) | Rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| environment latest, -7d | 0.160 | 0.150 | 0.139-0.160 | 0.160 | 15 |
| air latest, -30m | 0.020 | 0.027 | 0.018-0.028 | 0.028 | 0 |
| environment inventory, start 0 | 2.404 | 2.420 | 2.364-2.791 | 2.791 | 15 |
| air inventory, start 0 | 5.265 | 5.190 | 4.963-5.523 | 5.523 | 18 |
| combined inventory | 5.692 | 5.362 | 5.095-5.692 | 5.692 | 33 |
| full latest Flux | 5.374 | 5.364 | 5.207-5.553 | 5.553 | 48 |
| full `latest()` method | 5.386 | 5.386 | 5.288-5.964 | 5.964 | 1 payload |
| air-quality context | 0.048 | 0.040 | 0.035-0.048 | 0.048 | 2 |
| production HTTP `/api/latest` | 5.357 | 5.357 | 5.178-5.759 | 5.759 | 17,747 bytes |
| production HTTP `/api/nodes` | 5.631 | 5.419 | 5.152-5.903 | 5.903 | 1,566 bytes |

Measured alternatives:

- Removing the explicit `group()` did not enable an effective `last()`
  pushdown: environment was 2.731 seconds median and air was 5.440 seconds
  median in a two-run rejection sample.
- `schema.measurementTagValues()` was fast: 0.019 seconds median for the one air
  location and 0.020 seconds for the three environment node IDs. It does not
  preserve the relationships among identity, topic, sensor type, node ID, and
  capabilities, so it is not sufficient as the durable inventory record.
- Filtering the all-history air lookup to the known `office` location was still
  5.833 seconds median (5.651-7.053 seconds). An indexed discovery query followed
  by targeted reconstruction therefore does not fix reconstruction latency.
- A dedicated local state file or Influx inventory measurement could make
  startup cheaper, but would add atomicity, permissions, migration, recovery,
  and write-path concerns. Neither is justified while a one-time reconstruction
  is acceptable.

## Design decision

The repository synchronously reconstructs a durable process-local snapshot at
startup. Normal `latest()` calls issue only the bounded latest-value Flux query,
merge newer observations into process state, and merge that state with the
durable snapshot. Durable history is refreshed by a single guarded background
query every 15 minutes; requests never wait for that refresh.

The selected design has these properties:

- Request cost is bounded by recent environment and live air data, independent
  of the age of the permanent bucket.
- Startup is fail-closed: the repository does not become available with an empty
  inventory if the initial Influx reconstruction fails.
- Refresh failure retains and serves the previous known-good durable snapshot.
  The next scheduled attempt can recover automatically.
- A successful refresh is authoritative for observations that existed when it
  began. Observations made while the query is in flight are retained and merged
  over the refreshed snapshot. This both permits durable reconciliation and
  prevents a five-second refresh from discarding newer request-path data.
- Identities observed by the running process appear immediately, even before a
  first permanent aggregate exists. A sensor missed by bounded observation is
  discovered by the next successful durable refresh.
- The lock protects snapshot replacement and merge operations. A refresh guard
  prevents concurrent or repeated API requests from starting duplicate scans.
- Restart reconstruction uses permanent history, not `environment_live`, so
  expiration of the 72-hour live bucket cannot remove an established identity.
- Historical `/api/readings` queries remain independent of this cache.
- There is no persistent local state, schema migration, new service, Influx
  write, or production configuration change.

## Runtime baseline

- Dashboard: Flask 3.1.3, Gunicorn 23.0.0, Python 3.13.5, two workers.
- Dashboard unit: `home-sensor-dashboard.service`, working directory
  `/opt/home-sensor/server/backend`, with Gunicorn binding `0.0.0.0:8080`.
- Influx client: `influxdb-client` 1.50.0.
- InfluxDB: 2.9.1 (`d4fa1941fd`, build 2026-05-11).
- Permanent bucket: `environment`, infinite retention.
- Live bucket: `environment_live`, 72-hour retention.
- Query construction: `server/backend/app/queries.py`; the requirements range is
  Flask `>=3.1,<4`, Gunicorn `>=23,<24`, and `influxdb-client>=1.48,<2`.

## Implementation validation

The after measurements use the same live read-only Influx data. HTTP after
measurements use Flask's in-process test client with the production-equivalent
repository and routes; the production Gunicorn service was not changed or
restarted. Times are 12 runs. Startup was measured separately.

| Operation | Before median | After median | After min-max | After p95 | Runs |
| --- | ---: | ---: | ---: | ---: | ---: |
| durable inventory query | 5.362 s/request | 5.365 s/startup or refresh | 5.127-5.783 s | 5.783 s | 12 after |
| bounded latest Flux | included above | 0.154 s | 0.145-0.169 s | 0.169 s | 12 |
| full `latest()` method | 5.386 s | 0.153 s | 0.147-0.173 s | 0.173 s | 12 |
| HTTP `/api/latest` | 5.357 s | 0.166 s | 0.152-0.191 s | 0.191 s | 12 |
| HTTP `/api/nodes` | 5.419 s | 0.155 s | 0.144-0.172 s | 0.172 s | 12 |

Three isolated repository startups took 6.216, 5.959, and 5.816 seconds. The
first query of the 12-run durable-inventory series took 5.246 seconds. Startup
blocks before the repository can serve requests, so it cannot expose an empty
transient inventory. If reconstruction fails, startup logs the failure, closes
the Influx client, and raises rather than silently starting empty.

Live semantic validation returned one air-quality identity with:

```text
id=office
location=office
node_id=100
sensor_type=air_quality
topic=home/air/office
last_seen=2026-08-21T20:00:00Z
```

The station remained offline, its last-known fields remained available, and the
normal API enrichment marked `values_are_current=false`.

Validation completed before commit:

- `pytest -q server/backend/tests`: 286 passed in 10.75 seconds (final run).
- Focused query/web suite: 50 passed.
- Ruff on all changed Python files: passed.
- `git diff --check`: passed.

The regression tests cover durable live-expiry and restart behavior, bounded
request query shape, post-start discovery, explicit stale-value semantics,
legacy empty identities, valid node-ID preservation, multiple sensor types,
null battery semantics, refresh failure/recovery, authoritative reconciliation,
concurrent refresh suppression, and fail-closed startup.

## Adversarial pre-merge review

The follow-up review found that freshness did not freeze in the cached payload.
`age_seconds`, node status, stale reason, `values_are_current`, and air-quality
interpretation are derived from `last_seen` at response time. Deterministic
clock-controlled tests cover the transition from online/current through stale
and offline, including cached data and an already-offline SEN66.

It did confirm and fix two cache defects:

- A successful refresh formerly cleared every opportunistic observation,
  including a new identity or newer reading observed after the refresh began.
  The cache now separates the pre-refresh baseline from observations made while
  the loader is running. Success discards only the baseline; failure restores
  it. This prevents regression without retaining observations forever.
- Cached payloads were formerly selected as whole entities. That differed from
  the previous union path, which chose the newest value independently for each
  field. Internal field and identity timestamps are now retained until the
  durable and bounded snapshots are merged, and public battery/air-quality
  finalization runs only after that per-field merge. Sparse environmental and
  air-quality records now match the old union semantics, while a battery value
  cannot be inherited by a newer packet whose battery flag is absent or false.

The final isolated WSGI benchmark, after these fixes, remained within noise of
the accepted implementation: `/api/latest` was 0.171 seconds median
(0.152-0.196, 12 runs) and `/api/nodes` was 0.156 seconds median
(0.148-0.168, 12 runs). Startup reconstruction took 5.839 seconds. The offline
office/node 100 air-quality identity was still present with its truthful
`last_seen`, offline status, stale reason, and `values_are_current=false`.

One forced background refresh took 5.949 seconds. During it, 12 requests per
endpoint measured:

| Endpoint | Idle median | During-refresh median | During min-max |
| --- | ---: | ---: | ---: |
| `/api/latest` | 0.193 s | 0.206 s | 0.182-0.258 s |
| `/api/nodes` | 0.185 s | 0.187 s | 0.163-0.262 s |

The production unit uses two synchronous Gunicorn workers. Each worker owns its
own repository, durable snapshot, Influx client, and at most one temporary
daemon refresh thread, so each performs startup reconstruction and periodic
refresh independently. A two-worker worst-case simulation deliberately
overlapped both refreshes: 10 requests per endpoint remained below 0.5 seconds
(`/api/latest` median/max 0.300/0.372 seconds; `/api/nodes` 0.310/0.378 seconds).
Both refresh queries completed without client errors, in 7.740 and 7.653
seconds. The process load average moved from 1.43 to 1.90 over the sample.

There is no repository teardown hook in the actual Flask/Gunicorn lifecycle.
Refresh threads are daemon threads and therefore cannot keep a worker alive;
systemd gives the Gunicorn master 30 seconds to stop. The explicit cache
`close()` waits for an active refresh and could wait indefinitely for a hung
Influx query, but that method is not called by the deployed shutdown path.
After closure the cache refuses to start another refresh. No production
lifecycle change was justified by the observed execution model.

Final validation after the adversarial fixes:

- `pytest -q server/backend/tests`: 293 passed.
- Focused query/web suite: 56 passed.
- Ruff on all changed Python files: passed.
- `git diff --check`: passed.
