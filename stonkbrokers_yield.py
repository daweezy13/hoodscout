#!/usr/bin/env python3
"""
stonkbrokers_yield.py

Measure the REAL yield of StonkBrokers (Clutch Markets, Robinhood Chain) from
on-chain data instead of the project's marketing numbers.

What it measures
----------------
StockBooster pays brokers in discrete, tier-weighted "reward rounds" (not
per-trade drops as the marketing framing implies) -- confirmed both by the
project's own "how it works" page and by StockBooster's verified ABI, which
tracks round state (currentRound, roundCursor, roundState) but has no
per-wallet cumulative-earnings getter. So the honest signal is StockBooster's
own round-lifecycle events:

    DropStarted(round, ethSpent, totalWeightSnapshot)   -- one per round
    DropFinished(round, recipients)                     -- one per round
    DropCancelled(round, cursorAtCancel)                -- one per round

That's ~2-3 events per round vs. thousands of individual ERC-20 Transfer
lines per round (each round fans out to hundreds of wallets x 3 stock
tokens). Pulled via eth_getLogs directly against the chain RPC, the entire
history (hundreds of rounds) comes back in a couple of seconds instead of
hours of paginated REST calls.

Per round you get: ETH spent, distinct recipients paid, and ETH/recipient --
tracked across rounds to see the DECAY in per-broker payout over time.

What you must fill in
---------------------
Only STOCKBOOSTER (or the keeper that signs the drops) is strictly required.
Use discover_booster_from_wallet() below to find it from any one broker
wallet, or read NFT_COLLECTION.tokenWallet(tokenId) to get a wallet directly.

Deps:  pip install requests pandas numpy web3
"""

import time
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd
import numpy as np
from web3 import Web3

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
BLOCKSCOUT = "https://robinhoodchain.blockscout.com/api/v2"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"  # public Robinhood Chain RPC, chainId 4663

TOKEN = "0xe934e36a439c94017b64a3fece66af12099abf50"  # $STONKBROKER ERC-20
BURN_ADDR = "0x000000000000000000000000000000000000dead"

STOCKBOOSTER = "0x038a7f4e4e89448ad74e044337c9ac25c11e726b"  # "StockBooster" contract, confirmed via discover_booster_from_wallet
NFT_COLLECTION = "0x539cdd042c2f3d93ebc5be7dfff0c79f3b4fabf0"  # StonkBrokers ERC-721 (verified, 4444 supply, matches OpenSea)
BOOSTER_DEPLOY_BLOCK = 12514721  # StockBooster creation block (search floor for eth_getLogs)

# Secondary contract also seen dropping into broker wallets — NOT included
# below. Investigate separately if you want its yield contribution too.
OVERTIME_BOOSTER = "0xf9ca5f6d8622c82758914681a12674e2d489259a"  # "OvertimeBooster" contract

# What a broker costs you all-in (buy price in ETH + activation), for APY %.
# Leave as None to skip the % and just report ETH/broker/round.
BROKER_COST_ETH = None

REQUEST_SLEEP = 0.25     # be polite to the public REST API (burns pull only)
MAX_PAGES = 500          # hard stop so a bad REST loop can't run forever

BOOSTER_ABI = [
    {"anonymous": False, "name": "DropStarted", "type": "event", "inputs": [
        {"name": "round", "type": "uint256", "indexed": True},
        {"name": "ethSpent", "type": "uint256", "indexed": False},
        {"name": "totalWeightSnapshot", "type": "uint256", "indexed": False}]},
    {"anonymous": False, "name": "DropFinished", "type": "event", "inputs": [
        {"name": "round", "type": "uint256", "indexed": True},
        {"name": "recipients", "type": "uint256", "indexed": False}]},
    {"anonymous": False, "name": "DropCancelled", "type": "event", "inputs": [
        {"name": "round", "type": "uint256", "indexed": True},
        {"name": "cursorAtCancel", "type": "uint256", "indexed": False}]},
]


def _w3():
    return Web3(Web3.HTTPProvider(RPC_URL))


# --------------------------------------------------------------------------- #
# HTTP layer (Blockscout v2 REST, cursor pagination) — used only for the
# low-volume $STONKBROKER burn pull and one-off booster discovery.
# --------------------------------------------------------------------------- #
_session = requests.Session()
_session.headers.update({"User-Agent": "stonkbrokers-yield/1.0"})


