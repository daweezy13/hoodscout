#!/usr/bin/env python3
"""
build_dashboard.py

Render out/pulse.json into a self-contained dashboard page ("HoodScout").

Kept separate from chain_pulse.py so the slow pull and the fast render are
independent: iterate on layout without re-scanning ~96k Seaport logs, and
re-publish without touching the data layer.

Chart colours: single-series plots use the brand lime. The one categorical
plot (stablecoin composition, 2 series) uses #7FA300 / #0F86C4, which passes
the palette validator on both the dark and light surfaces -- lime itself sits
at OKLCH L 0.93, far outside the 0.48-0.67 categorical band, so it cannot
carry category identity even though it carries the brand.

Usage:  python3 chain_pulse.py && python3 build_dashboard.py
"""

import json
import argparse
import datetime as dt
from pathlib import Path
from html import escape

OUT_DIR = Path(__file__).parent / "out"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def usd(v, digits=0):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    if 0 < a < 0.01:
        return "<$0.01"             # never render a real sale as "$0.00"
    if a < 1:
        return f"${v:.2f}"          # a $0.40 sale must not collapse to "$0"
    return f"${v:,.{digits}f}"


def usd_exact(v):
    return "—" if v is None else f"${v:,.0f}"


def num(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"


def price(v):
    if v is None:
        return "—"
    if v == 0:
        return "$0"
    if abs(v) < 0.01:
        return f"${v:.3g}"
    return f"${v:,.4f}".rstrip("0").rstrip(".")


def pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def trend_class(v):
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def short_addr(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "")


def eth_amt(v):
    """Trim float noise off OpenSea floors.

    The API returns values like 0.07537627999999941 and 0.026496703296703297;
    printed raw they read as spurious precision on a number that is really
    ~0.075 ETH. Four significant digits, no trailing zeros.
    """
    if v is None:
        return "—"
    if v == 0:
        return "0"
    if v >= 1:
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return f"{v:.4g}"


def ts_to_date(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date().isoformat()


def clean_series(points):
    """Drop leading zero-padding and any trailing partial (today) bucket.

    Blockscout answers a 400-day request by padding back to 2025 with zeros --
    the chain did not exist then. Left in, a 1Y chart is ~90% flat line and
    the range buttons imply history that isn't there. Series are also cut at
    yesterday: a bucket dated today is still filling and would render as a
    cliff at the right edge of every plot.
    """
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    pts = [p for p in points if p[0] < today]
    i = 0
    while i < len(pts) and not pts[i][1]:
        i += 1
    return pts[i:]


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
CORROB = {
    "corroborated": ("ok", "DexScreener and GeckoTerminal agree on this token's volume and liquidity"),
    "partial": ("part", "The two indexers differ by 3–20x on this token"),
    "disputed": ("bad", "The two indexers disagree by more than 20x, or the second sees no real market"),
    "single-source": ("bad", "DexScreener has no pairs for this token — GeckoTerminal only"),
    "unchecked": ("part", "Below the cross-check depth"),
}

SAFELIST = {
    "verified": ("ok", "verified", "OpenSea: verified collection"),
    "approved": ("ok", "approved", "OpenSea: approved collection"),
    "requested": ("part", "pending", "OpenSea: verification requested, not granted"),
    "not_requested": ("bad", "unverified", "OpenSea: never requested verification. On "
                                           "this chain that is the norm, not proof of a "
                                           "copycat — but it is not an endorsement either."),
}


def link(url, text, cls="ext"):
    if not url:
        return escape(text)
    return (f'<a class="{cls}" href="{escape(url)}" target="_blank" '
            f'rel="noopener noreferrer">{escape(text)}</a>')


ETHOS_LEVEL = {
    "untrusted": "bad", "questionable": "bad", "neutral": "part",
    "known": "ok", "established": "ok", "reputable": "ok",
    "exemplary": "ok", "distinguished": "ok", "revered": "ok",
}


GRADE_CLS = {"A": "ok", "B": "ok", "C": "part", "D": "part", "F": "bad"}


def trust_chip(ti):
    """Removed with the Ethos score it was 50% derived from."""
    return ""


def ethos_chip(e):
    """A LINK to the Ethos profile — deliberately no score.

    The scores proved too inconsistent to headline: most profiles sit at the
    bare 1200 default with no reviews or vouches, and the handle is
    self-declared anyway (SPCX on this chain links @elonmusk and would have
    inherited a "reputable" 1945). Rendering a number implied a verdict the
    data cannot support, so the page points at the profile and lets the reader
    judge.
    """
    if not e or not e.get("handle"):
        return ""
    return (f'<a class="ethos-link" href="{escape(e.get("profile_url") or "#")}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'title="Ethos profile for @{escape(e["handle"])} — the X account this '
            f'token declares. Self-declared, so not proof of anything.">ethos &#8599;</a>')


def meme_rows(tokens):
    out = []
    for i, t in enumerate(tokens, 1):
        cls, tip = CORROB.get(t.get("corroboration", "unchecked"), CORROB["unchecked"])
        dot = f'<span class="corrob {cls}" title="{escape(tip)}"></span>'
        badge = ('<span class="badge flag" title="24h volume is more than 75x pool '
                 'liquidity — thin book">thin</span>' if t.get("flagged") else "")
        chg = t.get("price_change_24h")
        fdv = t.get("fdv")
        mc = t.get("market_cap")
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape((t.get('symbol') or '?').lower())}">
              <span class="sym-name">{dot}{link(t.get('url'), t.get('symbol') or '?', 'ext strong')}{badge}</span>
              <span class="sym-sub">{escape(num(t.get('holders')))} holders
                {ethos_chip(t.get('ethos'))}</span>
            </td>
            <td class="n" data-v="{t.get('price_usd') or 0}">{escape(price(t.get('price_usd')))}
              <span class="alt {trend_class(chg)}">{escape(pct(chg))}</span></td>
            <td class="n" data-v="{mc or 0}">{escape(usd(mc))}
              <span class="alt" title="Fully diluted valuation">fdv {escape(usd(fdv))}</span></td>
            <td class="n strong" data-v="{t.get('volume_ranked') or 0}">{escape(usd(t.get('volume_ranked')))}
              <span class="alt">liq {escape(usd(t.get('liquidity_effective')))}</span></td>
          </tr>""")
    return "\n".join(out)


def nft_rows(cols):
    out = []
    for i, c in enumerate(cols, 1):
        name = c.get("name") or "—"
        badge = ('<span class="badge warn" title="Contract not verified on Blockscout">'
                 'unverified</span>' if name == "(unverified contract)" else "")
        sl = c.get("safelist_status")
        if sl:
            scls, slabel, stip = SAFELIST.get(
                sl, ("part", sl.replace("_", " "), f"OpenSea: {sl}"))
            kind = "ok" if scls == "ok" else "flag"
            badge += f'<span class="badge {kind}" title="{escape(stip)}">{escape(slabel)}</span>'
        supply = c.get("total_supply")
        # Prefer OpenSea's true listing floor when a key made it available;
        # otherwise fall back to the lowest paid sale, clearly relabelled.
        if c.get("floor_price"):
            floor_val = f"{eth_amt(c['floor_price'])} {c.get('floor_currency') or ''}".strip()
            fu = c.get("floor_price_usd")
            floor_lbl = f"floor · {usd(fu)}" if fu else "floor"
            floor_tip = "OpenSea floor — lowest open listing"
        else:
            floor_val = usd(c.get("min_sale_usd"))
            floor_lbl = "low sale"
            floor_tip = "Lowest paid sale in the window — not a listing floor"

        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape(name.lower())}">
              <span class="sym-name">{link(c.get('opensea_url'), name, 'ext strong')}{badge}</span>
              <span class="sym-sub">{escape(num(supply))} items ·
                {escape(num(c.get('holders')))} holders ·
                {link(c.get('explorer_url'), short_addr(c.get('address')), 'ext dim')}</span>
            </td>
            <td class="n" data-v="{c.get('floor_price_usd') or c.get('min_sale_usd') or 0}" title="{escape(floor_tip)}">
              {escape(floor_val)}
              <span class="alt">{escape(floor_lbl)}</span></td>
            <td class="n" data-v="{c.get('avg_price_usd') or 0}">{escape(usd(c.get('avg_price_usd')))}
              <span class="alt">{escape(num(c.get('buyers')))} buyers</span></td>
            <td class="n strong" data-v="{c.get('volume_usd') or 0}">{escape(usd(c.get('volume_usd')))}
              <span class="alt">{escape(num(c.get('sales')))} sales</span></td>
          </tr>""")
    return "\n".join(out)





