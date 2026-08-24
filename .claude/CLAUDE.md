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
- **The output is the Cloudflare Pages site** (hoodscout.pages.dev), built from `out/site/` and deployed by `refresh.sh` / `refresh.yml`. The Claude Artifact was a look-and-feel surface during build-out and was removed 2026-08-24 — do not reintroduce it.
- The page is fully static with no external network access from the browser: data pulls happen server-side (this Python/script layer), never client-side. "Live" means periodic re-pull + redeploy.
- No extra files not explicitly asked for.

## ⚠️ Verification and burn stats are forgeable here — only markets aren't
Confirmed 2026-08-13 chasing Zaibatsu Wagies. Searching one project name returned **four
contracts with byte-identical bytecode**, all `verified=True` on Blockscout, all reporting a
burn of *exactly* 306,483,516 tokens. The real token was `verified=False`. Vanity addresses
shared a `ba3` suffix to look alike.

**A burn percentage, a holder count and explorer verification can all be copied.** Liquidity and
24h volume were the only fields that separated the real token ($138k liquidity, $43k volume,
20 pools) from four identical fakes ($272–$330, zero volume). Cross-check on a market, never on
a badge — and never on a number the contract reports about itself.

## Reward payouts come in THREE contract families, not two
`rewardToken()` + `totalDividendsDistributed()` (RewardsCoin ERC-20s) and `getStockTokens()` +
`DropFinished` (NFT boosters) both discover by INTERFACE, and both missed a pool distributing
$159,481 — second-largest on the board. Zaibatsu's wage pool answers `rewardToken()` but not
`totalDividendsDistributed()`, so the probe called it, got a valid answer, then discarded it.

Third family: `nft()` + `rewardToken()`, discovered by the PAYOUT (a contract sending one ERC-20
to hundreds of distinct wallets) rather than by interface, since that generalises to templates
nobody has written yet. ⚠️ Do NOT discover it by event topic — its most common event is emitted
by 79 unrelated contracts, almost all false positives.

The tokenised equities are enumerable by naming convention: every one is `<Company> • Robinhood
Token`, so the explorer's own search yields the asset universe without hardcoding addresses.

## Dune can no longer create saved queries
POST `/v1/query` returns **402 Payment Required**; existing query ids still execute fine. Never
repoint an existing id at new SQL — each one is in use by another section. New analysis has to
run on the RPC.

## ⚠️ `nft()` does NOT mean "pays an NFT collection" — LP position managers implement it
Confirmed 2026-08-19. The third family's attribution test admits Uniswap-style infrastructure:
a **CLPool clone answered `nft()`** pointing at its own `NonfungiblePositionManager`, and came
out as the chain's largest holder payout at **$1,498,096 — 4× the real leader**. Its "recipients"
were LPs taking swap output, not holders being paid.

"Is the target a real ERC-721" does not separate them: that position manager has 21,543 supply
and 1,196 holders, *more than any genuine collection on this chain*. What separates them is dex
plumbing — it answers `factory()` and `WETH9()`; not one of the six real collections answers
either. Corroborate the address `nft()` returns; never accept it on the strength of the call
succeeding.

Two more leaked in by a different door, so check both paths: `V3Utils` ($183,304) and
`PonsV2LaunchAndBuy` ($68,990) implement no `nft()` at all and entered on the basket test at 11
and 10 assets, under the ceiling of 12 — QUOTRONS legitimately pays 10, so **the asset ceiling
cannot separate a router from a payer**. Read the verified ABI for SHAPE instead: router-shaped
(`swap|liquidity|launch`) AND NOT payout-shaped (`claim|pending|distribut|accrue`) is
infrastructure. ⚠️ Strip Ownable/AccessControl boilerplate first — `pendingOwner` contains
"pending" and alone rescued PonsV2LaunchAndBuy from the test.

## GeckoTerminal throttles on a rolling quota — pacing does not help, only fewer calls do
MEASURED twice: at 2.2s between calls 8 of 14 were throttled; slowing to 3.0s made it **worse**
(11 of 14). A 31-dex sweep spent 434 of its 650 seconds asleep in backoff. Retrying is also
futile — use `tries=2`, not the default 6, and carry a persisted registry so a throttled run
keeps the previous sweep's rows instead of dropping them.

Do NOT "fix" this by swapping the per-dex sweep for the chain-wide `/pools` endpoint: it returns
146 of the 577 tokens, ranked by volume, which deletes the small launchers (Mint Club, Hoodit)
from the index entirely.

