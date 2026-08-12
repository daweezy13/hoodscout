#!/usr/bin/env python3
"""
launch_watch.py — Tier 1 poller for the "just launched" feed.

Appends one observation per live pool to out/launches.jsonl every few minutes.
It does nothing else: no scoring, no rendering, no network beyond two calls.

WHY THIS EXISTS AS A SEPARATE, FREQUENT JOB
-------------------------------------------
Measured 2026-08-12 against the live chain:

  * GeckoTerminal's new_pools feed reaches back ~53 minutes (10 pages x 20).
    Pools launch at ~222/hr. A once-daily job therefore cannot see ~96% of the
    day's launches. Polling is not an optimisation here -- it is the only way
    to have the data at all.

  * A snapshot cannot tell a launch from a rug. Of 100 consecutive new pools,
    the SEVEN richest-looking ones all lost 92-99.6% of liquidity within 45
    minutes (BLINK $89,583 -> $2,581), while the three biggest gainers of the
    hour read $56, $0 and $0 of liquidity when first seen (MAUS -> $27,862).
    Any threshold on absolute liquidity at t=0 is not merely weak, it is
    backwards. Every useful signal is a DELTA, and a delta needs history.

So this file's only job is to accumulate that history, faithfully and without
interpretation. Judgement happens later, over the ledger.

The ledger is append-only on purpose. Observations are never overwritten or
compacted here: the sequence IS the product, and it cannot be reconstructed
afterwards because the upstream feed only exposes the last ~53 minutes.

Usage:
    python3 launch_watch.py              # one poll, append, exit
    python3 launch_watch.py --pages 3    # deeper history (bursts)
    python3 launch_watch.py --dry-run    # print, write nothing
"""
import os
import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path

import requests

GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
GT_NETWORK = "robinhood"
OUT_DIR = Path(__file__).parent / "out"
LEDGER = OUT_DIR / "launches.jsonl"

# 20 pools/page. At the measured ~3.7 pools/min, 2 pages ~= 10.8 minutes of
# coverage against a 3-minute poll -- a 3.6x margin for launch bursts. Raise
# --pages if the ledger ever shows a gap (see gap detection below).
DEFAULT_PAGES = 2
PAGE_SLEEP = 1.2          # GT 429s after ~5 rapid calls


def _get(url, params=None, tries=4, timeout=30):
    """GET -> parsed JSON, or None. Never raises: this runs unattended every
    few minutes and a transient failure must cost one observation, not the
    whole ledger."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "hoodscout-launchwatch/1.0"})
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == tries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tx(block, window):
    """transactions{window} -> (buys, sells, buyers, sellers), zeros if absent."""
    w = (block or {}).get(window) or {}
    return (w.get("buys") or 0, w.get("sells") or 0,
            w.get("buyers") or 0, w.get("sellers") or 0)


def poll(pages=DEFAULT_PAGES):
    """One sweep of new_pools. Returns a list of observation dicts."""
    now = dt.datetime.now(dt.timezone.utc)
    seen, obs = set(), []

    for page in range(1, pages + 1):
        d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/new_pools",
                 params={"include": "base_token,quote_token", "page": page})
        if not d:
            break
        inc = {i["id"]: i for i in (d.get("included") or [])}

        for p in d.get("data") or []:
            a = p.get("attributes") or {}
            rel = p.get("relationships") or {}
            addr = (a.get("address") or "").lower()
            if not addr or addr in seen:
                continue          # same pool can appear twice across pages
            seen.add(addr)

            base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id") or ""
            base = (inc.get(base_id) or {}).get("attributes") or {}
            quote = (inc.get(quote_id) or {}).get("attributes") or {}

            b5, s5, br5, sr5 = _tx(a.get("transactions"), "m5")
            b1, s1, br1, sr1 = _tx(a.get("transactions"), "h1")
            vol = a.get("volume_usd") or {}

            obs.append({
                "ts": now.isoformat(),
                "pool": addr,
                "created_at": a.get("pool_created_at"),
                "name": a.get("name"),
                "token": (base.get("address") or "").lower() or None,
                "symbol": base.get("symbol"),
                "token_name": base.get("name"),
                "decimals": base.get("decimals"),
                # GT's own normalised supply. Blockscout's total_supply is wrong
                # by six orders of magnitude on fresh tokens -- do not use it.
                "supply": _f(base.get("normalized_total_supply")),
                "quote": quote.get("symbol"),
                "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
                "liq": _f(a.get("reserve_in_usd")),
                "price": _f(a.get("base_token_price_usd")),
                "fdv": _f(a.get("fdv_usd")),
                "mcap": _f(a.get("market_cap_usd")),
                "vol_m5": _f(vol.get("m5")),
                "vol_h1": _f(vol.get("h1")),
                "vol_h24": _f(vol.get("h24")),
                "buys_m5": b5, "sells_m5": s5, "buyers_m5": br5, "sellers_m5": sr5,
                "buys_h1": b1, "sells_h1": s1, "buyers_h1": br1, "sellers_h1": sr1,
                "chg_m5": _f((a.get("price_change_percentage") or {}).get("m5")),
                "chg_h1": _f((a.get("price_change_percentage") or {}).get("h1")),
            })

        if page < pages:
            time.sleep(PAGE_SLEEP)

    return obs


def coverage_gap(obs, ledger=LEDGER):
    """Did we miss any launches between polls?

    The oldest pool in this sweep should be older than the newest pool we had
    never seen before. If every pool in the sweep is new to us, the feed moved
    further than our window and launches fell through the gap -- the signal to
    raise --pages.
    """
    if not obs or not ledger.exists():
        return None
    known = set()
    try:
        with ledger.open() as fh:
            for line in fh:
                try:
                    known.add(json.loads(line).get("pool"))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    fresh = [o for o in obs if o["pool"] not in known]
    return {"polled": len(obs), "new_to_us": len(fresh),
            "gap_suspected": len(fresh) == len(obs) and len(obs) > 0}


def main():
    ap = argparse.ArgumentParser(description="Poll new pools into the launch ledger")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    ap.add_argument("--out", default=str(LEDGER))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    obs = poll(pages=args.pages)
    if not obs:
        print("no pools returned (upstream down or rate-limited); nothing written")
        return 1

    gap = coverage_gap(obs, Path(args.out))

    if args.dry_run:
        for o in obs[:8]:
            print(f"  {(o['symbol'] or '?')[:14]:<15} liq=${o['liq'] or 0:>10,.0f} "
                  f"vol_h1=${o['vol_h1'] or 0:>10,.0f} "
                  f"b/s={o['buys_h1']}/{o['sells_h1']} "
                  f"buyers={o['buyers_h1']} created={o['created_at']}")
        print(f"\n[dry run] {len(obs)} observations, {time.time()-t0:.1f}s")
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as fh:
            for o in obs:
                fh.write(json.dumps(o, separators=(",", ":")) + "\n")
        print(f"appended {len(obs)} observations to {out} in {time.time()-t0:.1f}s")

    if gap:
        note = "  ** GAP SUSPECTED: raise --pages **" if gap["gap_suspected"] else ""
        print(f"  {gap['new_to_us']}/{gap['polled']} pools new to the ledger{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