def reward_rows(projects):
    out = []
    for i, p in enumerate(projects, 1):
        # A few of these contracts have no readable symbol(). Showing a bare
        # "?" reads like a render failure; the short address is at least a
        # thing the reader can click through and identify.
        if not p.get("symbol") or p["symbol"] == "?":
            p = dict(p, symbol=short_addr(p.get("address")) or "unnamed")
        amt = p.get("distributed") or 0
        amt_s = f"{amt:,.2f}" if amt >= 0.01 else f"{amt:.4g}"
        tracker = ('<span class="badge warn" title="Payouts run through a separate '
                   'DIVIDEND_TRACKER contract owned by this token">via tracker</span>'
                   if p.get("tracker_address") else "")
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape((p.get('symbol') or '?').lower())}">
              <span class="sym-name">{link(p.get('explorer_url'), p.get('symbol') or '?', 'ext strong')}{tracker}</span>
              <span class="sym-sub">{escape((p.get('name') or '')[:40])}</span>
            </td>
            <td class="n" data-v="{escape((p.get('reward_symbol') or '').lower())}">
              <span class="pays">{escape(p.get('reward_symbol') or '?')}</span></td>
            <td class="n strong" data-v="{p.get('distributed_usd') or 0}">{escape(usd(p.get('distributed_usd')))}
              <span class="alt">{escape(amt_s)} {escape(p.get('reward_symbol') or '')}</span></td>
          </tr>""")
    return "\n".join(out)



def booster_rows(projects, today=None):
    """NFT boosters pay a BASKET, not one asset, so the asset column is a set
    of chips with per-asset amount and value in the tooltip."""
    out = []
    for i, p in enumerate(projects, 1):
        assets = p.get("assets") or []
        # In the narrow two-up column 11 chips overflowed into the Value cell.
        # Show the three largest by value and roll the rest into a count whose
        # tooltip still names them, so nothing is silently dropped.
        shown, rest = assets[:3], assets[3:]
        chips = "".join(
            f'<span class="pays" title="{escape(a["symbol"])}: {a["amount"]:,.2f} '
            f'= {escape(usd(a["usd"]))}">{escape(a["symbol"])}</span>'
            for a in shown)
        if rest:
            more = ", ".join(f'{a["symbol"]} {usd(a["usd"])}' for a in rest)
            chips += (f'<span class="pays more-chip" title="{escape(more)}">'
                      f'+{len(rest)}</span>')
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape((p.get('name') or '').lower())}">
              <span class="sym-name">{link(p.get('explorer_url'), p.get('name') or '?', 'ext strong')}</span>
              <span class="sym-sub">{escape(num(p.get('holders')))} holders ·
                {escape(num(p.get('drops')))} drops · last {escape(p.get('last') or '')}</span>
            </td>
            <td class="n basket" data-v="{len(p.get('assets') or [])}">{chips}</td>
            <td class="n strong" data-v="{p.get('distributed_usd') or 0}">{escape(usd(p.get('distributed_usd')))}
              <span class="alt">{len(p.get('assets') or [])} assets</span></td>
          </tr>""")
    return "\n".join(out)


def stable_rows(rows):
    out = []
    for r in rows:
        out.append(f"""        <li class="stable">
          <span class="sw" data-sym="{escape(r.get('symbol') or '')}"></span>
          <span class="st-sym">{escape(r.get('symbol') or '?')}</span>
          <span class="st-name">{escape(r.get('name') or '')}</span>
          <span class="st-mech">{escape(r.get('peg_mechanism') or '')}</span>
          <span class="st-val">{escape(usd(r.get('circulating')))}</span>
          <span class="st-share">{r.get('share_pct') or 0:.1f}%</span>
        </li>""")
    return "\n".join(out)


