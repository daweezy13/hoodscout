#!/usr/bin/env python3
"""Independent cross-check of HoodScout's headline numbers against Dune.

Why this exists: every stat on the dashboard has exactly one source and no
second opinion. Blockscout's `activeAccounts` methodology in particular is
undocumented, so we report a number whose definition we cannot state. Dune
holds the raw chain, so the same quantities can be recomputed from an explicit
definition and compared.

What it does NOT do: TVL and stablecoin supply are valuation/attribution
problems, not raw-chain questions -- DefiLlama stays authoritative there and
this script does not touch them.

Only raw/decoded tables exist for this chain (no curated spellbook -- no
dex.trades, no nft.trades), so every query below is written against
robinhood.transactions / robinhood.logs directly.

Usage:
    python3 verify_dune.py                  # verify against out/pulse.json
    python3 verify_dune.py --days 14
    python3 verify_dune.py --rebuild        # force-recreate the saved queries
"""
import os
import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path

import requests

DUNE = "https://api.dune.com/api/v1"
OUT_DIR = Path(__file__).parent / "out"
QUERY_CACHE = OUT_DIR / "dune_queries.json"
SEAPORT = "0x0000000000000068f116a894984e2db1123eb395"
ORDER_FULFILLED = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"

# Divergence bands. These are deliberately loose: two indexers counting the
# same chain will never agree exactly (reorg handling, ingestion lag, and in
# DAU's case genuinely different definitions of "active"). The point is to
# catch order-of-magnitude disagreement, not to chase decimals.
AGREE_PCT = 5.0        # within 5% -> agree
WARN_PCT = 20.0        # 5-20% -> minor divergence; beyond -> flagged


def _load_env():
    f = Path(__file__).parent / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()


# --------------------------------------------------------------------------- #
# Dune client
# --------------------------------------------------------------------------- #
class Dune:
    def __init__(self, key=None, performance="medium"):
        self.key = key or os.environ.get("DUNE_API_KEY")
        if not self.key:
            raise SystemExit("DUNE_API_KEY not set (put it in .env beside this script)")
        self.h = {"X-Dune-API-Key": self.key}
        self.performance = performance

    def create(self, name, sql):
        r = requests.post(f"{DUNE}/query", headers=self.h, timeout=60,
                          json={"name": name, "query_sql": sql, "is_private": True})
        r.raise_for_status()
        return r.json()["query_id"]

    def update(self, qid, sql):
        r = requests.patch(f"{DUNE}/query/{qid}", headers=self.h, timeout=60,
                           json={"query_sql": sql})
        r.raise_for_status()
        return qid

    def run(self, qid, params=None, poll=4, timeout=600):
        """Execute and block until results land. Returns the rows."""
        body = {"performance": self.performance}
        if params:
            body["query_parameters"] = params
        r = requests.post(f"{DUNE}/query/{qid}/execute", headers=self.h,
                          json=body, timeout=60)
        r.raise_for_status()
        eid = r.json()["execution_id"]

        waited = 0
        while waited < timeout:
            time.sleep(poll)
            waited += poll
            s = requests.get(f"{DUNE}/execution/{eid}/status",
                             headers=self.h, timeout=30).json()
            st = s.get("state")
            if st == "QUERY_STATE_COMPLETED":
                break
            if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                raise RuntimeError(f"dune execution {st}: {s.get('error')}")
        else:
            raise TimeoutError(f"dune query {qid} still running after {timeout}s")

        res = requests.get(f"{DUNE}/execution/{eid}/results",
                           headers=self.h, timeout=120).json()
        return res.get("result", {}).get("rows", [])


# --------------------------------------------------------------------------- #
# Queries -- explicit definitions, which is the whole point of the exercise
# --------------------------------------------------------------------------- #
# "Active user" = an address that SENT at least one transaction that day.
# Stating it plainly is the value here; Blockscout's activeAccounts may well
# count something else (recipients, internal txns), and a stable offset
# between the two is itself the finding.
Q_DAU = """
select
    date_trunc('day', block_time) as day,
    count(distinct "from")        as active_senders,
    count(*)                      as txns
from robinhood.transactions
where block_time >= date_trunc('day', now() - interval '{days}' day)
  and block_time <  date_trunc('day', now())
group by 1
order by 1 desc
"""

