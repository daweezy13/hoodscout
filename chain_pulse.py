#!/usr/bin/env python3
"""
chain_pulse.py

Pull an ecosystem-wide "pulse check" for Robinhood Chain (Clutch Markets,
chainId 4663) and write a single JSON blob the dashboard page renders from.

Six stats, six verified sources (all key-free, confirmed 2026-08-04):

    TVL                 DefiLlama  /v2/historicalChainTvl/Robinhood%20Chain
    Stablecoin supply   DefiLlama  stablecoins.llama.fi/stablecoincharts/...
    Daily fees          BOTH:
                          - chain gas fees  Blockscout stats-service txnsFee
                          - app/protocol    DefiLlama /overview/fees/...
                        These differ by ~30x and measure different things
                        (gas paid TO the chain vs fees earned BY apps on it).
                        Reported separately and never summed.
    Daily active users  Blockscout  /stats-service/api/v1/lines/activeAccounts
    Top memecoins       GeckoTerminal network `robinhood`, pools -> tokens
    Top NFT collections Seaport 1.6 OrderFulfilled logs via the chain RPC

Two things this file exists to work around
------------------------------------------
1. The RPC caps eth_getLogs at 10,000 matched logs. Earlier StockBooster work
   never hit it (those events are ~2/round); Seaport runs ~95k fills/day. The
   cap error also fires TRANSIENTLY on ranges well under the limit, so the
   scan splits on failure and retries rather than trusting a fixed chunk size.

2. Every obvious ranking metric on this chain is poisoned. Blockscout's
   ERC-721-by-holders list is topped by spam airdrops ("# IMPORTANT ALERT",
   8.6k holders); GeckoTerminal's #2 pool by volume was a copycat "OpenAI"
   token showing $12.4M volume against NEGATIVE liquidity. So memecoins are
   ranked by volume behind a USD liquidity floor, and NFTs by actual paid
   Seaport fills, which airdrops cannot inflate.

Addresses below were resolved by volume weight from GeckoTerminal's real
pools, NOT by Blockscout name search -- that search returns six different
"USDG" and six different "WETH" contracts on this chain.

Deps:  pip install requests eth-abi
"""

import os
import re
import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path
from collections import defaultdict

import requests
from eth_abi import decode as abi_decode
from eth_hash.auto import keccak

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"          # public, chainId 4663
BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api/v2"
BLOCKSCOUT_STATS = "https://robinhoodchain.blockscout.com/stats-service/api/v1"
DEFILLAMA = "https://api.llama.fi"
DEFILLAMA_STABLES = "https://stablecoins.llama.fi"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
DEXSCREENER = "https://api.dexscreener.com"
OPENSEA = "https://api.opensea.io/api/v2"

# OpenSea gates every v2 endpoint behind a key, and checks auth BEFORE it
# validates the chain, so this slug could not be confirmed without one. If a
# key is present and this is wrong, the enrichment degrades to "unknown"
# rather than failing the run.
OPENSEA_CHAIN = "robinhood"
OPENSEA_API_KEY_ENV = "OPENSEA_API_KEY"

CHAIN_NAME = "Robinhood Chain"        # DefiLlama's exact chain key (URL-encoded in paths)
GT_NETWORK = "robinhood"              # GeckoTerminal's network id for this chain

# Seaport 1.6, canonical cross-chain address. Confirmed deployed here
# (47,964 bytes of code at this address on Robinhood Chain).
SEAPORT = "0x0000000000000068f116a894984e2db1123eb395"
ORDER_FULFILLED = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"

# Payment tokens, resolved by 24h volume weight through GeckoTerminal's pools.
# Do NOT replace these from a Blockscout name search -- copycats abound.
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"   # $152M/24h touched
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"   # $130M/24h touched
NATIVE = "0x0000000000000000000000000000000000000000"

# Seaport ItemType enum
IT_NATIVE, IT_ERC20, IT_ERC721, IT_ERC1155 = 0, 1, 2, 3
NFT_ITEM_TYPES = (IT_ERC721, IT_ERC1155)
PAY_ITEM_TYPES = (IT_NATIVE, IT_ERC20)

# Ranking knobs. MIN_LIQUIDITY_USD is the primary defence against wash-traded
# copycats -- at $25k it drops COOPER ($2.0M volume on $1 of liquidity),
# RGRID ($2.3M on $5.3k) and the negative-liquidity "OpenAI" pool, while
# keeping legitimate USDG-quoted pairs like nvda/USDG and spacex/USDG.
MIN_LIQUIDITY_USD = 25_000
WASH_RATIO_FLAG = 75.0          # 24h volume / liquidity above this = flagged, not dropped
TOP_N = 20

# Cross-source corroboration. GeckoTerminal and DexScreener index the same
# chain independently, so querying both BY CONTRACT ADDRESS gives two readings
# of one quantity -- and they disagree hard on this chain. Measured 2026-08-05:
# TRSA read $4.32M on GT and $6k on DS (720x); AnsemCat 46x; DORK agreed on
# volume but reported $175k vs $6 of liquidity. Meanwhile every token the
# liquidity floor had already rejected showed ~$0 on both.
#
# We do not adjudicate which source is right -- we rank on the LOWER of the
# two. A token only one index believes in gets ranked by the smaller number,
# which is the conservative reading and self-documenting in the output.
AGREE_RATIO = 3.0        # within 3x on both volume and liquidity = corroborated
DISPUTE_RATIO = 20.0     # beyond 20x apart = actively disputed
DS_DEAD_LIQUIDITY = 1_000    # DS sees under this = it does not see a real market

# Tokens that are chain infrastructure rather than memecoins.
INFRA_SYMBOLS = {"WETH", "ETH", "USDG", "USDC", "USDT", "USDE", "SYRUPUSDG", "VIRTUAL"}

REQUEST_SLEEP = 0.35            # GeckoTerminal 429s easily under rapid calls
SEAPORT_CHUNK_BLOCKS = 60_000   # ~1.7h at ~101ms blocks; stays under the 10k log cap

OUT_DIR = Path(__file__).parent / "out"
DUNE_API_KEY_ENV = "DUNE_API_KEY"


def _load_env():
    """Read .env beside this script into os.environ without overriding a real
    environment variable. Keeps API keys out of the shell history and out of
    this file; nothing here is required for the key-free path to work.
    """
    import os
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
# HTTP layer
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.headers.update({"User-Agent": "robinhood-chain-pulse/1.0"})