def tile(label, value, sub, change=None):
    chg = f'<span class="chg {trend_class(change)}">{escape(pct(change))}</span>' if change is not None else ""
    return f"""      <article class="tile">
        <div class="tile-head"><span class="tile-label">{escape(label)}</span>{chg}</div>
        <div class="tile-value">{escape(value)}</div>
        <div class="tile-sub">{escape(sub)}</div>
      </article>"""


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(p, logo=None):
    s, l = p["stats"], p["defillama"]
    m, n = p["memecoins"], p["nfts"]

    gen = dt.datetime.fromisoformat(p["generated_at"])
    gen_str = gen.strftime("%d %b %Y, %H:%M UTC")

    stables = l.get("stables_breakdown") or []
    act = l.get("activity_breakdown") or {"series": [], "categories": []}

    # agg says how a headline should aggregate over the selected range, and it
    # is not cosmetic. A stock (TVL, supply) can only be read as its latest
    # value -- summing 30 daily balances is meaningless. A flow (fees) is
    # genuinely additive. A distinct count (active accounts) is neither: the
    # same wallet active on 30 days would be counted 30 times, so it reports a
    # daily average instead.
    charts = {
        "tvl": {
            "title": "Total value locked", "unit": "usd", "agg": "last", "series": [
                {"name": "TVL", "color": "accent",
                 "points": clean_series([[ts_to_date(x["date"]), x["tvl"]] for x in l.get("tvl_series", [])])}],
        },
        "stables": {
            "title": "Stablecoin supply", "unit": "usd", "agg": "last", "stacked": True, "series": [
                {"name": r["symbol"], "color": f"s{i + 1}",
                 "points": []} for i, r in enumerate(stables[:2])],
        },
        "dau": {
            "title": "Daily active users", "unit": "num", "agg": "avg", "series": [
                {"name": "Active accounts", "color": "accent",
                 "points": clean_series([[x["date"], x["value"]] for x in s.get("dau_series", [])])}],
        },
        "activity": {
            "title": "Activity by category", "unit": "usd", "agg": "sum", "stacked": True,
            "series": [
                {"name": c, "color": f"s{i + 1}",
                 "points": clean_series([[r["date"], r.get(c, 0)] for r in act["series"]])}
                for i, c in enumerate(act.get("categories", []))],
        },
    }

    # Split the stablecoin total across assets by today's share. DefiLlama's
    # per-chain history is an aggregate; only the current split is published,
    # so the composition series is explicitly an approximation and is labelled
    # as one on the page rather than presented as measured history.
    ser = l.get("stables_series", [])
    for i, r in enumerate(stables[:2]):
        share = (r.get("share_pct") or 0) / 100.0
        charts["stables"]["series"][i]["points"] = clean_series(
            [[ts_to_date(x["date"]), (x["total"] or 0) * share] for x in ser])

    first_dates = [c["series"][0]["points"][0][0] for c in charts.values() if c["series"] and c["series"][0]["points"]]
    oldest = min(first_dates) if first_dates else None
    span_days = (dt.date.today() - dt.date.fromisoformat(oldest)).days if oldest else 0

    tvl_pts = (charts.get("tvl") or {}).get("series") or []
    tvl_pts = tvl_pts[0]["points"] if tvl_pts else []
    # tvl_current is a live reading that includes TODAY -- a partial UTC day.
    # Pairing it with tvl_date (which clean_series() resolves to the last
    # COMPLETE day) put $481.03M/"2026-08-10" in the hero while the chart 300px
    # below correctly showed $471.36M for that same date. Two answers to one
    # question destroys the page's whole premise, so the hero now headlines the
    # same complete day the chart does.
    tvl_date = tvl_pts[-1][0] if tvl_pts else "latest"
    tvl_headline = tvl_pts[-1][1] if tvl_pts else l.get("tvl_current")

    stb_pts = (charts.get("stables") or {}).get("series") or []
    stb_headline = None
    if stb_pts and stb_pts[0].get("points"):
        # stacked series: sum the assets at the last complete day
        last_day = stb_pts[0]["points"][-1][0]
        stb_headline = sum((s["points"][-1][1] or 0) for s in stb_pts
                           if s.get("points") and s["points"][-1][0] == last_day)
    if not stb_headline:
        stb_headline = l.get("stables_current")

    # The three numbers that answer "is this chain alive right now", sat beside
    # the TVL hero. Stablecoin supply is a stock, the other two are daily flows.
    hero_tiles = "\n".join([
        tile("Stablecoin supply", usd(stb_headline),
             f"{len(l.get('stables_breakdown') or [])} assets on-chain",
             l.get("stables_change_7d_pct")),
        tile("Daily active users", num(s.get("dau_current")),
             f"distinct senders · {s.get('dau_date') or ''}",
             s.get("dau_change_1d_pct")),
        tile("Daily fees — apps", usd(l.get("app_fees_24h")),
             "earned by protocols, not the chain",
             l.get("app_fees_change_1d_pct")),
    ])

    tiles = "\n".join([
        tile("Daily fees — gas", usd(s.get("gas_fees_usd_current")),
             f"paid to the chain, {s.get('gas_fees_eth_current') or 0:.1f} ETH · {s.get('gas_fees_date') or ''}",
             s.get("gas_fees_change_1d_pct")),
        tile("Transactions", num(s.get("txns_current")),
             f"{num(s.get('total_transactions'))} lifetime"),
        tile("Addresses", num(s.get("total_addresses")), "cumulative on-chain"),
        tile("Block time", f"{s.get('average_block_time_ms') or 0:.0f} ms",
             f"ETH ${s.get('eth_price_usd') or 0:,.0f}"),
    ])

    cc = m.get("corroboration_counts", {})
    n_ok, n_shown = cc.get("corroborated", 0), len(m.get("tokens", []))
    floor = m.get("min_liquidity_usd", 0)
    n_excl = len(m.get("excluded", []))
    nft_window = n.get("window_hours", 24)
    has_opensea = any(c.get("safelist_status") for c in n.get("collections", []))
    floor_hdr = "Floor" if any(c.get("floor_price") for c in n.get("collections", [])) else "Low sale"
    # The window used to be a rolling 24h from the current block while every
    # other metric on the page came from a UTC-keyed daily series -- close
    # enough to look comparable, but overlapping two UTC days and sliding
    # every run. It is now pinned to the same last-complete-UTC-day, and says so.
    if n.get("align") == "utc-day":
        # Don't just assert the two agree -- check. The Seaport scan reads the
        # chain directly and is always current, while Blockscout's daily series
        # lag ingestion, so shortly after UTC midnight the vitals can still be
        # a day behind. Claiming alignment when it isn't true is worse than the
        # misalignment itself.
        nft_day = str(n.get("window_label") or "")
        vitals_day = str(s.get("dau_date") or "")
        if nft_day and vitals_day and nft_day != vitals_day:
            nft_window_note = (
                f"Window is {escape(nft_day)} UTC. The chain vitals above headline "
                f"{escape(vitals_day)} — Seaport fills are read straight off the chain, "
                "so they are current, while the stats provider is still ingesting "
                "the newer day.")
        else:
            nft_window_note = (f"Window is {escape(nft_day)} UTC, "
                               "the same complete day the chain vitals above use.")
    else:
        nft_window_note = ("Window is a rolling 24h from the latest block, so it is "
                           "not aligned to the UTC days the chain vitals use.")

    # Audit strip. Every headline used to have exactly one source and no second
    # opinion; verify_dune.py recomputes three of them from raw chain data with
    # an explicit definition. Rendered only when a report exists.
    v = p.get("_verify")
    audit_line = ""
    if v:
        vchecks = v.get("checks") or []
        # The Seaport check is pinned to one exact block range. If the verify
        # step failed on this run (Dune outage, exhausted credits) we are
        # holding a report from an EARLIER pulse, and that range no longer
        # matches the scan on the page — it renders as a fake ~77% divergence.
        # The daily series checks are keyed by date and stay valid, so only
        # the range check is dropped when the snapshots do not correspond.
        fresh = v.get("pulse_generated_at") == p.get("generated_at")
        sc = v.get("seaport_range_check") or {}
        if sc.get("verdict") and fresh:
            vchecks = vchecks + [sc]
        agreed = sum(1 for c in vchecks if c.get("verdict") == "agree")
        worst = max((c.get("pct_diff") or 0) for c in vchecks) if vchecks else 0
        vday = (v.get("generated_at") or "")[:10]
        stale_note = "" if fresh else " against an earlier snapshot"
        cls = "ok" if agreed == len(vchecks) else "part"
        audit_line = (
            f"Independently cross-checked against Dune: {agreed} of {len(vchecks)} "
            f"checks agree, worst divergence {worst:.1f}%. Active users is "
            f"<code>count(distinct sender)</code> per UTC day and matches Blockscout "
            f"exactly, which pins down that provider's otherwise undocumented "
            f"definition. Checked {escape(vday)}.")

    rw = p.get("rewards") or {"projects": []}
    bst = rw.get("boosters") or {"projects": []}
    bst_note = (f"{len(bst.get('projects', []))} live, paying "
                f"{', '.join(bst.get('reward_assets', [])[:8])}."
                if bst.get("projects") else "None detected in this run.")
    rw_assets = rw.get("reward_assets") or []
    rw_assets_note = (
        f"{len(rw.get('projects', []))} projects paying in "
        f"{', '.join(rw_assets)} — including tokenised equities."
        if rw_assets else "None detected in this run.")

    n_verified = sum(1 for c in n.get("collections", [])
                     if c.get("safelist_status") in ("verified", "approved"))
    os_note = (f"{n_verified} of {len(n.get('collections', []))} carry OpenSea "
               "verification; the rest never requested it."
               if has_opensea else
               "OpenSea verification badges appear here once an API key is configured.")

    payload = json.dumps({"charts": charts, "spanDays": span_days},
                         separators=(",", ":")).replace("</", "<\\/")

    return f"""<title>HoodScout — Robinhood Chain</title>
<style>
{font_faces()}
:root {{
  --ground:#F6F5EF; --panel:#FFFFFF; --panel-2:#EFEEE4; --line:#DEDCCF;
  --rule:#0F100C; --shadow:#0F100C; --rule-w:2px;
  --ink:#0F100C; --ink-2:#3A3E31; --muted:#6B7060;
  --accent:#D2F53C; --accent-soft:rgba(210,245,60,.34); --accent-ink:#46600A;
  --on-accent:#0F100C; --live:#3E9E4E;
  --s1:#7FA300; --s2:#0F86C4; --s3:#B5642A; --s4:#8A6BC8;
  --flag:#8A5B0C; --flag-soft:rgba(138,91,12,.12);
  --reject:#A8253A; --reject-soft:rgba(168,37,58,.09);
  --up:#3D6B00; --down:#A8253A;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,"Cascadia Mono",Consolas,monospace;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  /* Grotesk display face. The CSP blocks font CDNs and inlining a Black
     weight as a data URI would add ~40KB to an already-large page, so this
     leans on faces that ship with the OS instead. */
  --display:"Silkscreen","Courier New",monospace;      /* pixel face, inlined above */
  --grotesk:"Helvetica Neue","Arial Black",Helvetica,Arial,sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#0C0D08; --panel:#131509; --panel-2:#1B1E10; --line:#282C1A;
    --rule:#E4EDCE; --shadow:#000000; --rule-w:2px;
    --ink:#ECF2DE; --ink-2:#B6C0A2; --muted:#7C866B;
    --accent:#D2F53C; --accent-soft:rgba(210,245,60,.15); --accent-ink:#D2F53C;
    --on-accent:#0F100C; --live:#6ED97E;
    --s1:#7FA300; --s2:#0F86C4; --s3:#B5642A; --s4:#8A6BC8;
    --flag:#E8B33D; --flag-soft:rgba(232,179,61,.13);
    --reject:#E4566E; --reject-soft:rgba(228,86,110,.12);
    --up:#D2F53C; --down:#E4566E;
  }}
}}
:root[data-theme="light"] {{
  --ground:#F6F5EF; --panel:#FFFFFF; --panel-2:#EFEEE4; --line:#DEDCCF;
  --rule:#0F100C; --shadow:#0F100C; --rule-w:2px;
  --ink:#0F100C; --ink-2:#3A3E31; --muted:#6B7060;
  --accent:#D2F53C; --accent-soft:rgba(210,245,60,.34); --accent-ink:#46600A;
  --on-accent:#0F100C; --live:#3E9E4E;
  --s1:#7FA300; --s2:#0F86C4; --s3:#B5642A; --s4:#8A6BC8;
  --flag:#8A5B0C; --flag-soft:rgba(138,91,12,.12);
  --reject:#A8253A; --reject-soft:rgba(168,37,58,.09);
  --up:#3D6B00; --down:#A8253A;
}}
:root[data-theme="dark"] {{
  --ground:#0C0D08; --panel:#131509; --panel-2:#1B1E10; --line:#282C1A;
  --rule:#E4EDCE; --shadow:#000000; --rule-w:2px;
  --ink:#ECF2DE; --ink-2:#B6C0A2; --muted:#7C866B;
  --accent:#D2F53C; --accent-soft:rgba(210,245,60,.15); --accent-ink:#D2F53C;
  --on-accent:#0F100C; --live:#6ED97E;
  --s1:#7FA300; --s2:#0F86C4; --s3:#B5642A; --s4:#8A6BC8;
  --flag:#E8B33D; --flag-soft:rgba(232,179,61,.13);
  --reject:#E4566E; --reject-soft:rgba(228,86,110,.12);
  --up:#D2F53C; --down:#E4566E;
}}

* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); line-height:1.55; -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:1240px; margin:0 auto; padding:26px 24px 48px; }}
a.ext {{ color:inherit; text-decoration:none;
  border-bottom:1px solid color-mix(in srgb, var(--accent-ink) 45%, transparent); }}
a.ext:hover {{ color:var(--accent-ink); border-bottom-color:var(--accent); }}
a.ext:focus-visible, button:focus-visible {{
  outline:2px solid var(--accent); outline-offset:2px; border-radius:2px;
}}
a.ext.dim {{ color:var(--muted); border-bottom:none; }}
a.ext.dim:hover {{ color:var(--accent-ink); }}

.tape {{
  display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 20px;
  font-family:var(--mono); font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--muted);
  border-bottom:var(--rule-w) solid var(--rule); padding-bottom:12px; margin-bottom:30px;
}}
.tape .live {{ color:var(--ink); font-weight:600; display:inline-flex; align-items:center; gap:7px; }}
.tape .dot {{
  width:9px; height:9px; border-radius:50%; background:var(--live); flex:none;
}}
.tape b {{ color:var(--ink-2); font-weight:500; }}

.masthead {{ display:flex; flex-wrap:wrap; align-items:flex-end; gap:10px 22px; margin-bottom:10px; }}
h1 {{
  font-family:var(--display); font-size:clamp(22px,3.4vw,38px); line-height:1.05;
  letter-spacing:0; font-weight:700; margin:0; text-wrap:balance;
  color:var(--ink);
}}
.kicker {{
  font-family:var(--mono); font-size:11px; letter-spacing:.2em; text-transform:uppercase;
  color:var(--muted); padding-bottom:12px;
}}
.lede {{
  max-width:60ch; color:var(--ink-2); margin:0 0 34px;
  font-family:var(--sans); font-weight:500;
  font-size:15px; line-height:1.5; letter-spacing:0; color:var(--ink-2);
}}

h2 {{
  font-family:var(--display); font-size:clamp(14px,1.7vw,19px); letter-spacing:0;
  color:var(--ink); font-weight:700; margin:0 0 6px; line-height:1.2;
  display:flex; align-items:center; gap:16px;
}}
h2::after {{ content:""; flex:1; height:var(--rule-w); background:var(--rule); }}

.sub-head {{
  font-family:var(--mono); font-size:11px; font-weight:700; letter-spacing:.16em;
  text-transform:uppercase; color:var(--ink); margin:20px 0 6px;
}}
.sub-head:first-of-type {{ margin-top:10px; }}
td.basket {{ text-align:right; white-space:normal; max-width:340px; }}
td.basket .pays {{ display:inline-block; margin:2px 0 2px 4px; }}
.pays {{
  font-family:var(--mono); font-size:11px; font-weight:700; letter-spacing:.08em;
  background:var(--accent-soft); color:var(--accent-ink);
  border:1px solid var(--accent-ink); padding:2px 7px; white-space:nowrap;
}}

/* ---- pixel frame ----
   The logo is pixel art, so the containers are cut to match: clip-path notches
   the four corners, and because the border is clipped along with the box the
   outline reads as a stepped pixel edge rather than a rounded one. The offset
   drop shadow is a ::before layer carrying the SAME clip, so the shadow steps
   too -- a plain box-shadow would be clipped away entirely. */
.pixel {{
  --px:6px;
  position:relative;
  clip-path:polygon(
    0 var(--px), var(--px) var(--px), var(--px) 0,
    calc(100% - var(--px)) 0, calc(100% - var(--px)) var(--px), 100% var(--px),
    100% calc(100% - var(--px)), calc(100% - var(--px)) calc(100% - var(--px)),
    calc(100% - var(--px)) 100%, var(--px) 100%,
    var(--px) calc(100% - var(--px)), 0 calc(100% - var(--px)));
}}
/* The offset shadow MUST live on a wrapper, not on the clipped element.
   CSS applies filter first and clip-path second, so a drop-shadow on the same
   element is clipped away entirely -- the shadow lies outside the notch
   polygon by definition. On a wrapper it survives AND inherits the stepped
   corners from the child's alpha. (box-shadow and a ::before layer fail the
   same way, for the same reason.) */
.pixel-shadow {{ filter:drop-shadow(9px 9px 0 var(--shadow)); }}
.pixel-shadow {{ display:block; }}

/* ---- brand mark ---- */
.brandmark {{
  display:inline-flex; align-items:center; color:var(--ink); flex:none;
}}
.brandmark, .brandmark svg, .brandmark img {{
  height:46px; width:auto; display:block;   /* integer 1:1 of the 46px source */
  image-rendering:pixelated;          /* never smooth pixel art */
}}
.masthead {{ align-items:center; }}
.cardmark {{ display:inline-flex; align-items:center; margin-right:14px; }}
.cardmark svg, .cardmark img {{ height:40px; width:auto; display:block; }}

/* ---- hero ---- */
.contract-link {{
  display:inline-block; font-family:var(--mono); font-size:12.5px;
  letter-spacing:.06em; color:var(--ink); text-decoration:none;
  border-bottom:var(--rule-w) solid var(--rule); padding-bottom:3px; margin-bottom:34px;
}}
.contract-link:hover {{ color:var(--accent-ink); border-bottom-color:var(--accent-ink); }}
.hero {{ display:grid; grid-template-columns:1fr; gap:22px; margin-bottom:8px; }}
@media (min-width:900px) {{ .hero {{ grid-template-columns:1.15fr 1fr; gap:26px; }} }}
.hero-card {{
  background:var(--accent); color:var(--on-accent);
  border:var(--rule-w) solid var(--on-accent);
  padding:22px 24px 20px; display:flex; flex-direction:column; justify-content:center;
}}
.hero-label {{
  font-family:var(--mono); font-size:11.5px; letter-spacing:.2em;
  text-transform:uppercase; font-weight:700; margin-bottom:14px;
}}
.hero-value {{
  font-family:var(--grotesk); font-weight:900; letter-spacing:-.035em; line-height:.95;
  font-size:clamp(44px,7vw,80px); font-variant-numeric:tabular-nums;
}}
.hero-meta {{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.11em;
  text-transform:uppercase; margin-top:16px; opacity:.72;
}}
.hero-side {{ display:grid; grid-template-columns:1fr; gap:0; align-content:stretch;
  border:var(--rule-w) solid var(--rule); background:var(--panel); }}
.hero-side .tile {{ border-bottom:1px solid var(--line); }}
.hero-side .tile:last-child {{ border-bottom:none; }}

/* ---- audit strip ---- */
.audit {{
  display:grid; grid-template-columns:1fr; gap:4px 18px; align-items:baseline;
  border:var(--rule-w) solid var(--rule); background:var(--panel);
  padding:14px 18px; margin-top:26px;
}}
@media (min-width:900px) {{
  .audit {{ grid-template-columns:auto auto 1fr; }}
}}
.audit-k {{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.18em;
  text-transform:uppercase; font-weight:700; color:var(--ink);
  display:inline-flex; align-items:center; gap:8px; white-space:nowrap;
}}
.audit-k::before {{
  content:""; width:9px; height:9px; flex:none; border-radius:50%;
  background:var(--live);
}}
.audit.part .audit-k::before {{ background:var(--flag); }}
.audit-v {{
  font-family:var(--mono); font-size:12.5px; font-weight:700;
  font-variant-numeric:tabular-nums; color:var(--ink); white-space:nowrap;
}}
.audit-d {{ font-size:12.5px; color:var(--muted); max-width:88ch; }}
.audit-d code {{
  font-family:var(--mono); font-size:11.5px; background:var(--panel-2);
  padding:1px 5px; color:var(--ink-2);
}}
.sec-sub {{ color:var(--muted); font-size:13px; margin:0 0 12px; max-width:74ch; }}
section {{ margin-top:34px; }}

/* ---- range selector ---- */
.ranges {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 16px; }}
.ranges button {{
  font-family:var(--mono); font-size:11px; letter-spacing:.09em; text-transform:uppercase;
  padding:7px 14px; cursor:pointer; font-weight:600;
  background:var(--panel); color:var(--ink);
  border:1.5px solid var(--rule);
}}
.ranges button:hover:not(:disabled) {{ color:var(--ink); border-color:var(--muted); }}
.ranges button[aria-pressed="true"] {{
  background:var(--accent); color:var(--on-accent); border-color:var(--rule);
  font-weight:700; box-shadow:3px 3px 0 var(--shadow);
}}
.ranges button:disabled {{ opacity:.34; cursor:not-allowed; }}
.range-note {{
  font-family:var(--mono); font-size:10.5px; color:var(--muted);
  margin:0 0 16px; letter-spacing:.03em;
}}

/* ---- chart grid ---- */
.charts {{ display:grid; grid-template-columns:1fr; gap:1px; background:var(--line);
  border:var(--rule-w) solid var(--rule); overflow:hidden;
  }}
@media (min-width:860px) {{ .charts {{ grid-template-columns:1fr 1fr; }} }}
.chart {{ background:var(--panel); padding:12px 14px 9px; min-width:0; }}
.chart-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; }}
.chart h3 {{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.11em; text-transform:uppercase;
  color:var(--muted); font-weight:600; margin:0;
}}
.chart-val {{
  font-family:var(--mono); font-size:19px; font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; margin:2px 0 1px;
}}
.chart-delta {{
  font-family:var(--mono); font-size:11px; font-variant-numeric:tabular-nums;
  display:flex; align-items:baseline; gap:8px; min-height:16px;
}}
.agg-label {{ color:var(--muted); font-size:10px; letter-spacing:.04em; }}
.plot {{ position:relative; margin-top:10px; }}
.plot svg {{ display:block; width:100%; height:132px; overflow:visible; }}
.grid-line {{ stroke:var(--line); stroke-width:1; }}
.axis-txt {{ fill:var(--muted); font-family:var(--mono); font-size:9.5px; }}
.crosshair {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; opacity:0; }}
.hover-dot {{ opacity:0; }}
.tip {{
  position:absolute; pointer-events:none; opacity:0; transform:translate(-50%,-100%);
  background:var(--panel-2); border:2px solid var(--rule);
  padding:7px 10px; font-family:var(--mono); font-size:11px; white-space:nowrap;
  color:var(--ink); z-index:5; box-shadow:3px 3px 0 var(--shadow);
}}
.tip .tip-d {{ color:var(--muted); font-size:10px; display:block; margin-bottom:3px; }}
.tip .tip-r {{ display:flex; align-items:center; gap:6px; }}
.tip i {{ width:7px; height:7px; border-radius:50%; flex:none; }}
.legend {{ display:flex; flex-wrap:wrap; gap:5px 16px; align-items:center; margin-top:8px;
  font-family:var(--mono); font-size:10px; letter-spacing:.05em; color:var(--muted); }}
.legend span {{ display:inline-flex; align-items:center; gap:6px; }}
.legend i {{ width:8px; height:8px; border-radius:2px; flex:none; }}

/* ---- tiles ---- */
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:1px;
  background:var(--line); border:var(--rule-w) solid var(--rule);
  overflow:hidden; margin-top:16px; }}
.tile {{ background:var(--panel); padding:10px 14px; }}
.tile-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
.tile-label {{ font-family:var(--mono); font-size:10px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--muted); font-weight:600; display:block; }}
.tile-value {{ font-family:var(--mono); font-size:21px; font-weight:600;
  font-variant-numeric:tabular-nums; letter-spacing:-.02em; margin-top:3px; }}
.tile-sub {{ font-size:11.5px; color:var(--muted); }}
.chg {{ font-family:var(--mono); font-size:11px; font-weight:600; font-variant-numeric:tabular-nums; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat {{ color:var(--muted); }}

/* ---- stablecoin composition ---- */
.comp {{ background:var(--panel); border:var(--rule-w) solid var(--rule);
  padding:20px; }}
.compbar {{ display:flex; height:15px; border-radius:2px; overflow:hidden; gap:2px; margin-bottom:16px; }}
.compbar i {{ display:block; height:100%; }}
ul.stables {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:1px; }}
li.stable {{
  display:grid; grid-template-columns:auto auto 1fr auto auto auto; align-items:center;
  gap:12px; padding:9px 0; border-bottom:1px solid var(--line); font-size:13px;
}}
li.stable:last-child {{ border-bottom:none; }}
.sw {{ width:9px; height:9px; border-radius:2px; }}
.st-sym {{ font-family:var(--mono); font-weight:700; }}
.st-name {{ color:var(--muted); font-size:12px; }}
.st-mech {{ font-family:var(--mono); font-size:9.5px; letter-spacing:.06em; text-transform:uppercase;
  color:var(--muted); background:var(--panel-2); padding:2px 6px; border-radius:2px; }}
.st-val, .st-share {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
.st-share {{ color:var(--muted); min-width:48px; text-align:right; }}

/* ---- tables ---- */
.board {{ background:var(--panel); border:var(--rule-w) solid var(--rule);
  overflow:hidden; }}
.scroll {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
thead th {{
  font-family:var(--mono); font-size:10px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink); font-weight:700; text-align:right; padding:8px 12px;
  border-bottom:var(--rule-w) solid var(--rule); background:var(--panel-2);
  white-space:nowrap; letter-spacing:.13em;
}}
thead th:first-child, thead th:nth-child(2) {{ text-align:left; }}
thead th[data-sort] {{ cursor:pointer; user-select:none; position:relative; }}
thead th[data-sort]:hover {{ color:var(--accent-ink); }}
thead th[data-sort]::after {{
  content:"↕"; opacity:.5; margin-left:7px; font-size:11px;
  display:inline-block; width:1em; text-align:center;
}}
thead th[aria-sort="descending"]::after {{ content:"↓"; opacity:1; }}
thead th[aria-sort="ascending"]::after {{ content:"↑"; opacity:1; }}
thead th[data-sort]:focus-visible {{ outline:2px solid var(--accent-ink); outline-offset:-2px; }}
tbody td {{ padding:6px 12px; border-bottom:1px solid var(--line); vertical-align:middle; }}
tbody tr:last-child td {{ border-bottom:none; }}
tbody tr:hover {{ background:var(--panel-2); }}
td.n {{ text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.n.strong {{ font-weight:600; }}
td.dim {{ color:var(--muted); }}
td.rank {{ font-family:var(--mono); font-size:11px; color:var(--muted); width:34px;
  font-variant-numeric:tabular-nums; }}
td.sym {{ min-width:190px; }}
td.sym > * {{ display:block; }}
.sym-name {{ font-weight:600; font-size:13.5px; display:flex; align-items:center; }}
.sym-name.rejected {{ color:var(--reject); }}
.sym-addr {{ font-family:var(--mono); font-size:10.5px; color:var(--muted); }}
td.sym .sym-name {{ margin-bottom:3px; }}
.badge {{
  font-family:var(--mono); font-size:9px; letter-spacing:.07em; text-transform:uppercase;
  padding:2px 5px; border-radius:2px; margin-left:7px; font-weight:600; white-space:nowrap;
}}
.badge.flag {{ color:var(--flag); background:var(--flag-soft); }}
.badge.ok {{ color:var(--accent-ink); background:var(--accent-soft); }}
.badge.warn {{ color:var(--muted); background:var(--panel-2); border:1px solid var(--line); }}
td.reason {{ color:var(--muted); font-size:12px; }}
.corrob {{ display:inline-block; width:7px; height:7px; border-radius:50%;
  margin-right:7px; flex:none; box-shadow:0 0 0 1px var(--rule); }}
.corrob.ok {{ background:var(--accent-ink); }}   /* lime on white is 1.25:1 */
.corrob.part {{ background:var(--flag); }}
.corrob.bad {{ background:var(--reject); }}
.agrees {{ color:var(--accent-ink); font-size:11px; }}
.alt {{
  display:block; font-size:10px; color:var(--muted); font-weight:400;
  letter-spacing:.02em; margin-top:1px;
}}
.alt.up {{ color:var(--up); }} .alt.down {{ color:var(--down); }}
td.n {{ line-height:1.3; }}

.two-col {{ display:grid; grid-template-columns:1fr; gap:28px; align-items:stretch;
  margin-top:34px; }}   /* section{{margin-top}} is cancelled by .two-col>section */
@media (min-width:1040px) {{ .two-col {{ grid-template-columns:1fr 1fr; gap:28px; }} }}
.two-col > section {{ margin-top:0; display:flex; flex-direction:column; min-width:0; }}
/* keep both board headers on the same baseline even though the blurbs differ */
.two-col .sec-sub {{ min-height:3.1em; }}
.reward-cols .sub-head {{ margin-top:0; min-height:auto; }}
/* max-width on a <td> is ignored unless the table is fixed-layout, which is
   why the asset chips kept overflowing into the Value column. */
.reward-cols table {{ table-layout:fixed; width:100%; }}
.reward-cols th:nth-child(1), .reward-cols td:nth-child(1) {{ width:32px; }}
.reward-cols th:nth-child(3), .reward-cols td:nth-child(3) {{ width:36%; }}
.reward-cols th:nth-child(4), .reward-cols td:nth-child(4) {{ width:27%; }}
.reward-cols td.basket {{ white-space:normal; }}
.reward-cols td.sym {{ min-width:0; overflow-wrap:anywhere; }}
.pays.more-chip {{ background:transparent; color:var(--muted);
  border-color:color-mix(in srgb, var(--muted) 50%, transparent); }}   /* 2 lines at 1.55 — blurbs are one line now */
@media (max-width:1039px) {{ .two-col .sec-sub {{ min-height:0; }} }}
.two-col .board {{ flex:1; display:flex; flex-direction:column; }}
.two-col .scroll {{ flex:1; }}
.two-col table {{ height:100%; }}

/* board footer keeps both cards the same shape and carries the key */
.board-foot {{
  display:flex; flex-wrap:wrap; align-items:center; gap:6px 16px;
  padding:9px 14px; border-top:1px solid var(--line); background:var(--panel-2);
  font-family:var(--mono); font-size:10px; letter-spacing:.05em; color:var(--muted);
}}
.board-foot span {{ display:inline-flex; align-items:center; gap:6px; }}
tbody tr.over {{ display:none; }}
.board.expanded tbody tr.over {{ display:table-row; }}
button.more {{
  font-family:var(--mono); font-size:10px; letter-spacing:.09em; text-transform:uppercase;
  background:var(--panel); color:var(--ink); border:1.5px solid var(--rule);
  padding:2px 8px; cursor:pointer; font-weight:700;
}}
button.more:hover {{ background:var(--accent); }}
.board-foot .foot-r {{ margin-left:auto; }}


.sym-sub {{
  font-family:var(--mono); font-size:10.5px; color:var(--muted);
  display:flex; align-items:center; flex-wrap:wrap; gap:5px;
}}
footer {{ margin-top:56px; padding-top:20px; border-top:var(--rule-w) solid var(--rule);
  font-size:12.5px; color:var(--muted); }}
footer p {{ max-width:78ch; }}
.notes summary {{
  font-family:var(--mono); font-size:10px; letter-spacing:.16em; text-transform:uppercase;
  color:var(--muted); cursor:pointer; padding:4px 0; list-style:none;
}}
.notes summary::-webkit-details-marker {{ display:none; }}
.notes summary::before {{ content:"+ "; }}
.notes[open] summary::before {{ content:"\2212 "; }}
.notes summary:hover {{ color:var(--ink); }}
.notes p {{ font-size:12px; margin:8px 0 0; }}
.stamp {{ font-family:var(--mono); font-size:10px; letter-spacing:.06em;
  color:var(--muted); margin:12px 0 0; }}
footer h2.foot-h {{ font-family:var(--mono); font-size:10px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-2); margin:0 0 8px; font-weight:600; }}
footer p {{ margin:0 0 10px; }}
footer code {{ font-family:var(--mono); font-size:11.5px; color:var(--ink-2); }}
/* Below ~700px the boards need ~560px in a ~468px scroller, which pushed
   Volume -- the default sort and the whole basis of the ranking -- off the
   right edge. Drop the secondary numerics so Token + Volume always fit. */
@media (max-width:700px) {{
  .board th:nth-child(3), .board td:nth-child(3),
  .board th:nth-child(4), .board td:nth-child(4) {{ display:none; }}
  td.sym {{ min-width:150px; }}
  tbody td, thead th {{ padding-left:10px; padding-right:10px; }}
}}
@media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; }} }}
</style>

<div class="wrap">
  <div class="tape">
    <span class="live"><span class="dot"></span>Robinhood Chain</span>
    <span>chain id <b>4663</b></span>
    <span>block <b>{num(n.get('to_block'))}</b></span>
    <span>eth <b>${s.get('eth_price_usd') or 0:,.0f}</b></span>
    <span>snapshot <b>{escape(gen_str)}</b></span>
  </div>

  <div class="masthead">
    {logo_html(logo)}<h1>HoodScout</h1>
    <span class="kicker">Robinhood Chain, end to end</span>
  </div>
  <p class="lede">
    {span_days} days of chain history, screened against two independent indexers,
    priced from DexScreener and measured from Seaport fills on-chain.
  </p>
  <a class="contract-link" href="https://robinhoodchain.blockscout.com/" target="_blank"
     rel="noopener noreferrer">View explorer <span aria-hidden="true">&#8599;</span></a>

  <div class="hero">
    <div class="pixel-shadow"><div class="hero-card pixel">
      <span class="hero-label">Total value locked</span>
      <span class="hero-value">{usd(tvl_headline)}</span>
      <span class="hero-meta">
        Robinhood Chain &middot; DefiLlama &middot; {escape(tvl_date)}
        &middot; {pct(l.get('tvl_change_7d_pct'))} 7d
      </span>
    </div></div>
    <div class="hero-side pixel">
{hero_tiles}
    </div>
  </div>

  <section>
    <h2>Chain vitals</h2>
    <p class="sec-sub">Last complete UTC day. Hover any plot for exact values.</p>
    <div class="ranges" id="ranges" role="group" aria-label="Time range"></div>
    <p class="range-note" id="rangeNote"></p>
    <div class="pixel-shadow"><div class="charts pixel" id="charts"></div></div>
    <div class="pixel-shadow"><div class="tiles pixel">
{tiles}
    </div></div>
  </section>

  <section>
    <h2>Stablecoins on the chain</h2>
    <p class="sec-sub">
      All {usd(stb_headline)} of it. No real USDC or USDT exists on this chain.
    </p>
    <div class="pixel-shadow"><div class="comp pixel">
      <div class="compbar" id="compbar"></div>
      <ul class="stables">
{stable_rows(stables)}
      </ul>
    </div>
  </section>

  <div class="two-col">
    <section>
      <h2>Top memecoins</h2>
      <p class="sec-sub">
        By 24h volume, behind a {usd(floor)} liquidity floor. {n_ok}/{n_shown} corroborated
        by a second indexer.
      </p>
      <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
        <table>
          <thead><tr>
            <th></th><th data-sort="text">Token</th><th data-sort="num">Price</th>
            <th data-sort="num">Mkt cap</th><th data-sort="num" data-default="1">Volume 24h</th>
          </tr></thead>
          <tbody>
{meme_rows(m.get('tokens', []))}
          </tbody>
        </table>
      </div>
      <div class="board-foot">
        <button class="more" type="button"></button>
        <span><i class="corrob ok"></i>agree</span>
        <span><i class="corrob part"></i>3–20x apart</span>
        <span><i class="corrob bad"></i>disputed</span>
        <span class="foot-r">{n_excl} more screened out below the {usd_exact(floor)} floor</span>
      </div></div></div>
    </section>

    <section>
      <h2>Top NFT collections</h2>
      <p class="sec-sub">
        By paid Seaport fills, {escape(str(n.get('window_label') or ''))} UTC.
        {n_verified}/{len(n.get('collections', []))} OpenSea-verified.
      </p>
      <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
        <table>
          <thead><tr>
            <th></th><th data-sort="text">Collection</th><th data-sort="num">{floor_hdr}</th>
            <th data-sort="num">Avg</th><th data-sort="num" data-default="1">Volume {nft_window}h</th>
          </tr></thead>
          <tbody>
{nft_rows(n.get('collections', []))}
          </tbody>
        </table>
      </div>
      <div class="board-foot">
        <button class="more" type="button"></button>
        <span>{num(n.get('logs_scanned'))} Seaport fills decoded</span>
        <span class="foot-r">{escape(usd(n.get('total_volume_usd')))} total</span>
      </div></div></div>
    </section>
  </div>

  <section>
    <h2>Who pays their holders</h2>
    <p class="sec-sub">
      Projects routing fees into buying a different asset and paying it out. {rw_assets_note}
    </p>
    <div class="two-col reward-cols">
      <section>
        <h3 class="sub-head">Memecoins &middot; ERC-20 holders</h3>
        <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
          <table>
            <thead><tr>
              <th></th><th data-sort="text">Project</th><th data-sort="text">Pays in</th>
              <th data-sort="num" data-default="1">Value</th>
            </tr></thead>
            <tbody>
{reward_rows(rw.get('projects', []))}
            </tbody>
          </table>
        </div>
        <div class="board-foot">
          <button class="more" type="button"></button>
          <span class="foot-r">{escape(usd(rw.get('total_distributed_usd')))} paid out</span>
        </div></div></div>
      </section>

      <section>
        <h3 class="sub-head">NFT collections &middot; the bigger half</h3>
        <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
          <table>
            <thead><tr>
              <th></th><th data-sort="text">Collection</th><th data-sort="num">Pays in</th>
              <th data-sort="num" data-default="1">Value</th>
            </tr></thead>
            <tbody>
{booster_rows(bst.get('projects', []))}
            </tbody>
          </table>
        </div>
        <div class="board-foot">
          <span>{len(bst.get('reward_assets', []))} assets</span>
          <span class="foot-r">{escape(usd(bst.get('total_distributed_usd')))} to NFT holders</span>
        </div></div></div>
      </section>
    </div>
  </section>


  <footer>
    <details class="notes">
      <summary>Method &amp; caveats</summary>
      <p>
        TVL, stablecoins and app fees from DefiLlama; active users and gas from
        Blockscout; memecoins from DexScreener cross-checked against GeckoTerminal;
        NFT volume by decoding Seaport <code>OrderFulfilled</code> logs off the chain
        RPC; holder payouts from <code>DividendsDistributed</code> events and booster
        transfers, both read on-chain.
      </p>
      <p>
        <b>Gas fees and app fees are separate numbers</b> — paid to the chain versus
        earned by protocols on it, ~30x apart, never summed. <b>The stablecoin split is
        an approximation</b>: DefiLlama publishes per-chain history only in aggregate,
        so the USDG/USDe division is held constant backwards. Daily is the finest
        resolution available. A matching contract name is not proof of authenticity —
        it is exactly what a copycat is built to have.
      </p>
      <p>{audit_line}</p>
    </details>
    <p class="stamp">Snapshot {escape(gen_str)}.</p>
  </footer>
</div>

<script>
const D = {payload};

const RANGES = [
  // Deliberately stops at 3M: the chain is ~97 days old, so anything longer
  // would be padding. Add 6M/1Y here as it matures -- the guard below already
  // disables any range that outruns the available history.
  {{id:'max', label:'All', days:99999, note:'every day the chain has existed'}},
  {{id:'7d',  label:'1W',  days:7,     note:'the last 7 days'}},
  {{id:'1m',  label:'1M',  days:30,    note:'the last 30 days'}},
  {{id:'3m',  label:'3M',  days:90,    note:'the last 90 days'}},
];
let active = '1m';

const css = k => getComputedStyle(document.documentElement).getPropertyValue('--'+k).trim();
const fmtUsd = v => {{
  const a = Math.abs(v);
  if (a >= 1e9) return '$'+(v/1e9).toFixed(2)+'B';
  if (a >= 1e6) return '$'+(v/1e6).toFixed(2)+'M';
  if (a >= 1e3) return '$'+(v/1e3).toFixed(1)+'K';
  return '$'+v.toFixed(0);
}};
const fmtNum = v => {{
  const a = Math.abs(v);
  if (a >= 1e9) return (v/1e9).toFixed(2)+'B';
  if (a >= 1e6) return (v/1e6).toFixed(2)+'M';
  if (a >= 1e3) return (v/1e3).toFixed(1)+'K';
  return v.toFixed(0);
}};
const fmt = (v,u) => v==null ? '—' : (u==='usd' ? fmtUsd(v) : fmtNum(v));
const shortDate = d => {{
  const [y,m,dd] = d.split('-');
  return dd+' '+['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+m-1];
}};

function windowed(points, days) {{
  if (!points.length) return [];
  if (days >= 99999) return points;
  const last = new Date(points[points.length-1][0]+'T00:00:00Z').getTime();
  const cut = last - days*864e5;
  return points.filter(p => new Date(p[0]+'T00:00:00Z').getTime() >= cut);
}}

function buildRanges() {{
  const box = document.getElementById('ranges');
  box.innerHTML = '';
  RANGES.forEach(r => {{
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = r.label;
    b.setAttribute('aria-pressed', r.id === active ? 'true' : 'false');
    // A range longer than the chain has existed would render mostly empty
    // axis. Offer it, but disabled and explained, rather than faking depth.
    if (r.days > 90 && r.days < 99999 && r.days > D.spanDays) {{
      b.disabled = true;
      b.title = 'The chain only has ' + D.spanDays + ' days of history';
    }}
    b.addEventListener('click', () => {{ active = r.id; buildRanges(); renderAll(); }});
    box.appendChild(b);
  }});
  const r = RANGES.find(x => x.id === active);
  const note = document.getElementById('rangeNote');
  note.textContent = (r.days > D.spanDays && r.days < 99999)
    ? 'Showing all ' + D.spanDays + ' days of history — the chain is younger than this range.'
    : 'Showing ' + r.note + '. Daily granularity is the finest the chain\\'s stats API offers.';
}}

function renderChart(key, cfg) {{
  const r = RANGES.find(x => x.id === active);
  const series = cfg.series.map(s => ({{...s, pts: windowed(s.points, r.days)}}))
                           .filter(s => s.pts.length);
  const host = document.getElementById('c-'+key);
  if (!series.length || series[0].pts.length < 2) {{
    host.innerHTML = '<div class="axis-txt" style="padding:60px 0;text-align:center">not enough history for this range</div>';
    return;
  }}
  const W = host.clientWidth || 520, H = 190;
  const PL = 52, PR = 10, PT = 12, PB = 22;
  const n = series[0].pts.length;
  const stacked = !!cfg.stacked;

  let maxV = 0;
  for (let i=0;i<n;i++) {{
    let v = stacked ? series.reduce((a,s)=>a+(s.pts[i]?.[1]||0),0)
                    : Math.max(...series.map(s=>s.pts[i]?.[1]||0));
    if (v > maxV) maxV = v;
  }}
  let minV = stacked ? 0 : Math.min(...series.flatMap(s=>s.pts.map(p=>p[1])));
  if (minV === maxV) {{ maxV = maxV || 1; minV = 0; }}
  if (!stacked) minV = Math.min(minV, maxV * 0.92);

  const X = i => PL + (i*(W-PL-PR))/(n-1);
  const Y = v => H-PB - ((v-minV)/(maxV-minV))*(H-PT-PB);

  const cols = series.map(s => css(s.color));
  let svg = '';
  for (let g=0; g<=3; g++) {{
    const y = PT + g*(H-PT-PB)/3;
    const val = minV + (maxV-minV)*(1-g/3);
    svg += `<line class="grid-line" x1="${{PL}}" y1="${{y}}" x2="${{W-PR}}" y2="${{y}}"/>`;
    svg += `<text class="axis-txt" x="${{PL-8}}" y="${{y+3}}" text-anchor="end">${{fmt(val,cfg.unit)}}</text>`;
  }}
  const step = Math.max(1, Math.floor(n/5));
  for (let i=0;i<n;i+=step) {{
    svg += `<text class="axis-txt" x="${{X(i)}}" y="${{H-6}}" text-anchor="middle">${{shortDate(series[0].pts[i][0])}}</text>`;
  }}

  if (stacked) {{
    const base = new Array(n).fill(0);
    series.forEach((s,si) => {{
      const top = s.pts.map((p,i)=>base[i]+(p[1]||0));
      let d = 'M'+X(0)+','+Y(top[0]);
      for (let i=1;i<n;i++) d += ' L'+X(i)+','+Y(top[i]);
      for (let i=n-1;i>=0;i--) d += ' L'+X(i)+','+Y(base[i]);
      svg += `<path d="${{d}} Z" fill="${{cols[si]}}" opacity=".85" stroke="var(--panel)" stroke-width="2"/>`;
      for (let i=0;i<n;i++) base[i] = top[i];
    }});
  }} else {{
    series.forEach((s,si) => {{
      let d = 'M'+X(0)+','+Y(s.pts[0][1]);
      for (let i=1;i<n;i++) d += ' L'+X(i)+','+Y(s.pts[i][1]);
      svg += `<path d="${{d}} L${{X(n-1)}},${{H-PB}} L${{X(0)}},${{H-PB}} Z" fill="${{cols[si]}}" opacity=".12"/>`;
      svg += `<path d="${{d}}" fill="none" stroke="${{cols[si]}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      svg += `<circle cx="${{X(n-1)}}" cy="${{Y(s.pts[n-1][1])}}" r="3.5" fill="${{cols[si]}}" stroke="var(--panel)" stroke-width="2"/>`;
    }});
  }}
  svg += `<line class="crosshair" id="ch-${{key}}" y1="${{PT}}" y2="${{H-PB}}"/>`;
  series.forEach((s,si)=>{{ svg += `<circle class="hover-dot" id="hd-${{key}}-${{si}}" r="4" fill="${{cols[si]}}" stroke="var(--panel)" stroke-width="2"/>`; }});
  svg += `<rect x="${{PL}}" y="${{PT}}" width="${{W-PL-PR}}" height="${{H-PT-PB}}" fill="transparent" id="hit-${{key}}"/>`;

  host.innerHTML = `<svg viewBox="0 0 ${{W}} ${{H}}" preserveAspectRatio="none" role="img" aria-label="${{cfg.title}}">${{svg}}</svg>`
    + `<div class="tip" id="tip-${{key}}"></div>`;

  // Headline responds to the selected range, aggregated the way the metric
  // actually permits (see the `agg` note where the charts are defined).
  const agg = cfg.agg || 'last';
  const perDay = i => series.reduce((a,s)=>a+(s.pts[i]?.[1]||0), 0);
  let headline, label;
  if (agg === 'sum') {{
    headline = 0; for (let i=0;i<n;i++) headline += perDay(i);
    label = 'total over ' + (r.days>D.spanDays ? 'all history' : r.label.toLowerCase());
  }} else if (agg === 'avg') {{
    let t=0; for (let i=0;i<n;i++) t += perDay(i);
    headline = t/n;
    label = 'daily average over ' + (r.days>D.spanDays ? 'all history' : r.label.toLowerCase());
  }} else {{
    headline = perDay(n-1);
    label = 'latest — ' + s0date(series[0].pts[n-1][0]);
  }}
  document.getElementById('v-'+key).textContent = fmt(headline, cfg.unit);

  // Change over a window that reaches back near launch is technically a
  // percentage but reads as noise -- TVL's first day was $1,342, so 3M shows
  // "+32,135,366%". Past ~10x, report a multiple instead, and say plainly
  // that the baseline is launch rather than implying a normal growth rate.
  const dEl = document.getElementById('d-'+key);
  const first = perDay(0), last = perDay(n-1);
  if (first > 0 && last > 0) {{
    const mult = last / first;
    const dir = mult > 1 ? 'up' : mult < 1 ? 'down' : 'flat';
    let txt;
    const base = ' vs ' + s0date(series[0].pts[0][0]);
    if (mult >= 11) {{
      txt = '×' + fmtNum(mult) + base;
    }} else if (mult <= 1/11) {{
      txt = '÷' + fmtNum(1/mult) + base;
    }} else {{
      const ch = (mult-1)*100;
      txt = (ch>=0?'+':'') + ch.toFixed(1) + '%';
    }}
    dEl.innerHTML = `<span class="${{dir}}">${{txt}}</span><span class="agg-label">${{label}}</span>`;
  }} else {{
    dEl.innerHTML = `<span class="agg-label">${{label}}</span>`;
  }}

  const hit = document.getElementById('hit-'+key);
  const ch_ = document.getElementById('ch-'+key);
  const tip = document.getElementById('tip-'+key);
  const svgEl = host.querySelector('svg');
  const move = ev => {{
    const bb = svgEl.getBoundingClientRect();
    const px = (ev.clientX - bb.left) / bb.width * W;
    let i = Math.round((px-PL)/((W-PL-PR)/(n-1)));
    i = Math.max(0, Math.min(n-1, i));
    ch_.setAttribute('x1', X(i)); ch_.setAttribute('x2', X(i)); ch_.style.opacity = '.7';
    let acc = 0, rows = '';
    series.forEach((s,si)=>{{
      const v = s.pts[i][1];
      const yv = stacked ? (acc += v) : v;
      const dot = document.getElementById(`hd-${{key}}-${{si}}`);
      dot.setAttribute('cx', X(i)); dot.setAttribute('cy', Y(yv)); dot.style.opacity = '1';
      rows += `<div class="tip-r"><i style="background:${{cols[si]}}"></i>${{series.length>1? s.name+' ':''}}${{fmt(v,cfg.unit)}}</div>`;
    }});
    tip.innerHTML = `<span class="tip-d">${{s0date(series[0].pts[i][0])}}</span>${{rows}}`;
    tip.style.opacity = '1';
    const tw = tip.offsetWidth / 2;
    tip.style.left = Math.min(Math.max(tw, X(i)/W*bb.width), bb.width - tw) + 'px';
    tip.style.top = (Y(stacked?acc:series[0].pts[i][1])/H*bb.height - 10) + 'px';
  }};
  const leave = () => {{
    ch_.style.opacity = '0'; tip.style.opacity = '0';
    series.forEach((s,si)=>{{ document.getElementById(`hd-${{key}}-${{si}}`).style.opacity='0'; }});
  }};
  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', leave);
  svgEl.addEventListener('touchmove', e => {{ if(e.touches[0]) move(e.touches[0]); }}, {{passive:true}});
  svgEl.addEventListener('touchend', leave);
}}
const s0date = d => {{
  const [y,m,dd] = d.split('-');
  return dd+' '+['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+m-1]+' '+y;
}};

function scaffold() {{
  const box = document.getElementById('charts');
  box.innerHTML = Object.entries(D.charts).map(([k,c]) => {{
    const legend = c.series.length > 1
      ? '<div class="legend">' + c.series.map(s =>
          `<span><i style="background:var(--${{s.color}})"></i>${{s.name}}</span>`).join('') + '</div>'
      : '';
    return `<div class="chart">
      <div class="chart-head"><h3>${{c.title}}</h3></div>
      <div class="chart-val" id="v-${{k}}">—</div>
      <div class="chart-delta" id="d-${{k}}"></div>
      <div class="plot" id="c-${{k}}"></div>${{legend}}
    </div>`;
  }}).join('');
}}

function compbar() {{
  const bar = document.getElementById('compbar');
  const items = [...document.querySelectorAll('li.stable')];
  bar.innerHTML = '';
  items.forEach((li,i) => {{
    const share = parseFloat(li.querySelector('.st-share').textContent) || 0;
    const col = css('s'+(i+1)) || css('accent');
    li.querySelector('.sw').style.background = col;
    const el = document.createElement('i');
    el.style.width = share+'%'; el.style.background = col;
    el.title = li.querySelector('.st-sym').textContent + ' ' + share.toFixed(1) + '%';
    bar.appendChild(el);
  }});
}}

function renderAll() {{ Object.entries(D.charts).forEach(([k,c]) => renderChart(k,c)); }}

scaffold(); buildRanges(); renderAll(); compbar();
let rt; addEventListener('resize', () => {{ clearTimeout(rt); rt = setTimeout(renderAll, 150); }});
new MutationObserver(() => {{ renderAll(); compbar(); }})
  .observe(document.documentElement, {{attributes:true, attributeFilter:['data-theme']}});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {{ renderAll(); compbar(); }});

/* ---- sortable leaderboards ----
   Sort keys come from data-v on the cells rather than the rendered text: the
   display strings are abbreviated ($1.2M, 12.75 ETH, <$0.01) and would sort
   lexicographically into nonsense. Rank cells are renumbered after each sort
   so the left column always reads 1..n for the current ordering. */
function makeSortable(table) {{
  const heads = [...table.tHead.rows[0].cells];
  const body = table.tBodies[0];
  heads.forEach((th, idx) => {{
    if (!th.dataset.sort) return;
    th.tabIndex = 0;
    const run = () => {{
      const numeric = th.dataset.sort === 'num';
      // First click should be descending for numbers (biggest first) but
      // ASCENDING for text (A-Z) -- a name column that opens on Z-A reads as
      // broken. Subsequent clicks toggle.
      const cur = th.getAttribute('aria-sort');
      const dir = cur ? (cur === 'descending' ? 1 : -1) : (numeric ? -1 : 1);
      heads.forEach(h => h.removeAttribute('aria-sort'));
      th.setAttribute('aria-sort', dir === -1 ? 'descending' : 'ascending');
      const rows = [...body.rows];
      rows.sort((a, b) => {{
        const av = a.cells[idx]?.dataset.v ?? '';
        const bv = b.cells[idx]?.dataset.v ?? '';
        if (numeric) return (parseFloat(av) - parseFloat(bv)) * dir;
        return av.localeCompare(bv) * dir;
      }});
      rows.forEach((r, i) => {{
        const rk = r.querySelector('td.rank');
        if (rk) rk.textContent = i + 1;
        body.appendChild(r);
      }});
    }};
    th.addEventListener('click', run);
    th.addEventListener('keydown', e => {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); run(); }}
    }});
    if (th.dataset.default) th.setAttribute('aria-sort', 'descending');
  }});
}}
document.querySelectorAll('.board table').forEach(makeSortable);


/* Boards open at 10 rows. At 20 each board ran ~1,220px and the page passed
   6,000px total, so the lower half was rarely reached. */
document.querySelectorAll('.board').forEach(board => {{
  const btn = board.querySelector('button.more');
  if (!btn) return;
  const total = board.querySelectorAll('tbody tr').length;
  if (total <= 10) {{ btn.remove(); return; }}
  const label = () => {{ btn.textContent = board.classList.contains('expanded')
      ? 'show top 10' : 'show all ' + total; }};
  label();
  btn.addEventListener('click', () => {{ board.classList.toggle('expanded'); label(); }});
}});

</script>
"""


