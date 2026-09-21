"""Provider-reported OpenAI usage and cost, kept apart from local accounting.

Butters keeps two accounting concepts and must never confuse them.

**Local safety accounting** (`butters.cloud.usage`, `butters.pricing`) is what
decides, *before* a paid call, whether that call is allowed. Where a billable
dimension is not observable it records a deliberate over-estimate, so the
budget fails closed. It is the only authority for admission.

**Provider-reported accounting** is this module: what OpenAI says was actually
spent, read *after* the fact from the organization Usage and Costs APIs. It
exists for billing visibility and reconciliation. It is observational and can
never admit or refuse anything. If OpenAI's billing API is down, delayed or
malformed, Chat and speech keep working and this surface says it is stale.

The two numbers legitimately differ. Butters records `gpt-4o-mini-tts` as an
`estimated_upper_bound` because `POST /v1/audio/speech` returns audio bytes
and no usage object, so neither the text input tokens nor the audio output
tokens are observable at request time. The ceiling is well above the real
figure on purpose. Their difference is a *reconciliation difference*, not a
saving, and this module never calls it one.

Endpoints, from the official OpenAPI specification
(https://github.com/openai/openai-openapi, `openapi.yaml`, read 2026-09-21):

* ``GET /v1/organization/costs`` — the only provider source of money.
  ``bucket_width`` accepts ``1d`` only. Results are ``CostsResult`` objects
  carrying ``amount.value``/``amount.currency`` and, when grouped,
  ``line_item``/``project_id``.
* ``GET /v1/organization/usage/completions`` — token quantities and
  ``num_model_requests``. Carries no cost. Observed 2026-09-21: this is
  where ``gpt-4o-mini-tts`` appears, because it is billed on text input and
  audio output *tokens*. It is not in the audio-speeches feed at all.
* ``GET /v1/organization/usage/audio_speeches`` — ``characters`` and
  ``num_model_requests``. Carries no cost and no tokens. Observed
  2026-09-21: only the character-billed legacy models (``tts-1``) appear
  here.

Neither usage endpoint carries money, and both aggregate by day, so a local
TTS request can never be reconciled individually. Cost comes only from the
Costs endpoint, per day and per line item, and stays at that grain.

All three take ``start_time`` (Unix seconds, inclusive, required),
``end_time`` (exclusive), ``project_ids``, ``limit`` and a ``page`` cursor,
and all three return ``{object: "page", data: [bucket], has_more, next_page}``.
All three require an Admin API key presented as a bearer token.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from butters.cloud.usage_admin_credential import (
    UsageAdminCredentialError,
    UsageAdminCredentialStore,
)

__all__ = [
    "AUDIO_SPEECHES",
    "COMPLETIONS",
    "COSTS",
    "AccountingError",
    "OpenAIAccountingClient",
    "ProviderAccountingService",
    "ProviderAccountingStore",
]

# ---------------------------------------------------------------- limits --

API_HOST = "api.openai.com"
API_ORIGIN = f"https://{API_HOST}"

COSTS = "/v1/organization/costs"
COMPLETIONS = "/v1/organization/usage/completions"
AUDIO_SPEECHES = "/v1/organization/usage/audio_speeches"

# The complete set of requests this credential may ever produce. Not a
# prefix, not a pattern: three exact paths, GET only. An OpenAI Admin key can
# create keys and move spend limits, and the only thing standing between this
# credential and those endpoints is that no code path can name them.
ALLOWED_PATHS = frozenset({COSTS, COMPLETIONS, AUDIO_SPEECHES})

REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 12
MAX_BUCKETS = 180  # the documented ceiling for `limit`


class AccountingError(RuntimeError):
    """A failure that is safe to show an administrator.

    Never carries the Admin key, a request URL with credentials, or an
    upstream body, which can quote a submitted key back.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    A 3xx to another host would send the Admin key there. Nothing on these
    three endpoints legitimately redirects, so any redirect is a failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccountingError(
            "redirect_refused", "OpenAI redirected an accounting read; refused"
        )


# ---------------------------------------------------------------- client --