def _get(url, params=None, tries=6, timeout=45):
    """GET with backoff. Returns parsed JSON, or None if every try failed."""
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 2.0 * (attempt + 1)
                print(f"    [429] {url} -> backing off {wait}s", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == tries - 1:
                print(f"    [give up] {url} -> {e}", flush=True)
                return None
            time.sleep(min(1.5 * (attempt + 1), 15.0))
    return None


def _rpc(method, params, tries=5, timeout=120):
    """Single JSON-RPC call. Returns (result, error_message)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(tries):
        try:
            r = _session.post(RPC_URL, json=payload, timeout=timeout)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                return None, d["error"].get("message", str(d["error"]))
            return d.get("result"), None
        except requests.RequestException as e:
            if attempt == tries - 1:
                return None, str(e)
            time.sleep(1.5 * (attempt + 1))
    return None, "exhausted retries"


# --------------------------------------------------------------------------- #
# DefiLlama — TVL, stablecoin supply, app-level fees
# --------------------------------------------------------------------------- #
def fetch_defillama(chain=CHAIN_NAME):
    slug = requests.utils.quote(chain)
    out = {}

    tvl = _get(f"{DEFILLAMA}/v2/historicalChainTvl/{slug}") or []
    out["tvl_series"] = [{"date": p["date"], "tvl": p["tvl"]} for p in tvl]
    out["tvl_current"] = tvl[-1]["tvl"] if tvl else None
    out["tvl_change_7d_pct"] = _pct_change(
        [p["tvl"] for p in tvl], lookback=7)

    stables = _get(f"{DEFILLAMA_STABLES}/stablecoincharts/{slug}") or []
    ser = []
    for p in stables:
        ser.append({
            "date": int(p["date"]),
            "total": _peg(p.get("totalCirculatingUSD")),
            "minted": _peg(p.get("totalMintedUSD")),
            "bridged": _peg(p.get("totalBridgedToUSD")),
        })
    out["stables_series"] = ser
    out["stables_current"] = ser[-1]["total"] if ser else None
    out["stables_change_7d_pct"] = _pct_change([s["total"] for s in ser], lookback=7)

    # Per-asset stablecoin breakdown. The aggregate series says how much
    # stablecoin value sits on the chain; this says WHICH. Answer on 2026-08-05
    # is only two assets -- and notably neither is USDC or USDT, which do not
    # exist here in real form (Blockscout name-searches for them return only
    # copycats: "UnicornSpaceDogeCoin" and "United States Dump Coin" both wear
    # the USDC ticker; the one literally named "USD Coin" has 175 holders
    # against USDG's 45,256).
    out["stables_breakdown"] = fetch_stablecoin_breakdown(chain)
    out["activity_breakdown"] = fetch_activity_breakdown(chain)

    fees = _get(f"{DEFILLAMA}/overview/fees/{slug}",
                params={"excludeTotalDataChart": "false",
                        "excludeTotalDataChartBreakdown": "true",
                        "dataType": "dailyFees"}) or {}
    out["app_fees_24h"] = fees.get("total24h")
    out["app_fees_7d"] = fees.get("total7d")
    out["app_fees_30d"] = fees.get("total30d")
    out["app_fees_alltime"] = fees.get("totalAllTime")
    out["app_fees_change_1d_pct"] = fees.get("change_1d")
    out["app_fees_series"] = [{"date": int(d), "fees": v}
                              for d, v in (fees.get("totalDataChart") or [])]
    out["app_fees_by_protocol"] = sorted(
        [{"name": p.get("name"), "fees_24h": p.get("total24h") or 0}
         for p in (fees.get("protocols") or [])],
        key=lambda x: -x["fees_24h"])[:10]
    return out


def fetch_activity_breakdown(chain=CHAIN_NAME):
    """Daily fees split by what kind of activity produced them.

    This is what disaggregates "NFT trading vs memecoin trading" over time.
    Volume itself has no per-chain history on either DEX index, but DefiLlama
    publishes a daily per-PROTOCOL fee breakdown, and fees are a fixed cut of
    volume, so the split is a faithful proxy for where activity happens.
    Sanity-checked against our own numbers: Seaport shows ~$32k/day of fees
    against the ~$800k/day of sale volume the Seaport scan measures -- about
    the ~2.5% marketplace rate, so the two agree.
    """
    d = _get(f"{DEFILLAMA}/overview/fees/{requests.utils.quote(chain)}",
             params={"excludeTotalDataChartBreakdown": "false", "dataType": "dailyFees"})
    if not d:
        return {"series": [], "categories": []}

    def categorise(name):
        n = (name or "").lower()
        if any(k in n for k in ("seaport", "opensea", "blur", "magic eden")):
            return "NFT marketplace"
        if any(k in n for k in ("uniswap", "pancake", "sushi", "curve", "kyber",
                                "ekubo", "0x", "aerodrome", "balancer", "dex")):
            return "DEX trading"
        if any(k in n for k in ("morpho", "aave", "compound", "euler", "lend")):
            return "Lending"
        return "Other apps"

    order = ["DEX trading", "NFT marketplace", "Lending", "Other apps"]
    series = []
    for ts, rows in (d.get("totalDataChartBreakdown") or []):
        flat = defaultdict(float)
        for k, v in (rows or {}).items():
            if isinstance(v, dict):
                for proto, val in v.items():
                    flat[categorise(proto)] += (val or 0)
            else:
                flat[categorise(k)] += (v or 0)
        series.append({"date": dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
                      .date().isoformat(),
                       **{c: flat.get(c, 0.0) for c in order}})
    return {"series": series, "categories": order}


# Ethos tiers, re-derived against the live API on 2026-08-06.
#
# The table in ~/projects/polymarket-ethos/backend/tiers.py is STALE in the
# mid-range -- it puts "known" at 1100 and "neutral" at 800, but the live API
# returns "questionable" for 1161, "neutral" for 1200/1246/1317 and "known"
# only at 1465. The upper tiers still agree (1945 reputable, 2326
# distinguished). Boundaries below match all nine handles sampled. Colours are
# carried over from that project so the two stay visually consistent.
ETHOS_TIERS = [
    ("renowned", 2600, "#7B61B5"), ("revered", 2400, "#8E79B9"),
    ("distinguished", 2200, "#1B8F3A"), ("exemplary", 2000, "#4F8F6A"),
    ("reputable", 1800, "#2F80C0"), ("established", 1600, "#4A90C2"),
    ("known", 1400, "#7F93B3"), ("neutral", 1200, "#D9D9D6"),
    ("questionable", 800, "#D4A017"), ("untrusted", 0, "#C62828"),
]


def ethos_tier(score):
    for name, floor, colour in ETHOS_TIERS:
        if score >= floor:
            return name, colour
    return ETHOS_TIERS[-1][0], ETHOS_TIERS[-1][2]


# --------------------------------------------------------------------------- #
# Trust Index — ported from Trust Capital Markets (~/ethos_tge)
#
# Post-TGE composite from backend/src/routes/leaderboard.ts:
#   trustNorm = (trustScore / 2800) * 100
#   fdvNorm   = ((log10(fdv) - 6) / 5) * 100      # $1M -> 0, $100B -> 100
#   score     = 0.5 * trustNorm + 0.5 * fdvNorm   # clamped to 0..100
#
# NOTE the shipped code divides by 5; `New formula.md` in that repo says 4.
# The code and its own comment agree with each other ("$100B -> 100"), so the
# markdown is stale -- following the code, which is what the live site serves.
#
# Every token here has launched, so the post-TGE (FDV) branch applies; the
# pre-TGE branch swaps FDV for chain TVL.
# --------------------------------------------------------------------------- #
TRUST_MAX = 2800


def effective_fdv(fdv, market_cap, volume_24h):
    if not fdv and not market_cap:
        return None
    base = max(fdv or 0, market_cap or 0)
    if not volume_24h or not market_cap or market_cap <= 0:
        return base
    return base * (1 + min(volume_24h / market_cap, 1.0) * 0.25)


def composite_grade(score):
    for floor, grade in ((85, "A"), (70, "B"), (55, "C"), (40, "D")):
        if score >= floor:
            return grade
    return "F"


def trust_index(trust_score, fdv, market_cap, volume_24h):
    """Trust Capital Markets composite, or None when an input is missing."""
    fdv_used = effective_fdv(fdv, market_cap, volume_24h)
    if not trust_score or not fdv_used or fdv_used <= 0:
        return None
    import math
    trust_norm = (trust_score / TRUST_MAX) * 100.0
    fdv_norm = ((math.log10(fdv_used) - 6) / 5) * 100.0
    score = 0.5 * trust_norm + 0.5 * fdv_norm
    score = round(min(max(score, 0.0), 100.0) * 10) / 10
    return {"score": score, "grade": composite_grade(score),
            "trust_norm": round(trust_norm, 1), "fdv_norm": round(fdv_norm, 1),
            "fdv_used": fdv_used}


def fetch_ethos(handle, tries=2):
    """Ethos profile for an X account, or None when no profile exists.

    Uses /user/by/x/{handle} rather than the score endpoint, following the
    approach in the polymarket-ethos project: the score endpoint answers for
    ANY input (a random address returns the 1200 default, and two unrelated
    contracts on this chain both return an identical 1185), whereas this
    endpoint returns a real user object -- so a missing `id` means genuinely
    no profile rather than a defaulted score. That distinction is the whole
    difference between "unrated" and "rated neutral".

    CAUTION: a social link is self-declared. SPCX on this chain links
    @elonmusk and would inherit 1945/"reputable". The score describes the
    LINKED ACCOUNT, never the token, so callers must show the handle beside
    it.
    """
    if not handle:
        return None
    for _ in range(tries):
        try:
            r = _session.get(f"https://api.ethos.network/api/v2/user/by/x/{handle}",
                             headers={"X-Ethos-Client": "greenwood-dashboard"},
                             timeout=20)
            if r.status_code != 200:
                return None
            u = r.json()
            if not isinstance(u, dict) or not u.get("id"):
                return None
            score = u.get("score") or 0
            level, colour = ethos_tier(score)
            rv = ((u.get("stats") or {}).get("review") or {}).get("received") or {}
            vouch = ((u.get("stats") or {}).get("vouch") or {}).get("received") or {}
            return {
                "handle": u.get("username") or handle,
                "display_name": u.get("displayName"),
                "score": score,
                "level": level,
                "colour": colour,
                "status": u.get("status"),
                "reviews_positive": rv.get("positive", 0),
                "reviews_negative": rv.get("negative", 0),
                "vouches": vouch.get("count", 0),
                "profile_url": f"https://app.ethos.network/profile/x/{u.get('username') or handle}",
                # An unrated profile sits at the 1200 default with nothing
                # backing it; flagged so the UI can present it as "no signal"
                # instead of a verdict.
                "unrated": score == 1200 and not rv.get("positive") and not vouch.get("count"),
            }
        except requests.RequestException:
            time.sleep(1.0)
    return None


_X_BAD = {"i", "search", "home", "hashtag", "intent", "share", "status", "explore"}


def x_handle_from(urls):
    """Pull a usable X handle out of a token's social links."""
    for u in urls:
        m = re.search(r"(?:twitter|x)\.com/(?:#!/)?@?([A-Za-z0-9_]{1,15})(?:[/?].*)?$", u or "")
        if m and m.group(1).lower() not in _X_BAD:
            return m.group(1)
    return None


def fetch_stablecoin_breakdown(chain=CHAIN_NAME):
    """Which stablecoins are actually circulating on this chain, by size."""
    d = _get(f"{DEFILLAMA_STABLES}/stablecoins", params={"includePrices": "true"})
    if not d:
        return []
    rows = []
    for s in d.get("peggedAssets", []):
        entry = (s.get("chainCirculating") or {}).get(chain)
        if not entry:
            continue
        cur = (entry.get("current") or {}).get("peggedUSD")
        if not cur:
            continue
        prev = (entry.get("circulatingPrevDay") or {}).get("peggedUSD")
        rows.append({
            "symbol": s.get("symbol"),
            "name": s.get("name"),
            "circulating": cur,
            "change_1d_pct": ((cur / prev - 1) * 100) if prev else None,
            "peg_mechanism": s.get("pegMechanism"),
            "price": s.get("price"),
        })
    rows.sort(key=lambda x: -x["circulating"])
    total = sum(r["circulating"] for r in rows) or 1.0
    for r in rows:
        r["share_pct"] = r["circulating"] / total * 100.0
    return rows


def _peg(v):
    return (v or {}).get("peggedUSD")


def _pct_change(values, lookback):
    vals = [v for v in values if v is not None]
    if len(vals) <= lookback or not vals[-1 - lookback]:
        return None
    return (vals[-1] / vals[-1 - lookback] - 1.0) * 100.0


# --------------------------------------------------------------------------- #
# Blockscout — DAU, chain gas fees, headline counters
# --------------------------------------------------------------------------- #
def _stats_line(line_id, days=30):
    to = dt.date.today()
    frm = to - dt.timedelta(days=days)
    d = _get(f"{BLOCKSCOUT_STATS}/lines/{line_id}",
             params={"from": frm.isoformat(), "to": to.isoformat()})
    if not d:
        return []
    out = []
    for p in d.get("chart", []):
        try:
            out.append({"date": p["date"], "value": float(p["value"])})
        except (TypeError, ValueError):
            continue
    return out


def _split_partial(series, sum_like=True, ratio=0.6, floor_ratio=0.35, window=7):
    """Separate still-filling buckets from complete days.

    Two distinct failure modes, and the calendar alone catches only the first:

    1. The bucket dated today (UTC) is obviously mid-flight. Headlining it read
       DAU as ~60k against a true ~235k.
    2. The provider's ingestion LAGS the calendar. On 2026-08-06 UTC,
       Blockscout's newest bucket was dated 2026-08-05 -- complete by the
       calendar -- but held 9.59 ETH of gas against ~30 for the days around
       it. A date-only rule happily reports that as a finished day.

    So sum-like series (fees, transaction counts -- quantities that accumulate
    linearly through a day) drop a trailing point that sits below `ratio` of
    the trailing median.

    Distinct-count series like active accounts must NOT use that same 0.6
    test: a unique-address count saturates early in the day, so a genuinely
    partial bucket still looks near-full and a real drop would be wrongly
    suppressed. But "calendar alone" turned out to be too weak for them too --
    on 2026-08-11T00:03Z the Aug 10 DAU bucket was calendar-complete and held
    62,363 against a 225k-382k neighbourhood, because the provider had barely
    begun ingesting the day. That shipped a headline reading a 75% collapse in
    users that had not happened.

    So distinct-count series get the same test at a much looser `floor_ratio`.
    0.35 sits far below any plausible one-day swing (the observed 10-day range
    bottoms out near 78% of median) while still catching an under-ingested
    bucket sitting at 25%.

    The dropped point stays in the series for charting; it is only barred from
    driving a headline, and is returned separately so it can be labelled.
    """
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    complete = [p for p in series if p["date"] < today]
    partial = next((p for p in series if p["date"] >= today), None)

    cutoff = ratio if sum_like else floor_ratio
    if len(complete) > window:
        recent = sorted(p["value"] for p in complete[-(window + 1):-1])
        median = recent[len(recent) // 2] if recent else 0
        if median > 0 and complete[-1]["value"] < cutoff * median:
            partial = complete[-1]
            complete = complete[:-1]
    return complete, partial


def fetch_blockscout(days=400):
    out = {}
    stats = _get(f"{BLOCKSCOUT}/stats") or {}
    out["eth_price_usd"] = _f(stats.get("coin_price"))
    out["total_addresses"] = _i(stats.get("total_addresses"))
    out["total_transactions"] = _i(stats.get("total_transactions"))
    out["transactions_today"] = _i(stats.get("transactions_today"))
    out["average_block_time_ms"] = stats.get("average_block_time")

    dau_all = _stats_line("activeAccounts", days)
    dau, dau_partial = _split_partial(dau_all, sum_like=False)
    out["dau_series"] = dau
    out["dau_date"] = dau[-1]["date"] if dau else None
    out["dau_current"] = dau[-1]["value"] if dau else None
    out["dau_prev"] = dau[-2]["value"] if len(dau) >= 2 else None
    out["dau_change_1d_pct"] = _pct_change([p["value"] for p in dau], lookback=1)
    out["dau_change_7d_pct"] = _pct_change([p["value"] for p in dau], lookback=7)
    out["dau_today_partial"] = dau_partial["value"] if dau_partial else None

    eth_px = out["eth_price_usd"] or 0
    gas_all = _stats_line("txnsFee", days)
    gas, gas_partial = _split_partial(gas_all)
    out["gas_fees_series"] = [{"date": g["date"], "eth": g["value"],
                               "usd": g["value"] * eth_px} for g in gas]
    out["gas_fees_date"] = gas[-1]["date"] if gas else None
    out["gas_fees_eth_current"] = gas[-1]["value"] if gas else None
    out["gas_fees_usd_current"] = (gas[-1]["value"] * eth_px) if gas else None
    out["gas_fees_change_1d_pct"] = _pct_change([p["value"] for p in gas], lookback=1)
    out["gas_fees_today_partial_eth"] = gas_partial["value"] if gas_partial else None

    txns, txns_partial = _split_partial(_stats_line("newTxns", days))
    out["txns_series"] = txns
    out["txns_current"] = txns[-1]["value"] if txns else None
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# GeckoTerminal — top memecoins
#
# Ranked at the TOKEN level, not the pool level: CASHCAT alone runs three fee
# tiers, and a pool-level list would show it three times while pushing real
# projects off the bottom. Volume and liquidity are summed across a token's
# pools; GT already reports both in USD regardless of whether the pool is
# quoted in WETH, USDG or VIRTUAL, so one USD floor is valid for all of them.
# --------------------------------------------------------------------------- #
def fetch_dexscreener_token(address):
    """Second, independent reading of one token's volume and liquidity.

    Queried by contract address, so token identity is exact -- a disagreement
    here is a real conflict between two indexers about the same contract, not
    a name-matching artifact. Only pairs where this token is the BASE side are
    counted, to match how the GeckoTerminal side is aggregated.
    """
    d = _get(f"{DEXSCREENER}/token-pairs/v1/{GT_NETWORK}/{address}")
    if not isinstance(d, list):
        return None
    base = [p for p in d
            if ((p.get("baseToken") or {}).get("address") or "").lower() == address.lower()]
    if not base:
        return None
    socials, websites = set(), set()
    for p in base:
        info = p.get("info") or {}
        for sm in (info.get("socials") or []):
            if sm.get("url"):
                socials.add(sm["url"])
        for wb in (info.get("websites") or []):
            if wb.get("url"):
                websites.add(wb["url"])
    # Deepest pair carries the token's identity: its URL, its quoted price, and
    # the price change we display. DexScreener supplies the canonical link in
    # its own `url` field -- use that rather than composing one.
    top = max(base, key=lambda p: _f((p.get("volume") or {}).get("h24")) or 0)
    return {
        "social_urls": sorted(socials) + sorted(websites),
        "volume_24h": sum(_f((p.get("volume") or {}).get("h24")) or 0 for p in base),
        "liquidity": sum(_f((p.get("liquidity") or {}).get("usd")) or 0 for p in base),
        "pairs": len(base),
        "socials": len(socials),
        "websites": len(websites),
        "price_usd": _f(top.get("priceUsd")),
        "price_change_24h": _f((top.get("priceChange") or {}).get("h24")),
        "url": top.get("url"),
        "pair_address": top.get("pairAddress"),
        "dex": top.get("dexId"),
        "name": (top.get("baseToken") or {}).get("name"),
        "symbol": (top.get("baseToken") or {}).get("symbol"),
        "fdv": _f(top.get("fdv")),
        "market_cap": _f(top.get("marketCap")),
    }


def _corroborate(t, ds):
    """Attach DexScreener's figures as authoritative, GeckoTerminal as check.

    DexScreener is the primary source for these tokens -- it tracks this
    chain's memecoins more reliably than the CoinGecko/GeckoTerminal side,
    which is used for DISCOVERY (it is the only one that enumerates a chain's
    pools ranked by volume) and then kept as a second opinion. Where the two
    disagree the divergence is surfaced rather than silently resolved.
    """
    t["gt_volume_24h"] = t["volume_24h"]
    t["gt_liquidity"] = t["liquidity"]

    if not ds or ds["pairs"] == 0:
        t["corroboration"] = "single-source"
        t["ds_volume_24h"] = None
        t["ds_liquidity"] = None
        t["volume_ranked"] = t["volume_24h"]
        t["source"] = "geckoterminal"
        return t

    t["ds_volume_24h"] = ds["volume_24h"]
    t["ds_liquidity"] = ds["liquidity"]
    t["socials"] = ds["socials"]
    t["websites"] = ds["websites"]
    t["x_handle"] = x_handle_from(ds.get("social_urls") or [])
    t["url"] = ds.get("url")
    t["dex"] = ds.get("dex")
    t["fdv"] = ds.get("fdv")
    t["market_cap"] = ds.get("market_cap")
    if ds.get("name"):
        t["name"] = ds["name"]
    if ds.get("symbol"):
        t["symbol"] = ds["symbol"]

    def ratio(a, b):
        lo, hi = min(a, b), max(a, b)
        return (hi / lo) if lo > 0 else float("inf")

    vr = ratio(t["gt_volume_24h"], ds["volume_24h"])
    lr = ratio(t["gt_liquidity"], ds["liquidity"])
    t["volume_ratio"] = None if vr == float("inf") else vr
    t["liquidity_ratio"] = None if lr == float("inf") else lr

    if ds["liquidity"] < DS_DEAD_LIQUIDITY:
        t["corroboration"] = "disputed"
    elif vr <= AGREE_RATIO and lr <= AGREE_RATIO:
        t["corroboration"] = "corroborated"
    elif vr >= DISPUTE_RATIO or lr >= DISPUTE_RATIO:
        t["corroboration"] = "disputed"
    else:
        t["corroboration"] = "partial"

    # DexScreener's reading is the one displayed and ranked on.
    t["source"] = "dexscreener"
    t["volume_ranked"] = ds["volume_24h"]
    if ds.get("price_usd") is not None:
        t["price_usd"] = ds["price_usd"]
    if ds.get("price_change_24h") is not None:
        t["price_change_24h"] = ds["price_change_24h"]
    return t


def fetch_opensea_safelist(address, chain=OPENSEA_CHAIN):
    """OpenSea's own verification verdict for a collection, if a key exists.

    safelist_status is the field that actually encodes 'is this the real
    collection' -- 'verified'/'approved' vs 'not_requested'. Returns None when
    no key is configured, so the pull still works without one.
    """
    import os
    key = os.environ.get(OPENSEA_API_KEY_ENV)
    if not key:
        return None
    try:
        r = _session.get(f"{OPENSEA}/chain/{chain}/contract/{address}/nfts",
                         params={"limit": 1}, headers={"x-api-key": key}, timeout=30)
        if r.status_code != 200:
            return {"safelist_status": None, "error": f"http {r.status_code}"}
        coll = (r.json().get("nfts") or [{}])[0].get("collection")
        if not coll:
            return {"safelist_status": None, "error": "no collection on contract"}
        c = _session.get(f"{OPENSEA}/collections/{coll}",
                         headers={"x-api-key": key}, timeout=30)
        if c.status_code != 200:
            return {"safelist_status": None, "error": f"http {c.status_code}"}
        cj = c.json()
        out = {
            "collection_slug": coll,
            "collection_name": cj.get("name"),
            "safelist_status": cj.get("safelist_status"),
            "is_disabled": cj.get("is_disabled"),
            "is_nsfw": cj.get("is_nsfw"),
            "opensea_url": cj.get("opensea_url"),
            "twitter_username": (cj.get("twitter_username") or "").strip() or None,
            "project_url": (cj.get("project_url") or "").strip() or None,
            # Blockscout returns null total_supply for some ERC-721s here
            # (Zaibatsu Wagies, PitBoys). OpenSea carries it, so it backfills.
            "total_supply": _i(cj.get("total_supply")),
        }
        # A real floor is the lowest open LISTING, which only a marketplace
        # knows. Until this succeeds the dashboard shows the lowest paid sale
        # instead, labelled as such rather than passed off as a floor.
        st = _session.get(f"{OPENSEA}/collections/{coll}/stats",
                          headers={"x-api-key": key}, timeout=30)
        if st.status_code == 200:
            total = (st.json() or {}).get("total") or {}
            out["floor_price"] = _f(total.get("floor_price"))
            out["floor_currency"] = total.get("floor_price_symbol")
            out["os_volume_24h"] = _f(((st.json() or {}).get("intervals") or [{}])[0]
                                      .get("volume"))
        return out
    except requests.RequestException as e:
        return {"safelist_status": None, "error": str(e)}


def fetch_memecoins(pages=4, min_liquidity=MIN_LIQUIDITY_USD, top_n=TOP_N):
    tokens = {}
    pools_seen = 0

    for page in range(1, pages + 1):
        d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/pools",
                 params={"sort": "h24_volume_usd_desc", "page": page,
                         "include": "base_token,quote_token"})
        time.sleep(REQUEST_SLEEP)
        if not d or not d.get("data"):
            break
        inc = {i["id"]: i for i in d.get("included", [])}

        for p in d["data"]:
            a = p["attributes"]
            pools_seen += 1
            base = inc.get(p["relationships"]["base_token"]["data"]["id"])
            if not base:
                continue
            ba = base["attributes"]
            addr = (ba.get("address") or "").lower()
            sym = (ba.get("symbol") or "?").strip()
            if not addr or addr == NATIVE:
                continue

            vol = _f(a.get("volume_usd", {}).get("h24")) or 0.0
            liq = _f(a.get("reserve_in_usd")) or 0.0
            t = tokens.setdefault(addr, {
                "address": addr, "symbol": sym, "name": ba.get("name") or sym,
                "volume_24h": 0.0, "liquidity": 0.0, "pools": 0,
                "price_usd": _f(a.get("base_token_price_usd")),
                "price_change_24h": None, "top_pool": None, "top_pool_vol": 0.0,
            })
            t["volume_24h"] += vol
            t["liquidity"] += liq
            t["pools"] += 1
            if vol > t["top_pool_vol"]:
                t["top_pool_vol"] = vol
                t["top_pool"] = a.get("name")
                t["price_change_24h"] = _f((a.get("price_change_percentage") or {}).get("h24"))
                if t["price_usd"] is None:
                    t["price_usd"] = _f(a.get("base_token_price_usd"))

    candidates = [t for t in tokens.values() if t["symbol"].upper() not in INFRA_SYMBOLS]
    candidates.sort(key=lambda x: -x["volume_24h"])

    # Corroborate BEFORE applying the floor. GeckoTerminal reports negative
    # liquidity for some pools (UNIFROG -$17,997, INTERN -$14,090 on
    # 2026-08-05) while DexScreener shows those same contracts holding $87k
    # and $135k. Liquidity cannot be negative, so that is invalid data rather
    # than a low reading -- excluding on it drops real tokens. A working set
    # wider than the final cut is checked, since repair and conservative
    # ranking both reorder the list.
    working = candidates[:top_n * 3]
    for t in working:
        _corroborate(t, fetch_dexscreener_token(t["address"]))
        time.sleep(REQUEST_SLEEP)
    for t in candidates[top_n * 3:]:
        t["corroboration"] = "unchecked"
        t["volume_ranked"] = t["volume_24h"]

    ranked, excluded = [], []
    for t in candidates:
        gt_liq, ds_liq = t["gt_liquidity"], t.get("ds_liquidity")
        if ds_liq is not None and ds_liq > 0:
            t["liquidity_effective"] = ds_liq
            t["liquidity_source"] = "dexscreener"
        elif gt_liq > 0:
            t["liquidity_effective"] = gt_liq
            t["liquidity_source"] = "geckoterminal (dexscreener has no pairs)"
        else:
            t["liquidity_effective"] = max(gt_liq, ds_liq or gt_liq)
            t["liquidity_source"] = "neither source reports liquidity"

        eff = t["liquidity_effective"]
        t["wash_ratio"] = (t["volume_ranked"] / eff) if eff > 0 else None

        if eff < min_liquidity:
            t["exclude_reason"] = (
                "no liquidity found by either source" if eff <= 0
                else f"liquidity ${eff:,.0f} below ${min_liquidity:,.0f} floor")
            excluded.append(t)
            continue
        t["flagged"] = bool(t["wash_ratio"] and t["wash_ratio"] > WASH_RATIO_FLAG)
        ranked.append(t)

    ranked.sort(key=lambda x: -x.get("volume_ranked", 0))
    for t in ranked[:top_n]:
        t["ethos"] = fetch_ethos(t.get("x_handle"))
        meta = _get(f"{BLOCKSCOUT}/tokens/{t['address']}") or {}
        t["holders"] = _i(meta.get("holders_count") or meta.get("holders"))
        # DexScreener's marketCap can be absent on thin pairs; Blockscout's
        # circulating figure is the fallback, and FDV stays DexScreener-only.
        t["market_cap"] = t.get("market_cap") or _f(meta.get("circulating_market_cap"))
        t["decimals"] = _i(meta.get("decimals"))
        e = t.get("ethos") or {}
        t["trust_index"] = trust_index(
            None if e.get("unrated") else e.get("score"),
            t.get("fdv"), t.get("market_cap"), t.get("volume_ranked"))
        time.sleep(0.15)
    excluded.sort(key=lambda x: -x["volume_24h"])
    counts = defaultdict(int)
    for t in ranked[:top_n]:
        counts[t.get("corroboration", "unchecked")] += 1
    # Cases where the two indexers materially disagreed on liquidity, kept for
    # display -- GeckoTerminal reporting negative liquidity against a healthy
    # DexScreener figure is the recurring one.
    repaired = [{"symbol": t["symbol"], "gt_liquidity": t.get("gt_liquidity"),
                 "ds_liquidity": t.get("ds_liquidity"),
                 "in_top_n": t in ranked[:top_n]}
                for t in ranked
                if (t.get("gt_liquidity") or 0) <= 0 and (t.get("ds_liquidity") or 0) > 0]
    return {
        "tokens": ranked[:top_n],
        "excluded": excluded[:12],
        "pools_scanned": pools_seen,
        "min_liquidity_usd": min_liquidity,
        "total_volume_24h": sum(t.get("volume_ranked", 0) for t in ranked[:top_n]),
        "corroboration_counts": dict(counts),
        "liquidity_repaired": repaired,
    }


# --------------------------------------------------------------------------- #
# Seaport — top NFT collections by real secondary-sale volume
# --------------------------------------------------------------------------- #
def _block_number():
    res, err = _rpc("eth_blockNumber", [])
    if err:
        raise RuntimeError(f"eth_blockNumber failed: {err}")
    return int(res, 16)


def _block_time(bn):
    res, err = _rpc("eth_getBlockByNumber", [hex(bn), False])
    if err or not res:
        raise RuntimeError(f"eth_getBlockByNumber({bn}) failed: {err}")
    return int(res["timestamp"], 16)


def _find_block_at(target_ts, latest_bn, latest_ts):
    """Estimate the block nearest target_ts, refining off real block times.

    Cheaper than a full binary search and accurate to a few hundred blocks,
    which is well inside the noise for a 24h volume window.
    """
    bn = latest_bn
    ts = latest_ts
    for _ in range(6):
        drift = ts - target_ts
        if abs(drift) < 120:
            break
        span = max(latest_bn - bn, 1)
        rate = ((latest_ts - ts) / span) if span and (latest_ts - ts) > 0 else 0.101
        rate = rate if rate > 0 else 0.101
        bn = max(1, int(bn - drift / rate))
        ts = _block_time(bn)
    return bn


def _get_logs_chunked(address, topic0, from_block, to_block,
                      chunk=SEAPORT_CHUNK_BLOCKS, depth=0):
    """eth_getLogs across a range, splitting on the 10k-log cap.

    The cap error also fires transiently on ranges that are nowhere near it,
    so a failed chunk is retried by halving rather than being trusted as a
    genuine "too many logs" signal.
    """
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        res, err = _rpc("eth_getLogs", [{
            "address": address, "topics": [topic0],
            "fromBlock": hex(start), "toBlock": hex(end),
        }])
        if err:
            if chunk <= 500:
                print(f"    [skip] {start}-{end} irreducible: {err}", flush=True)
                start = end + 1
                continue
            sub = _get_logs_chunked(address, topic0, start, end,
                                    chunk=max(chunk // 4, 500), depth=depth + 1)
            logs.extend(sub)
            start = end + 1
            continue
        logs.extend(res or [])
        start = end + 1
        if depth == 0:
            print(f"    blocks {start - 1:,}/{to_block:,}  logs={len(logs):,}", flush=True)
    return logs


_OFFER_T = "(uint8,address,uint256,uint256)[]"
_CONSID_T = "(uint8,address,uint256,uint256,address)[]"


def _decode_order_fulfilled(data_hex):
    raw = bytes.fromhex(data_hex[2:]) if data_hex.startswith("0x") else bytes.fromhex(data_hex)
    # non-indexed: orderHash, recipient, offer[], consideration[]
    return abi_decode(["bytes32", "address", _OFFER_T, _CONSID_T], raw)


def fetch_nft_collections(hours=24, top_n=TOP_N, eth_price=None, usdg_decimals=6,
                          align="utc-day"):
    """Seaport secondary-sale volume by collection.

    `align` controls the window, and this is not cosmetic. Every other metric
    on the dashboard comes from a daily series keyed by UTC date, so a rolling
    24h scan from the current block is NOT comparable to them -- it overlaps
    two UTC days and slides every run. Default 'utc-day' ends the scan at the
    last UTC midnight so the whole page reads one identical period.
    'rolling' keeps the old behaviour for a deliberate "right now" read.
    """
    latest_bn = _block_number()
    latest_ts = _block_time(latest_bn)

    if align == "utc-day":
        midnight = int(dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        to_ts, from_ts = midnight, midnight - hours * 3600
        to_bn = _find_block_at(to_ts, latest_bn, latest_ts)
    else:
        to_ts, from_ts = latest_ts, latest_ts - hours * 3600
        to_bn = latest_bn
    from_bn = _find_block_at(from_ts, latest_bn, latest_ts)

    print(f"  Seaport scan: blocks {from_bn:,} -> {to_bn:,} "
          f"({(to_bn - from_bn):,} blocks, ~{hours}h, align={align})", flush=True)

    logs = _get_logs_chunked(SEAPORT, ORDER_FULFILLED, from_bn, to_bn)
    print(f"  decoded from {len(logs):,} OrderFulfilled logs", flush=True)

    cols = defaultdict(lambda: {"sales": 0, "volume_usd": 0.0, "buyers": set(),
                                "unpriced_sales": 0, "min_sale_usd": None})
    eth_px = eth_price or 0.0
    skipped = 0

    for lg in logs:
        try:
            _, _recipient, offer, consideration = _decode_order_fulfilled(lg["data"])
        except Exception:
            skipped += 1
            continue

        nft_side, pay_side = None, None
        if any(o[0] in NFT_ITEM_TYPES for o in offer):
            nft_side, pay_side = offer, consideration
        elif any(c[0] in NFT_ITEM_TYPES for c in consideration):
            nft_side, pay_side = consideration, offer
        else:
            continue        # ERC20<->ERC20 order, not an NFT sale

        nft = next(i for i in nft_side if i[0] in NFT_ITEM_TYPES)
        collection = nft[1].lower()

        usd, priced = 0.0, False
        for item in pay_side:
            it, token, _ident, amount = item[0], item[1].lower(), item[2], item[3]
            if it not in PAY_ITEM_TYPES:
                continue
            if it == IT_NATIVE or token == WETH:
                usd += (amount / 1e18) * eth_px
                priced = True
            elif token == USDG:
                usd += amount / (10 ** usdg_decimals)
                priced = True

        c = cols[collection]
        c["sales"] += 1
        if priced:
            c["volume_usd"] += usd
            # Lowest PAID sale in the window. This is NOT a floor price -- a
            # floor is the lowest open listing, which only a marketplace API
            # exposes. Named accordingly everywhere it surfaces.
            if usd > 0 and (c["min_sale_usd"] is None or usd < c["min_sale_usd"]):
                c["min_sale_usd"] = usd
        else:
            c["unpriced_sales"] += 1
        try:
            c["buyers"].add(lg["topics"][1])
        except (KeyError, IndexError):
            pass

    ranked = sorted(
        ({"address": a, "sales": v["sales"], "volume_usd": v["volume_usd"],
          "buyers": len(v["buyers"]), "unpriced_sales": v["unpriced_sales"],
          "min_sale_usd": v["min_sale_usd"],
          "avg_price_usd": (v["volume_usd"] / (v["sales"] - v["unpriced_sales"]))
          if v["sales"] > v["unpriced_sales"] else None}
         for a, v in cols.items()),
        key=lambda x: -x["volume_usd"])[:top_n]

    for c in ranked:
        meta = _get(f"{BLOCKSCOUT}/tokens/{c['address']}") or {}
        c["name"] = meta.get("name") or "(unverified contract)"
        c["symbol"] = meta.get("symbol") or ""
        c["holders"] = _i(meta.get("holders") or meta.get("holders_count"))
        c["total_supply"] = _i(meta.get("total_supply"))
        c["explorer_url"] = f"https://robinhoodchain.blockscout.com/token/{c['address']}"

        os_meta = fetch_opensea_safelist(c["address"])
        c["opensea"] = os_meta
        c["safelist_status"] = (os_meta or {}).get("safelist_status")
        c["floor_price"] = (os_meta or {}).get("floor_price")
        c["floor_currency"] = (os_meta or {}).get("floor_currency")
        # Floors come back denominated in whatever the collection trades in --
        # ETH for most, USDG for some (Boomer Stockholders, PonsBrokers). USDG
        # is a dollar stablecoin, so it converts 1:1; anything else is left
        # unconverted rather than guessed at.
        _fc = (c["floor_currency"] or "").upper()
        if c["floor_price"] and _fc in ("ETH", "WETH"):
            c["floor_price_usd"] = c["floor_price"] * eth_px
        elif c["floor_price"] and _fc in ("USDG", "USDC", "USDT", "USD"):
            c["floor_price_usd"] = c["floor_price"]
        else:
            c["floor_price_usd"] = None
        # OpenSea's own collection page beats a composed /assets/ URL, and its
        # name is the marketplace-canonical one where Blockscout has none.
        c["opensea_url"] = ((os_meta or {}).get("opensea_url")
                            or f"https://opensea.io/assets/{OPENSEA_CHAIN}/{c['address']}")
        if not c["total_supply"]:
            c["total_supply"] = (os_meta or {}).get("total_supply")
        if c["name"] == "(unverified contract)" and (os_meta or {}).get("collection_name"):
            c["name"] = os_meta["collection_name"]
        # Independent 24h volume reading for the same collection. Ours is
        # Seaport-only in ETH terms; OpenSea's covers its own order flow.
        c["os_volume_24h"] = (os_meta or {}).get("os_volume_24h")

        # Collections declare an X handle on OpenSea; look up Ethos for it just
        # as memecoins do. Same caveat applies -- the handle is self-declared,
        # so it identifies a claim, not a verified owner.
        handle = (os_meta or {}).get("twitter_username")
        c["x_handle"] = handle
        c["ethos"] = fetch_ethos(handle) if handle else None
        c["project_url"] = (os_meta or {}).get("project_url")
        time.sleep(REQUEST_SLEEP)

    return {
        "collections": ranked,
        "window_hours": hours,
        "align": align,
        "from_block": from_bn,
        "to_block": to_bn,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "window_label": (dt.datetime.fromtimestamp(from_ts, dt.timezone.utc).strftime("%Y-%m-%d")
                         if align == "utc-day" else "rolling 24h"),
        "logs_scanned": len(logs),
        "logs_undecodable": skipped,
        "total_volume_usd": sum(c["volume_usd"] for c in ranked),
    }


# --------------------------------------------------------------------------- #
# Reward distributors -- the "RewardsCoin" family
# --------------------------------------------------------------------------- #
# A distinctive Robinhood Chain pattern: projects route Uniswap v4 hook fees
# into buying a DIFFERENT asset and paying it out to their own holders.
# CASHKITTEN buys CASHCAT; others pay USDG, and a few pay tokenised equities
# (MSFT, AAPL). cashkitten.fun tracks exactly one project this way -- this
# tracks all of them.
#
# Discovery is by EVENT, not by name, which matters on a chain this polluted:
# a contract either emitted DividendsDistributed or it did not, so there is no
# copycat exposure and no name-matching to get wrong.
#
# The template is verified on Blockscout as "RewardsCoin" and exposes
# rewardToken() + totalDividendsDistributed(). Reading those live over RPC is
# authoritative and self-consistent: CASHKITTEN's event sum and its view
# function agree to the wei, which is the cross-check for this whole section.
DIVIDENDS_DISTRIBUTED = "0xa493a9229478c3fcd73f66d2cdeb7f94fd0f341da924d1054236d78454116511"
DISTRIBUTOR_CACHE = OUT_DIR / "distributors.json"

Q_DISTRIBUTORS = """
select
    contract_address as token,
    count(*)         as distributions,
    min(block_time)  as first_distribution,
    max(block_time)  as last_distribution
from robinhood.logs
where topic0 = {topic}
group by 1
order by 2 desc
limit 200
"""


def _eth_call(to, sig, ret="uint", tries=2):
    """One read-only call, decoded. Returns None rather than raising -- an
    address that does not implement the interface is the normal case here."""
    sel = "0x" + keccak(sig.encode()).hex()[:8]
    for _ in range(tries):
        try:
            r = _session.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                             "params": [{"to": to, "data": sel}, "latest"]},
                              timeout=25).json()
            res = r.get("result")
            if not res or res == "0x":
                return None
            if ret == "addr":
                return "0x" + res[-40:]
            if ret == "str":
                b = bytes.fromhex(res[2:])
                if len(b) >= 64:                     # dynamic string
                    n = int.from_bytes(b[32:64], "big")
                    return b[64:64 + n].decode("utf8", "replace").strip("\x00")
                return b.decode("utf8", "replace").strip("\x00")   # bytes32 name
            return int(res, 16)
        except (requests.RequestException, ValueError):
            time.sleep(0.4)
    return None


def dune_query(sql, name, performance="large", timeout=400):
    """Run one Dune query, reusing a saved query id per logical name.

    Recreating queries every night would leak hundreds of saved queries into
    the account, so ids are cached in out/dune_queries.json and the SQL is
    PATCHed when it changes. Returns [] on any failure -- every caller must
    degrade rather than take the whole run down.
    """
    key = os.environ.get(DUNE_API_KEY_ENV)
    if not key:
        return []
    h = {"X-Dune-API-Key": key}
    cache_f = OUT_DIR / "dune_queries.json"
    meta = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    try:
        if name in meta:
            qid = meta[name]["id"]
            if meta[name].get("sql") != sql:
                requests.patch(f"https://api.dune.com/api/v1/query/{qid}", headers=h,
                               json={"query_sql": sql}, timeout=60)
                meta[name]["sql"] = sql
        else:
            r = requests.post("https://api.dune.com/api/v1/query", headers=h, timeout=60,
                              json={"name": f"hoodscout/{name}", "query_sql": sql,
                                    "is_private": True})
            r.raise_for_status()
            qid = r.json()["query_id"]
            meta[name] = {"id": qid, "sql": sql}
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        cache_f.write_text(json.dumps(meta, indent=2))

        r = requests.post(f"https://api.dune.com/api/v1/query/{qid}/execute", headers=h,
                          json={"performance": performance}, timeout=60)
        r.raise_for_status()
        eid = r.json()["execution_id"]
        waited = 0
        while waited < timeout:
            time.sleep(4)
            waited += 4
            st = requests.get(f"https://api.dune.com/api/v1/execution/{eid}/status",
                              headers=h, timeout=30).json().get("state")
            if st == "QUERY_STATE_COMPLETED":
                break
            if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                print(f"        dune {name}: {st}", flush=True)
                return []
        else:
            return []
        return requests.get(f"https://api.dune.com/api/v1/execution/{eid}/results",
                            headers=h, timeout=120).json()["result"]["rows"]
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"        dune {name} failed: {e}", flush=True)
        return []


def discover_distributors():
    """Find every contract that has ever emitted DividendsDistributed.

    Uses Dune because the alternative -- replaying the topic over ~33M blocks
    of eth_getLogs against a 10k-log cap -- is ~550 chunked calls for a few
    thousand events. Falls back to the cached set when Dune is unavailable so
    the nightly run degrades instead of losing the whole section.
    """
    key = os.environ.get(DUNE_API_KEY_ENV)
    cached = (json.loads(DISTRIBUTOR_CACHE.read_text())
              if DISTRIBUTOR_CACHE.exists() else [])
    if not key:
        return cached, "cache (no DUNE_API_KEY)"
    try:
        h = {"X-Dune-API-Key": key}
        sql = Q_DISTRIBUTORS.format(topic=DIVIDENDS_DISTRIBUTED)
        qid = None
        meta = json.loads(DISTRIBUTOR_CACHE.with_name("dune_queries.json").read_text()) \
            if DISTRIBUTOR_CACHE.with_name("dune_queries.json").exists() else {}
        if "distributors" in meta:
            qid = meta["distributors"]["id"]
            requests.patch(f"https://api.dune.com/api/v1/query/{qid}", headers=h,
                           json={"query_sql": sql}, timeout=60)
        else:
            r = requests.post("https://api.dune.com/api/v1/query", headers=h, timeout=60,
                              json={"name": "hoodscout/distributors", "query_sql": sql,
                                    "is_private": True})
            r.raise_for_status()
            qid = r.json()["query_id"]
            meta["distributors"] = {"id": qid, "sql": sql}
            DISTRIBUTOR_CACHE.with_name("dune_queries.json").write_text(json.dumps(meta, indent=2))

        r = requests.post(f"https://api.dune.com/api/v1/query/{qid}/execute", headers=h,
                          json={"performance": "medium"}, timeout=60)
        r.raise_for_status()
        eid = r.json()["execution_id"]
        for _ in range(75):
            time.sleep(4)
            st = requests.get(f"https://api.dune.com/api/v1/execution/{eid}/status",
                              headers=h, timeout=30).json().get("state")
            if st in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED"):
                break
        if st != "QUERY_STATE_COMPLETED":
            return cached, f"cache (dune {st})"
        rows = requests.get(f"https://api.dune.com/api/v1/execution/{eid}/results",
                            headers=h, timeout=60).json()["result"]["rows"]
        DISTRIBUTOR_CACHE.write_text(json.dumps(rows, indent=2, default=str))
        return rows, "dune"
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"        distributor discovery failed ({e}); using cache", flush=True)
        return cached, "cache (dune error)"