def _get(path, params=None, tries=8):
    url = f"{BLOCKSCOUT}/{path.lstrip('/')}"
    for attempt in range(tries):
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = 1.5 * (attempt + 1)
                print(f"    [429 rate-limited] {path} -> backing off {wait}s (try {attempt + 1}/{tries})", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise
            wait = min(2.0 * (attempt + 1), 20.0)
            print(f"    [retry] {path} -> {e} -> backing off {wait}s (try {attempt + 1}/{tries})", flush=True)
            time.sleep(wait)
    return {}


def paginate(path, params=None, max_pages=MAX_PAGES):
    """Yield every item across Blockscout v2 pages."""
    params = dict(params or {})
    for page_num in range(max_pages):
        data = _get(path, params)
        for item in data.get("items", []):
            yield item
        if (page_num + 1) % 10 == 0:
            print(f"  ...page {page_num + 1}/{max_pages} of {path}", flush=True)
        nxt = data.get("next_page_params")
        if not nxt:
            return
        params = {**(params or {}), **nxt}
        time.sleep(REQUEST_SLEEP)


def _hash(node):
    if isinstance(node, dict):
        return (node.get("hash") or node.get("address") or "").lower()
    return (node or "").lower()


def _ts(item):
    t = item.get("timestamp") or item.get("block_timestamp")
    if t:
        return pd.to_datetime(t, utc=True, errors="coerce")
    return pd.NaT


def _amount(item):
    tot = item.get("total") or {}
    raw = tot.get("value") or item.get("value") or "0"
    dec = tot.get("decimals")
    if dec is None:
        dec = (item.get("token") or {}).get("decimals", 18)
    try:
        return int(raw) / (10 ** int(dec))
    except (ValueError, TypeError):
        return float("nan")


# --------------------------------------------------------------------------- #
# Discovery: find the booster/keeper from a single broker wallet
# --------------------------------------------------------------------------- #
def discover_booster_from_wallet(broker_wallet, top=10):
    """
    Grab any activated broker's wallet address (from NFT_COLLECTION.tokenWallet
    (tokenId), the marketplace, or an owner's tx), pass it here, and this
    tallies who keeps sending it stock tokens. The dominant sender is the
    StockBooster/keeper -> put it in CONFIG.
    """
    senders = Counter()
    for it in paginate(
        f"addresses/{broker_wallet}/token-transfers",
        {"type": "ERC-20"},
        max_pages=20,
    ):
        if _hash(it.get("to")) == broker_wallet.lower():
            senders[_hash(it.get("from"))] += 1
    print(f"Top senders into {broker_wallet}:")
    for addr, n in senders.most_common(top):
        print(f"  {addr}  x{n}")
    return senders.most_common(1)[0][0] if senders else None


# --------------------------------------------------------------------------- #
# Core pull: StockBooster's own round-lifecycle events, via eth_getLogs
# --------------------------------------------------------------------------- #
def pull_rounds(booster=STOCKBOOSTER, from_block=BOOSTER_DEPLOY_BLOCK):
    """Every round StockBooster has ever run: ETH spent + recipients paid,
    straight from DropStarted/DropFinished/DropCancelled events. No transfer
    replay needed -- ~2-3 events per round instead of hundreds."""
    if not booster:
        raise ValueError("Set STOCKBOOSTER (run discover_booster_from_wallet).")
    w3 = _w3()
    addr = Web3.to_checksum_address(booster)
    c = w3.eth.contract(address=addr, abi=BOOSTER_ABI)
    latest = w3.eth.block_number

    print(f"Pulling round events from block {from_block} to {latest} ...", flush=True)
    started = c.events.DropStarted().get_logs(fromBlock=from_block, toBlock=latest)
    finished = c.events.DropFinished().get_logs(fromBlock=from_block, toBlock=latest)
    cancelled = c.events.DropCancelled().get_logs(fromBlock=from_block, toBlock=latest)
    print(f"  {len(started)} started, {len(finished)} finished, {len(cancelled)} cancelled", flush=True)

    by_round = {}
    for ev in started:
        a = ev["args"]
        by_round[a["round"]] = {
            "round": a["round"],
            "start_block": ev["blockNumber"],
            "eth_spent": a["ethSpent"] / 1e18,
            "total_weight_snapshot": a["totalWeightSnapshot"],
        }
    for ev in finished:
        a = ev["args"]
        row = by_round.setdefault(a["round"], {"round": a["round"]})
        row["finish_block"] = ev["blockNumber"]
        row["recipients"] = a["recipients"]
    cancelled_rounds = {ev["args"]["round"] for ev in cancelled}

    rows = [r for rnd, r in by_round.items() if rnd not in cancelled_rounds]
    df = pd.DataFrame(rows)
    # drop the currently-active/incomplete round (started, not yet finished)
    df = df.dropna(subset=["recipients", "eth_spent"]).copy()
    df["recipients"] = df["recipients"].astype(int)

    print(f"  fetching timestamps for {len(df)} rounds ...", flush=True)

    def _block_ts(bn, tries=6):
        for attempt in range(tries):
            try:
                return w3.eth.get_block(int(bn))["timestamp"]
            except Exception:
                if attempt == tries - 1:
                    raise
                time.sleep(min(1.0 * (attempt + 1), 5.0))

    blocks = df["start_block"].tolist()
    with ThreadPoolExecutor(max_workers=4) as ex:
        timestamps = list(ex.map(_block_ts, blocks))
    df["ts"] = pd.to_datetime(timestamps, unit="s", utc=True)

    df["eth_per_recipient"] = df["eth_spent"] / df["recipients"]
    return df.sort_values("round").reset_index(drop=True)


def pull_burns(token=TOKEN):
    """$STONKBROKER burns (activation sink proxy). Volume is exact; the number
    of activations is NOT (tiers vary), so treat this as burn pressure only."""
    rows = []
    for it in paginate(f"tokens/{token}/transfers", {}):
        if _hash(it.get("to")) in (BURN_ADDR, "0x" + "0" * 40):
            rows.append({"ts": _ts(it), "burned": _amount(it)})
    return pd.DataFrame(rows).dropna(subset=["ts"]).sort_values("ts")


# --------------------------------------------------------------------------- #
# Aggregation: the numbers that actually answer "what's the real yield?"
# --------------------------------------------------------------------------- #
def daily_from_rounds(rounds):
    d = rounds.copy()
    d["date"] = d["ts"].dt.floor("D")
    g = d.groupby("date").agg(
        n_rounds=("round", "count"),
        eth_spent=("eth_spent", "sum"),
        recipients=("recipients", "sum"),
        avg_eth_per_recipient=("eth_per_recipient", "mean"),
    ).reset_index()
    g["annualized_eth_per_broker"] = g["avg_eth_per_recipient"] * 365
    if BROKER_COST_ETH:
        g["apy_pct"] = g["annualized_eth_per_broker"] / BROKER_COST_ETH * 100
    return g


def decay_summary(df, col):
    """Fit log(col) vs observation index -> per-observation decay + half-life."""
    d = df.dropna(subset=[col])
    d = d[d[col] > 0].reset_index(drop=True)
    out = {"metric": col, "observations": len(d)}
    if len(d) >= 3:
        x = np.arange(len(d))
        slope, intercept = np.polyfit(x, np.log(d[col].values), 1)
        decay = 1 - np.exp(slope)
        out["decay_pct_per_obs"] = round(decay * 100, 2)
        if slope < 0:
            out["half_life_obs"] = round(np.log(0.5) / slope, 1)
        first = d[col].iloc[: max(1, len(d) // 4)].mean()
        last = d[col].iloc[-max(1, len(d) // 4):].mean()
        out["first_quartile_avg"] = round(first, 8)
        out["last_quartile_avg"] = round(last, 8)
        out["drawdown_pct"] = round((1 - last / first) * 100, 1)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="StonkBrokers real-yield puller")
    ap.add_argument("--discover", metavar="BROKER_WALLET",
                     help="find the booster/keeper from one broker wallet, then exit")
    ap.add_argument("--out", default=".", help="output dir for CSVs")
    args = ap.parse_args()

    if args.discover:
        discover_booster_from_wallet(args.discover)
        return

    rounds = pull_rounds()
    print(f"  {len(rounds)} completed rounds, "
          f"{rounds['eth_spent'].sum():.4f} ETH spent lifetime, "
          f"round {int(rounds['round'].min())}-{int(rounds['round'].max())}")

    daily = daily_from_rounds(rounds)
    round_decay = decay_summary(rounds, "eth_per_recipient")
    daily_decay = decay_summary(daily, "avg_eth_per_recipient")

    rounds.to_csv(f"{args.out}/rounds.csv", index=False)
    daily.to_csv(f"{args.out}/daily_timeseries.csv", index=False)

    print("\n=== ROUNDS (tail) ===")
    with pd.option_context("display.max_columns", None, "display.width", 160):
        print(rounds.tail(10).to_string(index=False))
    print("\n=== DAILY (tail) ===")
    with pd.option_context("display.max_columns", None, "display.width", 160):
        print(daily.tail(10).to_string(index=False))

    print("\n=== DECAY / YIELD SUMMARY (per round) ===")
    for k, v in round_decay.items():
        print(f"  {k}: {v}")
    print("\n=== DECAY / YIELD SUMMARY (per day) ===")
    for k, v in daily_decay.items():
        print(f"  {k}: {v}")

    print("\nSaved: rounds.csv, daily_timeseries.csv")


if __name__ == "__main__":
    main()