class OpenAIAccountingClient:
    """GET-only reads of three reviewed organization endpoints.

    There is no generic request method, no caller-supplied path, no
    caller-supplied host and no method parameter. Adding an endpoint is an
    edit to `ALLOWED_PATHS` and a review, not a call-site argument.
    """

    def __init__(
        self,
        credentials: UsageAdminCredentialStore,
        *,
        opener: Callable[[urllib.request.Request, float], object] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._clock = clock
        self._opener = opener or _default_opener

    # ----- the three reviewed reads ---------------------------------------

    def costs(
        self,
        *,
        start_time: int,
        end_time: int | None = None,
        project_ids: Sequence[str] = (),
        limit: int = 31,
    ) -> list[dict[str, object]]:
        """Daily cost buckets. `bucket_width` is `1d`; the API allows no other."""

        return self._paged(
            COSTS,
            {
                "start_time": start_time,
                "end_time": end_time,
                "bucket_width": "1d",
                "limit": _bounded_limit(limit),
                "group_by": ["line_item"],
                "project_ids": list(project_ids),
            },
        )

    def completions(
        self,
        *,
        start_time: int,
        end_time: int | None = None,
        project_ids: Sequence[str] = (),
        limit: int = 31,
    ) -> list[dict[str, object]]:
        return self._paged(
            COMPLETIONS,
            {
                "start_time": start_time,
                "end_time": end_time,
                "bucket_width": "1d",
                "limit": _bounded_limit(limit),
                "group_by": ["model"],
                "project_ids": list(project_ids),
            },
        )

    def audio_speeches(
        self,
        *,
        start_time: int,
        end_time: int | None = None,
        project_ids: Sequence[str] = (),
        limit: int = 31,
    ) -> list[dict[str, object]]:
        return self._paged(
            AUDIO_SPEECHES,
            {
                "start_time": start_time,
                "end_time": end_time,
                "bucket_width": "1d",
                "limit": _bounded_limit(limit),
                "group_by": ["model"],
                "project_ids": list(project_ids),
            },
        )

    # ----- internals ------------------------------------------------------

    def _paged(self, path: str, parameters: dict[str, object]) -> list[dict[str, object]]:
        """Follow `next_page` a bounded number of times.

        A bucket is keyed by its own `start_time`, and the caller upserts on
        that key, so a server that repeated a page could not double-count.
        """

        buckets: list[dict[str, object]] = []
        cursor: str | None = None
        for _page in range(MAX_PAGES):
            payload = self._get(path, {**parameters, "page": cursor})
            buckets.extend(_validated_buckets(payload))
            if not payload.get("has_more"):
                return buckets
            cursor = payload.get("next_page")
            if not isinstance(cursor, str) or not cursor:
                return buckets
        raise AccountingError(
            "too_many_pages", "the accounting read did not terminate within its page budget"
        )

    def _get(self, path: str, parameters: Mapping[str, object]) -> dict[str, object]:
        if path not in ALLOWED_PATHS:
            # Unreachable through the public methods; a structural guard so a
            # future edit cannot introduce a caller-named path.
            raise AccountingError("path_not_allowed", "that endpoint is not permitted")
        secret = self._credentials.secret()
        if secret is None:
            raise AccountingError(
                "credential_missing", "no OpenAI Admin API key is configured"
            )
        url = f"{API_ORIGIN}{path}?{_encode(parameters)}"
        # Scheme and host are fixed constants above; the path is allow-listed.
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/json",
                "User-Agent": "butters-accounting/1",
            },
        )
        try:
            raw = self._opener(request, REQUEST_TIMEOUT_SECONDS)
        except AccountingError:
            raise
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc.code) from None
        except TimeoutError:
            raise AccountingError(
                "timeout", "the accounting read timed out"
            ) from None
        except (urllib.error.URLError, OSError):
            raise AccountingError(
                "unavailable", "OpenAI could not be reached for accounting"
            ) from None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AccountingError(
                "malformed_response", "OpenAI returned a response that is not JSON"
            ) from None
        if not isinstance(payload, dict) or payload.get("object") != "page":
            raise AccountingError(
                "malformed_response", "OpenAI returned an unexpected accounting shape"
            )
        return payload


def _default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_NoRedirects)
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise _http_failure(response.status)
        # One read of a bounded size: a hostile or broken upstream cannot make
        # this allocate without limit.
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise AccountingError(
            "response_too_large", "the accounting response exceeded its size limit"
        )
    return body


def _http_failure(status: int) -> AccountingError:
    if status in {401, 403}:
        return AccountingError(
            "unauthorized",
            "OpenAI rejected the Admin key. An Admin API key from the "
            "organization settings is required; a project or inference key "
            "cannot read organization usage.",
        )
    if status == 429:
        return AccountingError("rate_limited", "OpenAI rate limited the accounting read")
    if 500 <= status < 600:
        return AccountingError("upstream_error", f"OpenAI returned HTTP {status}")
    return AccountingError("upstream_status", f"OpenAI returned HTTP {status}")


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_BUCKETS))


