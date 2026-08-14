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



# --------------------------------------------------------------------------- #
# Observation quality
# --------------------------------------------------------------------------- #
# The ledger records everything faithfully and NEVER discards -- an observation
# that looks wrong now may be the evidence later. Judgement lives here instead,
# so every consumer inherits one definition rather than re-deriving it.
#
# The failure this exists for: EAGLE's opening quote read $2.42 of FDV at a
# price of 2.4e-11 while its pool already held $39,899 of liquidity. Anything
# dividing by that -- a multiple, a percent change, a rug threshold -- produces
# nonsense (it showed as 115,747x and flattened the whole chart).
#
# MEASURED over 4,250 observations, fdv/liquidity is a remarkably tight ratio:
#   p0.1 0.236 | p1 0.633 | p25 0.992 | MEDIAN 1.001 | p75 1.406
# The lowest plausible reading in the whole ledger is 0.078. A floor of 0.02 is
# ~4x below that and ~300x above EAGLE, so it separates cleanly with no real
# launch caught. The HIGH side is deliberately not gated: a huge fdv/liq ratio
# just means a large nominal supply, which is odd but not wrong, and it cancels
# out when a pool is measured against its own base.
FDV_LIQ_FLOOR = 0.02

# Depth below which a quote is not a measurement. Set to match the existing
# "no market" threshold rather than inventing a second number for the same idea.
MIN_DEPTH_USD = 500

# Upper sanity bound on a single pool's reserve. The whole chain holds ~$481M of
# TVL, so a pool reporting more than this is a corrupt reading, not a large
# pool: the ledger contains one at $60,520,012,210,442. GeckoTerminal is known
# to emit broken reserves here -- it also reported NEGATIVE liquidity for RBH --
# so both tails need bounding, not just the low one.
MAX_DEPTH_USD = 100_000_000


def usable_liquidity(r):
    """The row's reserve if it is a believable number, else None.

    Separate from observation_quality() on purpose. That answers "can this row
    carry a PRICE", which needs depth behind it; this answers "is this reserve
    reading itself sane", and a drained pool reporting $10 is a perfectly sane
    reading -- indeed it is the one that matters most.

    ⚠️ Rows tagged src="multi" are the retired token-endpoint follow-ups, whose
    `liq` is a token-level total_reserve_in_usd rather than this pool's reserve.
    On this chain that number is simply wrong -- HMM read $6 there while its
    pool held $362,750 -- so those readings are refused outright rather than
    compared against pool-level ones. They stay in the ledger; only their
    liquidity is unusable."""
    if r.get("src") == "multi":
        return None
    liq = r.get("liq")
    if liq is None or liq < 0 or liq > MAX_DEPTH_USD:
        return None
    return liq


def observation_quality(r):
    """None if the row is usable, else a short human reason it is not."""
    fdv, liq = r.get("fdv"), r.get("liq")
    if fdv is None or fdv <= 0:
        return "no fdv"
    # Legacy token-endpoint rows: the FDV is sound (spot-checked against the
    # pool endpoint on three pools -- $42,285, $453,333 and $481,213 all
    # matched), but their `liq` is a different quantity entirely, so the depth
    # tests below cannot run. Keep the price, refuse the depth. Discarding these
    # outright would drop 68% of the window and flatten every trace back to its
    # one or two discovery rows. The BASE is unaffected: discovery rows come
    # from new_pools and are still fully validated.
    if r.get("src") == "multi":
        return None
    # An FDV quoted against no real pool depth is not a low price, it is an
    # unpriceable one -- nobody can trade at it in either direction, so the
    # number is noise rather than a measurement. The ratio test below cannot
    # catch these: it needs liq > 0 to divide at all, and dividing by dust
    # produces a huge ratio that sails through a LOW floor. Measured examples
    # from the ledger: $107,104 of FDV on $1 of liquidity, $379,323 on $1,
    # $319,209 on $22 -- and PAYPOOL's $371 opening quote on $0.49, which
    # rendered as a 64x.
    #
    # NOTE this is emphatically NOT a launch filter. Screening new pools by
    # liquidity LEVEL is backwards here and always has been -- the biggest
    # gainers in the sample opened at $56, $0 and $0. A pool is still tracked
    # from birth; this only decides which of its quotes can carry a number.
    # In practice the base becomes the first reading with a real market behind
    # it, which is exactly the point the multiple should be measured from.
    if not liq or liq < MIN_DEPTH_USD:
        return f"${liq or 0:,.0f} liquidity — no market to price against"
    if (fdv / liq) < FDV_LIQ_FLOOR:
        return f"fdv ${fdv:,.2f} vs ${liq:,.0f} liquidity — mispriced quote"
    return None


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
# pools/multi takes 30 addresses per call and keeps returning the same pool
# record afterwards, which is what lets a trace run past 60 minutes.
#
# ⚠️ MUST be pools/multi, NOT tokens/multi. The token endpoint's
# total_reserve_in_usd is NOT the pool's reserve and is broken on this chain --
# VERIFIED live against three pools: HMM read $6 and $0 at the token endpoint
# while its pools held $362,750 and $385,089 at that same moment. Following
# tokens wrote that number into the same `liq` field that new_pools fills with a
# real pool reserve, so every trace showed a catastrophic liquidity collapse the
# instant it aged out of the feed. That single mismatch manufactured 287 false
# "rugs" out of 900 judged pools. The pool endpoint also returns live
# transaction counts, so the extended rows no longer carry stale ones forward.
MULTI_BATCH = 30
EXTEND_MIN_AGE = 12       # below this the pool is still in the feed
EXTEND_MAX_AGE = 1440     # follow for a full 24h -- see the tiering note below

