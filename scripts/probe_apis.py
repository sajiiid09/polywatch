#!/usr/bin/env python3
"""STEP 1 probe: hit Polymarket's public APIs and dump raw JSON to disk.

Throwaway. Deliberately has no parsers, no models, no schema, no DB -- the whole
point is to learn the real field names before any of that gets written.

Read-only: GET only, no auth headers, no signing, no keys. Never trades.
Stdlib only, so it runs before any environment exists.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "probe"
UA = "polywatch-probe/0.0.1 (read-only analytics)"
TIMEOUT = 30


def get(url, params=None):
    """GET a URL. Returns (status, raw_bytes, parsed_or_None, error_or_None)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:  # noqa: BLE001 - a dead endpoint is a finding, not a crash
        return None, b"", None, f"{type(e).__name__}: {e}"
    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return status, raw, None, f"not JSON: {type(e).__name__}: {e}"
    return status, raw, parsed, None


def sample_of(parsed):
    """Pull one representative record out of whatever shape came back."""
    if isinstance(parsed, list):
        return (parsed[0] if parsed else None), len(parsed), "list"
    if isinstance(parsed, dict):
        # common envelope shapes: {"data": [...]}, {"history": [...]}, {"results": [...]}
        for key in ("data", "history", "results", "trades", "markets", "leaderboard"):
            v = parsed.get(key)
            if isinstance(v, list):
                return (v[0] if v else None), len(v), f"dict envelope -> '{key}' list"
        return parsed, 1, "dict"
    return parsed, 1, type(parsed).__name__


def probe(name, url, params=None):
    """Fetch, dump raw bytes to disk BEFORE reading anything out of them, then summarize."""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    print(f"\n=== {name}\n    GET {full}")
    status, raw, parsed, err = get(url, params)

    path = OUT / f"{name}.json"
    path.write_bytes(raw)
    meta = OUT / f"{name}.meta.json"
    meta.write_text(json.dumps({"url": full, "status": status, "bytes": len(raw), "error": err}, indent=2))
    print(f"    status={status} bytes={len(raw)} -> {path.relative_to(OUT.parent.parent.parent)}")

    if err:
        print(f"    ERROR: {err}")
        print(f"    body[:400]: {raw[:400]!r}")
        return None
    if status != 200:
        print(f"    NON-200 body[:400]: {raw[:400]!r}")
        return parsed

    rec, count, shape = sample_of(parsed)
    print(f"    shape={shape} records={count}")
    if isinstance(rec, dict):
        print(f"    top-level keys of one record ({len(rec)}):")
        for k in rec:
            v = rec[k]
            tv = type(v).__name__
            preview = json.dumps(v)[:70] if not isinstance(v, (dict, list)) else f"<{tv}>"
            print(f"      - {k}: {tv} = {preview}")
    else:
        print(f"    sample record (not a dict): {json.dumps(rec)[:300]}")
    return parsed


def first_str(obj, keys):
    """Find the first non-empty string value among `keys`, searching nested dicts one level."""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            if isinstance(v, dict):
                got = first_str(v, keys)
                if got:
                    return got
    return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"dumping raw JSON to {OUT}")

    # --- probe 1: gamma markets (closed=true so we get resolved markets, the ones scoring needs)
    gamma = probe("1_gamma_markets", "https://gamma-api.polymarket.com/markets",
                  {"limit": 5, "closed": "true"})
    time.sleep(0.5)

    # --- probe 2: leaderboard. Bare path 404s; /v1/leaderboard is the live one.
    #     Try param variants to learn which windowing/ranking knobs exist.
    lb = None
    variants = [
        ("2a_leaderboard_v1_bare", {"limit": 10}),
        ("2b_leaderboard_v1_window", {"limit": 10, "window": "all"}),
        ("2c_leaderboard_v1_rankby", {"limit": 10, "window": "all", "rankBy": "pnl"}),
        ("2d_leaderboard_v1_vol", {"limit": 10, "window": "all", "rankBy": "vol"}),
    ]
    for name, params in variants:
        got = probe(name, "https://data-api.polymarket.com/v1/leaderboard", params)
        time.sleep(0.5)
        if got and lb is None:
            rec, count, _ = sample_of(got)
            if count and rec is not None:
                lb = got
                print(f"    -> using {name} as the leaderboard sample")

    # --- probe 3: trades for the top leaderboard wallet
    addr = None
    trades = None
    if lb is not None:
        rec, _, _ = sample_of(lb)
        addr = first_str(rec, ["proxyWallet", "proxy_wallet", "wallet", "user", "address",
                               "userAddress", "account"])
    if not addr:
        print("\n=== 3_trades SKIPPED: leaderboard gave no usable address.")
    else:
        print(f"\n(top leaderboard address: {addr})")
        trades = probe("3a_trades_by_user", "https://data-api.polymarket.com/trades",
                       {"user": addr, "limit": 5})
        time.sleep(0.5)
        # pagination shape: does offset work, and is the response ordered?
        probe("3b_trades_by_user_offset", "https://data-api.polymarket.com/trades",
              {"user": addr, "limit": 5, "offset": 5})
        time.sleep(0.5)

    # --- probe 3c: global trades feed, no user filter.
    #     This is the candidate sampling frame for Step 5's control group.
    feed = probe("3c_trades_global_feed", "https://data-api.polymarket.com/trades", {"limit": 5})
    time.sleep(0.5)

    # --- probe 4: prices-history. Needs a CLOB token id from a RECENT trade, because an
    #     asset from a 2020 market has no data in any window we can ask for.
    token = None
    for src in (trades, feed):
        if token or src is None:
            continue
        rec, _, _ = sample_of(src)
        token = first_str(rec, ["asset", "assetId", "asset_id", "tokenId", "token_id"])

    if not token:
        print("\n=== 4_prices_history SKIPPED: no CLOB token id found in probes 3a/3c.")
    else:
        print(f"\n(clob token id: {token})")
        # 4a: interval windowing. fidelity=1 was rejected for '1m' (min is 10), so ask for 10.
        probe("4a_prices_history_interval", "https://clob.polymarket.com/prices-history",
              {"market": token, "interval": "1m", "fidelity": 10})
        time.sleep(0.5)
        # 4b: explicit start/end at minute fidelity -- this is the call Step 3 needs for
        #     bounded per-trade windows, so confirm it accepts fidelity=1.
        now = int(time.time())
        probe("4b_prices_history_startts", "https://clob.polymarket.com/prices-history",
              {"market": token, "startTs": now - 86400, "endTs": now, "fidelity": 1})
        time.sleep(0.5)

    # --- probe 5: market metadata by conditionId -- the trades -> markets join Step 3 depends on.
    cond = None
    for src in (trades, feed):
        if cond or src is None:
            continue
        rec, _, _ = sample_of(src)
        cond = first_str(rec, ["conditionId", "condition_id"])
    if cond:
        print(f"\n(condition id: {cond})")
        probe("5_gamma_market_by_condition", "https://gamma-api.polymarket.com/markets",
              {"condition_ids": cond})
    else:
        print("\n=== 5_gamma_market_by_condition SKIPPED: no conditionId found in trades.")

    print(f"\ndone. raw dumps in {OUT}")


if __name__ == "__main__":
    sys.exit(main())