# Gas actually paid.
#
# MUST use effective_gas_price, not gas_price. On this chain gas_price holds
# the price the sender SET (0.3 gwei on a sampled tx) while the protocol
# actually charged effective_gas_price (0.0276 gwei) -- a ~10.9x gap that
# floats with the base fee. Using gas_price overstated daily fees by 7-25x
# with a ratio that varied day to day, which is what made it look like a real
# divergence rather than a column mistake. Confirmed against RPC receipts:
# one sampled block held 0.0000680 ETH of fees, which at ~862k blocks/day
# extrapolates to ~59 ETH/day and matches Blockscout, not gas_price's ~322.
Q_GAS = """
select
    date_trunc('day', block_time) as day,
    sum(cast(gas_used as double) * cast(effective_gas_price as double)) / 1e18 as gas_eth
from robinhood.transactions
where block_time >= date_trunc('day', now() - interval '{days}' day)
  and block_time <  date_trunc('day', now())
group by 1
order by 1 desc
"""

# Direct check on the RPC scanner's completeness. Our chunked eth_getLogs walk
# splits and retries around a 10k-log cap; a silently dropped chunk would show
# up here as Dune seeing more fills than we did over the identical blocks.
Q_SEAPORT_RANGE = """
select count(*) as fills
from robinhood.logs
where contract_address = {seaport}
  and topic0 = {topic0}
  and block_number >= {from_block}
  and block_number <= {to_block}
"""

# History the RPC path cannot practically reach: ~96k fills/day against a
# 10k-log cap makes 30 days ~450 chunked calls. One query here.
Q_SEAPORT_DAILY = """
select
    date_trunc('day', block_time) as day,
    count(*)                      as fills
from robinhood.logs
where contract_address = {seaport}
  and topic0 = {topic0}
  and block_time >= date_trunc('day', now() - interval '{days}' day)
  and block_time <  date_trunc('day', now())
group by 1
order by 1 desc
"""


def _sql(template, **kw):
    return template.format(**kw)


def _load_cache():
    if QUERY_CACHE.exists():
        return json.loads(QUERY_CACHE.read_text())
    return {}


def _save_cache(c):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    QUERY_CACHE.write_text(json.dumps(c, indent=2))


def get_query(d, cache, name, sql, rebuild=False):
    """Create a saved Dune query once, then reuse its id across runs.

    Recreating a query every run would leak hundreds of saved queries into the
    account, so ids are cached and the SQL is PATCHed when it changes.
    """
    if not rebuild and name in cache:
        qid = cache[name]["id"]
        if cache[name].get("sql") != sql:
            d.update(qid, sql)
            cache[name]["sql"] = sql
            _save_cache(cache)
        return qid
    qid = d.create(f"hoodscout/{name}", sql)
    cache[name] = {"id": qid, "sql": sql}
    _save_cache(cache)
    return qid


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def _pct_diff(a, b):
    if not a or not b:
        return None
    return abs(a - b) / max(abs(a), abs(b)) * 100.0


def _verdict(pct):
    if pct is None:
        return "no-data"
    if pct <= AGREE_PCT:
        return "agree"
    if pct <= WARN_PCT:
        return "minor"
    return "FLAGGED"


def _row_by_day(rows, key):
    """Dune returns ISO timestamps; index by plain YYYY-MM-DD for joining."""
    out = {}
    for r in rows:
        day = str(r.get("day", ""))[:10]
        if day:
            out[day] = r.get(key)
    return out


def compare(pulse, dune_dau, dune_gas, eth_price):
    """Join Dune's daily series onto Blockscout's by UTC date."""
    stats = pulse.get("stats", {})
    checks = []

    bs_dau = {p["date"][:10]: p["value"] for p in (stats.get("dau_series") or [])
              if p.get("date")}
    dn_dau = _row_by_day(dune_dau, "active_senders")
    for day in sorted(set(bs_dau) & set(dn_dau), reverse=True):
        a, b = bs_dau[day], dn_dau[day]
        checks.append({"metric": "DAU", "day": day, "blockscout": a, "dune": b,
                       "pct_diff": _pct_diff(a, b), "verdict": _verdict(_pct_diff(a, b))})

    bs_gas = {p["date"][:10]: p["eth"] for p in (stats.get("gas_fees_series") or [])
              if p.get("date")}
    dn_gas = _row_by_day(dune_gas, "gas_eth")
    for day in sorted(set(bs_gas) & set(dn_gas), reverse=True):
        a, b = bs_gas[day], dn_gas[day]
        checks.append({"metric": "Gas (ETH)", "day": day, "blockscout": a, "dune": b,
                       "pct_diff": _pct_diff(a, b), "verdict": _verdict(_pct_diff(a, b)),
                       "usd_dune": (b * eth_price) if (b and eth_price) else None})
    return checks