# HOW OFTEN EACH AGE BAND IS RE-READ, in polls (the loop polls every ~3 min).
#
# MEASURED, and the reason this exists: with a flat 120-address cap taken
# youngest-first, the cap was only ever deep enough to reach the 12-44 minute
# band. Every pool stopped being observed at ~45 minutes old, so no pool in the
# ledger had more than FIVE observations and the median was THREE -- against a
# chart whose axis runs to 24 hours. Peak and last were routinely the same
# point, which is why no rug ever showed the mountain shape: two points cannot
# describe one.
#
# Sampling density should follow where the information is. A memecoin's first
# hour decides it, so that band is read every poll; after that the question is
# only "is it still alive", which a half-hourly read answers just as well.
EXTEND_TIERS = (
    (60,   1),            # first hour      -- every poll, ~3 min
    (360,  4),            # 1h to 6h        -- ~12 min
    (1440, 10),           # 6h to 24h       -- ~30 min
)


def _tier_period(age_min):
    """Polls between re-reads for a pool of this age, or None if past following."""
    for ceiling, period in EXTEND_TIERS:
        if age_min <= ceiling:
            return period
    return None


def extend_ages(ledger=LEDGER, now=None, tick=None):
    """Pools we already know that are past the feed window but inside the
    follow-up window, newest launch first.

    `tick` selects which age tiers are due this run. It is derived from the wall
    clock rather than a stored counter because every poll is a SEPARATE process
    invocation -- there is no in-memory state to increment, and a state file
    would desync the moment a run is cancelled mid-loop."""
    if not Path(ledger).exists():
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    if tick is None:
        tick = int(now.timestamp() // 180)
    born, tok = {}, {}
    for line in Path(ledger).read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Keyed by POOL, because the follow-up reads the pool endpoint. Keying
        # by token also silently collapsed the many same-symbol copycat pools
        # this chain is full of down to whichever one was seen last.
        if not r.get("pool") or not r.get("created_at"):
            continue
        born[r["pool"]] = r["created_at"]
        tok[r["pool"]] = r
    aged = []
    for addr, created in born.items():
        try:
            age = (now - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            continue
        if not EXTEND_MIN_AGE <= age <= EXTEND_MAX_AGE:
            continue
        period = _tier_period(age)
        # Older bands are read on a slower rotation, so most polls skip them and
        # the young band is never crowded out of the batch budget.
        if period is None or tick % period:
            continue
        aged.append((age, addr))
    # YOUNGEST FIRST within whatever is due -- if the budget still binds, it
    # should bind on the pools whose next reading matters least.
    aged.sort()
    return {addr: tok[addr] for _, addr in aged}


def poll_extended(ledger=LEDGER, max_batches=40):
    """Re-read known pools that have aged out of new_pools.

    40 batches is 1,200 addresses -- enough to sweep the entire 24h cohort on
    the one poll in twenty where all three tiers come due at once. At 1.2s
    between calls that worst case costs ~50s of a 180s poll, and every other
    poll is far cheaper."""
    pending = extend_ages(ledger)
    if not pending:
        return []
    now = dt.datetime.now(dt.timezone.utc)
    addrs = list(pending)[: MULTI_BATCH * max_batches]
    obs = []
    for i in range(0, len(addrs), MULTI_BATCH):
        chunk = addrs[i:i + MULTI_BATCH]
        d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/pools/multi/{','.join(chunk)}")
        if not d:
            break
        for item in d.get("data") or []:
            a = item.get("attributes") or {}
            addr = (a.get("address") or "").lower()
            prev = pending.get(addr)
            if not prev:
                continue
            b5, s5, br5, sr5 = _tx(a.get("transactions"), "m5")
            b1, s1, br1, sr1 = _tx(a.get("transactions"), "h1")
            vol = a.get("volume_usd") or {}
            obs.append({
                "ts": now.isoformat(),
                "pool": addr,
                "created_at": a.get("pool_created_at") or prev["created_at"],
                "name": a.get("name") or prev.get("name"),
                # The pool endpoint does not inline the token records, so the
                # token identity carries over from the row that discovered it.
                "token": prev.get("token"),
                "symbol": prev.get("symbol"),
                "token_name": prev.get("token_name"),
                "decimals": prev.get("decimals"),
                "supply": prev.get("supply"),
                "quote": prev.get("quote"),
                "dex": prev.get("dex"),
                # Same field, same meaning as poll(): THIS pool's reserve.
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
                # Distinct from the retired "multi" tag on purpose: those older
                # rows hold a token-level reserve in `liq` and must never be
                # compared against a pool-level one. Consumers key off this.
                "src": "pool_multi",
            })
        time.sleep(PAGE_SLEEP)
    return obs



# --------------------------------------------------------------------------- #
# NFT mint scanner
# --------------------------------------------------------------------------- #
# Same restrictions as the memecoin side: free sources only, bounded call count.
# Discovery is one incremental eth_getLogs per poll against a stored watermark,
# so cost is O(1) per run rather than O(window).
#
# MEASURED: a 3-minute span returns ~6,830 mint logs (1,890 ERC-721) and a
# 6-minute span EXCEEDS the RPC's 10,000-log cap outright. So the per-run span
# is capped near 3 minutes and the watermark catches up over successive polls
# instead of widening the query.
#
# ERC-721 is discriminated from ERC-20 by topic count: a 721 Transfer indexes
# three params (from, to, tokenId) so it carries topic3; an ERC-20 Transfer
# indexes two and does not.
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64
NFT_REGISTRY = OUT_DIR / "nft_registry.json"
NFT_LEDGER = OUT_DIR / "nft_launches.jsonl"
MAX_SPAN_BLOCKS = 1700          # ~2.9 min at ~101ms blocks, under the 10k cap
MINTER_CAP = 4000               # per-collection minter set ceiling, keeps the file bounded


def _rpc(method, params, tries=3, timeout=60):
    """Returns (result, error_message). The error matters: a 10k-log cap hit is
    recoverable by narrowing the range, while a transport failure is not."""
    for attempt in range(tries):
        try:
            r = requests.post(RPC_URL, timeout=timeout,
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            j = r.json()
            if "error" in j:
                return None, str(j["error"].get("message") or j["error"])
            return j.get("result"), None
        except (requests.RequestException, ValueError) as e:
            if attempt == tries - 1:
                return None, str(e)[:80]
            time.sleep(1.2 * (attempt + 1))
    return None, "exhausted retries"


def scan_nft_mints(registry_path=NFT_REGISTRY, ledger=NFT_LEDGER, window_hours=6,
                   max_calls=14):
    """Incremental sweep of ERC-721 mints, catching up over several chunks.

    ONE chunk is not enough. The chain produces ~594 blocks/min, so a 10-minute
    poll must cover ~5,940 blocks, while a single getLogs call caps out around
    850-1,700 before hitting the 10,000-log limit. Scanning one chunk per poll
    would fall behind forever. The loop below keeps consuming chunks until it
    catches the head or spends its call budget, so the watermark converges
    instead of drifting.
    """
    reg = {}
    if Path(registry_path).exists():
        try:
            reg = json.loads(Path(registry_path).read_text())
        except json.JSONDecodeError:
            reg = {}

    head_hex, err = _rpc("eth_blockNumber", [])
    if not head_hex:
        return {"ok": False, "reason": f"no head block ({err})"}
    head = int(head_hex, 16)
    last = int(reg.get("_watermark") or 0)
    if not last:
        last = head - MAX_SPAN_BLOCKS          # cold start: one span back
    if head <= last:
        return {"ok": True, "scanned": 0, "collections": 0, "behind": 0}

    # Mint volume is not stable: a 1,700-block span measured 6,830 logs one day
    # and blew the 10,000 cap the next. Narrow-and-retry per chunk, and keep
    # taking chunks until caught up or the call budget is spent.
    now_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    all_logs, calls, cursor, span = [], 0, last + 1, MAX_SPAN_BLOCKS
    while cursor <= head and calls < max_calls:
        want = min(span, head - cursor + 1)
        logs, err = _rpc("eth_getLogs", [{"fromBlock": hex(cursor),
                                          "toBlock": hex(cursor + want - 1),
                                          "topics": [TRANSFER_TOPIC, ZERO_TOPIC]}])
        calls += 1
        if logs is None:
            if err and ("exceeds" in err.lower() or "limit" in err.lower()) and span > 100:
                span //= 2                 # cap hit: narrow and retry this range
                continue
            break                          # transport error: stop, keep the watermark
        all_logs.extend(logs)
        cursor += want
        # creep back up so a quiet period is not scanned in tiny slices forever
        if len(logs) < 4000 and span < MAX_SPAN_BLOCKS:
            span = min(int(span * 1.5), MAX_SPAN_BLOCKS)
    to = cursor - 1
    if to < last + 1:
        return {"ok": False, "reason": "no range scanned", "behind": head - last}
    logs = all_logs

    now = now_ts
    touched = set()
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) != 4:                    # ERC-20 Transfer, not 721
            continue
        addr = (lg.get("address") or "").lower()
        to_addr = "0x" + topics[2][-40:]
        e = reg.setdefault(addr, {"first_block": int(lg["blockNumber"], 16),
                                  "first_seen": now, "mints": 0, "minters": [],
                                  "last_seen": now})
        e["mints"] += 1
        e["last_seen"] = now
        if len(e["minters"]) < MINTER_CAP and to_addr not in e["minters"]:
            e["minters"].append(to_addr)
        touched.add(addr)

    reg["_watermark"] = to
    Path(registry_path).parent.mkdir(parents=True, exist_ok=True)
    Path(registry_path).write_text(json.dumps(reg, separators=(",", ":")))

    # snapshot only collections born inside the window -- that is the feed
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=window_hours)
    rows = []
    for addr in touched:
        e = reg[addr]
        try:
            born = dt.datetime.fromisoformat(e["first_seen"])
        except ValueError:
            continue
        if born < cutoff:
            continue
        rows.append({"ts": now, "c": addr, "first_seen": e["first_seen"],
                     "mints": e["mints"], "minters": len(e["minters"]),
                     "first_block": e["first_block"]})
    if rows:
        with Path(ledger).open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    return {"ok": True, "scanned": to - last, "logs": len(logs), "calls": calls,
            "collections": len(rows), "behind": head - to}



def prune_ledger(path, days=7):
    """Trim the ledger to the last N days.

    Committing this file every 10 minutes would otherwise grow the repo without
    bound (~1.5MB/day at the measured launch rate). Seven days is well past the
    24h the chart reads and the 48h the thresholds calibrate on, while keeping
    the repo small enough that a clone stays fast.
    """
    p = Path(path)
    if not p.exists():
        return 0
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    kept, dropped = [], 0
    for line in p.read_text().splitlines():
        try:
            if json.loads(line).get("ts", "") >= cutoff:
                kept.append(line)
            else:
                dropped += 1
        except json.JSONDecodeError:
            dropped += 1
    if dropped:
        p.write_text("\n".join(kept) + ("\n" if kept else ""))
    return dropped


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
    ap.add_argument("--prune-days", type=int, default=0,
                    help="drop ledger rows older than N days (0 = keep all)")
    ap.add_argument("--no-nft", action="store_true", help="skip the ERC-721 mint scan")
    ap.add_argument("--no-extend", action="store_true",
                    help="skip the pools/multi follow-up for aged-out pools")
    args = ap.parse_args()

    t0 = time.time()
    obs = poll(pages=args.pages)
    ext = [] if args.no_extend else poll_extended(Path(args.out))
    if not obs:
        print("no pools returned (upstream down or rate-limited); nothing written")
        return 1

    nft = None if args.no_nft else scan_nft_mints()
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

    if nft and nft.get("ok"):
        print(f"  nft: {nft['scanned']:,} blocks, {nft.get('logs',0):,} mint logs, "
              f"{nft['collections']} new collections, {nft['behind']:,} blocks behind")
    elif nft:
        print(f"  nft: skipped ({nft.get('reason')})")
    if args.prune_days:
        d1 = prune_ledger(Path(args.out), args.prune_days)
        d2 = prune_ledger(NFT_LEDGER, args.prune_days)
        if d1 or d2:
            print(f"  pruned {d1:,} + {d2:,} rows older than {args.prune_days}d")
    if gap:
        note = "  ** GAP SUSPECTED: raise --pages **" if gap["gap_suspected"] else ""
        print(f"  {gap['new_to_us']}/{gap['polled']} pools new to the ledger{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
