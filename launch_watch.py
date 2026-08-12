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



# GeckoTerminal drops a pool from new_pools once ~53 minutes of newer launches
# have arrived, so the feed alone cannot follow a pool through its first hour.
# tokens/multi takes 30 addresses per call and keeps returning price/FDV/reserve
# after that, which is what lets a trace run to 60 minutes and beyond.
MULTI_BATCH = 30
EXTEND_MIN_AGE = 12       # below this the pool is still in the feed
EXTEND_MAX_AGE = 180      # stop following after 3h


def extend_ages(ledger=LEDGER, now=None):
    """Pools we already know that are past the feed window but inside the
    follow-up window, newest launch first."""
    if not Path(ledger).exists():
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    born, tok = {}, {}
    for line in Path(ledger).read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("token") or not r.get("created_at"):
            continue
        born[r["token"]] = r["created_at"]
        tok[r["token"]] = r
    out = {}
    for addr, created in born.items():
        try:
            age = (now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            continue
        if EXTEND_MIN_AGE <= age <= EXTEND_MAX_AGE:
            out[addr] = tok[addr]
    return out


def poll_extended(ledger=LEDGER, max_batches=4):
    """Re-read known tokens that have aged out of new_pools."""
    pending = extend_ages(ledger)
    if not pending:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    addrs = list(pending)[: MULTI_BATCH * max_batches]
    obs = []
    for i in range(0, len(addrs), MULTI_BATCH):
        chunk = addrs[i:i + MULTI_BATCH]
        d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/tokens/multi/{','.join(chunk)}")
        if not d:
            break
        for item in d.get("data") or []:
            a = item.get("attributes") or {}
            addr = (a.get("address") or "").lower()
            prev = pending.get(addr)
            if not prev:
                continue
            obs.append({
                "ts": now.isoformat(),
                "pool": prev["pool"],
                "created_at": prev["created_at"],
                "name": prev.get("name"),
                "token": addr,
                "symbol": a.get("symbol") or prev.get("symbol"),
                "token_name": a.get("name"),
                "decimals": a.get("decimals"),
                "supply": _f(a.get("normalized_total_supply")),
                "quote": prev.get("quote"),
                "dex": prev.get("dex"),
                "liq": _f(a.get("total_reserve_in_usd")),
                "price": _f(a.get("price_usd")),
                "fdv": _f(a.get("fdv_usd")),
                "mcap": _f(a.get("market_cap_usd")),
                "vol_h24": _f((a.get("volume_usd") or {}).get("h24")),
                # tokens/multi carries no tx breakdown -- carry the last known
                # counts forward so the verdict rules still have a value, and
                # mark the row so they can be excluded if that matters.
                "vol_m5": None, "vol_h1": None,
                "buys_m5": prev.get("buys_m5"), "sells_m5": prev.get("sells_m5"),
                "buyers_m5": prev.get("buyers_m5"), "sellers_m5": prev.get("sellers_m5"),
                "buys_h1": prev.get("buys_h1"), "sells_h1": prev.get("sells_h1"),
                "buyers_h1": prev.get("buyers_h1"), "sellers_h1": prev.get("sellers_h1"),
                "chg_m5": None, "chg_h1": None,
                "src": "multi",
            })
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
    ap.add_argument("--no-extend", action="store_true",
                    help="skip the tokens/multi follow-up for aged-out pools")
    args = ap.parse_args()

    t0 = time.time()
    obs = poll(pages=args.pages)
    ext = [] if args.no_extend else poll_extended(Path(args.out))
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
            for o in obs + ext:
                fh.write(json.dumps(o, separators=(",", ":")) + "\n")
        print(f"appended {len(obs)} new + {len(ext)} followed observations "
              f"to {out} in {time.time()-t0:.1f}s")

    if gap:
        note = "  ** GAP SUSPECTED: raise --pages **" if gap["gap_suspected"] else ""
        print(f"  {gap['new_to_us']}/{gap['polled']} pools new to the ledger{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