def _encode(parameters: Mapping[str, object]) -> str:
    pairs: list[tuple[str, str]] = []
    for name, value in parameters.items():
        if value is None or value == []:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((name, str(item)) for item in value)
        else:
            pairs.append((name, str(value)))
    return urllib.parse.urlencode(pairs)


def _validated_buckets(payload: Mapping[str, object]) -> list[dict[str, object]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise AccountingError(
            "malformed_response", "OpenAI returned no accounting buckets"
        )
    buckets: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict) or item.get("object") != "bucket":
            continue
        start = item.get("start_time")
        end = item.get("end_time")
        results = item.get("results")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        buckets.append(
            {
                "start_time": start,
                "end_time": end,
                "results": [row for row in (results or []) if isinstance(row, dict)],
            }
        )
    return buckets


# ----------------------------------------------------------------- store --

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_accounting_snapshots (
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    scope TEXT NOT NULL,
    bucket_start INTEGER NOT NULL,
    bucket_end INTEGER NOT NULL,
    line_item TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    reported_cost_usd REAL,
    currency TEXT,
    num_model_requests INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    characters INTEGER,
    PRIMARY KEY (provider, endpoint, scope, bucket_start, line_item)
);
CREATE TABLE IF NOT EXISTS provider_accounting_sync (
    provider TEXT PRIMARY KEY,
    last_attempt_at REAL,
    last_success_at REAL,
    window_start INTEGER,
    window_end INTEGER,
    scope TEXT,
    failure_code TEXT,
    failure_message TEXT
);
CREATE TABLE IF NOT EXISTS provider_accounting_settings (
    provider TEXT PRIMARY KEY,
    project_id TEXT
);
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    endpoint: str
    scope: str
    bucket_start: int
    bucket_end: int
    line_item: str
    reported_cost_usd: float | None = None
    currency: str | None = None
    num_model_requests: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    characters: int | None = None