def fetch_reward_distributors(eth_price=None, min_usd=1.0):
    """Which projects pay their holders, in what, and how much so far."""
    candidates, source = discover_distributors()
    print(f"        {len(candidates)} candidate contracts from {source}", flush=True)

    price_cache = {}

    def reward_price(addr):
        if addr not in price_cache:
            ds = fetch_dexscreener_token(addr) or {}
            price_cache[addr] = _f(ds.get("price_usd"))
            time.sleep(0.2)
        return price_cache[addr]

    out = []
    for c in candidates:
        addr = c["token"]
        reward = _eth_call(addr, "rewardToken()", "addr")
        if not reward or int(reward, 16) == 0:
            continue                      # not a RewardsCoin, or misconfigured
        total = _eth_call(addr, "totalDividendsDistributed()")
        if not total:
            continue
        rdec = _eth_call(reward, "decimals()") or 18
        amount = total / (10 ** rdec)
        px = reward_price(reward)
        usd = amount * px if px else None
        if usd is not None and usd < min_usd:
            continue                      # dust: a deployed template that never ran

        # Half these contracts are satellite "DIVIDEND_TRACKER" instances, not
        # the project itself -- listing those by their own symbol answers the
        # wrong question. owner() points back at the token that deployed the
        # tracker, which is the project a reader actually recognises
        # (LOOT, $GIB, MACROHARD, HST).
        symbol = _eth_call(addr, "symbol()", "str") or "?"
        name = _eth_call(addr, "name()", "str") or ""
        project_addr = addr
        if "DIVIDEND" in (name or "").upper() or "DIVIDEND" in (symbol or "").upper():
            owner = _eth_call(addr, "owner()", "addr")
            if owner and int(owner, 16) != 0:
                osym = _eth_call(owner, "symbol()", "str")
                if osym:
                    symbol, name, project_addr = osym, _eth_call(owner, "name()", "str") or "", owner

        out.append({
            "address": project_addr,
            "tracker_address": addr if project_addr != addr else None,
            "symbol": symbol,
            "name": name,
            "reward_address": reward,
            "reward_symbol": _eth_call(reward, "symbol()", "str") or "?",
            "reward_decimals": rdec,
            "distributed": amount,
            "distributed_usd": usd,
            "reward_price_usd": px,
            "distributions": c.get("distributions"),
            "first_distribution": str(c.get("first_distribution") or "")[:10],
            "last_distribution": str(c.get("last_distribution") or "")[:10],
            "explorer_url": f"https://robinhoodchain.blockscout.com/token/{project_addr}",
        })
        time.sleep(0.05)

    out.sort(key=lambda x: -(x["distributed_usd"] or 0))
    return {
        "projects": out,
        "source": source,
        "candidates_scanned": len(candidates),
        "total_distributed_usd": sum(p["distributed_usd"] or 0 for p in out),
        "reward_assets": sorted({p["reward_symbol"] for p in out}),
    }