# Cloudflare appended -bv1 because the bare "hoodscout" project name was taken.

FONT_DIR = OUT_DIR.parent / "fonts"


def font_faces():
    """Inline Silkscreen as data URIs.

    The Artifact CSP blocks font CDNs outright, so a linked webfont would fail
    silently to a system fallback. The build has network access even though the
    page does not, so the woff2 is fetched once into fonts/ and embedded here --
    6.7KB for both weights, which is cheaper than the fallback being wrong.
    """
    import base64
    out = []
    for weight in (400, 700):
        p = FONT_DIR / f"Silkscreen-{weight}.woff2"
        if not p.exists():
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        # Single braces: this string is the RETURN VALUE spliced into the
        # already-formatted stylesheet, not part of the f-string template.
        # Doubling them emitted "@font-face{{...}}", an unclosed block that
        # swallowed the entire rest of the CSS.
        out.append(
            "@font-face{font-family:'Silkscreen';font-style:normal;"
            f"font-weight:{weight};font-display:block;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');""}")
    return "\n".join(out)


SITE_URL = "https://hoodscout.pages.dev"

# Drop a logo beside the script and it is picked up. SVG first (it can inherit
# currentColor), then raster.
LOGO_CANDIDATES = ["logo.svg", "logo.png", "logo.webp", "logo.jpg"]
LOGO_PATH = OUT_DIR.parent / "logo.svg"


