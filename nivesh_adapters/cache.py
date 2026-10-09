"""TTL cache over the DuckDB `cache_entry` table, keyed by (adapter, hash of canonical params).

`--refresh` / NIVESH_REFRESH=1 skip the cache read but still write the fresh result. If the
refetch fails the stale entry is returned flagged `stale=True` (also under refresh: degraded data
that is visibly marked beats no data); with no entry the error propagates. DataQualityError is
never masked by stale data and bad data is never cached.
"""

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

import duckdb

from nivesh_adapters.base import Adapter, AdapterResult
from nivesh_adapters.quality import DataQualityError
from nivesh_core.config import Ttls
from nivesh_core.redact import redact_json
from nivesh_core.timeutil import to_iso, utcnow


def params_hash(params: dict[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def cached_fetch(
    adapter: Adapter,
    params: dict[str, Any],
    data_type: str,
    *,
    conn: duckdb.DuckDBPyConnection,
    ttls: Ttls,
    refresh: bool = False,
    now: Callable[[], datetime] = utcnow,
) -> AdapterResult:
    if data_type not in Ttls.model_fields:
        raise ValueError(
            f"unknown data type {data_type!r}; expected one of {sorted(Ttls.model_fields)}"
        )
    ttl = getattr(ttls, data_type)
    key = (adapter.name, params_hash(params))
    row = conn.execute(
        "SELECT payload, source, as_of, fetched_at FROM cache_entry "
        "WHERE adapter = ? AND params_hash = ?",
        key,
    ).fetchone()
    cached = (
        AdapterResult(
            data=json.loads(row[0]),
            source=row[1],
            as_of=datetime.fromisoformat(row[2]),
            fetched_at=datetime.fromisoformat(row[3]),
        )
        if row
        else None
    )
    refresh = refresh or os.environ.get("NIVESH_REFRESH") == "1"
    if cached and not refresh and now() - cached.fetched_at < ttl:
        return cached
    try:
        fresh = adapter.fetch(**params)
    except DataQualityError:
        raise
    except Exception:
        if cached:
            return cached.model_copy(update={"stale": True})
        raise
    conn.execute(
        "INSERT OR REPLACE INTO cache_entry VALUES (?, ?, ?, ?, ?, ?)",
        (
            *key,
            json.dumps(redact_json(fresh.data), default=str),
            fresh.source,
            to_iso(fresh.as_of),
            to_iso(now()),
        ),
    )
    return fresh