def main():
    ap = argparse.ArgumentParser(description="Cross-check HoodScout against Dune")
    ap.add_argument("--pulse", default=str(OUT_DIR / "pulse.json"))
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--rebuild", action="store_true",
                    help="force-recreate the saved Dune queries")
    ap.add_argument("--out", default=str(OUT_DIR / "dune_verify.json"))
    ap.add_argument("--performance", default="medium", choices=["medium", "large"])
    args = ap.parse_args()

    pulse_path = Path(args.pulse)
    if not pulse_path.exists():
        raise SystemExit(f"{pulse_path} not found -- run chain_pulse.py first")
    pulse = json.loads(pulse_path.read_text())
    eth_price = (pulse.get("stats") or {}).get("eth_price_usd")

    d = Dune(performance=args.performance)
    cache = _load_cache()
    t0 = time.time()

    print("Dune cross-check — recomputing from raw chain data", flush=True)

    print("  [1/4] DAU (count distinct tx senders per UTC day)...", flush=True)
    qid = get_query(d, cache, "dau", _sql(Q_DAU, days=args.days), args.rebuild)
    dune_dau = d.run(qid)

    print("  [2/4] Gas fees (sum gas_used * gas_price)...", flush=True)
    qid = get_query(d, cache, "gas", _sql(Q_GAS, days=args.days), args.rebuild)
    dune_gas = d.run(qid)

    print("  [3/4] Seaport fills over our exact scanned block range...", flush=True)
    nfts = pulse.get("nfts") or {}
    seaport_check = None
    if nfts.get("from_block") and nfts.get("to_block"):
        sql = _sql(Q_SEAPORT_RANGE, seaport=f"from_hex('{SEAPORT[2:]}')",
                   topic0=f"from_hex('{ORDER_FULFILLED[2:]}')",
                   from_block=nfts["from_block"], to_block=nfts["to_block"])
        qid = get_query(d, cache, "seaport_range", sql, rebuild=True)
        rows = d.run(qid)
        dune_fills = rows[0]["fills"] if rows else None
        ours = nfts.get("logs_scanned")
        pct = _pct_diff(ours, dune_fills)
        seaport_check = {"metric": "Seaport fills", "blocks":
                         f"{nfts['from_block']:,}-{nfts['to_block']:,}",
                         "ours_rpc": ours, "dune": dune_fills,
                         "pct_diff": pct, "verdict": _verdict(pct)}
    else:
        print("        skipped — pulse.json has no NFT block range", flush=True)

    print("  [4/4] Seaport daily history (RPC can't reach this cheaply)...", flush=True)
    qid = get_query(d, cache, "seaport_daily",
                    _sql(Q_SEAPORT_DAILY, seaport=f"from_hex('{SEAPORT[2:]}')",
                         topic0=f"from_hex('{ORDER_FULFILLED[2:]}')", days=30),
                    args.rebuild)
    seaport_daily = d.run(qid)

    checks = compare(pulse, dune_dau, dune_gas, eth_price)

    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pulse_generated_at": pulse.get("generated_at"),
        "definitions": {
            "dau": 'count(distinct "from") over robinhood.transactions, per UTC day',
            "gas": "sum(gas_used * gas_price) / 1e18, per UTC day",
            "seaport": "count of OrderFulfilled logs at the Seaport 1.6 address",
        },
        "checks": checks,
        "seaport_range_check": seaport_check,
        "seaport_daily": [{"day": str(r.get("day"))[:10], "fills": r.get("fills")}
                          for r in seaport_daily],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))

    print(f"\nWrote {out} in {time.time() - t0:.1f}s\n")
    print(f"  {'metric':<12} {'day':<12} {'ours':>14} {'dune':>14} {'diff':>8}  verdict")
    for c in checks:
        pct = f"{c['pct_diff']:.1f}%" if c["pct_diff"] is not None else "—"
        a = f"{c['blockscout']:,.2f}" if isinstance(c["blockscout"], float) else f"{c['blockscout']:,}"
        b = f"{c['dune']:,.2f}" if isinstance(c["dune"], float) else f"{c['dune']:,}"
        print(f"  {c['metric']:<12} {c['day']:<12} {a:>14} {b:>14} {pct:>8}  {c['verdict']}")
    if seaport_check:
        s = seaport_check
        pct = f"{s['pct_diff']:.1f}%" if s["pct_diff"] is not None else "—"
        print(f"\n  Seaport fills over blocks {s['blocks']}")
        print(f"    our RPC scan {s['ours_rpc']:,}   dune {s['dune']:,}   "
              f"{pct}  {s['verdict']}")
    if seaport_daily:
        print(f"\n  Seaport daily history: {len(seaport_daily)} days available")


if __name__ == "__main__":
    main()
