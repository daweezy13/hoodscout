#!/usr/bin/env python3
"""
build_dashboard.py

Render out/pulse.json into a self-contained dashboard page ("HoodScan").

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
    """Trust Index from Trust Capital Markets — 50% Ethos, 50% log-scaled FDV.

    Only rendered when the project has a real Ethos score; an unrated profile
    would otherwise contribute a flat 1200 default and make the composite look
    like a measurement of nothing.
    """
    if not ti:
        return ""
    cls = GRADE_CLS.get(ti.get("grade"), "part")
    return (f'<span class="trust {cls}" title="Trust Index {ti["score"]} (grade '
            f'{ti["grade"]}) — Trust Capital Markets composite: 50% Ethos '
            f'({ti["trust_norm"]}) + 50% log-scaled FDV ({ti["fdv_norm"]})">'
            f'<span class="t-grade">{escape(ti["grade"])}</span>'
            f'<span class="t-score">{ti["score"]}</span></span>')


def ethos_chip(e):
    """Ethos credibility for the account a token CLAIMS, never for the token.

    The handle is always rendered beside the score. A social link is
    self-declared: SPCX on this chain points at @elonmusk and would otherwise
    inherit 1945/"reputable". Showing "@elonmusk" next to a SpaceX knockoff is
    what makes that visible instead of laundering it into a trust badge.
    """
    if not e:
        return ""
    if e.get("unrated"):
        cls, shown = "part", "unrated"
    else:
        cls = ETHOS_LEVEL.get((e.get("level") or "").lower(), "part")
        shown = str(e.get("score"))
    rv = e.get("reviews_positive", 0), e.get("reviews_negative", 0)
    detail = (f'{rv[0]}+ / {rv[1]}− reviews · {e.get("vouches", 0)} vouches · '
              f'profile {(e.get("status") or "unknown").lower()}')
    return (f'<a class="ethos {cls}" href="{escape(e.get("profile_url") or "#")}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'title="Ethos {escape(e.get("level") or "")} — @{escape(e["handle"])}. '
            f'{escape(detail)}. Scores the linked X account, which is self-declared, '
            f'not the token.">'
            f'<span class="e-score">{escape(shown)}</span>'
            f'<span class="e-at">@{escape(e["handle"])}</span></a>')


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
        out.append(f"""          <tr>
            <td class="rank">{i}</td>
            <td class="sym">
              <span class="sym-name">{dot}{link(t.get('url'), t.get('symbol') or '?', 'ext strong')}{badge}</span>
              <span class="sym-sub">{escape(num(t.get('holders')))} holders
                {trust_chip(t.get('trust_index'))}{ethos_chip(t.get('ethos'))}</span>
            </td>
            <td class="n">{escape(price(t.get('price_usd')))}
              <span class="alt {trend_class(chg)}">{escape(pct(chg))}</span></td>
            <td class="n">{escape(usd(mc))}
              <span class="alt" title="Fully diluted valuation">fdv {escape(usd(fdv))}</span></td>
            <td class="n strong">{escape(usd(t.get('volume_ranked')))}
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

        out.append(f"""          <tr>
            <td class="rank">{i}</td>
            <td class="sym">
              <span class="sym-name">{link(c.get('opensea_url'), name, 'ext strong')}{badge}</span>
              <span class="sym-sub">{escape(num(supply))} items ·
                {escape(num(c.get('holders')))} holders ·
                {link(c.get('explorer_url'), short_addr(c.get('address')), 'ext dim')}</span>
            </td>
            <td class="n" title="{escape(floor_tip)}">
              {escape(floor_val)}
              <span class="alt">{escape(floor_lbl)}</span></td>
            <td class="n">{escape(usd(c.get('avg_price_usd')))}
              <span class="alt">{escape(num(c.get('buyers')))} buyers</span></td>
            <td class="n strong">{escape(usd(c.get('volume_usd')))}
              <span class="alt">{escape(num(c.get('sales')))} sales</span></td>
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
        <div class="tile-head"><h3>{escape(label)}</h3>{chg}</div>
        <div class="tile-value">{escape(value)}</div>
        <div class="tile-sub">{escape(sub)}</div>
      </article>"""


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def render(p):
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
    tvl_date = tvl_pts[-1][0] if tvl_pts else "latest"

    # The three numbers that answer "is this chain alive right now", sat beside
    # the TVL hero. Stablecoin supply is a stock, the other two are daily flows.
    hero_tiles = "\n".join([
        tile("Stablecoin supply", usd(l.get("stables_current")),
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
        nft_window_note = (f"Window is {escape(str(n.get('window_label') or ''))} UTC, "
                           "the same complete day the chain vitals above use.")
    else:
        nft_window_note = ("Window is a rolling 24h from the latest block, so it is "
                           "not aligned to the UTC days the chain vitals use.")

    # Audit strip. Every headline used to have exactly one source and no second
    # opinion; verify_dune.py recomputes three of them from raw chain data with
    # an explicit definition. Rendered only when a report exists.
    v = p.get("_verify")
    audit = ""
    if v:
        vchecks = v.get("checks") or []
        sc = v.get("seaport_range_check") or {}
        if sc.get("verdict"):
            vchecks = vchecks + [sc]
        agreed = sum(1 for c in vchecks if c.get("verdict") == "agree")
        worst = max((c.get("pct_diff") or 0) for c in vchecks) if vchecks else 0
        vday = (v.get("generated_at") or "")[:10]
        cls = "ok" if agreed == len(vchecks) else "part"
        audit = f"""
  <div class="audit {cls}">
    <span class="audit-k">Independently verified</span>
    <span class="audit-v">{agreed}/{len(vchecks)} checks agree</span>
    <span class="audit-d">
      Daily active users, gas fees and Seaport fills recomputed from raw chain
      data on Dune and compared against the live sources — worst divergence
      {worst:.1f}%. Active users is <code>count(distinct sender)</code> per UTC
      day; it matches Blockscout exactly, which is what pins down that
      provider's otherwise undocumented definition. Checked {escape(vday)}.
    </span>
  </div>"""

    n_verified = sum(1 for c in n.get("collections", [])
                     if c.get("safelist_status") in ("verified", "approved"))
    os_note = (f"{n_verified} of {len(n.get('collections', []))} carry OpenSea "
               "verification; the rest never requested it."
               if has_opensea else
               "OpenSea verification badges appear here once an API key is configured.")

    payload = json.dumps({"charts": charts, "spanDays": span_days},
                         separators=(",", ":")).replace("</", "<\\/")

    return f"""<title>HoodScan — Robinhood Chain</title>
<style>
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
  --display:"Helvetica Neue","Helvetica Now Display","Arial Black",Helvetica,Arial,sans-serif;
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
.wrap {{ max-width:1240px; margin:0 auto; padding:30px 24px 76px; }}
a.ext {{ color:inherit; text-decoration:none; border-bottom:1px solid var(--accent-soft); }}
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
  font-family:var(--display); font-size:clamp(52px,10vw,104px); line-height:.86;
  letter-spacing:-.045em; font-weight:900; margin:0; text-wrap:balance;
  color:var(--ink);
}}
.kicker {{
  font-family:var(--mono); font-size:11px; letter-spacing:.2em; text-transform:uppercase;
  color:var(--muted); padding-bottom:12px;
}}
.lede {{
  max-width:60ch; color:var(--ink-2); margin:0 0 34px;
  font-family:var(--display); font-weight:600;
  font-size:clamp(17px,2.1vw,22px); line-height:1.32; letter-spacing:-.014em;
}}

h2 {{
  font-family:var(--mono); font-size:11.5px; letter-spacing:.2em; text-transform:uppercase;
  color:var(--ink); font-weight:700; margin:0 0 6px;
  display:flex; align-items:center; gap:12px;
}}
h2::after {{ content:""; flex:1; height:var(--rule-w); background:var(--rule); }}

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
  box-shadow:10px 10px 0 var(--on-accent);
  padding:30px 30px 26px; display:flex; flex-direction:column; justify-content:center;
  min-height:230px;
}}
.hero-label {{
  font-family:var(--mono); font-size:11.5px; letter-spacing:.2em;
  text-transform:uppercase; font-weight:700; margin-bottom:14px;
}}
.hero-value {{
  font-family:var(--display); font-weight:900; letter-spacing:-.045em; line-height:.88;
  font-size:clamp(52px,8.5vw,92px); font-variant-numeric:tabular-nums;
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
.sec-sub {{ color:var(--muted); font-size:13px; margin:0 0 16px; max-width:74ch; }}
section {{ margin-top:44px; }}

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
  box-shadow:8px 8px 0 var(--shadow); }}
@media (min-width:860px) {{ .charts {{ grid-template-columns:1fr 1fr; }} }}
.chart {{ background:var(--panel); padding:16px 18px 12px; min-width:0; }}
.chart-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; }}
.chart h3 {{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.11em; text-transform:uppercase;
  color:var(--muted); font-weight:600; margin:0;
}}
.chart-val {{
  font-family:var(--mono); font-size:25px; font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; margin:4px 0 2px;
}}
.chart-delta {{
  font-family:var(--mono); font-size:11px; font-variant-numeric:tabular-nums;
  display:flex; align-items:baseline; gap:8px; min-height:16px;
}}
.agg-label {{ color:var(--muted); font-size:10px; letter-spacing:.04em; }}
.plot {{ position:relative; margin-top:10px; }}
.plot svg {{ display:block; width:100%; height:190px; overflow:visible; }}
.grid-line {{ stroke:var(--line); stroke-width:1; }}
.axis-txt {{ fill:var(--muted); font-family:var(--mono); font-size:9.5px; }}
.crosshair {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; opacity:0; }}
.hover-dot {{ opacity:0; }}
.tip {{
  position:absolute; pointer-events:none; opacity:0; transform:translate(-50%,-100%);
  background:var(--panel-2); border:1px solid var(--line); border-radius:3px;
  padding:7px 10px; font-family:var(--mono); font-size:11px; white-space:nowrap;
  color:var(--ink); z-index:5; box-shadow:0 4px 14px rgba(0,0,0,.22);
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
  overflow:hidden; margin-top:16px; box-shadow:8px 8px 0 var(--shadow); }}
.tile {{ background:var(--panel); padding:14px 18px; }}
.tile-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
.tile h3 {{ font-family:var(--mono); font-size:10px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--muted); font-weight:600; margin:0; }}
.tile-value {{ font-family:var(--mono); font-size:21px; font-weight:600;
  font-variant-numeric:tabular-nums; letter-spacing:-.02em; margin-top:3px; }}
.tile-sub {{ font-size:11.5px; color:var(--muted); }}
.chg {{ font-family:var(--mono); font-size:11px; font-weight:600; font-variant-numeric:tabular-nums; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat {{ color:var(--muted); }}

/* ---- stablecoin composition ---- */
.comp {{ background:var(--panel); border:var(--rule-w) solid var(--rule);
  padding:20px; box-shadow:8px 8px 0 var(--shadow); }}
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
  overflow:hidden; box-shadow:8px 8px 0 var(--shadow); }}
.scroll {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
thead th {{
  font-family:var(--mono); font-size:10px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink); font-weight:700; text-align:right; padding:11px 14px;
  border-bottom:var(--rule-w) solid var(--rule); background:var(--panel-2);
  white-space:nowrap; letter-spacing:.13em;
}}
thead th:first-child, thead th:nth-child(2) {{ text-align:left; }}
tbody td {{ padding:9px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }}
tbody tr:last-child td {{ border-bottom:none; }}
tbody tr:hover {{ background:var(--panel-2); }}
td.n {{ text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.n.strong {{ font-weight:600; }}
td.dim {{ color:var(--muted); }}
td.rank {{ font-family:var(--mono); font-size:11px; color:var(--muted); width:34px;
  font-variant-numeric:tabular-nums; }}
td.sym {{ display:flex; flex-direction:column; gap:1px; min-width:200px; }}
.sym-name {{ font-weight:600; font-size:13.5px; display:flex; align-items:center; }}
.sym-name.rejected {{ color:var(--reject); }}
.sym-addr {{ font-family:var(--mono); font-size:10.5px; color:var(--muted); }}
td.sym {{ gap:3px; }}
.badge {{
  font-family:var(--mono); font-size:9px; letter-spacing:.07em; text-transform:uppercase;
  padding:2px 5px; border-radius:2px; margin-left:7px; font-weight:600; white-space:nowrap;
}}
.badge.flag {{ color:var(--flag); background:var(--flag-soft); }}
.badge.ok {{ color:var(--accent-ink); background:var(--accent-soft); }}
.badge.warn {{ color:var(--muted); background:var(--panel-2); border:1px solid var(--line); }}
td.reason {{ color:var(--muted); font-size:12px; }}
.corrob {{ display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:7px; flex:none; }}
.corrob.ok {{ background:var(--accent); }}
.corrob.part {{ background:var(--flag); }}
.corrob.bad {{ background:var(--reject); }}
.agrees {{ color:var(--accent-ink); font-size:11px; }}
.alt {{
  display:block; font-size:10px; color:var(--muted); font-weight:400;
  letter-spacing:.02em; margin-top:1px;
}}
.alt.up {{ color:var(--up); }} .alt.down {{ color:var(--down); }}
td.n {{ line-height:1.3; }}

.two-col {{ display:grid; grid-template-columns:1fr; gap:40px; align-items:stretch; }}
@media (min-width:1040px) {{ .two-col {{ grid-template-columns:1fr 1fr; gap:28px; }} }}
.two-col > section {{ margin-top:0; display:flex; flex-direction:column; min-width:0; }}
/* keep both board headers on the same baseline even though the blurbs differ */
.two-col .sec-sub {{ min-height:6.2em; }}   /* 4 lines at 1.55 line-height */
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
.board-foot .foot-r {{ margin-left:auto; }}

/* Trust Index chip — grade + composite score */
.trust {{
  display:inline-flex; align-items:center; gap:4px; margin-left:8px;
  padding:1px 6px; border-radius:2px; font-family:var(--mono); font-size:9.5px;
  border:1px solid transparent;
}}
.trust .t-grade {{ font-weight:700; }}
.trust .t-score {{ opacity:.75; }}
.trust.ok {{ color:var(--accent-ink); background:var(--accent-soft); border-color:var(--accent-soft); }}
.trust.part {{ color:var(--flag); background:var(--flag-soft); border-color:var(--flag-soft); }}
.trust.bad {{ color:var(--reject); background:var(--reject-soft); border-color:var(--reject-soft); }}

/* Ethos chip — score and handle are inseparable by design */
.ethos {{
  display:inline-flex; align-items:center; gap:5px; margin-left:8px;
  padding:1px 6px 1px 5px; border-radius:2px; text-decoration:none;
  font-family:var(--mono); font-size:9.5px; border:1px solid transparent;
}}
.ethos .e-score {{ font-weight:700; }}
.ethos .e-at {{ color:var(--muted); }}
.ethos.ok {{ color:var(--accent-ink); background:var(--accent-soft); border-color:var(--accent-soft); }}
.ethos.part {{ color:var(--ink-2); background:var(--panel-2); border-color:var(--line); }}
.ethos.bad {{ color:var(--reject); background:var(--reject-soft); border-color:var(--reject-soft); }}
.ethos:hover {{ border-color:currentColor; }}

.sym-sub {{
  font-family:var(--mono); font-size:10.5px; color:var(--muted);
  display:flex; align-items:center; flex-wrap:wrap; gap:5px;
}}
footer {{ margin-top:56px; padding-top:20px; border-top:1px solid var(--line);
  font-size:12.5px; color:var(--muted); max-width:78ch; }}
footer h4 {{ font-family:var(--mono); font-size:10px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-2); margin:0 0 8px; font-weight:600; }}
footer p {{ margin:0 0 10px; }}
footer code {{ font-family:var(--mono); font-size:11.5px; color:var(--ink-2); }}
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
    <h1>HoodScan</h1>
    <span class="kicker">Robinhood Chain, end to end</span>
  </div>
  <p class="lede">
    {span_days} days of chain history, screened against two independent indexers,
    priced from DexScreener and measured from Seaport fills on-chain.
  </p>
  <a class="contract-link" href="https://robinhoodchain.blockscout.com/" target="_blank"
     rel="noopener noreferrer">View explorer <span aria-hidden="true">&#8599;</span></a>

  <div class="hero">
    <div class="hero-card">
      <span class="hero-label">Total value locked</span>
      <span class="hero-value">{usd(l.get('tvl_current'))}</span>
      <span class="hero-meta">
        Robinhood Chain &middot; DefiLlama &middot; {escape(tvl_date)}
        &middot; {pct(l.get('tvl_change_7d_pct'))} 7d
      </span>
    </div>
    <div class="hero-side">
{hero_tiles}
    </div>
  </div>
{audit}

  <section>
    <h2>Chain vitals</h2>
    <p class="sec-sub">
      Hover any plot for exact values. Daily figures use the last complete day —
      a bucket still filling is held out of the headline even when the calendar
      says the day is over.
    </p>
    <div class="ranges" id="ranges" role="group" aria-label="Time range"></div>
    <p class="range-note" id="rangeNote"></p>
    <div class="charts" id="charts"></div>
    <div class="tiles">
{tiles}
    </div>
  </section>

  <section>
    <h2>Stablecoins on the chain</h2>
    <p class="sec-sub">
      Which assets make up the {usd(l.get('stables_current'))} of stablecoin supply.
      There is no real USDC or USDT here — name-searching either returns only
      copycats, so this list is the whole picture, not a top slice.
    </p>
    <div class="comp">
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
        DexScreener figures behind a {usd_exact(floor)} liquidity floor, cross-checked
        against GeckoTerminal — {n_ok} of {n_shown} agree. Projects that link an X
        account carry a Trust Index (Trust Capital Markets: 50% Ethos credibility,
        50% log-scaled FDV) and the handle it was derived from — that scores
        <em>the linked account</em>, which is self-declared, not the token.
      </p>
      <div class="board"><div class="scroll">
        <table>
          <thead><tr>
            <th></th><th>Token</th><th>Price</th><th>Mkt cap</th><th>Volume 24h</th>
          </tr></thead>
          <tbody>
{meme_rows(m.get('tokens', []))}
          </tbody>
        </table>
      </div>
      <div class="board-foot">
        <span><i class="corrob ok"></i>agree</span>
        <span><i class="corrob part"></i>3–20x apart</span>
        <span><i class="corrob bad"></i>disputed</span>
        <span class="foot-r">{n_excl} more screened out below the {usd_exact(floor)} floor</span>
      </div></div>
    </section>

    <section>
      <h2>Top NFT collections</h2>
      <p class="sec-sub">
        By real paid Seaport fills over {nft_window}h — a measure airdrops cannot
        inflate. {nft_window_note} {os_note}
      </p>
      <div class="board"><div class="scroll">
        <table>
          <thead><tr>
            <th></th><th>Collection</th><th>{floor_hdr}</th><th>Avg</th><th>Volume {nft_window}h</th>
          </tr></thead>
          <tbody>
{nft_rows(n.get('collections', []))}
          </tbody>
        </table>
      </div>
      <div class="board-foot">
        <span>{num(n.get('logs_scanned'))} Seaport fills decoded</span>
        <span class="foot-r">{escape(usd(n.get('total_volume_usd')))} total</span>
      </div></div>
    </section>
  </div>


  <footer>
    <h4>Method &amp; caveats</h4>
    <p>
      TVL, stablecoins and app-level fees come from DefiLlama's chain-level endpoints.
      Active users and gas fees come from Blockscout's stats service. Memecoins use
      DexScreener as the primary source, cross-checked against GeckoTerminal, which is
      also what enumerates candidates in the first place. NFT volume is computed here by
      decoding Seaport <code>OrderFulfilled</code> logs straight from the chain RPC.
    </p>
    <p>
      <strong>Fees are two separate numbers.</strong> Gas fees are paid to the chain;
      app fees are earned by protocols on it. They differ by roughly 30x and answer
      different questions, so they are never summed.
    </p>
    <p>
      <strong>The stablecoin composition chart is an approximation.</strong> DefiLlama
      publishes per-chain stablecoin history only in aggregate and the per-asset split
      only as of now, so the split is held constant backwards. The totals are measured;
      the division between USDG and USDe on past dates is not.
    </p>
    <p>
      Daily granularity is the finest the chain's stats API offers — there is no hourly
      resolution, so ranges shorter than a week cannot be plotted. Contract names come
      from Blockscout and are <em>not</em> proof of authenticity: a matching name is
      exactly what a copycat is built to have.
    </p>
    <p>Snapshot taken {escape(gen_str)}. Figures are only as fresh as that.</p>
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

  host.innerHTML = `<svg viewBox="0 0 ${{W}} ${{H}}" role="img" aria-label="${{cfg.title}}">${{svg}}</svg>`
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
    tip.style.left = (X(i)/W*bb.width) + 'px';
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
</script>
"""


def main():
    ap = argparse.ArgumentParser(description="Render the HoodScan dashboard")
    ap.add_argument("--data", default=str(OUT_DIR / "pulse.json"))
    ap.add_argument("--out", default=str(OUT_DIR / "dashboard.html"))
    ap.add_argument("--verify", default=str(OUT_DIR / "dune_verify.json"),
                    help="optional verify_dune.py report; the audit strip is "
                         "omitted entirely when it is absent")
    args = ap.parse_args()

    pulse = json.loads(Path(args.data).read_text())
    vp = Path(args.verify)
    pulse["_verify"] = json.loads(vp.read_text()) if vp.exists() else None
    html = render(pulse)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Wrote {out} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
