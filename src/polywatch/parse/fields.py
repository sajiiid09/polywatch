"""Strict field access. Every read of upstream JSON goes through here.

The point is blast radius: when Polymarket renames a field, exactly one kind of exception is
raised, it names the field, the record type and the offending record, and nothing downstream
silently sees a None.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable


class FieldError(KeyError):
    """An upstream field is missing, null, or the wrong type -- i.e. schema drift."""


def req(rec: dict, key: str, cast: Callable[[Any], Any], ctx: str) -> Any:
    if key not in rec:
        raise FieldError(f"{ctx}: missing required field {key!r}; keys present: {sorted(rec)}")
    val = rec[key]
    if val is None:
        raise FieldError(f"{ctx}: field {key!r} is null")
    try:
        return cast(val)
    except (TypeError, ValueError) as e:
        raise FieldError(f"{ctx}: field {key!r}={val!r} not castable by {cast.__name__}: {e}") from e


def opt(rec: dict, key: str, cast: Callable[[Any], Any], ctx: str, default=None) -> Any:
    """For fields that are genuinely optional upstream (absent on old or open markets)."""
    val = rec.get(key)
    if val is None or val == "":
        return default
    try:
        return cast(val)
    except (TypeError, ValueError):
        return default


def json_str(rec: dict, key: str, ctx: str, default=None):
    """Gamma ships arrays as JSON-ENCODED STRINGS: outcomes, outcomePrices, clobTokenIds,
    umaResolutionStatuses. Decode defensively -- some are already real lists."""
    val = rec.get(key)
    if val is None:
        return default
    if isinstance(val, (list, dict)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError as e:
            raise FieldError(f"{ctx}: field {key!r} is not decodable JSON: {val[:80]!r}") from e
    raise FieldError(f"{ctx}: field {key!r} has unexpected type {type(val).__name__}")


def iso_ts(rec: dict, key: str, ctx: str) -> int | None:
    """ISO-8601 -> unix seconds. Gamma mixes 'Z', '+00:00' and '2020-11-02 16:31:01+00' forms."""
    val = rec.get(key)
    if not val or not isinstance(val, str):
        return None
    s = val.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("+00"):
        s += ":00"
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None