# --------------------------------------------------------------------------- #
# NFT boosters -- the second, larger reward family
# --------------------------------------------------------------------------- #
# The RewardsCoin family above pays ERC-20 holders. A separate family pays NFT
# holders, and it is BIGGER: StonkBrokers' StockBooster alone has sent ~$294k
# of tokenised AAPL/AMZN/NVDA to 2,051 holders, against ~$66k across every
# RewardsCoin project combined.
#
# It is a completely different contract shape -- no rewardToken(), no
# totalDividendsDistributed() -- so the ERC-20 probe cannot see it. These
# expose getStockTokens() and emit DropFinished, and pay in a basket rather
# than a single asset (OvertimeBooster pays SLV, MSFT, COST and USAR).
#
# Value distributed is measured as ERC-20 Transfers OUT of the booster, which
# is the money actually leaving the contract rather than an announced figure.
DROP_FINISHED = "0xf37cd5e9c8e09ef905b10bb36512f016d147a88e3a5d850289ec0b41fb20eae8"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BOOSTER_CACHE = OUT_DIR / "boosters.json"

Q_BOOSTERS = """
select contract_address as booster, count(*) as drops,
       min(block_time) as first_drop, max(block_time) as last_drop
from robinhood.logs
where topic0 = {topic}
group by 1 having count(*) >= {min_drops}
order by 2 desc
limit 50
"""