def _find_logo():
    for n in LOGO_CANDIDATES:
        p = OUT_DIR.parent / n
        if p.exists():
            return p
    return None


def load_logo(path=None):
    """Inline a logo so it survives the Artifact CSP, which blocks every
    external host including image URLs.

    SVG is inlined as markup rather than as a data URI on purpose: that lets
    the mark inherit `currentColor`, so a single file works on both the cream
    and the near-black ground. A raster file can't do that, so it is embedded
    as a data URI and has to already read on both surfaces.
    """
    import base64
    p = Path(path) if path else _find_logo()
    if not p or not p.exists():
        return None
    raw = p.read_bytes()
    if p.suffix.lower() == ".svg":
        svg = raw.decode("utf-8", "replace")
        svg = svg[svg.index("<svg"):] if "<svg" in svg else svg
        return {"kind": "svg", "markup": svg}
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}.get(p.suffix.lower())
    if not mime:
        return None
    return {"kind": "img",
            "uri": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}


def logo_html(logo, cls="brandmark"):
    if not logo:
        return ""
    if logo["kind"] == "svg":
        return f'<span class="{cls}" role="img" aria-label="HoodScout">{logo["markup"]}</span>'
    return f'<img class="{cls}" src="{logo["uri"]}" alt="HoodScout">'



