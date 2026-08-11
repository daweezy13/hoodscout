# Claude Instructions — robinhood-chain-pulse

## Project
Live ecosystem dashboard for Robinhood Chain (Clutch Markets) — a "pulse check" on the chain as a whole. Covers top memecoins, top NFT collections, TVL, stablecoin supply, daily active users, daily fees. Output is a published web dashboard.

Pivoted 2026-08-04 from a narrower project (`stonkbrokers-yield`) analyzing one NFT collection's yield decay. `stonkbrokers_yield.py` is retained as prior work / a possible input to the "top NFTs" section, not the current focus.

## Chain infra (confirmed working)
- **RPC:** `https://rpc.mainnet.chain.robinhood.com` (public, chainId 4663) — reliable, tolerates very large `eth_getLogs` block ranges (10M+ blocks in one call with no chunking needed observed so far). Prefer this over Blockscout's own eth-rpc proxy, which rate-limits aggressively (429s under light use).
- **Blockscout v2 REST:** `https://robinhoodchain.blockscout.com/api/v2` — fine for low-volume lookups (search, single-address/token queries), chokes badly on high-volume pagination (50 items/page, hits practical limits fast under real trading volume).
- **Event-log reads beat REST pagination for any historical/volume data.** Read verified contracts' ABIs for purpose-built lifecycle events before assuming you need to replay raw Transfer logs — this chain's contracts (see StockBooster) often expose exactly the aggregate signal needed in 1-2 events per unit of activity instead of hundreds of transfer rows.

## ⚠️ Scam/copycat contract pollution on this chain
Robinhood Chain has heavy squatting on popular project names — 40+ results for a single project name search on Blockscout, including vanity addresses crafted to share suffixes with real contract addresses. **Never trust a Blockscout/explorer name-search hit alone.** Cross-check supply/holders/verification status against an independent source (OpenSea, project docs, DefiLlama) before treating any address as real. This matters more here than in most projects since "top memecoins" scope means encountering many unfamiliar/unverified contracts by design.

## Known addresses (StonkBrokers, from prior work — not the current focus but confirmed real)
- $STONKBROKER ERC-20: `0xe934e36A439C94017B64a3FecE66AF12099aBF50`
- StonkBrokers ERC-721 (4,444 supply): `0x539CdD042c2f3d93EbC5BE7DfFf0c79F3B4fAbF0`
- StockBooster: `0x038a7f4e4e89448ad74e044337c9ac25c11e726b`

## Conventions
- Read-only — no wallet signing, no transactions sent, pure on-chain/API data analysis.
- No dry-run needed currently for the same reason. If write/transaction logic is ever added, dry-run mode becomes required per global convention.
- Reusable, parameterized functions over one-off scripts.
- Artifacts (the likely dashboard output) run under a strict CSP with no external network access from the browser — data pulls happen server-side (this Python/script layer), not client-side; "live" means periodic re-pull + republish unless a specific live-data capability is deliberately adopted.
- No extra files not explicitly asked for.