Q_BOOSTER_PAYOUTS = """
select
    bytearray_substring(topic1, 13, 20) as booster,
    contract_address                    as asset,
    count(*)                            as transfers,
    count(distinct bytearray_substring(topic2, 13, 20)) as recipients,
    sum(bytearray_to_uint256(bytearray_substring(data, greatest(1,length(data)-31), 32))) as raw
from robinhood.logs
where topic0 = {transfer}
  and bytearray_substring(topic1, 13, 20) in ({boosters})
group by 1, 2
"""


def _decode_address_array(hexdata):
    """getStockTokens() is address[3] on one booster and address[] on another,
    so sniff which: a dynamic array starts with a 0x20 offset word."""
    if not hexdata or hexdata == "0x":
        return []
    body = hexdata[2:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    if words and int(words[0], 16) == 32:
        n = int(words[1], 16) if len(words) > 1 else 0
        words = words[2:2 + n]
    return [a for a in ("0x" + w[24:] for w in words) if int(a, 16) > 0xFFFF]


def fetch_nft_boosters(dune_run, min_usd=1.0):
    """Which NFT projects pay their holders, in which real-world assets."""
    rows = dune_run(Q_BOOSTERS.format(topic=DROP_FINISHED, min_drops=2), "boosters")
    if not rows:
        return {"projects": [], "skipped": "discovery failed"}
    BOOSTER_CACHE.write_text(json.dumps(rows, indent=2, default=str))

    boosters = [r["booster"] for r in rows]
    lst = ", ".join(b if b.startswith("0x") else "0x" + b for b in boosters)
    pay = dune_run(Q_BOOSTER_PAYOUTS.format(transfer=TRANSFER_TOPIC, boosters=lst),
                   "booster_payouts")

    by_booster = defaultdict(list)
    for p in pay:
        by_booster[(p["booster"] or "").lower()].append(p)

    price_cache = {}

    def px(addr):
        if addr not in price_cache:
            ds = fetch_dexscreener_token(addr) or {}
            price_cache[addr] = _f(ds.get("price_usd")) or 0.0
            time.sleep(0.2)
        return price_cache[addr]

    out = []
    for r in rows:
        b = (r["booster"] or "").lower()
        name = (_get(f"{BLOCKSCOUT}/smart-contracts/{b}") or {}).get("name") or "booster"
        assets, total, holders = [], 0.0, 0
        for p in by_booster.get(b, []):
            a = (p["asset"] or "").lower()
            if a in (WETH, USDG, NATIVE):
                continue          # refunds / funding legs, not the reward basket
            dec = _eth_call(a, "decimals()") or 18
            amt = float(p["raw"] or 0) / (10 ** dec)
            usd = amt * px(a)
            if usd < min_usd:
                continue
            assets.append({"address": a, "symbol": _eth_call(a, "symbol()", "str") or "?",
                           "amount": amt, "usd": usd,
                           "recipients": p.get("recipients")})
            total += usd
            holders = max(holders, p.get("recipients") or 0)
        if not assets:
            continue
        assets.sort(key=lambda x: -x["usd"])
        out.append({
            "address": b, "name": name, "kind": "nft-booster",
            "assets": assets,
            "asset_symbols": [a["symbol"] for a in assets],
            "distributed_usd": total,
            "holders": holders,
            "drops": r.get("drops"),
            "first": str(r.get("first_drop") or "")[:10],
            "last": str(r.get("last_drop") or "")[:10],
            "explorer_url": f"https://robinhoodchain.blockscout.com/address/{b}",
        })
    out.sort(key=lambda x: -x["distributed_usd"])
    return {"projects": out,
            "total_distributed_usd": sum(p["distributed_usd"] for p in out),
            "reward_assets": sorted({s for p in out for s in p["asset_symbols"]})}


def _token_decimals(addr, default=6):
    d = _get(f"{BLOCKSCOUT}/tokens/{addr}") or {}
    return _i(d.get("decimals")) or default


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build_pulse(days=400, nft_hours=24, top_n=TOP_N,
                min_liquidity=MIN_LIQUIDITY_USD, skip_nfts=False,
                nft_align="utc-day", skip_rewards=False):
    print("Robinhood Chain Pulse — pulling", flush=True)

    print("  [1/5] Blockscout stats (DAU, gas fees)...", flush=True)
    chain = fetch_blockscout(days=days)

    print("  [2/5] DefiLlama (TVL, stablecoins, app fees)...", flush=True)
    llama = fetch_defillama()

    print("  [3/5] GeckoTerminal (memecoins)...", flush=True)
    memes = fetch_memecoins(min_liquidity=min_liquidity, top_n=top_n)
    print(f"        {len(memes['tokens'])} tokens kept, "
          f"{len(memes['excluded'])} excluded by the ${min_liquidity:,} floor", flush=True)

    nfts = {"collections": [], "skipped": True}
    if not skip_nfts:
        print("  [4/5] Seaport NFT sales (RPC log scan)...", flush=True)
        usdg_dec = _token_decimals(USDG)
        nfts = fetch_nft_collections(hours=nft_hours, top_n=top_n,
                                     eth_price=chain.get("eth_price_usd"),
                                     usdg_decimals=usdg_dec, align=nft_align)
        nfts["usdg_decimals"] = usdg_dec
    else:
        print("  [4/5] Seaport scan SKIPPED (--skip-nfts)", flush=True)

    print("  [5/5] Reward distributors (v4-hook holder payouts)...", flush=True)
    rewards = {"projects": [], "skipped": True}
    if not skip_rewards:
        rewards = fetch_reward_distributors(eth_price=chain.get("eth_price_usd"))
        print(f"        {len(rewards['projects'])} token projects, "
              f"${rewards['total_distributed_usd']:,.0f} to ERC-20 holders", flush=True)
        boosters = fetch_nft_boosters(dune_query)
        rewards["boosters"] = boosters
        rewards["combined_usd"] = (rewards.get("total_distributed_usd", 0)
                                   + boosters.get("total_distributed_usd", 0))
        rewards["all_assets"] = sorted(set(rewards.get("reward_assets", []))
                                       | set(boosters.get("reward_assets", [])))
        print(f"        {len(boosters.get('projects', []))} NFT boosters, "
              f"${boosters.get('total_distributed_usd', 0):,.0f} to NFT holders", flush=True)
    else:
        print("        SKIPPED (--skip-rewards)", flush=True)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chain": {"name": CHAIN_NAME, "chain_id": 4663},
        "stats": chain,
        "defillama": llama,
        "memecoins": memes,
        "nfts": nfts,
        "rewards": rewards,
    }


