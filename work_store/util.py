"""Canonical serialization and time helpers shared across the store."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import unicodedata


def now_rfc3339() -> str:
    return to_rfc3339(_dt.datetime.now(_dt.timezone.utc))


def to_rfc3339(dt: _dt.datetime) -> str:
    dt = dt.astimezone(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def _normalize(obj):
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def canonical_json(obj) -> str:
    """Compact, key-sorted, NFC-normalized JSON. Objects only (no NaN/Inf)."""
    normalized = _normalize(obj)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
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