class ProviderAccountingStore:
    """Provider snapshots, stored beside the local ledger and never inside it.

    `provider_usage` rows are what the admission controller actually used at
    request time. They are evidence and are never rewritten from provider
    reporting: an upper bound that was correct to reserve is still the figure
    that was reserved, whatever the invoice later says.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def replace_snapshots(
        self, provider: str, endpoint: str, scope: str, snapshots: Iterable[Snapshot], *, fetched_at: float
    ) -> int:
        """Upsert on the bucket key, so a repeated sync is idempotent."""

        rows = [
            (
                provider,
                endpoint,
                scope,
                item.bucket_start,
                item.bucket_end,
                item.line_item,
                fetched_at,
                item.reported_cost_usd,
                item.currency,
                item.num_model_requests,
                item.input_tokens,
                item.output_tokens,
                item.characters,
            )
            for item in snapshots
        ]
        with self._lock, self._connect() as connection:
            connection.executemany(
                """INSERT INTO provider_accounting_snapshots
                (provider, endpoint, scope, bucket_start, bucket_end, line_item,
                 fetched_at, reported_cost_usd, currency, num_model_requests,
                 input_tokens, output_tokens, characters)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider, endpoint, scope, bucket_start, line_item)
                DO UPDATE SET
                    bucket_end=excluded.bucket_end,
                    fetched_at=excluded.fetched_at,
                    reported_cost_usd=excluded.reported_cost_usd,
                    currency=excluded.currency,
                    num_model_requests=excluded.num_model_requests,
                    input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    characters=excluded.characters""",
                rows,
            )
        return len(rows)

    def cost_between(self, provider: str, scope: str, start: int, end: int) -> float | None:
        """Reported cost for whole buckets inside the window, or None."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT SUM(reported_cost_usd), COUNT(*)
                FROM provider_accounting_snapshots
                WHERE provider=? AND endpoint=? AND scope=?
                  AND bucket_start >= ? AND bucket_start < ?""",
                (provider, COSTS, scope, start, end),
            ).fetchone()
        return None if not row or row[1] == 0 else round(float(row[0] or 0.0), 6)

    def usage_totals(self, provider: str, scope: str, start: int, end: int) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT endpoint, line_item,
                          SUM(COALESCE(num_model_requests,0)),
                          SUM(COALESCE(input_tokens,0)),
                          SUM(COALESCE(output_tokens,0)),
                          SUM(COALESCE(characters,0))
                   FROM provider_accounting_snapshots
                   WHERE provider=? AND scope=? AND endpoint != ?
                     AND bucket_start >= ? AND bucket_start < ?
                   GROUP BY endpoint, line_item ORDER BY 3 DESC""",
                (provider, scope, COSTS, start, end),
            ).fetchall()
        return {
            "by_model": [
                {
                    "endpoint": row[0],
                    "model": row[1],
                    "num_model_requests": int(row[2]),
                    "input_tokens": int(row[3]),
                    "output_tokens": int(row[4]),
                    "characters": int(row[5]),
                }
                for row in rows
            ]
        }

    def coverage(self, provider: str, scope: str) -> tuple[int | None, int | None]:
        """The span the stored buckets actually cover.

        This is not the range that was *requested*. The query asks for a
        window that runs slightly into the future so the current day is
        included; what came back is what the provider actually has.
        """

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT MIN(bucket_start), MAX(bucket_end)
                   FROM provider_accounting_snapshots
                   WHERE provider=? AND scope=?""",
                (provider, scope),
            ).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def cost_by_line_item(self, provider: str, scope: str, start: int, end: int) -> list[dict[str, object]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT line_item, ROUND(SUM(reported_cost_usd), 6)
                   FROM provider_accounting_snapshots
                   WHERE provider=? AND endpoint=? AND scope=?
                     AND bucket_start >= ? AND bucket_start < ?
                   GROUP BY line_item ORDER BY 2 DESC""",
                (provider, COSTS, scope, start, end),
            ).fetchall()
        return [{"line_item": row[0], "cost_usd": float(row[1] or 0.0)} for row in rows]

    # ----- sync bookkeeping ----------------------------------------------

    def record_sync(
        self,
        provider: str,
        *,
        attempted_at: float,
        succeeded_at: float | None,
        window: tuple[int, int] | None,
        scope: str | None,
        failure: tuple[str, str] | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT last_success_at, window_start, window_end, scope FROM provider_accounting_sync WHERE provider=?",
                (provider,),
            ).fetchone()
            # A failed attempt must not erase the last good sync: the page
            # needs to say "stale since", not "never".
            success = succeeded_at if succeeded_at is not None else (existing[0] if existing else None)
            start, end = window if window is not None else (
                (existing[1], existing[2]) if existing else (None, None)
            )
            kept_scope = scope if scope is not None else (existing[3] if existing else None)
            connection.execute(
                """INSERT INTO provider_accounting_sync
                (provider, last_attempt_at, last_success_at, window_start, window_end, scope, failure_code, failure_message)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    scope=excluded.scope,
                    failure_code=excluded.failure_code,
                    failure_message=excluded.failure_message""",
                (
                    provider,
                    attempted_at,
                    success,
                    start,
                    end,
                    kept_scope,
                    None if failure is None else failure[0],
                    None if failure is None else failure[1],
                ),
            )

    def sync_state(self, provider: str) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT last_attempt_at, last_success_at, window_start, window_end,
                          scope, failure_code, failure_message
                   FROM provider_accounting_sync WHERE provider=?""",
                (provider,),
            ).fetchone()
        if row is None:
            return {
                "last_attempt_at": None,
                "last_success_at": None,
                "window_start": None,
                "window_end": None,
                "scope": None,
                "failure_code": None,
                "failure_message": None,
            }
        return {
            "last_attempt_at": row[0],
            "last_success_at": row[1],
            "window_start": row[2],
            "window_end": row[3],
            "scope": row[4],
            "failure_code": row[5],
            "failure_message": row[6],
        }

    # ----- non-secret settings -------------------------------------------

    def project_id(self, provider: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT project_id FROM provider_accounting_settings WHERE provider=?",
                (provider,),
            ).fetchone()
        value = None if row is None else row[0]
        return value or None

    def set_project_id(self, provider: str, project_id: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO provider_accounting_settings (provider, project_id)
                VALUES (?,?)
                ON CONFLICT(provider) DO UPDATE SET project_id=excluded.project_id""",
                (provider, project_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection


# --------------------------------------------------------------- service --

PROVIDER = "openai"
ORGANIZATION_SCOPE = "organization"
DEFAULT_CACHE_SECONDS = 600.0
MANUAL_REFRESH_SECONDS = 60.0
DEFAULT_WINDOW_DAYS = 31
# Provider billing lags. Past this, the figure is shown as stale rather than
# quietly presented as current.
STALE_AFTER_SECONDS = 3600.0

_PROJECT_ID = re.compile(r"^proj_[A-Za-z0-9]{8,64}$")


@dataclass
class ProviderAccountingService:
    """Cached, bounded provider reads. Observational, never an authority.

    Nothing here can refuse a paid call, change a budget, or write to
    `provider_usage`. The admission controller does not import this module.
    """

    credentials: UsageAdminCredentialStore
    store: ProviderAccountingStore
    client: OpenAIAccountingClient
    clock: Callable[[], float] = time.time
    cache_seconds: float = DEFAULT_CACHE_SECONDS
    window_days: int = DEFAULT_WINDOW_DAYS
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ----- scope ----------------------------------------------------------

    def scope(self) -> str:
        project = self.store.project_id(PROVIDER)
        return project or ORGANIZATION_SCOPE

    def set_project_id(self, raw: object) -> str | None:
        """An explicit, reviewed project identifier, or none at all.

        No enumeration and no name matching: a project is identified by the
        id an administrator pasted, or the scope is honestly organization-wide.
        """

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            self.store.set_project_id(PROVIDER, None)
            return None
        if not isinstance(raw, str) or not _PROJECT_ID.match(raw.strip()):
            raise AccountingError(
                "invalid_project_id",
                "an OpenAI project id looks like proj_ followed by its identifier",
            )
        value = raw.strip()
        self.store.set_project_id(PROVIDER, value)
        return value

    # ----- sync -----------------------------------------------------------

    def sync(self, *, force: bool = False) -> dict[str, object]:
        """Refresh the cached provider snapshots.

        Failure is recorded and returned, never raised into the caller's
        request path: a billing API outage must not become a Butters outage.
        """

        with self._lock:
            now = self.clock()
            state = self.store.sync_state(PROVIDER)
            if not self.credentials.configured():
                return self.state()
            minimum = MANUAL_REFRESH_SECONDS if force else self.cache_seconds
            last = state.get("last_attempt_at")
            if isinstance(last, (int, float)) and now - last < minimum:
                return self.state()

            scope = self.scope()
            projects = [scope] if scope != ORGANIZATION_SCOPE else []
            end = int(now) + 86400
            start = int(now) - self.window_days * 86400
            try:
                costs = self.client.costs(start_time=start, end_time=end, project_ids=projects)
                completions = self.client.completions(
                    start_time=start, end_time=end, project_ids=projects
                )
                speeches = self.client.audio_speeches(
                    start_time=start, end_time=end, project_ids=projects
                )
            except (AccountingError, UsageAdminCredentialError) as exc:
                self.store.record_sync(
                    PROVIDER,
                    attempted_at=now,
                    succeeded_at=None,
                    window=None,
                    scope=scope,
                    failure=(exc.code, exc.message),
                )
                return self.state()

            self.store.replace_snapshots(
                PROVIDER, COSTS, scope, _cost_snapshots(costs, scope), fetched_at=now
            )
            self.store.replace_snapshots(
                PROVIDER,
                COMPLETIONS,
                scope,
                _usage_snapshots(completions, scope, COMPLETIONS),
                fetched_at=now,
            )
            self.store.replace_snapshots(
                PROVIDER,
                AUDIO_SPEECHES,
                scope,
                _usage_snapshots(speeches, scope, AUDIO_SPEECHES),
                fetched_at=now,
            )
            self.store.record_sync(
                PROVIDER,
                attempted_at=now,
                succeeded_at=now,
                window=(start, end),
                scope=scope,
                failure=None,
            )
            return self.state()

    # ----- projection -----------------------------------------------------

    def state(self, windows: Mapping[str, tuple[int, int]] | None = None) -> dict[str, object]:
        """Everything Admin needs, with its scope and freshness attached."""

        configured = self.credentials.configured()
        sync = self.store.sync_state(PROVIDER)
        scope = self.scope()
        now = self.clock()
        success = sync.get("last_success_at")
        if not configured:
            freshness = "not_configured"
        elif success is None:
            freshness = "never_synced"
        elif now - float(success) > STALE_AFTER_SECONDS:
            freshness = "stale"
        else:
            freshness = "fresh"
        reported: dict[str, object] = {}
        if windows:
            for label, (start, end) in windows.items():
                reported[label] = self.store.cost_between(PROVIDER, scope, start, end)
        window_start = sync.get("window_start")
        window_end = sync.get("window_end")
        # What the provider actually returned, as opposed to what was asked
        # for. The requested end runs into the future on purpose so the
        # current day is included; reporting it as coverage would claim data
        # that does not exist yet.
        data_from, data_through = self.store.coverage(PROVIDER, scope)
        current_bucket_open = bool(data_through is not None and data_through > now)
        return {
            "provider": PROVIDER,
            "configured": configured,
            "credential": self.credentials.state().as_dict(),
            "scope": scope,
            "scope_kind": "project" if scope != ORGANIZATION_SCOPE else "organization",
            "freshness": freshness,
            "last_attempt_at": sync.get("last_attempt_at"),
            "last_success_at": success,
            # `requested` is the query bound; `data_from`/`data_through` are
            # the buckets that came back. They are different facts.
            "reporting_window": {
                "requested_start": window_start,
                "requested_end": window_end,
                "data_from": data_from,
                "data_through": data_through,
                "current_bucket_open": current_bucket_open,
            },
            "failure_code": sync.get("failure_code"),
            "failure_message": sync.get("failure_message"),
            "reported_cost_usd": reported,
            "cost_by_line_item": (
                self.store.cost_by_line_item(PROVIDER, scope, int(window_start), int(window_end))
                if isinstance(window_start, int) and isinstance(window_end, int)
                else []
            ),
            "usage": (
                self.store.usage_totals(PROVIDER, scope, int(window_start), int(window_end))
                if isinstance(window_start, int) and isinstance(window_end, int)
                else {"by_model": []}
            ),
            # Named so nobody can read it as an endorsement of the local figure.
            "source_endpoints": sorted(ALLOWED_PATHS),
        }

    def validate(self) -> dict[str, object]:
        """The smallest read that proves the key works: one day of costs.

        Deliberately not a user, key or project listing. A bounded accounting
        read is enough to establish that the credential is an Admin key with
        usage access, and it mutates nothing.
        """

        now = self.clock()
        if not self.credentials.configured():
            raise AccountingError(
                "credential_missing", "no OpenAI Admin API key is configured"
            )
        start = int(now) - 86400
        try:
            self.client.costs(start_time=start, end_time=int(now) + 86400, limit=1)
        except AccountingError as exc:
            outcome = {
                "authenticated": False,
                "code": exc.code,
                "detail": exc.message,
                "checked_at": now,
            }
            self.credentials.record_validation(outcome)
            return outcome
        outcome = {
            "authenticated": True,
            "code": "valid",
            "detail": "OpenAI accepted the Admin key for a bounded usage read.",
            "checked_at": now,
        }
        self.credentials.record_validation(outcome)
        return outcome


def _cost_snapshots(buckets: Iterable[Mapping[str, object]], scope: str) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    for bucket in buckets:
        start = int(bucket["start_time"])  # validated upstream
        end = int(bucket["end_time"])
        for result in bucket.get("results", []):
            if result.get("object") != "organization.costs.result":
                continue
            amount = result.get("amount")
            value = amount.get("value") if isinstance(amount, dict) else None
            currency = amount.get("currency") if isinstance(amount, dict) else None
            snapshots.append(
                Snapshot(
                    endpoint=COSTS,
                    scope=scope,
                    bucket_start=start,
                    bucket_end=end,
                    line_item=str(result.get("line_item") or "all"),
                    reported_cost_usd=float(value) if isinstance(value, (int, float)) else None,
                    currency=str(currency) if isinstance(currency, str) else None,
                )
            )
    return snapshots


def _usage_snapshots(
    buckets: Iterable[Mapping[str, object]], scope: str, endpoint: str
) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    for bucket in buckets:
        start = int(bucket["start_time"])
        end = int(bucket["end_time"])
        for result in bucket.get("results", []):
            object_name = result.get("object")
            if not isinstance(object_name, str) or not object_name.startswith(
                "organization.usage."
            ):
                continue
            snapshots.append(
                Snapshot(
                    endpoint=endpoint,
                    scope=scope,
                    bucket_start=start,
                    bucket_end=end,
                    line_item=str(result.get("model") or "all"),
                    num_model_requests=_optional_int(result.get("num_model_requests")),
                    input_tokens=_optional_int(result.get("input_tokens")),
                    output_tokens=_optional_int(result.get("output_tokens")),
                    characters=_optional_int(result.get("characters")),
                )
            )
    return snapshots


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
