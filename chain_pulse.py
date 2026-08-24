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
# 30, not 20. The boards used to expand to their full height to show the tail,
# so every extra row was page length you paid for whether or not anyone looked.
# Paged boards are a fixed height at any row count, which makes the cut a
# question of what is worth ranking rather than what fits. 20 was leaving real
# coins off: HMM (0x7fe995a8, $895k liquidity, $1.7M daily volume) sat ~23rd
# and was invisible, while three tokenised equities held slots above it.
TOP_N = 30

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
            # A 4xx is an ANSWER, not a failure to get one, so it is never
            # retried. Blockscout 404s /smart-contracts/<addr> for every
            # unverified contract, and the payout scan asks about a lot of
            # them: at six tries with escalating sleeps that is ~22s to be told
            # "unverified" -- twice per candidate, in a loop with no budget on
            # it. Retrying still covers the cases that deserve it (5xx,
            # timeouts, dropped connections), which are the transient ones.
            if 400 <= r.status_code < 500:
                return None
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
    base = [p for p in (d if isinstance(d, list) else [])
            if ((p.get("baseToken") or {}).get("address") or "").lower() == address.lower()]
    if not base:
        # ⚠️ token-pairs/v1 returns an EMPTY LIST for tokens that latest/dex
        # /tokens indexes perfectly well -- measured on HMM (0x7fe995a8),
        # $897k liquidity across 12 pairs, invisible on the first endpoint and
        # complete on the second. An empty list is not "no market", it is one
        # index not covering the token, and treating the two as the same thing
        # silently dropped corroboration for whole tokens. Any token failing
        # corroboration falls back to GeckoTerminal's liquidity, which is the
        # figure known to go NEGATIVE here -- so this gap fed straight into the
        # $25k floor and removed real coins from the board.
        alt = _get(f"{DEXSCREENER}/latest/dex/tokens/{address}") or {}
        base = [p for p in (alt.get("pairs") or [])
                if ((p.get("baseToken") or {}).get("address") or "").lower() == address.lower()
                and (p.get("chainId") or "") == GT_NETWORK]
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

    # ⚠️ Tokenised equities are not memecoins, and they were ranking high enough
    # to take four of the twenty slots (SPCX 3rd, NVDA 5th, SPY 15th by volume)
    # -- pushing real coins off the board entirely.
    #
    # Excluded by ADDRESS, never by symbol. The distinction is the whole game
    # here: 0xdaa8f3f5 trades as HOOD and is "TheGreenHood", a memecoin with
    # 1,074 holders and a 1.01e27 supply, while three OTHER contracts also
    # answer to HOOD and are genuinely Robinhood Markets equity. A symbol
    # blocklist would delete the memecoin and keep whichever equity happened to
    # be indexed; matching the address deletes exactly the right ones.
    equities = set()
    try:
        equities = {a.lower() for a in discover_stock_tokens()}
    except Exception as e:                       # never lose the board over this
        print(f"        equity list unavailable ({e}); not filtering", flush=True)
    candidates = [t for t in tokens.values()
                  if t["symbol"].upper() not in INFRA_SYMBOLS
                  and t["address"].lower() not in equities]
    if equities:
        print(f"        {len(equities)} tokenised equities excluded by address", flush=True)
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
    # ⚠️ The unchecked tail still needs gt_liquidity. Only _corroborate sets it,
    # and it runs on the working set alone, but the ranking loop below reads
    # t["gt_liquidity"] for EVERY candidate. That is a latent KeyError that only
    # fires when the chain shows more than top_n*3 distinct tokens across the
    # four pages -- 32 one morning, past 60 that evening, which took the whole
    # refresh down. Mirrors the assignment in _corroborate: unchecked means
    # nobody corroborated the number, not that there isn't one.
    for t in candidates[top_n * 3:]:
        t["corroboration"] = "unchecked"
        t["gt_liquidity"] = t["liquidity"]
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
                      chunk=SEAPORT_CHUNK_BLOCKS, depth=0, topics=None,
                      quiet=False, gaps=None):
    """eth_getLogs across a range, splitting on the 10k-log cap.

    The cap error also fires transiently on ranges that are nowhere near it,
    so a failed chunk is retried by halving rather than being trusted as a
    genuine "too many logs" signal.

    `topics` overrides the single-topic0 filter for callers that also pin an
    indexed argument. `gaps` is an optional list that collects any range this
    could not read even at minimum chunk size -- a caller that persists a
    watermark MUST check it, because a silent gap plus an advanced watermark
    makes the loss permanent.

    ⚠️ Never pass positional nulls in `topics` to skip an indexed slot. This
    RPC ignores them and returns an EMPTY LIST rather than an error, which is
    indistinguishable from "no such events". Filter on the topics you can pin
    from the left and match the rest client-side.
    """
    logs = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        res, err = _rpc("eth_getLogs", [{
            "address": address, "topics": topics or [topic0],
            "fromBlock": hex(start), "toBlock": hex(end),
        }])
        if err:
            if chunk <= 500:
                print(f"    [skip] {start}-{end} irreducible: {err}", flush=True)
                if gaps is not None:
                    gaps.append((start, end))
                start = end + 1
                continue
            sub = _get_logs_chunked(address, topic0, start, end,
                                    chunk=max(chunk // 4, 500), depth=depth + 1,
                                    topics=topics, quiet=quiet, gaps=gaps)
            logs.extend(sub)
            start = end + 1
            continue
        logs.extend(res or [])
        start = end + 1
        if depth == 0 and not quiet:
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
        """DexScreener's price, refused when the explorer flatly contradicts it.

        ⚠️ MEASURED 2026-08-20: DexScreener quoted USDG -- this chain's dollar
        stablecoin, the reward asset for most of these contracts -- at $503.54.
        Blockscout reported exchange_rate 1.0 for the same token in the same
        minute. Every USDG payout went out multiplied by ~503, and the board
        published $4,064,517 against a true ~$65k. A $2.5M top row was $5,045.

        The pipeline was already right to prefer market data over a number a
        contract reports about itself -- but a market source is still ONE
        source, and a bad pair quote is indistinguishable from a real move
        unless something else is asked. So the two are compared: when they
        disagree by more than 5x the explorer's rate wins, because a 5x gap is
        not a price move, it is one of them being wrong, and the explorer's is
        the conservative failure. Where Blockscout has no rate (most memecoins)
        DexScreener stands alone exactly as before.
        """
        if addr not in price_cache:
            ds = fetch_dexscreener_token(addr) or {}
            dsp = _f(ds.get("price_usd"))
            rate = _f((_get(f"{BLOCKSCOUT}/tokens/{addr}") or {}).get("exchange_rate"))
            if dsp and rate and (dsp / rate > 5 or rate / dsp > 5):
                print(f"        ⚠️ price disagreement on {addr[:10]}: "
                      f"dexscreener ${dsp:,.4f} vs explorer ${rate:,.4f} "
                      f"-- using the explorer", flush=True)
                dsp = rate
            price_cache[addr] = dsp or rate
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
            "pending_usd": _pending_usd(b, [a["address"] for a in assets], px),
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


# --------------------------------------------------------------------------- #
# NFT wage pools — the third family
# --------------------------------------------------------------------------- #
# The first two families are found by INTERFACE: RewardsCoin ERC-20s answer
# rewardToken() + totalDividendsDistributed(), boosters answer getStockTokens()
# and emit DropFinished. Both miss a contract that pays holders by any other
# shape, and one such contract was distributing more than every ERC-20
# distributor on the board combined.
#
# MEASURED, Zaibatsu Wagies' "wage pool" 0xf22554273505a0d59323ca6e3a03877810238b97:
# 8,570 GME ($159,481) paid to 272 wallets across 3,400 transfers -- which ranks
# it SECOND on this board, ahead of OvertimeBooster. It answers rewardToken()
# (returning GME) but not totalDividendsDistributed(), so the ERC-20 probe
# called it, got a valid answer, and then discarded it on the second call.
#
# ⚠️ Do NOT discover this family by interface or by event topic. Its most common
# event is emitted by 79 unrelated contracts on this chain, only one of which is
# a wage pool -- keying on it would be almost entirely false positives. Discover
# by the PAYOUT instead: a contract sending one ERC-20 to hundreds of distinct
# wallets is doing a distribution whatever its interface, and nft() then
# attributes it to a collection. That test is mechanism-agnostic, so it also
# catches the next template nobody has written yet.
WAGE_POOL_REGISTRY = OUT_DIR / "wage_pools.json"

# A curated basket tops out around a dozen; past that it is an aggregator.
MAX_BASKET_ASSETS = 12
PAYOUT_NAME_RE = re.compile(
    r"distribut|reward|payout|reflect|dividend|salary|booster|clockin|wage", re.I)

# Contracts that move many tokens to many wallets as a matter of routine. They
# pass every fanout test ever written and none of them pays holders.
PAYOUT_INFRA = {
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",   # Uniswap v4 PoolManager
    "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",   # RelayRouterV3
    "0x0000000000000000000000000000000000000000",
    # A CLPool clone (eip-1167 -> CLPool). Its swap outflow reached 1,196
    # wallets and it answers nft() -- pointing at its own position manager --
    # so it came out as the chain's largest "payout" at $1.5M, 4x the real
    # leader. A concentrated-liquidity pool paying its LPs is not a project
    # paying its holders.
    "0x9d590437abaae12cf9fe0627caf4cfd633152599",
    "0xb4acbc082b5e7ded571c98ee4257778a9d784b36",   # V3Utils, LP swap+mint helper
    "0xe33e9e479df8802cb0866d5d05258bec4cf62948",   # PonsV2LaunchAndBuy, launchpad router
    # A venue, not a payer: 57% of the wallets it pays also SEND it tokens.
    # Unverified eip1967 proxy at a leading-zero vanity address, moving ten
    # tokenised equities -- it passed the basket test and would have published
    # as a $271,152 row, top three on the board.
    "0x00000000e91fc5bad977c0cc4ad60557c06886a2",
    # Same one-way-flow test, same run: 35.3% and 55.2% of their recipients also
    # send them tokens. Together they would have published $4,087,544 -- the top
    # two rows, dwarfing every real project on the board.
    #
    # ⚠️ NAME IS NOT THE TELL. "TransparentUpgradeableProxy" is a venue, but
    # "BeaconProxy" ($90,603) and "ProtocolProxy" ($57,866) score 0.0% and are
    # real payers -- ProtocolProxy pays 150 wallets and receives from none. A
    # generic proxy name means the verified name is the wrapper's, nothing more.
    "0x1195c074f898b7644ba732407619c9804dfe6dce",
    "0x2ca37ff95caf25366ef16fc2e655b78a165d125f",
}

# ⚠️ THE TEST THAT CAUGHT IT, worth generalising: a payout contract's flow is
# ONE-WAY. Its recipients are holders, and holders do not send it the asset back.
# A venue's counterparties are the same wallets in both directions. MEASURED on
# the explorer's per-address transfer index, which answers this in a few pages:
#
#   QuotronReflectionsV2   out=30  in=1   overlap=0    (0.0%)
#   Zaibatsu wage pool     out=124 in=12  overlap=4    (3.2%)
#   0x00000000e9 (venue)   out=28  in=32  overlap=16  (57.1%)
#
# Clean separation with an empty middle, like the basket-size split. Interface
# and ABI tests both said "maybe" here -- unverified, no nft() -- and BEHAVIOUR
# answered decisively. Not yet wired into discovery; it is the next filter to
# add if another of these appears.

# What an nft() answer has to survive to be believed, and what a basket-path
# candidate has to survive to be called a payer. Both are read off the
# contract's own INTERFACE, never off a name badge or a self-reported number.
#
# An LP position manager is an ERC-721 with holders and supply, so "is it a real
# collection" does not separate it -- 0x07f4 (NonfungiblePositionManager) has
# 21,543 supply and 1,196 holders, more than any genuine collection here.
# MEASURED across the seven collections currently on the board (StonkBrokers,
# Zaibatsu, Stackers, RoboBrokers, LMRH, StonkPepe) plus the position manager:
# the position manager answers factory() AND WETH9(); not one real collection
# answers either. Dex plumbing is the thing a PFP contract has no reason to
# carry.
POSITION_MANAGER_SELECTORS = ("factory()", "WETH9()")

# The basket path has no nft() to corroborate, so it reads the verified ABI for
# SHAPE: a router exposes swap/liquidity/launch entry points, a payer exposes
# distribution machinery. MEASURED on the four verified basket-path contracts:
#   V3Utils              swapRouter, swapAndMint, swapAndIncreaseLiquidity  -- and
#                        no claim, no pending, no distribute            REJECT
#   PonsV2LaunchAndBuy   launchAndBuy, factory, rescue (8 functions total) REJECT
#   QuotronReflectionsV2 claim, pending, basketPending, notifyFees         KEEP
#   USDGBuyerDistributorV2 distributeBatch, accrued, eligible              KEEP
# USDGBuyerDistributorV2 also carries router() and buyStocksV4Fallback --
# a payer legitimately routes to BUY the reward -- so router words alone must
# never reject. Only router-shaped AND NOT payout-shaped is infrastructure.
#
# ⚠️ Rejects only what it can positively identify. An unverified contract has no
# ABI to read, so it is left to the existing basket and name tests rather than
# being dropped on absence of evidence.
# ⚠️ Ownership and role boilerplate is stripped BEFORE the payout vocabulary is
# matched. Ownable2Step's `pendingOwner` contains "pending", which is enough to
# read as payout machinery -- PonsV2LaunchAndBuy has eight functions, six of
# them ownership boilerplate, and it was rescued from the router test by that
# one word. These names say nothing about what a contract does with money.
ABI_BOILERPLATE = {
    "owner", "transferownership", "renounceownership", "acceptownership",
    "pendingowner", "grantrole", "revokerole", "renouncerole", "hasrole",
    "getroleadmin", "supportsinterface", "initialize", "pause", "unpause",
    "paused", "implementation", "upgradeto", "upgradetoandcall", "rescue",
}
ROUTER_ABI_RE = re.compile(r"swap|liquidity|launch|multicall|exactinput|exactoutput", re.I)
PAYOUT_ABI_RE = re.compile(
    r"claim|pending|accrue|distribut|earned|harvest|reward|dividend|payout|eligible",
    re.I)


def _is_collection(addr):
    """True if `addr` looks like a real NFT collection rather than dex plumbing.

    The nft() accessor was meant to be the clean attribution test -- AMMs and
    routers never implement it. LP position managers do, which is how a CLPool
    clone came out as the chain's biggest holder payout. So the answer is
    corroborated instead of taken at face value: it must be an ERC-721 the
    explorer knows, and it must not carry a position manager's dex plumbing.
    """
    if not addr or not int(addr, 16):
        return False
    if any(_eth_call(addr, sel, "addr") for sel in POSITION_MANAGER_SELECTORS):
        return False
    return ((_get(f"{BLOCKSCOUT}/tokens/{addr}") or {}).get("type") or "") == "ERC-721"


def _is_router(addr):
    """True if the verified ABI is router-shaped and carries no payout machinery.

    Unverified contracts return False: no ABI is no evidence, not evidence of
    innocence, and the basket and name tests still apply to them.
    """
    abi = (_get(f"{BLOCKSCOUT}/smart-contracts/{addr}") or {}).get("abi") or []
    fns = [f.get("name") or "" for f in abi if f.get("type") == "function"]
    if not fns:
        return False
    names = " ".join(f for f in fns
                     if f.lower().strip("_") not in ABI_BOILERPLATE)
    return bool(ROUTER_ABI_RE.search(names)) and not PAYOUT_ABI_RE.search(names)

# Discovery runs on the RPC, not Dune. The Dune account cannot create new saved
# queries (the API 402s on POST /v1/query while existing query ids still
# execute), and reusing another section's query id would clobber it. The RPC is
# also the authoritative source for the payout total either way.
#
# The asset universe is derived, not hardcoded: Robinhood's tokenised equities
# all carry the name "<Company> • Robinhood Token", so the explorer's own search
# enumerates them (50 including GME, AAPL, NVDA, TSLA...). A pool paying holders
# in something else is still caught once it lands in the registry below.
STOCK_TOKEN_MARK = "Robinhood Token"


_STOCK_TOKEN_CACHE = {}


def discover_stock_tokens(limit=200, pages=4):
    """Addresses of the tokenised equities, by their naming convention.

    ⚠️ PAGINATED. It read page one only and returned exactly 50 while the
    explorer was still handing back `next_page_params` -- so the equity
    universe was silently truncated at whatever the first page held, and every
    caller that excludes equities was working from a partial list.

    Cached per process: three separate sections ask for this set, and it does
    not change within a run.
    """
    if _STOCK_TOKEN_CACHE:
        return dict(_STOCK_TOKEN_CACHE)
    out, params = {}, {"q": STOCK_TOKEN_MARK}
    for _ in range(pages):
        d = _get(f"{BLOCKSCOUT}/search", params=params) or {}
        for it in (d.get("items") or []):
            name = str(it.get("name") or "")
            addr = (it.get("address") or it.get("address_hash") or "").lower()
            if STOCK_TOKEN_MARK in name and addr.startswith("0x"):
                out[addr] = it.get("symbol") or "?"
        nxt = d.get("next_page_params")
        if not nxt or len(out) >= limit:
            break
        params = {"q": STOCK_TOKEN_MARK, **nxt}
        time.sleep(0.2)
    _STOCK_TOKEN_CACHE.update(out)
    return dict(list(out.items())[:limit])


LAUNCHPAD_REGISTRY = OUT_DIR / "launchpad_tokens.json"


def fetch_launchpad_pools(pages=2, sleep=2.2, budget_s=240):
    """Every dex's top pools, live, with the fields the launchpad index needs.

    Replaces replaying the observation ledger for this. The ledger was only ever
    being asked for FDV here, and it answers with a STALE figure (whatever was
    last polled) over a window bounded by retention -- which is what pushed it
    to 115 MB and past GitHub's hard limit. GeckoTerminal answers with the
    CURRENT figure, for every dex on the chain rather than the ~15 our poller
    happened to sample, in about a minute.

    Returned per token, not per pool: a coin routinely holds several pools on one
    dex, and its EARLIEST pool across all dexes is what says where it launched.
    That earliest-pool date also does the job the ledger's first-sight FDV used
    to do -- an established token opening a new pool has an older pool elsewhere,
    so it is attributed to that older pad and does not flatter the new one.

    ⚠️ PACING DOES NOT FIX GECKOTERMINAL'S 429s -- MEASURED, twice. A 31-dex
    sweep spent 434 of its 650 seconds asleep in backoff, and raising the gap
    from 2.2s to 3.0s made it WORSE (8 of 14 calls throttled at 2.2s, 11 of 14
    at 3.0s). The limit is a rolling quota over a long window, not a per-minute
    rate, so a slower sweep buys nothing and simply runs longer.

    The cost has to come off the CALL COUNT instead, and the per-dex sweep is
    not negotiable: the index needs each pad's top ten coins BY FDV, and the
    small launchers (Mint Club at 5 coins, Hoodit at 1) never surface in a
    chain-wide ranking by volume -- a 10-page chain-wide sweep returned 146 of
    the 577 tokens and would have deleted those pads from the board outright.

    So the sweep persists, exactly like the wage-pool registry: what it reads is
    merged into a stored set, a wall-clock budget bounds the run, and dexes are
    visited LEAST-RECENTLY-SWEPT FIRST so a short run rotates through them
    across days instead of always starving the same tail. A throttled run keeps
    yesterday's record for the dexes it did not reach rather than dropping them.
    """
    reg = {}
    if LAUNCHPAD_REGISTRY.exists():
        try:
            reg = json.loads(LAUNCHPAD_REGISTRY.read_text())
        except (ValueError, OSError):
            reg = {}
    tokens = {t["addr"]: t for t in (reg.get("tokens") or []) if t.get("addr")}
    swept = dict(reg.get("swept") or {})            # dex id -> ISO of last sweep
    print(f"        {len(tokens)} tokens carried over from the last sweep", flush=True)

    dexes = []
    for page in (1, 2):
        d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/dexes", params={"page": page})
        rows = (d or {}).get("data") or []
        if not rows:
            break
        dexes += [r.get("id") for r in rows if r.get("id")]
        time.sleep(sleep)
    print(f"        {len(dexes)} dexes on chain", flush=True)

    deadline = time.time() + budget_s
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    done = 0
    for dex in sorted(dexes, key=lambda d: swept.get(d) or ""):
        if time.time() > deadline:
            print(f"        launchpad budget reached after {done} of {len(dexes)} "
                  f"dexes; the rest keep their stored pools", flush=True)
            break
        for page in range(1, pages + 1):
            # tries=2, not the default 6: against a rolling quota a retry is
            # just another throttled call, and six of them burn 30s to learn
            # what two learn in six. The registry covers what this run missed.
            d = _get(f"{GECKOTERMINAL}/networks/{GT_NETWORK}/dexes/{dex}/pools",
                     params={"page": page, "include": "base_token",
                             "sort": "h24_volume_usd_desc"}, tries=2)
            rows = (d or {}).get("data") or []
            if not rows:
                break
            inc = {x["id"]: (x.get("attributes") or {})
                   for x in ((d or {}).get("included") or [])}
            for r in rows:
                a = r.get("attributes") or {}
                rel = r.get("relationships") or {}
                bid = ((rel.get("base_token") or {}).get("data") or {}).get("id")
                bt = inc.get(bid) or {}
                addr = (bt.get("address") or "").lower()
                born = a.get("pool_created_at") or ""
                if not addr or not born:
                    continue
                fdv = _f(a.get("fdv_usd")) or 0.0
                liq = _f(a.get("reserve_in_usd")) or 0.0
                prev = tokens.get(addr)
                # earliest pool decides the launchpad; richest pool carries value
                if prev is None or born < prev["born"]:
                    tokens[addr] = {"addr": addr, "sym": bt.get("symbol") or "?",
                                    "dex": dex, "born": born,
                                    "fdv": fdv, "liq": liq}
                else:
                    prev["fdv"] = max(prev["fdv"], fdv)
                    prev["liq"] = max(prev["liq"], liq)
            # A short page is the last page. Asking for the next one costs a
            # call against the quota to be told nothing, and most dexes here
            # carry well under one page of pools.
            if len(rows) < 20:
                break
            time.sleep(sleep)
        swept[dex] = now
        done += 1
    LAUNCHPAD_REGISTRY.write_text(json.dumps(
        {"tokens": sorted(tokens.values(), key=lambda t: t["addr"]),
         "swept": swept}, indent=2))
    print(f"        {len(tokens)} distinct tokens across those dexes "
          f"({done} dexes swept this run)", flush=True)
    return list(tokens.values())


def _pending_usd(holder, assets, price_of):
    """Value of reward assets still sitting in a payout contract, unclaimed.

    Distributed and pending are DIFFERENT FACTS and must not be summed: one has
    reached holders, the other has not. Reporting only the first quietly
    penalises the epoch/claim model -- a pool that converts fees periodically
    looks smaller than a push-based one between conversions, purely because of
    when the job happened to run.

    Measured as the contract's own balance of the assets it pays, which is
    generic (one balanceOf per asset) and true for both models: for a claim
    pool it is money holders can take right now.

    ⚠️ It does NOT include fees still upstream of the contract. QUOTRONS accrues
    trading fees in a Uniswap v4 hook (QuotronWethHook, 4.795 WETH ≈ $9,046 at
    the time of writing, two thirds of it earmarked for stocks under their
    documented 3% split) and only converts on an epoch. Counting that would need
    each project's hook, which is only knowable here because their docs name it,
    so it is deliberately out of scope rather than counted for one project and
    not the rest.
    """
    total = 0.0
    for a in assets:
        res, err = _rpc("eth_call", [{"to": a,
                                      "data": "0x70a08231" + holder[2:].rjust(64, "0")},
                                     "latest"])
        if err or not res or res == "0x":
            continue
        try:
            bal = int(res, 16)
        except ValueError:
            continue
        if not bal:
            continue
        dec = _eth_call(a, "decimals()") or 18
        total += bal / (10 ** dec) * (price_of(a) or 0.0)
    return total


def _contract_name(addr):
    """The verified contract name, if the explorer has one."""
    d = _get(f"{BLOCKSCOUT}/addresses/{addr}") or {}
    return d.get("name") or (_get(f"{BLOCKSCOUT}/smart-contracts/{addr}") or {}).get("name")


def _pool_assets(pool, pages=6):
    """Every token this contract has ever SENT, from the explorer's index.

    Deriving the asset set from what the discovery window happened to see made
    the reported total depend on when the job ran -- StonkBrokers came out at
    $3,537 on one run and $2,525 on the next, purely from which equities showed
    up in the window. A single-address transfer index answers it directly and
    identically every time. This is the low-volume lookup Blockscout is good at;
    only the asset SET comes from here, never an amount.
    """
    assets, params = set(), {}
    for _ in range(pages):
        d = _get(f"{BLOCKSCOUT}/addresses/{pool}/token-transfers", params=params) or {}
        items = d.get("items") or []
        for it in items:
            frm = ((it.get("from") or {}).get("hash") or "").lower()
            tok = ((it.get("token") or {}).get("address") or "").lower()
            if frm == pool.lower() and tok.startswith("0x"):
                assets.add(tok)
        params = d.get("next_page_params") or {}
        if not params:
            break
        time.sleep(0.2)
    return assets


def _fanout_scan(asset, from_block, to_block, rec, chunk=400_000):
    """Accumulate sender -> ({recipients}, {assets}) for one asset.

    ⚠️ Accumulates ACROSS assets instead of thresholding within each one, which
    is the whole point. QUOTRONS V2 converts trading fees into TEN tokenised
    stocks each epoch, so its reflections contract paid 60 distinct wallets in
    8 hours while its BIGGEST single asset reached only 26 -- under a per-asset
    threshold of 40 it was invisible, despite having paid 252 wallets overall.
    A basket payer is the normal shape here, not the exception.
    """
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        logs = _get_logs_chunked(asset, TRANSFER_TOPIC, start, end)
        for l in logs:
            t = l.get("topics") or []
            if len(t) != 3:                      # 4 topics == ERC-721
                continue
            sender = "0x" + t[1][-40:].lower()
            if sender in PAYOUT_INFRA:
                continue
            rec[sender][0].add(t[2][-40:].lower())
            rec[sender][1].add(asset)
        start = end + 1


def fetch_nft_wage_pools(known=(), min_usd=1.0, min_recipients=40, hours=8,
                         budget_s=300, measure_budget_s=360):
    """Contracts paying an ERC-20 to the holders of a specific NFT collection.

    `known` is the set of addresses the other two families already claim, so a
    booster is never reported twice under a second name.

    Confirmed pools persist in a registry. Discovery only looks at a recent
    window -- cheap, but blind to a pool that paid before it and has been quiet
    since -- so remembering what was already proven is what keeps the board
    stable. The registry stores addresses only; every figure is re-measured
    from chain logs on each run.
    """
    claimed = {a.lower() for a in known if a}
    reg = []
    if WAGE_POOL_REGISTRY.exists():
        try:
            reg = json.loads(WAGE_POOL_REGISTRY.read_text())
        except (ValueError, OSError):
            reg = []
    candidates = {r["pool"].lower() for r in reg if r.get("pool")}
    # Which assets each pool has actually been seen paying. Without this the
    # payout scan would sweep all ~50 equities against every pool -- ~600 RPC
    # calls each, for tokens it has never touched.
    seen_assets = defaultdict(set)
    for r in reg:
        if r.get("pool"):
            seen_assets[r["pool"].lower()] |= {a.lower() for a in (r.get("assets") or [])}

    head, err = _rpc("eth_blockNumber", [])
    if err or not head:
        return {"projects": [], "skipped": "rpc unavailable"}
    head = int(head, 16)
    span = int(hours * 3600 / 0.101)             # ~101ms blocks
    stocks = discover_stock_tokens()
    print(f"        scanning {len(stocks)} stock tokens over {hours}h for payouts",
          flush=True)
    # Discovery is the open-ended part -- a heavily traded equity can carry a lot
    # of Transfer logs -- and this runs unattended inside a 30-minute job. So it
    # gets a wall-clock budget: whatever is scanned by then is used, and the
    # registry keeps every pool already proven. Better a late discovery than a
    # refresh that times out and publishes nothing.
    #
    # 300s + 360s, down from 420 + 600. MEASURED per phase: everything else in
    # the pull (chain stats, DefiLlama, memecoins, the Seaport scan, the ERC-20
    # payers and the NFT boosters) comes to ~220s TOTAL, and the launchpad sweep
    # is capped at 240s, so this phase is the entire difference between a
    # 20-minute pull and the 59-minute one that overran CI's timeout. Cutting it
    # costs nothing permanent now that BOTH loops rotate: measurement resumes at
    # its stored watermark and discovery starts at a different equity each day,
    # so a short run defers work rather than dropping it.
    deadline = time.time() + budget_s
    rec = defaultdict(lambda: (set(), set()))
    # ⚠️ ROTATE THE START. The measurement loop was starving its tail until it
    # was reordered oldest-watermark-first; discovery has the identical flaw and
    # no watermark to sort by. Sweeping a fixed order under a wall-clock budget
    # means the same trailing equities are never reached on ANY run, so a pool
    # that only ever pays in one of them stays invisible forever rather than
    # being found late. Offsetting by day-of-year walks the whole list across a
    # few daily runs and needs no extra state to do it.
    stocks = list(stocks)
    if stocks:
        off = dt.datetime.now(dt.timezone.utc).timetuple().tm_yday % len(stocks)
        stocks = stocks[off:] + stocks[:off]
    for i, asset in enumerate(stocks):
        if time.time() > deadline:
            print(f"        discovery budget reached after {i} of {len(stocks)} "
                  f"tokens; registry still measured in full", flush=True)
            break
        try:
            _fanout_scan(asset, max(head - span, 0), head, rec)
        except Exception as e:                   # one asset must not kill the run
            print(f"        fanout scan failed on {asset[:10]}: {e}", flush=True)
    for a, (wallets, assets_seen) in rec.items():
        if len(wallets) >= min_recipients:
            candidates.add(a)
            seen_assets[a] |= assets_seen

    # nft() is the attribution test AND the filter: AMMs, routers and bridges
    # all move tokens to many wallets, and none of them implement it.
    # ATTRIBUTION. nft() is the clean case: it names the collection outright and
    # excludes AMMs and routers, which never implement it. It is not the only
    # shape though -- QuotronReflectionsV2 is a verified, working payout contract
    # that implements none of the usual accessors (nft, collection, nftContract,
    # erc721, token all return nothing), so requiring one dropped it even after
    # discovery had correctly found it.
    #
    # Without nft(), the discriminator is that A PAYOUT BASKET IS CURATED AND AN
    # AGGREGATOR IS NOT. MEASURED over one sweep, the split has an empty middle:
    #   payers   1, 3, 10, 10, 11, 11 assets  (QUOTRONS pays its documented ten)
    #   routers  18, 22, 25, 26, 27, 29, 30, 31  (LiFiDiamond, RobinHoodSettler,
    #            bare ERC1967/Transparent proxies, unnamed aggregators)
    # A project picks the few equities it pays in; an aggregator touches nearly
    # every one on the chain. An unbounded "3 or more" rule put a bridge and a
    # settlement contract on the board, so the ceiling is the real test.
    #
    # A verified name that says what the contract does is evidence too, and it
    # rescues the curated-but-broad case (USDGBuyerDistributorV2, 18 assets)
    # without readmitting LiFiDiamond, which claims nothing of the sort. Names
    # are weak evidence, so they only ever ADD to the asset test, never replace
    # it. A pool kept this way carries no collection link rather than a guess.
    # A pool that has ALREADY been proven to pay is never re-litigated. The
    # tests below are for identifying new candidates; re-running them on known
    # payers means one transient RPC failure erases a real project from the
    # registry permanently. Zaibatsu pays a single asset (GME), so it would fail
    # the basket test and survive only on nft() answering -- every single run.
    proven = {r["pool"]: r for r in reg if r.get("tallies")}
    pools = []
    for a in sorted(candidates):
        if a in claimed or a in PAYOUT_INFRA:
            continue
        if a in proven:
            pools.append({"addr": a, "nft": proven[a].get("nft")})
            continue
        nft = _eth_call(a, "nft()", "addr")
        if nft and _is_collection(nft):
            pools.append({"addr": a, "nft": nft.lower()})
            continue
        n_assets = len(seen_assets.get(a, ()))
        if n_assets < 3:
            continue
        # Router test LAST: it costs an ABI fetch, so it is only worth paying
        # for a candidate that is otherwise about to be admitted (~20 a run,
        # against ~35 that reach this point).
        if n_assets <= MAX_BASKET_ASSETS or PAYOUT_NAME_RE.search(_contract_name(a) or ""):
            if _is_router(a):
                continue
            pools.append({"addr": a, "nft": None})
    print(f"        {len(pools)} wage pools from {len(candidates)} candidates",
          flush=True)
    if not pools:
        return {"projects": [], "total_distributed_usd": 0.0, "reward_assets": []}


    # Lifetime payouts, measured as Transfers OUT of the pool -- the money that
    # actually left the contract, not an announced figure.
    #
    # INCREMENTAL, against a per-pool block watermark. Re-reading all of history
    # for every pool every run costs ~13 chunked calls per asset per pool; once
    # discovery started finding twelve pools instead of three that is ~1,500
    # calls, which does not fit the refresh's 30-minute budget alongside
    # everything else. Totals are cumulative and past transfers never change, so
    # each run only needs the blocks since the last. Same watermark pattern the
    # NFT mint scanner uses.
    prior = {r.get("pool"): r for r in reg}
    # Seed every pool with whatever was already known, so a pool the budget does
    # not reach this run keeps its tallies instead of being reset to zero.
    for p in pools:
        was = prior.get(p["addr"]) or {}
        p["tallies"] = was.get("tallies") or {}
        p["last_block"] = int(was.get("last_block") or 0)

    # The first run after a discovery change has no watermarks and must read all
    # of history for every pool, which is the one case that can overrun. Measure
    # until the budget is spent and persist what was finished: unmeasured pools
    # keep watermark 0 and are picked up next run, so the work CONVERGES across
    # runs rather than timing out the refresh and never completing at all.
    m_deadline = time.time() + measure_budget_s
    by_pool = defaultdict(list)
    # ⚠️ OLDEST WATERMARK FIRST. Iterating in address order meant the budget
    # always spent itself on the same early addresses and the tail was never
    # reached -- Zaibatsu (0xf225...) and Quotron (0xe04f...) sit at the end of
    # the alphabet and were starved every run.
    for p in sorted(pools, key=lambda q: q.get("last_block") or 0):
        if time.time() > m_deadline:
            print(f"        measurement budget spent; {len(by_pool)} of "
                  f"{len(pools)} pools priced, rest resume next run", flush=True)
            break
        was = prior.get(p["addr"]) or {}
        tallies = {a: dict(v) for a, v in (was.get("tallies") or {}).items()}
        frm = int(was.get("last_block") or 0)
        reward = _eth_call(p["addr"], "rewardToken()", "addr")
        assets = {reward.lower()} if reward and int(reward, 16) else set()
        assets |= _pool_assets(p["addr"]) | seen_assets.get(p["addr"], set())
        assets |= set(tallies)
        assets -= {WETH, USDG, NATIVE}
        topic1 = "0x" + p["addr"][2:].rjust(64, "0")
        # ⚠️ The deadline is checked PER ASSET, not only per pool. Checking it
        # once at the top of a pool bounds nothing: a pool carrying 18 assets
        # reads ~13 chunked getLogs each, so one fat pool can run minutes past
        # the budget after being admitted a second under it. That overshoot is
        # what put the job past CI's 30-minute timeout.
        finished = True
        for asset in assets:
            if time.time() > m_deadline:
                finished = False
                break
            tal = tallies.setdefault(asset, {"raw": 0, "transfers": 0, "recipients": []})
            recips = set(tal.get("recipients") or [])
            raw, n = int(tal.get("raw") or 0), int(tal.get("transfers") or 0)
            # ⚠️ WAS: a raw _rpc whose error was bound and never checked, so
            # `for l in (logs or [])` turned a FAILED request into "no
            # transfers". Two ways that goes wrong here and both are permanent:
            # the 10k-log cap returns a JSON-RPC error that _rpc does not retry,
            # and the watermark below advances past the blocks that were never
            # read. Tallies are cumulative and additive, so an undercount never
            # heals -- the pool just reports low forever.
            #
            # _get_logs_chunked already solved this for the Seaport scan: halve
            # on error rather than trusting the cap, and report what it could
            # not read. `gaps` is the part that matters -- a gap means the
            # watermark must NOT move.
            gaps = []
            logs = _get_logs_chunked(asset, TRANSFER_TOPIC, frm, head,
                                     chunk=3_000_000, quiet=True,
                                     topics=[TRANSFER_TOPIC, topic1], gaps=gaps)
            if gaps:
                print(f"        unreadable range on {p['addr'][:10]}/{asset[:10]}: "
                      f"{len(gaps)} gap(s); pool resumes from its stored "
                      f"watermark", flush=True)
                finished = False
                break
            for l in logs:
                tp = l.get("topics") or []
                if len(tp) != 3:
                    continue
                recips.add(tp[2][-40:])
                n += 1
                try:
                    raw += int(l.get("data") or "0x0", 16)
                except ValueError:
                    pass
            tal.update(raw=raw, transfers=n, recipients=sorted(recips))
            if n:
                by_pool[p["addr"]].append({"asset": asset, "raw": raw,
                                           "transfers": n, "recipients": len(recips)})
        # A pool abandoned mid-way keeps its PREVIOUS tallies and watermark, and
        # its partial work this run is discarded. Committing half a pool would
        # advance nothing safely: the watermark cannot move (the unscanned
        # assets would skip those blocks forever) but leaving it while keeping
        # the partial totals would re-add the same transfers from the old
        # watermark next run and inflate the pool. Whole pools or nothing.
        if not finished:
            by_pool.pop(p["addr"], None)
            print(f"        budget spent inside {p['addr'][:10]}; it resumes "
                  f"from its stored watermark next run", flush=True)
            break
        p["tallies"] = {a: v for a, v in tallies.items() if v.get("transfers")}
        p["last_block"] = head + 1

    # ⚠️ Rows come from the PERSISTED tallies, not from what this run happened to
    # scan. Emitting only freshly-measured pools made the board flicker between
    # runs: a pool the budget did not reach vanished entirely, even though its
    # cumulative total was sitting in the registry. Zaibatsu ($166k) and
    # QuotronReflectionsV2 both disappeared from a published board this way.
    # Tallies are cumulative and transfers are immutable, so a stored total is
    # still true whether or not it was refreshed today.
    for p in pools:
        if by_pool.get(p["addr"]):
            continue                       # measured this run; already current
        for asset, tal in (p.get("tallies") or {}).items():
            if tal.get("transfers"):
                by_pool[p["addr"]].append({
                    "asset": asset, "raw": tal.get("raw") or 0,
                    "transfers": tal.get("transfers") or 0,
                    "recipients": len(tal.get("recipients") or [])})

    # Written AFTER measuring: a registry saved before the scan would advance the
    # watermark past blocks whose transfers were never counted.
    #
    # A pool that has been fully measured and sent NOTHING is not a payer, so it
    # is dropped rather than rescanned every run forever. Two such entries were
    # carrying 10 and 18 assets from an earlier buggy state; a direct scan
    # confirmed zero transfers out for any of them. Dropping is safe because
    # discovery runs every time -- if one ever does pay, it comes straight back.
    keep = [p for p in pools if p.get("tallies") or not p.get("last_block")]
    dropped = len(pools) - len(keep)
    if dropped:
        print(f"        pruned {dropped} measured non-payers from the registry",
              flush=True)
    rows = [{"pool": p["addr"], "nft": p["nft"],
             "assets": sorted(seen_assets.get(p["addr"], [])),
             "last_block": p.get("last_block", 0),
             "tallies": p.get("tallies", {})} for p in keep]

    # ⚠️ THE REGISTRY IS APPEND-MOSTLY. It is rewritten from `pools`, so anything
    # that shrinks `pools` silently discards proven payers -- their tallies AND
    # their watermarks -- and the only way back is a full re-measure from block
    # 0. Seen for real: a caller passing an over-broad `known` set excluded 19 of
    # 20 pools as already-claimed, and the write truncated the registry to a
    # single entry. `if not pools: return` above catches a total wipe; it does
    # nothing about a partial one, which is the likelier accident.
    #
    # So a prior entry survives unless it was dropped ON PURPOSE: either it is
    # known infrastructure, or it was measured this run and proved to pay
    # nothing. Everything else is carried forward untouched. That keeps the
    # deliberate prunes above working while making an accidental exclusion cost
    # nothing.
    measured_empty = {p["addr"] for p in pools
                      if p.get("last_block") and not p.get("tallies")}
    written = {r["pool"] for r in rows}
    carried = [r for r in reg
               if r.get("pool")
               and r["pool"] not in written
               and r["pool"] not in measured_empty
               and r["pool"].lower() not in PAYOUT_INFRA
               and r.get("tallies")]
    if carried:
        print(f"        carried {len(carried)} proven pools not reached this run",
              flush=True)
    WAGE_POOL_REGISTRY.write_text(json.dumps(rows + carried, indent=2))

    price_cache = {}

    def px(addr):
        if addr not in price_cache:
            ds = fetch_dexscreener_token(addr) or {}
            price_cache[addr] = _f(ds.get("price_usd")) or 0.0
            time.sleep(0.2)
        return price_cache[addr]

    out = []
    for p in pools:
        assets, total, holders = [], 0.0, 0
        for row in by_pool.get(p["addr"], []):
            a = (row["asset"] or "").lower()
            if a in (WETH, USDG, NATIVE):
                continue              # funding legs, not the reward
            dec = _eth_call(a, "decimals()") or 18
            amt = float(row["raw"] or 0) / (10 ** dec)
            price = px(a)
            usd = amt * price
            # Keep an asset whose PRICE we could not fetch but whose amount is
            # real. Dropping on `usd < min_usd` deleted whole projects when
            # DexScreener rate-limited -- StonkBrokers vanished between two runs
            # that measured identical on-chain transfers. An unpriced payout is
            # a known quantity of tokens, so it is reported with usd 0 and the
            # total reads as the lower bound it is.
            if amt <= 0 or (price and usd < min_usd):
                continue
            assets.append({"address": a,
                           "symbol": _eth_call(a, "symbol()", "str") or "?",
                           "amount": amt, "usd": usd,
                           "unpriced": not price,
                           "recipients": row.get("recipients")})
            total += usd
            holders = max(holders, row.get("recipients") or 0)
        if not assets:
            continue
        assets.sort(key=lambda x: -x["usd"])
        # Name it after the COLLECTION it pays where we know it -- that is the
        # name a reader recognises -- else the contract's own verified name.
        #
        # Failing both, fall back to the ADDRESS, never a generic word. Three
        # unverified pools rendered as an identical "wage pool" row, which tells
        # a reader nothing and reads like a bug. A truncated address is at least
        # a key they can paste into the explorer, and the row already links
        # there. Prefixed so it is obviously an identifier, not a name.
        name = ((_get(f"{BLOCKSCOUT}/tokens/{p['nft']}") or {}).get("name")
                if p.get("nft") else None) \
            or _contract_name(p["addr"]) \
            or f"Pool {p['addr'][:6]}…{p['addr'][-4:]}"
        out.append({
            "address": p["addr"], "name": name, "kind": "nft-wage-pool",
            "nft": p["nft"],
            "pending_usd": _pending_usd(p["addr"], [a["address"] for a in assets], px),
            "assets": assets,
            "asset_symbols": [a["symbol"] for a in assets],
            "distributed_usd": total,
            "holders": holders,
            "explorer_url": f"https://robinhoodchain.blockscout.com/address/{p['addr']}",
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

    # Per-phase wall clock, printed at the end. This job has a hard 30-minute
    # CI timeout and overran it at 59 minutes; "the run is too slow" is not
    # actionable without knowing WHICH phase spent the time, and the two that
    # looked obvious (the Seaport scan, the wage pools) were not the whole
    # story. Cheap to keep, and it is the only way to notice a phase drifting
    # back over budget before the job starts failing.
    phase_s, _t = {}, [time.time()]

    def done(name):
        phase_s[name] = time.time() - _t[0]
        print(f"        [{name} took {phase_s[name]:.0f}s]", flush=True)
        _t[0] = time.time()

    print("  [1/5] Blockscout stats (DAU, gas fees)...", flush=True)
    chain = fetch_blockscout(days=days)
    done("blockscout")

    print("  [2/5] DefiLlama (TVL, stablecoins, app fees)...", flush=True)
    llama = fetch_defillama()
    done("defillama")

    print("  [3/5] GeckoTerminal (memecoins)...", flush=True)
    memes = fetch_memecoins(min_liquidity=min_liquidity, top_n=top_n)
    print(f"        {len(memes['tokens'])} tokens kept, "
          f"{len(memes['excluded'])} excluded by the ${min_liquidity:,} floor", flush=True)
    done("memecoins")

    nfts = {"collections": [], "skipped": True}
    if not skip_nfts:
        print("  [4/5] Seaport NFT sales (RPC log scan)...", flush=True)
        usdg_dec = _token_decimals(USDG)
        nfts = fetch_nft_collections(hours=nft_hours, top_n=top_n,
                                     eth_price=chain.get("eth_price_usd"),
                                     usdg_decimals=usdg_dec, align=nft_align)
        nfts["usdg_decimals"] = usdg_dec
        done("seaport")
    else:
        print("  [4/5] Seaport scan SKIPPED (--skip-nfts)", flush=True)

    print("  [5/5] Reward distributors (v4-hook holder payouts)...", flush=True)
    rewards = {"projects": [], "skipped": True}
    if not skip_rewards:
        rewards = fetch_reward_distributors(eth_price=chain.get("eth_price_usd"))
        print(f"        {len(rewards['projects'])} token projects, "
              f"${rewards['total_distributed_usd']:,.0f} to ERC-20 holders", flush=True)
        done("erc20-payers")
        boosters = fetch_nft_boosters(dune_query)
        print(f"        {len(boosters.get('projects', []))} NFT boosters, "
              f"${boosters.get('total_distributed_usd', 0):,.0f} to NFT holders", flush=True)
        done("nft-boosters")

        # Wage pools pay NFT holders exactly as boosters do, so they join the
        # same board rather than starting a third list nobody asked for. Pass
        # the addresses already claimed so nothing is counted twice.
        claimed = ([p["address"] for p in boosters.get("projects", [])]
                   + [p.get("address") for p in rewards.get("projects", [])])
        wage = fetch_nft_wage_pools(known=claimed)
        done("wage-pools")
        if wage.get("projects"):
            boosters["projects"] = sorted(
                boosters.get("projects", []) + wage["projects"],
                key=lambda x: -x["distributed_usd"])
            boosters["total_distributed_usd"] = sum(
                p["distributed_usd"] for p in boosters["projects"])
            boosters["reward_assets"] = sorted(
                {s for p in boosters["projects"] for s in p["asset_symbols"]})
            print(f"        {len(wage['projects'])} NFT wage pools, "
                  f"${wage.get('total_distributed_usd', 0):,.0f} to NFT holders",
                  flush=True)

        rewards["boosters"] = boosters
        rewards["wage_pools"] = wage
        rewards["combined_usd"] = (rewards.get("total_distributed_usd", 0)
                                   + boosters.get("total_distributed_usd", 0))
        rewards["all_assets"] = sorted(set(rewards.get("reward_assets", []))
                                       | set(boosters.get("reward_assets", [])))
    else:
        print("        SKIPPED (--skip-rewards)", flush=True)

    # ⚠️ A section that collapses to zero must not overwrite one that worked.
    # The whole-file guard below only covers CORE stats, so when Dune 402'd on
    # CI the ERC-20 and booster boards published as "$0 to holders" -- factually
    # wrong, and worse than stale, since the payouts plainly did happen. Carry
    # the previous block forward and say when it was measured. Same reasoning as
    # refusing to publish a zeroed page: stale is true as of its timestamp, zero
    # is just false.
    if not skip_rewards and not (rewards.get("projects")
                                 or (rewards.get("boosters") or {}).get("projects")):
        prev = {}
        try:
            prev = (json.loads((OUT_DIR / "pulse.json").read_text()) or {}).get("rewards") or {}
        except (OSError, ValueError):
            prev = {}
        if prev.get("projects") or (prev.get("boosters") or {}).get("projects"):
            print("        rewards came back empty -- keeping the last good block",
                  flush=True)
            prev["stale"] = True
            prev.setdefault("measured_at", prev.get("measured_at"))
            rewards = prev
        else:
            rewards["stale"] = False
    elif not skip_rewards:
        rewards["stale"] = False
        rewards["measured_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    print("  [6/6] Launchpad pools (live, all dexes)...", flush=True)
    try:
        pads = fetch_launchpad_pools()
    except Exception as e:                      # never block the pulse on this
        print(f"        launchpad fetch failed: {e}", flush=True)
        pads = []
    done("launchpads")

    print("  phase budget: "
          + ", ".join(f"{k} {v:.0f}s" for k, v in sorted(phase_s.items(),
                                                         key=lambda kv: -kv[1])),
          flush=True)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chain": {"name": CHAIN_NAME, "chain_id": 4663},
        "phase_seconds": phase_s,
        "launchpad_tokens": pads,
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

    # A degraded pull must NEVER overwrite a good one. On 2026-08-12 the
    # nightly job fired on wake before the network was up: every Blockscout
    # call failed DNS, and the run still wrote a pulse.json with null price,
    # block time and DAU -- which then rendered as "0 ms" and "$0" and was
    # republished to the artifact. Silent degradation is worse than a stale
    # page, because a stale page is still true as of its timestamp.
    core = pulse.get("stats") or {}
    missing = [k for k in ("eth_price_usd", "dau_current", "total_transactions")
               if not core.get(k)]
    out = Path(args.out)
    if missing and out.exists():
        print(f"\nREFUSING TO WRITE: core stats missing ({', '.join(missing)}) -- "
              f"upstream probably unreachable. Keeping the previous {out.name}.",
              flush=True)
        return 1
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
    sys.exit(main() or 0)
