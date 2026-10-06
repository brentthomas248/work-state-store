"""Canonical serialization and time helpers shared across the store."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import unicodedata


def now_rfc3339() -> str:
    return to_rfc3339(_dt.datetime.now(_dt.timezone.utc))


def to_rfc3339(dt: _dt.datetime) -> str:
    dt = dt.astimezone(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


_RFC3339 = re.compile(
    r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?([Zz]|[+-]\d{2}:\d{2})")


def canonical_timestamp(value: str) -> str:
    """Parse an RFC 3339 timestamp carrying an explicit offset and re-render
    it in the store's single canonical form (UTC, microseconds, trailing
    'Z'), so lexicographic comparison in SQL is chronological comparison.
    Naive or unparseable timestamps are rejected with ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"timestamp must be a string: {value!r}")
    m = _RFC3339.fullmatch(value.strip())
    if m is None:
        raise ValueError(f"timestamp not RFC 3339 with explicit offset (<=6 fraction digits): {value!r}")
    date, clock, fraction, offset = m.groups()
    offset = "+00:00" if offset in ("Z", "z") else offset
    try:
        dt = _dt.datetime.fromisoformat(f"{date}T{clock}.{(fraction or '').ljust(6, '0')}{offset}")
    except ValueError as e:
        raise ValueError(f"timestamp not RFC 3339: {value!r}") from e
    return to_rfc3339(dt)


def _normalize(obj):
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"canonical JSON object keys must be strings: {k!r}")
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                raise ValueError(f"canonical JSON keys collide after NFC normalization: {k!r}")
            out[nk] = _normalize(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    """Compact, key-sorted, NFC-normalized (keys and values) JSON. Non-finite
    numbers (NaN/Infinity) are rejected with ValueError."""
    normalized = _normalize(obj)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_bytes(obj) -> bytes:
    text = canonical_json(obj)
    if "﻿" in text:
        raise ValueError("BOM not permitted in canonical JSON")
    return (text + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj) -> str:
    return sha256_hex(canonical_json_bytes(obj))