def favicon_tags():
    """The pixel mark as the site favicon, inlined.

    Note this only applies to the self-hosted page. The Artifact publish tool
    accepts an emoji favicon and nothing else, so the artifact keeps the bow.
    """
    import base64
    out = []
    for size, rel in ((32, "icon"), (180, "apple-touch-icon")):
        p = OUT_DIR.parent / f"favicon-{size}.png"
        if not p.exists():
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        out.append(f'<link rel="{rel}" sizes="{size}x{size}" '
                   f'href="data:image/png;base64,{b64}">')
    return "\n".join(out)


def standalone(fragment, p, base_url=SITE_URL, public=False):
    """Wrap the artifact fragment in a real HTML document for public hosting.

    The Artifact host supplies its own <!doctype>/<head> and injects the
    fragment into <body>, so meta tags emitted there are inert. A page served
    from our own domain has to carry its own head -- and for X specifically the
    OG/Twitter tags ARE the product: a link with no card gets scrolled past.
    og:image must be an absolute URL, which is why the domain had to be
    settled before this could be written.
    """
    s, l = p["stats"], p["defillama"]
    tvl = usd(l.get("tvl_current"))
    dau = num(s.get("dau_current"))
    desc = (f"Robinhood Chain at a glance — {tvl} TVL, {dau} daily active users, "
            f"{usd(l.get('stables_current'))} in stablecoins. Memecoin and NFT "
            f"leaderboards screened against two independent indexers, with every "
            f"headline cross-checked against raw chain data.")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HoodScout — Robinhood Chain</title>