## Any budgeted loop here needs a rotation, not just a deadline
Three separate starvation bugs, same root cause: a wall-clock budget over a FIXED iteration order
never reaches the tail. Measurement now sorts oldest-watermark-first; discovery offsets its start
by day-of-year; the launchpad sweep visits least-recently-swept dexes first. A deadline alone
converts "slow" into "permanently blind to the same items".

Budgets are sized against a measured per-phase breakdown (`phase_seconds` in pulse.json).
Everything except wage pools and launchpads totals ~220s; those two are the whole timeout story.

## CI must commit the registries, or convergence never happens
`refresh.yml` runs on a fresh checkout. `out/wage_pools.json` and `out/launchpad_tokens.json` are
convergence state — watermarks and carry-over rows — so leaving them out of the commit step
silently resets them every night and the budgeted loops restart from zero forever.

## ⚠️ The RPC silently IGNORES null placeholders in `eth_getLogs` topics
Confirmed 2026-08-23. Filtering `topics: [TOPIC0, null, null, tokenIdTopic]` to find one ERC-721
token's transfers returned **0 logs** for a token that demonstrably exists and had transfers.
Measured on the same block window against the same address: no filter → 4 logs, null-placeholder
filter → **0 logs**.

It does not error. It returns an empty list, which reads exactly like "this never happened."

Two-element filters (`[TOPIC0, senderTopic]`) are fine — that is what the payout scans use. Only
positional nulls break. To filter on a LATER indexed arg, fetch on topic0 and filter client-side,
or use Blockscout's instance endpoint for a single token:
`/api/v2/tokens/<collection>/instances/<id>/transfers` (2 rows, instant — the low-volume lookup
Blockscout is good at).

## QUOTRONS pays StonkBrokers holders, and its ids are StonkBrokers ids
`QuotronReflectionsV2.brokers()` = `0x539cdd042c2f3d93ebc5be7dfff0c79f3b4fabf0` — the StonkBrokers
ERC-721. `quotron()` returns an **ERC-20** (1,867 supply), NOT the payee collection. So a "quotron
id" in that contract is a StonkBrokers token id, and `attrCount()` = 4444 matches that collection.

⚠️ Rewards are NOT pro-rata. `tierWeights` = **[100, 150, 250, 500]** — a 5x spread — and three ids
sit outside the weighted pot entirely: `BASKET_BPS` 1250 (12.5%) splits across ids 4441–4443, and
`GOLD_BPS` 500 (5%) goes to id 4444 alone. `terminals(id)` reads all-zero for basket ids because
they accrue via `basketAcc`/`basketClaimed`, not the weighted path — that is expected, not a bug.
Any "share of rewards" claim here is meaningless without a token id and tier.

Measure per token, never by apportioning a total: `basketClaimed(id, stock)` over the ten
`floorStocks(0..9)` is the exact received amount. The basket pays equal DOLLAR value per stock, not
equal token counts (id 4443: 22.56 GME vs 0.58 SPY, all ~$447) — that even spread is itself a
corroboration signal.

⚠️ `LegacyCredited` has **zero events chain-wide** — the v1→v2 legacy-credit path was never used for
any id. v1 payouts are not observable from V2; the $113,849.84 stays a published figure.

⚠️ `basketClaimed` is keyed on the TOKEN, not the holder. Use the `Claimed(id indexed, to indexed,
stock, amount)` event to attribute to a wallet. And an NFT can be owned by a contract —
StonkBrokers #4443 is held by `StonkNFTAMMVault`, so its owner and its claim recipient are
different addresses.

## ⚠️ NEVER discard the error from `_rpc("eth_getLogs", ...)`
`logs, e = _rpc(...)` then `for l in (logs or []):` turns a FAILED request into "no events found".
It shipped in the wage-pool measurement loop and it is the worst version of this bug, because:

* `_rpc` returns a JSON-RPC error **without retrying** (the 10k-log cap takes that path), and
* the loop then advances `last_block`, so the unread blocks are never revisited, and
* tallies are cumulative and additive, so the undercount **never heals** — the pool reports low
  forever.

Caught it after the same pattern in an ad-hoc script reported 80 claim events where there were
**340**, giving a confidently wrong answer about who received a token's rewards.

Use `_get_logs_chunked(..., topics=[...], quiet=True, gaps=gaps)` — it halves on error instead of
trusting the cap, and appends anything it still cannot read to `gaps`. **If `gaps` is non-empty the
watermark must not move.** Reconcile against a cumulative on-chain accessor where one exists
(`basketClaimed` vs summed `Claimed` events) — that is what exposed the gap.