def main():
    ap = argparse.ArgumentParser(description="Robinhood Chain ecosystem pulse")
    ap.add_argument("--days", type=int, default=400,
                    help="days of daily-series history to pull (page windows client-side)")
    ap.add_argument("--nft-hours", type=int, default=24, help="Seaport volume window")
    ap.add_argument("--top", type=int, default=TOP_N, help="entries per leaderboard")
    ap.add_argument("--min-liquidity", type=float, default=MIN_LIQUIDITY_USD,
                    help="USD liquidity floor for the memecoin ranking")
    ap.add_argument("--nft-align", choices=["utc-day", "rolling"], default="utc-day",
                    help="align the Seaport window to the last complete UTC day "
                         "(matches every other metric) or roll back from now")
    ap.add_argument("--skip-nfts", action="store_true",
                    help="skip the Seaport log scan (the slow step)")
    ap.add_argument("--skip-rewards", action="store_true",
                    help="skip the reward-distributor scan")
    ap.add_argument("--out", default=str(OUT_DIR / "pulse.json"))
    args = ap.parse_args()

    t0 = time.time()
    pulse = build_pulse(days=args.days, nft_hours=args.nft_hours, top_n=args.top,
                        min_liquidity=args.min_liquidity, skip_nfts=args.skip_nfts,
                        nft_align=args.nft_align, skip_rewards=args.skip_rewards)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pulse, indent=2, default=str))
    print(f"\nWrote {out} in {time.time() - t0:.1f}s", flush=True)

    s, l = pulse["stats"], pulse["defillama"]
    print(f"  TVL              ${l['tvl_current']:,.0f}" if l.get("tvl_current") else "  TVL n/a")
    print(f"  Stablecoins      ${l['stables_current']:,.0f}" if l.get("stables_current") else "  Stables n/a")
    print(f"  DAU              {s['dau_current']:,.0f}" if s.get("dau_current") else "  DAU n/a")
    print(f"  Gas fees (24h)   ${s['gas_fees_usd_current']:,.0f}" if s.get("gas_fees_usd_current") else "  Gas n/a")
    print(f"  App fees (24h)   ${l['app_fees_24h']:,.0f}" if l.get("app_fees_24h") else "  App fees n/a")
    print(f"  Memecoins        {len(pulse['memecoins']['tokens'])}")
    print(f"  NFT collections  {len(pulse['nfts'].get('collections', []))}")
    rw = pulse.get("rewards") or {}
    if rw.get("projects"):
        nb = rw.get("boosters") or {}
        print(f"  Reward payers    {len(rw['projects'])} tokens + "
              f"{len(nb.get('projects', []))} NFT boosters = "
              f"${rw.get('combined_usd', 0):,.0f} to holders")
        print(f"                   assets: {', '.join(rw.get('all_assets', [])[:10])}")


if __name__ == "__main__":
    main()