<meta name="description" content="{escape(desc)}">
<link rel="canonical" href="{escape(base_url)}/">
{'' if public else '<meta name="robots" content="noindex,nofollow">'}

<meta property="og:type" content="website">
<meta property="og:site_name" content="HoodScout">
<meta property="og:title" content="HoodScout — Robinhood Chain">
<meta property="og:description" content="{escape(desc)}">
<meta property="og:url" content="{escape(base_url)}/">
<meta property="og:image" content="{escape(base_url)}/card.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="HoodScout — Robinhood Chain">
<meta name="twitter:description" content="{escape(desc)}">
<meta name="twitter:image" content="{escape(base_url)}/card.png">

{favicon_tags()}
<style>*{{box-sizing:border-box}}html,body{{margin:0;padding:0}}</style>
</head>
<body>
{fragment}
</body>
</html>"""


def card_html(p, logo=None):
    """A purpose-built 1200x630 social card.

    Deliberately not a crop of the dashboard -- the page's proportions read
    badly at 1.91:1, and the card's job is one number big enough to stop a
    scroll, not a miniature of the whole site.
    """
    s, l = p["stats"], p["defillama"]
    gen = dt.datetime.fromisoformat(p["generated_at"])
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0}}
body{{width:1200px;height:630px;background:#D2F53C;color:#0F100C;overflow:hidden;
  font-family:"Helvetica Neue","Arial Black",Helvetica,Arial,sans-serif;
  display:flex;flex-direction:column;justify-content:space-between;padding:62px 66px;}}
.top{{display:flex;justify-content:space-between;align-items:flex-start}}
.mark{{font-size:38px;font-weight:900;letter-spacing:-.035em;display:flex;align-items:center}}
.cardmark{{display:inline-flex;align-items:center;margin-right:14px}}
.cardmark,.cardmark svg,.cardmark img{{height:46px;width:auto;display:block;image-rendering:pixelated}}
.eyebrow{{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:16px;
  letter-spacing:.22em;text-transform:uppercase;font-weight:700;padding-top:9px}}
.label{{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:19px;
  letter-spacing:.2em;text-transform:uppercase;font-weight:700;margin-bottom:6px}}
.big{{font-size:158px;font-weight:900;letter-spacing:-.05em;line-height:.84;
  font-variant-numeric:tabular-nums}}
.row{{display:flex;gap:64px;border-top:3px solid #0F100C;padding-top:24px}}
.stat .k{{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:14px;
  letter-spacing:.16em;text-transform:uppercase;opacity:.72;margin-bottom:5px}}
.stat .v{{font-size:40px;font-weight:900;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums}}
</style></head><body>
  <div class="top">
    <div class="mark">{logo_html(logo, "cardmark")}HoodScout</div>
    <div class="eyebrow">Robinhood Chain &middot; {gen.strftime('%d %b %Y')}</div>
  </div>
  <div>
    <div class="label">Total value locked</div>
    <div class="big">{usd(l.get('tvl_current'))}</div>
  </div>
  <div class="row">
    <div class="stat"><div class="k">Daily active users</div>
      <div class="v">{num(s.get('dau_current'))}</div></div>
    <div class="stat"><div class="k">Stablecoins</div>
      <div class="v">{usd(l.get('stables_current'))}</div></div>
    <div class="stat"><div class="k">App fees 24h</div>
      <div class="v">{usd(l.get('app_fees_24h'))}</div></div>
    <div class="stat"><div class="k">Gas fees 24h</div>
      <div class="v">{usd(s.get('gas_fees_usd_current'))}</div></div>
  </div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Render the HoodScout dashboard")
    ap.add_argument("--data", default=str(OUT_DIR / "pulse.json"))
    ap.add_argument("--out", default=str(OUT_DIR / "dashboard.html"))
    ap.add_argument("--verify", default=str(OUT_DIR / "dune_verify.json"),
                    help="optional verify_dune.py report; the audit strip is "
                         "omitted entirely when it is absent")
    ap.add_argument("--site-dir", default=str(OUT_DIR / "site"),
                    help="standalone site for public hosting (full HTML document "
                         "with OG/Twitter tags, plus the social card source)")
    ap.add_argument("--base-url", default=SITE_URL,
                    help="absolute origin the OG tags point at")
    ap.add_argument("--public", action="store_true",
                    help="drop the noindex guard. The site is a staging surface "
                         "until launch: reachable by URL but barred from search "
                         "indexes. Pass this only when it is meant to be found.")
    ap.add_argument("--logo", default=None,
                    help=f"logo file to inline; defaults to {LOGO_PATH.name} beside "
                         "this script if present. SVG preferred — it inherits "
                         "currentColor and so works on both themes from one file.")
    args = ap.parse_args()

    pulse = json.loads(Path(args.data).read_text())
    vp = Path(args.verify)
    pulse["_verify"] = json.loads(vp.read_text()) if vp.exists() else None

    # Fragment: what the Artifact host wants (it supplies its own head).
    logo = load_logo(args.logo)
    html = render(pulse, logo)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Wrote {out} ({len(html):,} bytes)")

    # Standalone document + card source: what a real domain needs.
    site = Path(args.site_dir)
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(
        standalone(html, pulse, args.base_url, public=args.public))
    (site / "card.html").write_text(card_html(pulse, logo))
    print(f"Wrote {site / 'index.html'} and card.html (base {args.base_url})")


if __name__ == "__main__":
    main()
