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

import re
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


ETHOS_MARK = ('<svg viewBox="0 0 512 512" aria-hidden="true" focusable="false">'
              '<path fill="currentColor" fill-rule="evenodd" d="M255.38 255.189a254.98 '
              '254.98 0 0 1-1.935 31.411H101v62.2h136.447a251.522 251.522 0 0 1-35.932 '
              '62.2H411v-62.2H237.447a250.584 250.584 0 0 0 15.998-62.2H411v-62.2H253.521a250.604 '
              '250.604 0 0 0-15.826-62.2H411V100H202.003a251.526 251.526 0 0 1 35.692 '
              '62.2H101v62.2h152.521a255 255 0 0 1 1.859 30.789Z" clip-rule="evenodd"/></svg>')

X_MARK = ('<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
          '<path fill="currentColor" d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17'
          'l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 '
          '17.52h1.833L7.084 4.126H5.117z"/></svg>')


def social_links(ethos, handle=None):
    """Ethos + X marks for a project.

    Takes the handle separately because NFT collections declare one on OpenSea
    (`twitter_username`) even when no Ethos profile exists for it — in that
    case the X link still renders and the Ethos mark is simply omitted, rather
    than dropping both.

    Either way the handle is SELF-DECLARED: it identifies a claim, not a
    verified owner, which the tooltip says out loud.
    """
    handle = handle or (ethos or {}).get("handle")
    if not handle:
        return ""
    h = escape(handle)
    out = ""
    if ethos and ethos.get("profile_url"):
        out += (f'<a class="sm" href="{escape(ethos["profile_url"])}" target="_blank" '
                f'rel="noopener noreferrer" aria-label="Ethos profile for @{h}" '
                f'title="Ethos profile for @{h}">{ETHOS_MARK}</a>')
    out += (f'<a class="sm" href="https://x.com/{h}" target="_blank" rel="noopener noreferrer" '
            f'aria-label="@{h} on X" title="@{h} on X — declared by the project, '
            f'not verified">{X_MARK}</a>')
    return out


def meme_rows(tokens):
    out = []
    for i, t in enumerate(tokens, 1):
        badge = ('<span class="badge flag" title="24h volume is more than 75x pool '
                 'liquidity — thin book">thin</span>' if t.get("flagged") else "")
        chg = t.get("price_change_24h")
        fdv = t.get("fdv")
        mc = t.get("market_cap")
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape((t.get('symbol') or '?').lower())}">
              <span class="sym-name">{link(t.get('url'), t.get('symbol') or '?', 'ext strong')}{badge}</span>
              <span class="sym-sub">{escape(num(t.get('holders')))} holders
                {social_links(t.get('ethos'))}</span>
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
              <span class="sym-name" title="{escape(name)}">{link(c.get('opensea_url'), name, 'ext strong')}{badge}</span>
              <span class="sym-sub">{link(c.get('explorer_url'), f"{num(supply)} items", 'ext dim')} ·
                {escape(num(c.get('holders')))} holders{social_links(c.get('ethos'), c.get('x_handle'))}</span>
            </td>
            <td class="n" data-v="{c.get('floor_price_usd') or c.get('min_sale_usd') or 0}" title="{escape(floor_tip)}">{escape(floor_val)}
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



# Contract GENERATIONS of one project, from its own published docs
# (stonkbrokers.cash/docs#abi). Clock In v1 is retired and Clock In v2 is the
# active payer, but both paid the same holders in the same equities, so the
# project's total distributed is the sum. Listing them as separate rows put a
# RETIRED contract at the top of the board as though it were the chain's biggest
# active payer, while the live one sat five rows below under a different name.
#
# Keyed by address, and deliberately narrow: there is no general way to know two
# contracts are the same project, so this only ever holds pairs a project has
# documented itself.
# A retired generation whose total the project publishes but whose contract
# address it does not. Added to the live contract's measured total so the row
# reflects the project's whole history, and labelled "published" so the two
# halves stay distinguishable -- one is measured from chain logs, one is taken
# on the project's word. A retired contract's total no longer moves, so a
# constant is safe here in a way it would not be for a live one.
#
# QUOTRONS v1: $113,849.84, from quotrons.cash. Their v2 figure counts WETH
# CONVERTED IN (29.3 WETH) while ours counts stock actually delivered out, so
# the two legs are not like-for-like; Dave's call is to sum them regardless.
PUBLISHED_PRIOR = {
    "0xe04fba61fd54ba78dd450a30d8af40167af5d3ec": {
        "name": "QUOTRONS", "usd": 113_849.84, "label": "v1 published",
        "live_label": "v2 measured",
    },
}

PROJECT_GENERATIONS = {
    "0x038a7f4e4e89448ad74e044337c9ac25c11e726b": ("StonkBrokers", "v1 retired"),
    "0x1f12fe622c11947f93f53d63f68f7f46b6d081c9": ("StonkBrokers", "v2 active"),
}


def merge_generations(projects):
    """Fold a project's contract generations into one row, summing what it paid."""
    out, byname = [], {}
    for p in projects:
        name, gen = PROJECT_GENERATIONS.get((p.get("address") or "").lower(), (None, None))
        if not name:
            out.append(p)
            continue
        if name not in byname:
            # Seed with the accumulators ZEROED, then add every generation
            # including this one. Copying the first row and adding it to itself
            # counted it twice -- $289.0K + $289.0K + $65.7K = $643.7K.
            q = dict(p)
            q.update(name=name, gens=[], distributed_usd=0.0, pending_usd=0.0,
                     holders=0, assets=[], asset_symbols=[])
            byname[name] = q
            out.append(q)
        q = byname[name]
        q["distributed_usd"] += (p.get("distributed_usd") or 0)
        q["pending_usd"] += (p.get("pending_usd") or 0)
        q["holders"] = max(q["holders"], p.get("holders") or 0)
        seen = {a["address"] for a in q["assets"]}
        q["assets"] += [a for a in (p.get("assets") or []) if a["address"] not in seen]
        q["asset_symbols"] = sorted(set(q["asset_symbols"]) | set(p.get("asset_symbols") or []))
        q["gens"].append(gen)
        # the live contract is the one worth linking to
        if gen and gen.startswith("v2"):
            q["explorer_url"] = p.get("explorer_url") or q.get("explorer_url")
            q["idle_days"] = p.get("idle_days")
    # Fold in a published prior generation where the project reports one.
    for p in out:
        prior = PUBLISHED_PRIOR.get((p.get("address") or "").lower())
        if not prior:
            continue
        p["name"] = prior["name"]
        p["distributed_usd"] = (p.get("distributed_usd") or 0) + prior["usd"]
        p["gens"] = [prior["label"], prior["live_label"]]

    for q in byname.values():
        q["assets"].sort(key=lambda a: -(a.get("usd") or 0))
    out.sort(key=lambda x: -(x.get("distributed_usd") or 0))
    return out


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
        last = p.get("last") or ""
        idle = ""
        if last:
            try:
                days = (dt.date.today() - dt.date.fromisoformat(last)).days
                if days >= 3:
                    idle = (f'<span class="badge warn" title="Last payout {escape(last)} — '
                            f'{days} days ago">idle {days}d</span>')
            except ValueError:
                pass
        # Wage pools have no drop count -- they pay continuously rather than in
        # discrete drops -- so the clause is omitted rather than rendered "-- drops".
        drops = f" \u00b7 {num(p['drops'])} drops" if p.get("drops") else ""
        # Say plainly that a row is two contracts summed, so the total is
        # checkable rather than mysterious.
        if p.get("gens"):
            drops += " \u00b7 " + " + ".join(p["gens"])
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape((p.get('name') or '').lower())}">
              <span class="sym-name">{link(p.get('explorer_url'), p.get('name') or '?', 'ext strong')}{idle}</span>
              <span class="sym-sub">{escape(num(p.get('holders')))} holders{escape(drops)}</span>
            </td>
            <td class="n basket" data-v="{len(p.get('assets') or [])}">{chips}</td>
            <td class="n strong" data-v="{p.get('distributed_usd') or 0}">{escape(usd(p.get('distributed_usd')))}
              <span class="alt">{len(p.get('assets') or [])} assets</span></td>
            <td class="n pend" data-v="{p.get('pending_usd') or 0}">{escape(usd(p.get('pending_usd'))) if (p.get('pending_usd') or 0) >= 1 else '&mdash;'}
              <span class="alt">{'unclaimed' if (p.get('pending_usd') or 0) >= 1 else ''}</span></td>
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
    # Prefer a 6h view — Dave wants recent movement, not a day's worth. But
    # Actions cron throttling means a quiet stretch can leave 6h with nothing
    # plottable, and the chart rendered empty. Widen only as far as needed.
    for _win in (6, 12, 24):
        lxc = load_launch_traces(hours=_win)
        if len(lxc.get("traces") or []) >= 12:
            break
    _lc = lxc.get("counts") or {}

    nftl = load_nft_launches()
    _nc = nftl.get("counts") or {}
    nft_note = (
        f"{_nc.get('public', 0)} reached a real spread of wallets, "
        f"{_nc.get('farm', 0)} were minted almost entirely by a handful, "
        f"{_nc.get('thin', 0)} are still thin. Ranked by distinct minters, not "
        "mint count &mdash; mint count alone cannot tell a public launch from a farm."
        if nftl.get("collections") else
        "The mint scanner is warming up.")

    summary_html = chain_summary(s, l, n, m, rw, lxc, tvl_headline)
    padx = launchpad_index(tokens=p.get("launchpad_tokens"),
                           tvl=l.get("tvl_current"))
    _lead = (padx.get("pads") or [{}])[0]
    pad_note_idx = (
        f"Every launchpad's {padx.get('top_n', 10)} biggest live coins, valued together &mdash; "
        f"the top ten is the filter, so how many a pad launches does not flatter it. "
        f"Counts a coin only for the pad it launched on, and only while it still holds a "
        f"market. Excluded: tokens already valuable when they opened a pool, plus "
        f"stablecoins, wrapped majors and the tokenised equities &mdash; those are new "
        f"pools, not new coins. Equities are matched by address, so a copycat memecoin "
        f"using a stock ticker still counts as the launch it is."
        if padx.get("pads") else "The ledger is still filling.")
    nav_html = nav_cards(s, l, n, m, rw, lxc, nftl, padx)

    pad_note = ("Launchpads: " + " &middot; ".join(
        f"{escape(k)} {v}" for k, v in (lxc.get("pads") or [])[:5]) + "."
        if lxc.get("pads") else "")
    launch_note = (
        f"The {len(lxc.get('traces', []))} biggest movers of "
        f"{num(lxc.get('total_pools', 0))} pools launched in the window, each averaged "
        f"by the hour and plotted as a multiple of its own launch value &mdash; so a "
        f"small launch that ran and a big one that died sit on the same scale. "
        + pad_note
        if lxc.get("traces") else
        "The poller is warming up &mdash; trajectories appear once pools have been "
        "observed more than once.")

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

    lx = load_launch_traces()
    payload = json.dumps({"charts": charts, "spanDays": span_days, "launch": lx},
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
  /* Grotesk, not the pixel face. Tried Silkscreen here twice; even with extra
     tracking it does not hold up at headline size for a number people are
     meant to read at a glance. The pixel face stays on section identity only,
     where the strings are short and recognisable rather than parsed. */
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
/* The hero's right panel is sized by the column beside it (TVL card + summary),
   so its three tiles inherit whatever height is left over and were floating in
   it at the same 21px the dense Chain-vitals grid uses. Equal rows plus vertical
   centring, and type scaled to the space it actually occupies. Scoped to
   .hero-side so the tile grid further down the page is untouched. */
.hero-side {{ grid-auto-rows:1fr; }}
.hero-side .tile {{
  padding:18px 20px; display:flex; flex-direction:column;
  justify-content:center; gap:5px;
}}
.hero-side .tile-label {{ font-size:11.5px; letter-spacing:.15em; }}
.hero-side .tile-value {{ font-size:clamp(26px, 2.6vw, 36px); line-height:1.05; }}
.hero-side .tile-sub {{ font-size:13px; }}
.hero-side .tile-head {{ margin-bottom:2px; }}

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
/* The NFT board carries one more numeric column than the memecoin board and
   overflowed its scroller by 21px, clipping Volume — the column the ranking
   is based on. Tighter name column and padding in the two-up context only. */
.two-col td.sym {{ min-width:162px; }}

/* The hero is two columns: TVL + its reading on the left, the other on-chain
   metrics on the right. The summary is a sibling card of the metrics panel,
   not a full-width paragraph under both -- it carries the same surface, rule
   and notch, and flex:1 makes it absorb the leftover height so the two columns
   finish level whatever the TVL card's clamped type does. */
.hero-main {{ display:flex; flex-direction:column; gap:22px; min-width:0; }}
.summary {{
  flex:1; margin:0; padding:16px 20px 18px;
  background:var(--panel); border:var(--rule-w) solid var(--rule);
  display:flex; flex-direction:column; justify-content:center;
  font-size:15.5px; line-height:1.6; color:var(--ink-2); text-wrap:pretty;
}}
/* ---- floating jump dock ---- */
.jumpdock {{
  /* Sits in the gutter beside the 1232px content column. A wider button would
     overlap the right-aligned value columns of the boards below. */
  position:fixed; right:8px; top:50%; transform:translateY(-50%);
  z-index:40; display:flex; align-items:center; gap:8px;
}}
/* The dock is a vertical bar: back-to-top above, section menu below. */
.jump-stack {{ display:flex; flex-direction:column; gap:6px; }}
.jump-btn, .jump-top {{
  width:34px; height:34px; flex:none; cursor:pointer; padding:0;
  display:flex; align-items:center; justify-content:center;
  background:var(--panel); border:var(--rule-w) solid var(--rule);
  color:var(--ink); font-family:var(--mono); font-size:17px; line-height:1;
  transition:background .12s ease;
}}
.jump-btn:hover, .jump-top:hover {{ background:var(--accent); color:var(--on-accent); }}
.jump-btn:focus-visible, .jump-top:focus-visible {{
  outline:2px solid var(--accent); outline-offset:2px; }}
/* Hidden until there is something to scroll back from.
   Uses display, not a height collapse: a zero-height button still paints its
   borders and glyph outside the box, which left two stray bars stacked above
   the menu button. */
.jump-top {{ display:none; }}
.jumpdock.scrolled .jump-top {{ display:flex; }}
/* Opens leftward from the button. Collapsed it is inert and unreachable by
   keyboard; visibility (not just opacity) is what takes it out of the tab
   order, and the delay lets the fade finish before it disappears. */
.jump-menu {{
  display:flex; flex-direction:column; min-width:150px;
  background:var(--panel); border:var(--rule-w) solid var(--rule);
  opacity:0; visibility:hidden; transform:translateX(6px);
  transition:opacity .13s ease, transform .13s ease, visibility 0s .13s;
}}
.jumpdock.open .jump-menu {{
  opacity:1; visibility:visible; transform:none; transition-delay:0s;
}}
.jump-menu a {{
  font-family:var(--mono); font-size:11px; letter-spacing:.02em;
  padding:9px 12px; text-decoration:none; color:var(--ink); white-space:nowrap;
  border-bottom:1px solid var(--line);
}}
.jump-menu a:last-child {{ border-bottom:none; }}
.jump-menu a:hover {{ background:var(--accent-soft); }}
.jump-menu a.here {{ color:var(--accent-ink); font-weight:700; }}
.jump-menu a.here::before {{ content:"\2192 "; }}
@media (prefers-reduced-motion:reduce) {{
  .jump-btn, .jump-top, .jump-menu {{ transition:none; }}
}}
/* Narrow screens: sit low-right, out of the way of a thumb reading the page. */
@media (max-width:700px) {{
  .jumpdock {{ top:auto; bottom:16px; transform:none; }}
}}

/* Anchored sections need headroom or the heading lands flush against the
   viewport edge and reads as cut off. */
section[id] {{ scroll-margin-top:18px; }}

.idxbar {{ height:5px; background:var(--panel-2); margin-top:6px; width:100%; }}
.idxbar i {{ display:block; height:100%; background:var(--accent);
  border-right:1px solid var(--on-accent); }}
.board td.pend {{ color:var(--ink-2); }}
.board td.pend .alt {{ color:var(--muted); }}
.summary-label {{
  font-family:var(--mono); font-size:10px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--muted); margin-bottom:10px;
}}
.summary p {{ margin:0; }}
/* The numbers ARE the content, so they carry the accent and the page's mono
   face; the prose around them stays recessive so the figures read first. */
.summary b {{
  color:var(--ink); font-weight:700;
  font-family:var(--mono); font-size:15px;
  font-variant-numeric:tabular-nums;
  background:var(--accent-soft); padding:1px 5px; border-radius:2px;
  white-space:nowrap;
}}
@media (max-width:700px) {{ .summary {{ font-size:14.5px; padding:14px 16px; }} }}

/* ---- 24h launch summary cards ---- */
.lx-cards {{
  display:grid; gap:14px; margin:0 0 6px;
  grid-template-columns:repeat(4, minmax(0, 1fr));
}}
.lx-card {{
  background:var(--panel); border:1px solid var(--line);
  padding:14px 14px 12px; min-width:0;
  display:flex; flex-direction:column; gap:2px;
}}
.lx-card .k {{
  font-family:var(--mono); font-size:10px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--muted);
}}
.lx-card .v {{
  font-size:34px; line-height:1.05; font-weight:700;
  font-variant-numeric:tabular-nums; color:var(--ink);
}}
/* The callout names the pool behind the count. Its own line, clipped rather
   than wrapped, so a long symbol cannot change the card's height and break
   the row's alignment. */
.lx-card .c {{
  font-family:var(--mono); font-size:11px; color:var(--ink-2);
  margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}}
.lx-card .c b {{ font-weight:700; }}
.lx-card .c.none {{ color:var(--muted); }}
.lx-card.win .v {{ color:{TRACE_ALIVE}; }}
.lx-card.lose .v, .lx-card.dead .v {{ color:{TRACE_DRAINED}; }}
.lx-rug {{
  font-family:var(--mono); font-size:11px; color:var(--ink-2);
  margin:0 0 16px; padding:9px 12px;
  background:var(--flag-soft); border-left:3px solid var(--flag);
}}
.lx-rug b {{ color:var(--ink); }}
@media (max-width:760px) {{
  .lx-cards {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }}
}}
.launch {{ background:var(--panel); border:var(--rule-w) solid var(--rule); padding:14px 16px 10px; }}
.launch-head {{ display:flex; flex-wrap:wrap; align-items:flex-start;
  justify-content:space-between; gap:10px 18px; margin-bottom:8px; }}
.launch-k {{ display:inline-block; font-family:var(--mono); font-size:10.5px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--ink); font-weight:700; }}
.launch-sub {{ display:block; font-family:var(--mono); font-size:10px; color:var(--muted); margin-top:3px; }}
.lx-legend {{ margin-top:0; align-items:center; }}
.lx-legend i.i-mute {{ background:var(--muted); opacity:.55; }}
.lx-legend button.more {{ margin-left:4px; }}
.lx-shape {{ color:var(--muted); }}
.lx-shape b {{ color:var(--ink-2); margin-right:3px; font-weight:400; }}
.launch-plot {{ position:relative; }}
.launch-plot canvas {{ display:block; width:100%; height:300px; }}
@media (max-width:700px) {{ .launch-plot canvas {{ height:230px; }} }}

.sm {{
  display:inline-flex; align-items:center; color:var(--muted);
  margin-left:7px; text-decoration:none; border-bottom:none; flex:none;
}}
.sm svg {{ width:11px; height:11px; display:block; }}
.sm:hover {{ color:var(--accent-ink); }}
.sm:focus-visible {{ outline:2px solid var(--accent-ink); outline-offset:2px; }}
.sym-sub {{ white-space:nowrap; line-height:1.3; }}
/* Every NFT row carries a verification badge and no memecoin row does, which
   made the name line 2px taller and desynced the two boards. Pin both lines
   so the row rhythm is identical regardless of what chips a row happens to
   carry. */
.two-col .sym-name {{ line-height:1.35; }}
/* Pin the row box outright. Chasing individual contributors (badges, alt
   lines, differing column counts) kept leaving a 2px drift between the two
   boards; a fixed row height makes the rhythm identical by construction. */
.two-col tbody td {{ vertical-align:middle; }}
.two-col .badge {{ line-height:1.25; }}
.two-col tbody td, .two-col thead th {{ padding-left:9px; padding-right:9px; }}
td.sym > * {{ display:block; }}
.sym-name {{ font-weight:600; font-size:13.5px; display:flex; align-items:center; min-width:0; }}
/* Long collection names ("Boomer Stockholders") wrapped to two lines and made
   one row 74px against 53 everywhere else. Truncate instead so both boards
   keep an identical row rhythm; the full name stays in the link title. */
.sym-name > a {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }}
.sym-name .badge {{ flex:none; }}
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
/* Payout boards STACK rather than sitting side by side. The NFT board carries
   five columns now (collection, assets, distributed, pending) and at half width
   `table-layout:fixed` starved the name column down to a single letter --
   "StockBooster" rendered as "S". Full width is what a five-column table needs;
   the memecoin board above it is unharmed by the extra room. */
.reward-stack {{ display:flex; flex-direction:column; gap:26px; }}
.reward-stack .sub-head {{ margin-top:0; min-height:auto; }}
.reward-stack table {{ width:100%; }}
.reward-cols .sub-head {{ margin-top:0; min-height:auto; }}
/* max-width on a <td> is ignored unless the table is fixed-layout, which is
   why the asset chips kept overflowing into the Value column. */
.reward-cols table {{ table-layout:fixed; width:100%; }}
.reward-cols th:nth-child(1), .reward-cols td:nth-child(1) {{ width:32px; }}
.reward-cols th:nth-child(3), .reward-cols td:nth-child(3) {{ width:36%; }}
.reward-cols th:nth-child(4), .reward-cols td:nth-child(4) {{ width:27%; }}
.reward-cols td.basket {{ white-space:normal; }}
.reward-cols td.sym {{ min-width:0; overflow:hidden; }}
.reward-cols .sym-sub {{ overflow:hidden; text-overflow:ellipsis; }}
/* td.sym > * {{display:block}} out-specifies .sym-name {{display:flex}}, so the
   idle badge wrapped onto its own line and made that row a line taller than
   its neighbour. Restate flex here and let the NAME truncate instead. */
.reward-cols .sym-name {{
  display:flex; align-items:center; gap:6px; flex-wrap:nowrap; overflow:hidden;
}}
.reward-cols .sym-name > a {{
  min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}
.reward-cols .sym-name .badge {{ flex:none; margin-left:0; }}
.reward-cols td.sym {{ min-width:0; overflow-wrap:anywhere; }}
.pays.more-chip {{ background:transparent; color:var(--muted);
  border-color:color-mix(in srgb, var(--muted) 50%, transparent); }}   /* 2 lines at 1.55 — blurbs are one line now */
@media (max-width:1039px) {{ .two-col .sec-sub {{ min-height:0; }} }}
/* The shadow wrapper is now the flex child, so the stretch has to go through
   it or the two boards end up different heights whenever their footers wrap. */
.two-col .pixel-shadow {{ flex:1; display:flex; flex-direction:column; }}
.two-col .board {{ flex:1; display:flex; flex-direction:column; }}
.two-col .scroll {{ flex:1; }}
/* NOT height:100%. That stretched rows to fill the board, so each board
   distributed ITS leftover height across ITS rows — the two boards differ
   slightly (footer wrap), so the rows drifted 2px apart and no amount of
   setting row/cell height could fix it. Rows now size to content, which is
   identical in both tables. */
.two-col table {{ height:auto; }}

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
button.more:hover:not(:disabled) {{ background:var(--accent); }}
button.more:disabled {{ opacity:.35; cursor:default; }}
.pager {{ display:inline-flex; align-items:center; gap:6px; }}
.pager-ind {{
  font-family:var(--mono); font-size:10px; letter-spacing:.08em;
  color:var(--muted); min-width:34px; text-align:center;
}}
.board-foot .foot-r {{ margin-left:auto; }}


.sym-sub {{
  font-family:var(--mono); font-size:10.5px; color:var(--muted);
  display:flex; align-items:center; flex-wrap:wrap; gap:5px;
  /* The 11px social marks make this line 15px tall; rows for projects with no
     declared handle would otherwise sit at 14 and break the row rhythm. */
  min-height:15px;
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
  color:var(--muted); margin:0; }}
.foot-end {{
  display:flex; align-items:center; justify-content:space-between;
  gap:16px; flex-wrap:wrap; margin-top:18px;
  border-top:1px solid var(--line); padding-top:14px;
}}
.byline {{ display:inline-flex; align-items:center; gap:9px; color:var(--muted);
  text-decoration:none; border-bottom:none; }}
.byline-label {{ font-family:var(--mono); font-size:10px; letter-spacing:.14em;
  text-transform:uppercase; }}
.byline:hover {{ color:var(--accent-ink); }}
.byline:focus-visible {{ outline:2px solid var(--accent-ink); outline-offset:3px; }}
.byline-mark {{
  display:block; width:82px; height:26px; background:currentColor;
  -webkit-mask-repeat:no-repeat; mask-repeat:no-repeat;
  -webkit-mask-size:contain; mask-size:contain;
  -webkit-mask-position:center; mask-position:center;
}}
footer h2.foot-h {{ font-family:var(--mono); font-size:10px; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-2); margin:0 0 8px; font-weight:600; }}
footer p {{ margin:0 0 10px; }}
footer code {{ font-family:var(--mono); font-size:11.5px; color:var(--ink-2); }}
/* Below ~700px the boards need ~560px in a ~468px scroller, which pushed
   Volume -- the default sort and the whole basis of the ranking -- off the
   right edge. Drop the secondary numerics so Token + Volume always fit. */
@media (max-width:700px) {{
  .two-col:not(.reward-cols) .board th:nth-child(3),
  .two-col:not(.reward-cols) .board td:nth-child(3),
  .two-col:not(.reward-cols) .board th:nth-child(4),
  .two-col:not(.reward-cols) .board td:nth-child(4) {{ display:none; }}
  td.sym {{ min-width:150px; }}
  tbody td, thead th {{ padding-left:10px; padding-right:10px; }}
}}
@media (prefers-reduced-motion:no-preference) {{ html {{ scroll-behavior:smooth; }} }}
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
    {span_days} days of chain history and counting.
  </p>
  <a class="contract-link" href="https://robinhoodchain.blockscout.com/" target="_blank"
     rel="noopener noreferrer">View explorer <span aria-hidden="true">&#8599;</span></a>

  <div class="hero">
    <div class="hero-main">
      <div class="pixel-shadow"><div class="hero-card pixel">
        <span class="hero-label">Total value locked</span>
        <span class="hero-value">{usd(tvl_headline)}</span>
        <span class="hero-meta">
          Robinhood Chain &middot; DefiLlama &middot; {escape(tvl_date)}
          &middot; {pct(l.get('tvl_change_7d_pct'))} 7d
        </span>
      </div></div>
{summary_html}
    </div>
    <div class="hero-side pixel">
{hero_tiles}
    </div>
  </div>
{nav_html}

  <section id="vitals">
    <h2>Chain vitals</h2>
    <p class="sec-sub">Last complete UTC day. Hover any plot for exact values.</p>
    <div class="ranges" id="ranges" role="group" aria-label="Time range"></div>
    <p class="range-note" id="rangeNote"></p>
    <div class="pixel-shadow"><div class="charts pixel" id="charts"></div></div>
    <div class="pixel-shadow"><div class="tiles pixel">
{tiles}
    </div></div>
  </section>

  <section id="memecoin-vitals">
    <h2>Memecoin vitals</h2>
    <p class="sec-sub">
      Every new pool's value from its own first minute. {launch_note}
    </p>
    <div class="launch pixel">
      <div class="launch-head">
        <div>
          <span class="launch-k">VALUE VS LAUNCH &middot; HOURS SINCE LAUNCH</span>
          <span class="launch-sub" id="lxSub"></span>
        </div>
        <div class="legend lx-legend">
          <span><i style="background:{TRACE_DRAINED}"></i>rugged</span>
          <span><i style="background:{TRACE_ALIVE}"></i>stable</span>
          <span><i class="i-mute"></i>early</span>
          <span class="lx-shape"><b>&#9679;</b> Pons</span>
          <span class="lx-shape"><b>&#9632;</b> Uniswap</span>
          <span class="lx-shape"><b>&#9650;</b> other</span>
          <button class="more" id="lxReplay" type="button">replay</button>
        </div>
      </div>
      <div class="launch-plot"><canvas id="lxCanvas"></canvas>
        <div class="tip" id="lxTip"></div>
      </div>
    </div>

{launch_cards(lx)}
  </section>

  <section id="nft-launches">
    <h2>NFT launches</h2>

    <p class="sec-sub">
      New ERC-721 contracts over the last {nftl.get('window_hours', 6)} hours, found by watching
      mints straight off the chain. {nft_note}
    </p>
    <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
      <table>
        <thead><tr>
          <th></th><th data-sort="text">Collection</th><th data-sort="num">Minters</th>
          <th data-sort="num" data-default="1">Mints</th>
        </tr></thead>
        <tbody>
{nft_launch_rows(nftl.get('collections', []))}
        </tbody>
      </table>
    </div>
    <div class="board-foot">
      <button class="more" type="button"></button>
      <span class="foot-r">{nftl.get('total', 0)} collections in the window</span>
    </div></div></div>
  </section>

  <section id="stablecoins">
    <h2>Stablecoins on the chain</h2>
    <p class="sec-sub">
      All {usd(stb_headline)} of it. No real USDC or USDT exists on this chain.
    </p>
    <div class="pixel-shadow"><div class="comp pixel">
      <div class="compbar" id="compbar"></div>
      <ul class="stables">
{stable_rows(stables)}
      </ul>
    </div></div>
  </section>

  <div class="two-col">
    <section id="top-memecoins">
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
        <span class="foot-r">{n_excl} more screened out below the {usd_exact(floor)} floor</span>
      </div></div></div>
    </section>

    <section id="top-nfts">
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

  <section id="launchpads">
    <h2>Launchpad index</h2>
    <p class="sec-sub">{pad_note_idx}</p>
    <h3 class="sub-head">Launchers</h3>
    <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
      <table>
        <thead><tr>
          <th></th><th data-sort="text">Launcher</th>
          <th data-sort="num" data-default="1">Top {padx.get('top_n', 10)} combined FDV</th>
        </tr></thead>
        <tbody>
{pad_rows(pad_split(padx.get('pads', []))[0], padx.get('top_n', 10))}
        </tbody>
      </table>
    </div>
    <div class="board-foot">
      <span>{len(pad_split(padx.get('pads', []))[0])} launchers</span>
      <span class="foot-r">{escape(usd(sum(p['index'] for p in pad_split(padx.get('pads', []))[0])))} combined</span>
    </div></div></div>

    <h3 class="sub-head">Direct deploys &middot; AMM venues</h3>
    <p class="sec-sub">Not launchpads: somebody deployed a token and opened a pool
      themselves. Measured, 90% of recent Uniswap v4 pools carry no hook at all, so
      the venue hosted the coin rather than producing it.</p>
    <div class="pixel-shadow"><div class="board pixel"><div class="scroll">
      <table>
        <thead><tr>
          <th></th><th data-sort="text">Venue</th>
          <th data-sort="num" data-default="1">Top {padx.get('top_n', 10)} combined FDV</th>
        </tr></thead>
        <tbody>
{pad_rows(pad_split(padx.get('pads', []))[1], padx.get('top_n', 10))}
        </tbody>
      </table>
    </div>
    <div class="board-foot">
      <span>{len(pad_split(padx.get('pads', []))[1])} venues</span>
      <span class="foot-r">{escape(usd(sum(p['index'] for p in pad_split(padx.get('pads', []))[1])))} combined</span>
    </div></div></div>
  </section>

  <section id="payouts">
    <h2>Holder payouts</h2>
    <p class="sec-sub">
      Projects routing fees into buying a different asset and paying it out. {rw_assets_note}
    </p>
    <div class="reward-stack">
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
              <th data-sort="num" data-default="1">Distributed</th>
              <th data-sort="num">Pending</th>
            </tr></thead>
            <tbody>
{booster_rows(merge_generations(bst.get('projects', [])))}
            </tbody>
          </table>
        </div>
        <div class="board-foot">
          <span>{len(bst.get('reward_assets', []))} assets</span>
          <span class="foot-r">{escape(usd(bst.get('total_distributed_usd')))} paid
            &middot; {escape(usd(sum((p.get('pending_usd') or 0) for p in bst.get('projects', []))))} waiting</span>
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
    <div class="foot-end">
      <p class="stamp">Snapshot {escape(gen_str)}.</p>
      {byline()}
    </div>
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
      // Re-sorting reorders every row, so whatever page you were on now shows
      // different data under the same page number. Reset to the first page and
      // let the pager repaint.
      const bd = table.closest('.board');
      if (bd) bd.dispatchEvent(new CustomEvent('board:sorted'));
    }};
    th.addEventListener('click', run);
    th.addEventListener('keydown', e => {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); run(); }}
    }});
    if (th.dataset.default) th.setAttribute('aria-sort', 'descending');
  }});
}}
document.querySelectorAll('.board table').forEach(makeSortable);


/* Boards page at 10 rows. At 20 each board ran ~1,220px and the page passed
   6,000px total, so the lower half was rarely reached.
   
   PAGED rather than an expand toggle: "show all 20" answered "is there more?"
   but not "how much more", and it solved length by doubling it -- the reason
   the toggle existed in the first place. A pager keeps every board a fixed
   height whatever the row count, and the "n / m" reads as a promise that the
   tail is reachable rather than hidden. It also scales: the boards can now
   carry more than 20 rows without the page growing at all. */
document.querySelectorAll('.board').forEach(board => {{
  const tbody = board.querySelector('tbody');
  const btn = board.querySelector('button.more');
  if (!tbody) return;
  const PAGE = 10;
  const rows = () => Array.from(tbody.rows);
  const total = rows().length;
  if (total <= PAGE) {{ if (btn) btn.remove(); return; }}
  const pages = Math.ceil(total / PAGE);
  let page = 1;

  const nav = document.createElement('span');
  nav.className = 'pager';
  const mk = (txt, lab) => {{
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'more'; b.textContent = txt;
    b.setAttribute('aria-label', lab);
    return b;
  }};
  const prev = mk('\u2039', 'previous page');
  const next = mk('\u203a', 'next page');
  const ind = document.createElement('span');
  ind.className = 'pager-ind';
  ind.setAttribute('aria-live', 'polite');
  nav.append(prev, ind, next);
  if (btn) btn.replaceWith(nav);
  else (board.querySelector('.board-foot') || board).appendChild(nav);

  function render() {{
    const rs = rows();
    const lo = (page - 1) * PAGE, hi = page * PAGE;
    rs.forEach((r, i) => {{
      // drop the server-rendered `over` class: it hid rows 11+ via CSS, and
      // with a pager the visibility is decided here for every row.
      r.classList.remove('over');
      r.style.display = (i >= lo && i < hi) ? '' : 'none';
    }});
    ind.textContent = page + ' / ' + pages;
    prev.disabled = page === 1;
    next.disabled = page === pages;
  }}
  prev.addEventListener('click', () => {{ if (page > 1) {{ page--; render(); }} }});
  next.addEventListener('click', () => {{ if (page < pages) {{ page++; render(); }} }});
  board.addEventListener('board:sorted', () => {{ page = 1; render(); }});
  render();
}});


/* ---- launch trajectories ----
   Moving dots, not static lines: each pool is a glowing dot travelling its own
   path, trailing a fading tail, looping continuously so the section reads as
   live rather than as a finished chart.

   Y is a LOG MULTIPLE OF EACH POOL'S OWN FIRST VALUE, not absolute FDV. On an
   absolute axis a $3k launch that 40x'd is invisible beneath a $90k launch that
   died; as a multiple both sit on one scale and the shape is the whole point.
   1.0x is drawn as the reference line — above it is up, below is down.

   Canvas because 120 animated dots + trails as DOM nodes would crawl. */
(function () {{
  const LX = (D.launch || {{}});
  const traces = (LX.traces || []).filter(t => (t.mx || []).length > 1);
  const cv = document.getElementById('lxCanvas');
  if (!cv || !traces.length) {{
    if (cv) cv.closest('.launch-plot').innerHTML =
      '<p class="range-note">No trajectories yet - the poller needs to see a pool more than once.</p>';
    return;
  }}
  const tip = document.getElementById('lxTip');
  const sub = document.getElementById('lxSub');
  const ctx = cv.getContext('2d');
  const COL = {{ rugged: '#d03b3b', stable: '#0F86C4' }};
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  let W = 0, H = 0, dpr = 1;
  const PAD = {{ l: 46, r: 92, t: 10, b: 22 }};
  // The axis is set by PERCENTILES, never by the extremes. One trace running
  // 20x compresses the ~98% of pools that live between 0.05x and 2x into a flat
  // smear. Anything beyond the bounds is pinned to the edge with a caret and
  // still carries its true multiple in the label, so the outlier is reported
  // without being allowed to set the scale.
  let maxT = 0;
  const allV = [];
  traces.forEach(tr => tr.mx.forEach(p => {{
    if (p[0] > maxT) maxT = p[0];
    allV.push(p[1]);
  }}));
  allV.sort((a, b) => a - b);
  const pct = q => allV[Math.min(Math.floor(allV.length * q), allV.length - 1)] || 1;
  // Hours now, not minutes: mx carries hour-since-launch buckets. The axis
  // follows the data rather than reserving the full 24h -- a fixed window drew
  // every trace as a stub against five hours of blank panel. It widens on its
  // own as the follow-up poller accumulates longer histories.
  maxT = Math.min(Math.max(Math.ceil(maxT) + 1, 3), 24);
  const loX = Math.min(Math.max(pct(0.01) * 0.8, 0.02), 0.5);
  const hiX = Math.max(Math.min(pct(0.99) * 1.6, 25), 3);
  const clampV = v => Math.min(Math.max(v, loX), hiX);
  let clipped = 0;

  const lg = v => Math.log10(Math.max(v, 0.001));
  const X = m => PAD.l + Math.min(m / maxT, 1) * (W - PAD.l - PAD.r);
  const Y = v => H - PAD.b - ((lg(clampV(v)) - lg(loX)) / Math.max(lg(hiX) - lg(loX), 0.0001))
                    * (H - PAD.t - PAD.b);
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  /* value of a trace at replay-minute m, linearly interpolated */
  function at(tr, m) {{
    const p = tr.mx;
    if (m <= p[0][0]) return null;
    for (let i = 1; i < p.length; i++) {{
      if (p[i][0] >= m) {{
        const a = p[i - 1], b = p[i], k = (m - a[0]) / Math.max(b[0] - a[0], 1e-6);
        return {{ x: a[0] + (b[0] - a[0]) * k, v: a[1] + (b[1] - a[1]) * k, done: false }};
      }}
    }}
    const last = p[p.length - 1];
    if (last[0] > maxT) return null;      // beyond the window: do not pin to the edge
    return {{ x: last[0], v: last[1], done: true }};
  }}

  function resize() {{
    dpr = Math.min(devicePixelRatio || 1, 2);
    W = cv.clientWidth; H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }}

  const TRAIL = 999;   // full path: sampling is too sparse for a short tail
  // Polling is every ~10 min and a pool can drop out of the feed and be picked
  // up again much later. Joining across that produced long horizontal rails
  // implying we watched a flat trajectory when we simply were not looking.
  // Anything past this gap is drawn as a BREAK, not a line.
  // 33% of observation gaps exceed 14 minutes (median 3.3, p75 51.9) because
  // the follow-up poller can only re-read ~120 pools a run. Severing the line
  // there left the 1x cluster as loose dots. A gap is now DRAWN, faintly and
  // dashed, so the trajectory stays readable while still saying plainly that
  // nothing was observed across it.
  const MAX_GAP = 2;   // hours — floor for the adaptive threshold
  traces.forEach(tr => {{
    const gs = [];
    for (let i = 1; i < tr.mx.length; i++) gs.push(tr.mx[i][0] - tr.mx[i - 1][0]);
    gs.sort((a, b) => a - b);
    const med = gs.length ? gs[Math.floor(gs.length / 2)] : MAX_GAP;
    tr.gapLim = Math.max(MAX_GAP, med * 2.5);
  }});

  function frame(cut) {{
    const line = css('--line'), muted = css('--muted'), ink = css('--ink');
    ctx.clearRect(0, 0, W, H);

    /* gridlines at decades of multiple */
    ctx.font = '9.5px ui-monospace, Menlo, monospace';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    [0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25].forEach(v => {{
      if (v < loX || v > hiX) return;
      const y = Y(v);
      ctx.strokeStyle = (v === 1) ? muted : line;
      ctx.lineWidth = 1;
      ctx.setLineDash(v === 1 ? [4, 4] : []);
      ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(W - PAD.r, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = (v === 1) ? ink : muted;
      ctx.fillText(v >= 1 ? v + 'x' : v + 'x', PAD.l - 6, y);
    }});
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.fillStyle = muted;
    const tick = maxT <= 6 ? 1 : maxT <= 12 ? 2 : 4;
    for (let h = 0; h <= maxT; h += tick) {{
      ctx.fillText(h + 'h', X(h), H - PAD.b + 5);
    }}

    /* trails, then dots — young first so decided traces sit on top */
    const order = traces.slice().sort((a, b) =>
      (a.st === 'early' ? 0 : 1) - (b.st === 'early' ? 0 : 1));
    const live = [];

    order.forEach(tr => {{
      const now = at(tr, cut);
      if (!now) return;
      // Colour reflects where the pool is AT THIS MOMENT of the replay, never
      // its final verdict — coluring by outcome showed minute-50 information at
      // minute 5, so a pool up 2.5x rendered red because it died later.
      const col = now.v >= 1.15 ? COL.stable : now.v <= 0.6 ? COL.rugged : muted;
      const seg = tr.mx.filter(p => p[0] <= now.x && p[0] >= now.x - TRAIL);
      if (seg.length > 1) {{
        ctx.strokeStyle = col;
        ctx.globalAlpha = tr.st === 'early' ? 0.18 : 0.42;
        ctx.lineWidth = tr.st === 'early' ? 1 : 1.75;
        ctx.beginPath();
        for (let i = 1; i < seg.length; i++) {{
          const a = seg[i - 1], b = seg[i];
          // Threshold adapts to the cadence that produced THIS trace. Actions
          // throttling means sampling has ranged from 3 minutes to an hour, and
          // a fixed 14-minute rule drew every hourly-sampled trace as entirely
          // dashed. 2.5x the trace's own median spacing marks a genuine absence
          // at any cadence.
          const unobserved = (b[0] - a[0]) > tr.gapLim;
          ctx.beginPath();
          ctx.setLineDash(unobserved ? [2, 3] : []);
          ctx.globalAlpha = (tr.st === 'early' ? 0.18 : 0.42) * (unobserved ? 0.45 : 1);
          ctx.moveTo(X(a[0]), Y(a[1])); ctx.lineTo(X(b[0]), Y(b[1]));
          ctx.stroke();
        }}
        const tail = seg[seg.length - 1];
        ctx.beginPath();
        ctx.setLineDash((now.x - tail[0]) > tr.gapLim ? [2, 3] : []);
        ctx.moveTo(X(tail[0]), Y(tail[1])); ctx.lineTo(X(now.x), Y(now.v));
        ctx.stroke();
        ctx.setLineDash([]);
      }}
      live.push({{ tr, now, col }});
    }});

    clipped = 0;
    live.forEach(({{ tr, now, col }}) => {{
      const x = X(now.x), y = Y(now.v);
      const over = now.v > hiX, under = now.v < loX;
      if (over || under) clipped++;
      if (col !== muted) {{                          // glow only on decided dots
        ctx.globalAlpha = 0.22; ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(x, y, 7.5, 0, 6.284); ctx.fill();
      }}
      ctx.globalAlpha = tr.st === 'early' ? 0.5 : 1;
      ctx.fillStyle = col;
      const r0 = tr.st === 'early' ? 1.9 : 3.4;
      ctx.beginPath();
      if (tr.pk === 1) {{                       // Uniswap - square
        ctx.rect(x - r0, y - r0, r0 * 2, r0 * 2);
      }} else if (tr.pk === 2) {{               // other pads - triangle
        ctx.moveTo(x, y - r0 * 1.2); ctx.lineTo(x + r0 * 1.1, y + r0 * 0.9);
        ctx.lineTo(x - r0 * 1.1, y + r0 * 0.9); ctx.closePath();
      }} else {{                                // Pons - circle
        ctx.arc(x, y, r0, 0, 6.284);
      }}
      ctx.fill();
      if (over || under) {{                    // caret: this one is off-scale
        ctx.beginPath();
        const d = over ? -1 : 1, ty = y + d * 8;
        ctx.moveTo(x, ty); ctx.lineTo(x - 4, ty + d * -5); ctx.lineTo(x + 4, ty + d * -5);
        ctx.closePath(); ctx.fillStyle = col; ctx.fill();
      }}
    }});
    ctx.globalAlpha = 1;

    /* label chips on the biggest movers, euphoria-style */
    const chips = live.filter(l => l.col !== muted)
      .sort((a, b) => Math.abs(Math.log10(b.now.v)) - Math.abs(Math.log10(a.now.v)))
      .slice(0, 5);
    ctx.font = '10px ui-monospace, Menlo, monospace';
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    const used = [];
    chips.forEach(({{ tr, now, col }}) => {{
      let x = X(now.x) + 10, y = Y(now.v);
      while (used.some(u => Math.abs(u - y) < 15)) y += 15;
      if (y > H - PAD.b - 4) return;
      used.push(y);
      const label = tr.s + '  ' + (now.v >= 1 ? now.v.toFixed(1) + 'x'
                                              : now.v.toFixed(2).replace(/^0/, '') + 'x');
      const w = ctx.measureText(label).width + 12;
      ctx.globalAlpha = 0.92; ctx.fillStyle = col;
      ctx.fillRect(x, y - 8, Math.min(w, W - x - 4), 16);
      ctx.globalAlpha = 1; ctx.fillStyle = '#0F100C';
      ctx.fillText(label, x + 6, y + 1);
    }});

    if (sub) sub.textContent = traces.length + ' pools · ' + Math.round(cut) + 'h in'
      + ' · axis ' + (loX < 0.1 ? loX.toFixed(2) : loX.toFixed(1)) + 'x-' + hiX.toFixed(0) + 'x'
      + (clipped ? ' · ' + clipped + ' beyond it' : '');
  }}

  let raf = 0, paused = false;
  function loop() {{
    cancelAnimationFrame(raf);
    if (reduce) {{ frame(maxT); return; }}             // no motion: show the end state
    const dur = 9000, hold = 1400;
    let t0 = performance.now();
    const step = now => {{
      if (!paused) {{
        const e = now - t0;
        if (e > dur + hold) {{ t0 = now; }}
        else frame(Math.min(e / dur, 1) * maxT);
      }}
      raf = requestAnimationFrame(step);
    }};
    raf = requestAnimationFrame(step);
  }}

  cv.addEventListener('mousemove', e => {{
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null, bd = 1e9;
    traces.forEach(tr => tr.mx.forEach((p, i) => {{
      const d = Math.hypot(X(p[0]) - mx, Y(p[1]) - my);
      if (d < bd) {{ bd = d; best = {{ tr, p, i }}; }}
    }}));
    if (!best || bd > 24) {{ tip.style.opacity = '0'; paused = false; return; }}
    paused = true;
    const tr = best.tr, abs = (tr.pts[best.i] || [])[1];
    tip.innerHTML = '<span class="tip-d">' + tr.s + ' · ' + tr.pad + ' · '
      + Math.round(best.p[0]) + 'h old</span>'
      + best.p[1].toFixed(2) + 'x'
      + (abs ? ' · $' + Math.round(abs).toLocaleString() : '')
      + '<br><span class="tip-d">from $' + Math.round(tr.base).toLocaleString()
      + ' at first sight</span>';
    tip.style.opacity = '1';
    tip.style.left = Math.min(Math.max(70, X(best.p[0])), W - 70) + 'px';
    tip.style.top = (Y(best.p[1]) - 14) + 'px';
  }});
  cv.addEventListener('mouseleave', () => {{ tip.style.opacity = '0'; paused = false; }});
  document.getElementById('lxReplay').addEventListener('click', loop);
  let rz; addEventListener('resize', () => {{ clearTimeout(rz);
    rz = setTimeout(() => {{ resize(); }}, 120); }});
  resize(); loop();
}})();

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





# --------------------------------------------------------------------------- #
# Launch trajectories
# --------------------------------------------------------------------------- #
LEDGER = OUT_DIR / "launches.jsonl"

# One definition of a usable observation, owned by the module that owns the
# ledger schema. Imported rather than re-implemented so the chart and any later
# verdict engine can never drift apart on what counts as bad data.
try:
    from launch_watch import observation_quality, usable_liquidity, MIN_DEPTH_USD
except ImportError:                       # renderer must still work standalone
    MIN_DEPTH_USD = 500

try:
    from chain_pulse import INFRA_SYMBOLS
except ImportError:
    INFRA_SYMBOLS = {"WETH", "ETH", "USDG", "USDC", "USDT", "USDE"}

    def observation_quality(_r):
        return None

    def usable_liquidity(r):
        return r.get("liq")

# GeckoTerminal's dex id already names the launchpad. Pons is dominant on this
# chain (~45% of new pools across its three ids); the rest are the generic AMM
# tiers plus a few smaller pads.
# A launch starts small. Measured medians at first sight run $3k-$10k across
# every pad and p90 tops out near $45k, so a million is ~20x above the busiest
# pad's p90 -- generous to real launches, decisive against tokens that were
# already established when they opened a new pool.
LAUNCH_FDV_CEILING = 1_000_000

# Venue is not the same thing as launcher, and ranking them together credits an
# AMM for work it did not do. MEASURED: 90% of recent Uniswap v4 Initialize
# events carry NO hook at all, so those pools are people deploying a token and
# opening a pool themselves -- the venue hosted it, nothing more. Grouping them
# apart is what lets the launchers actually compete on screen instead of being
# buried under v4's aggregate.
#
# Classification is by product, from on-chain behaviour and naming; there is no
# authoritative registry of which dex id is a launchpad on this chain. Up is the
# judgement call -- its coins (OAK, oakmont.fun) and its link to Oakmont Vault
# read as a launcher, but no source states it outright, so it is marked as
# inferred rather than asserted.
LAUNCHER_PADS = {"Pons", "Bankr", "Clanker", "Virtuals", "Easya Kickstart",
                 "Mint Club", "Up", "Hoodit"}
INFERRED_PADS = {"Up"}

# What is NOT a launched coin, however new its pool is. INFRA_SYMBOLS covers the
# obvious names; these catch the rest of the same idea.
#
#  * stablecoins and their wrappers -- USDUF, WUSDG, USDG0, rwaUSDi all appeared
#    with real pools. A dollar is not a launch, whatever the ticker.
#  * wrapped majors -- BTC opened a $470k pool on v3 and is bridged value, not a
#    coin somebody launched there.
#  * tokenised equities -- matched by ADDRESS, never by symbol, because ticker
#    squatting is endemic here. That distinction matters: the real RDDT is
#    Robinhood's Reddit token and does not belong in the index, while a copycat
#    memecoin calling itself RDDT genuinely is a launch and should stay.
STABLE_RE = re.compile(r"USD|^DAI$|^EUR", re.I)
WRAPPED_MAJORS = {"BTC", "WBTC", "CBBTC", "TBTC", "STETH", "WSTETH", "WETH", "ETH"}


def _equity_addresses():
    """Addresses of the tokenised equities, or an empty set if unreachable."""
    try:
        from chain_pulse import discover_stock_tokens
        return {a.lower() for a in discover_stock_tokens()}
    except Exception:
        return set()


LAUNCHPADS = {
    "pons-dot-family": "Pons", "pons-v2": "Pons", "pons-v2-dex": "Pons",
    "uniswap-v4-robinhood": "Uniswap v4", "uniswap-v3-robinhood": "Uniswap v3",
    "uniswap-v2-robinhood": "Uniswap v2", "uniswap-pools-trade": "Uniswap",
    "bankr-robinhood": "Bankr", "clanker-robinhood": "Clanker",
    "virtuals-robinhood": "Virtuals", "sushiswap-v3-robinhood": "SushiSwap",
    "ramses-v3-robinhood": "Ramses", "up-v3": "Up",
}


def pad_key(pad):
    """Shape slot for a launchpad. Colour is already carrying status, and shade
    within a hue fails the normal-vision separation floor, so provenance rides
    on mark SHAPE instead — an independent channel."""
    if pad == "Pons":
        return 0                      # circle
    if pad.startswith("Uniswap"):
        return 1                      # square
    return 2                          # triangle


def launchpad_of(dex):
    if not dex:
        return "unknown"
    return LAUNCHPADS.get(dex, dex.replace("-robinhood", "").replace("-", " ").title())



# Status colours, NOT categorical identity — there are far more pools than any
# categorical ramp allows, so hue encodes state and the line SHAPE carries the
# story (a cliff vs a climb). Validated with the dataviz palette checker on both
# surfaces: blue/red scores CVD dE 21.5 and passes all six checks.
#
# The obvious choice — green for alive, red for drained — FAILS at dE 4.1 under
# deuteranopia. Red/green is exactly the pair a colourblind reader cannot
# separate, so it is the one encoding this chart must not use.
TRACE_ALIVE = "#0F86C4"
TRACE_DRAINED = "#d03b3b"


def load_launch_traces(path=None, hours=24, max_traces=70, max_points=48):
    """Hourly-averaged trajectories for pools launched in the window.

    Per-observation plotting was too granular to show a trend: a coin can dump
    in its first hour and recover in its third, and at 3-minute resolution that
    reads as noise. Averaging FDV within each hour-since-launch bucket leaves at
    most 24 points per pool and makes the SHAPE legible.

    Shape is the point. A rug is not just "ended down" -- it is the classic
    mountain: climbed hard, then collapsed. That is detectable from the bucketed
    series and is a different failure from a slow bleed, so it gets its own bin.

    TWO SERIES, READ FROM DIFFERENT ROWS, and the distinction matters:

      * FDV is only a price where there is depth behind it, so the plotted
        multiple is built from quality-passing rows alone.
      * LIQUIDITY is valid in every row, and it is the series that actually
        collapses. Filtering the ledger up-front discarded precisely the
        evidence of a rug -- Choju's crash reading ($34,732 of liquidity down
        to $361) was thrown away for having too little depth to price, which is
        the very fact being measured.

    This is the same lesson the FDV freeze taught: when a pool is drained the
    last trade price persists in the feed forever, so a rugged token keeps
    reporting its peak FDV and looks like a winner on price alone. Death is
    legible in liquidity, never in FDV.
    """
    p = Path(path) if path else LEDGER
    if not p.exists():
        return {"traces": [], "counts": {}, "window_hours": hours}

    by = {}
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("created_at") or not r.get("pool"):
            continue
        by.setdefault(r["pool"], []).append(r)

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    traces = []
    counts = {"winner": 0, "loser": 0, "rug": 0, "dead": 0, "flat": 0, "early": 0}

    for pool, obs in by.items():
        try:
            born = dt.datetime.fromisoformat(obs[0]["created_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if born < cutoff:
            continue
        obs.sort(key=lambda r: r["ts"])

        # average FDV within each hour-since-launch
        buckets, liqs, raw = {}, [], []
        for r in obs:
            mins = (dt.datetime.fromisoformat(r["ts"]) - born).total_seconds() / 60.0
            if mins < 0 or mins > hours * 60:
                continue
            # liquidity from EVERY row -- a drained pool's reading is the signal
            lq = usable_liquidity(r)
            if lq is not None:
                liqs.append(lq)
            # price only from rows with a market behind them
            v = r.get("fdv")
            if observation_quality(r) or not v or v <= 0:
                continue
            buckets.setdefault(int(mins // 60), []).append(v)
            raw.append(v)
        if not liqs:
            continue

        age_h = (now - born).total_seconds() / 3600
        peak_liq, last_liq = max(liqs), liqs[-1]

        # The price track, where one exists. A pool can be judged dead without
        # ever having had a quotable price, so this is allowed to come out empty.
        hrs = sorted(buckets)
        series = [[h, sum(buckets[h]) / len(buckets[h])] for h in hrs]
        mult, base, peakx, lastx = [], None, 0.0, 0.0
        if series and series[0][1] >= 50:
            base = series[0][1]
            mult = [[h, v / base] for h, v in series]
            # Peak comes from the RAW series, not the hourly means. Averaging is
            # what makes the trend legible, but it also flattens the pump: most
            # rugs here complete inside one hour (BLINK ran $89k to $2.6k in 45
            # minutes), so a bucketed peak hides the very shape being named.
            # Detect on full resolution, draw the smoothed line.
            peakx = max(raw) / base
            lastx = mult[-1][1]

        # Liquidity decides life and death; price only ranks the survivors.
        # Ordered so the mountain is claimed BEFORE the quieter failures, since
        # a pool that had a real market and lost it is a different event from
        # one that never had a market at all -- and it is the one worth naming.
        drained = peak_liq >= 5000 and last_liq <= 0.30 * peak_liq
        if drained:
            state = "rug"
        elif last_liq < MIN_DEPTH_USD:
            state = "dead"                      # faded out; never had a market
        elif age_h < 1 or len(mult) < 2:
            state = "early"
        elif lastx >= 1.2:
            state = "winner"
        elif lastx <= 0.8:
            state = "loser"
        else:
            state = "flat"
        counts[state] += 1

        traces.append({
            "s": (obs[-1].get("symbol") or "?")[:14],
            "p": pool, "st": state,
            "pad": launchpad_of(obs[-1].get("dex")),
            "pk": pad_key(launchpad_of(obs[-1].get("dex"))),
            # absolute hourly means alongside the multiples, so the tooltip can
            # show the real dollar FDV rather than only a ratio
            "mx": mult, "pts": series, "base": base,
            "peakx": round(peakx, 2), "lastx": round(lastx, 3),
            "liq": last_liq, "peak_liq": peak_liq, "age_h": round(age_h, 1),
            "chg": round((lastx - 1) * 100, 1),
            "url": f"https://dexscreener.com/robinhood/{pool}",
        })

    # draw the ones with a story: rugs and the biggest movers either way.
    # A trace needs at least two priced points to be a line rather than a dot.
    traces.sort(key=lambda t: (t["st"] != "rug", -abs(t["chg"])))
    kept = [t for t in traces
            if t["st"] != "early" and len(t["mx"]) >= 2][:max_traces]
    pads = {}
    for tr in kept:
        pads[tr["pad"]] = pads.get(tr["pad"], 0) + 1

    def best(state, key, rev=True):
        pool = [t for t in traces if t["st"] == state]
        return sorted(pool, key=key, reverse=rev)[0] if pool else None

    return {"traces": kept, "counts": counts, "window_hours": hours,
            "total_pools": len(by), "judged": sum(counts.values()) - counts["early"],
            "top_winner": best("winner", lambda t: t["chg"]),
            "top_loser": best("loser", lambda t: -t["chg"]),
            # Rugs rank by the size of the market that vanished, not by price
            # multiple -- many drain without ever posting a quotable pump, and
            # the money that was in the pool is the honest measure of damage.
            "top_rug": best("rug", lambda t: t["peak_liq"]),
            "pads": sorted(pads.items(), key=lambda kv: -kv[1])}



def pad_split(pads):
    """Launchers and AMM venues, each ranked within its own group."""
    return ([p for p in pads if p.get("launcher")],
            [p for p in pads if not p.get("launcher")])


def pad_rows(pads, top_n=10):
    """One row per launchpad, with a proportional bar for the index.

    The bar is LINEAR against the leader, not logarithmic. The spread is the
    finding -- the busiest launchpad by coin count carries the smallest top-ten
    value on the chain -- and a log scale would flatter the tail into looking
    comparable when it is two orders of magnitude behind.
    """
    if not pads:
        return ""
    lead = max(p["index"] for p in pads) or 1   # scaled within the group
    out = []
    for i, p in enumerate(pads, 1):
        names = " &middot; ".join(escape(c["sym"]) for c in p["top"][:3])
        pct = max(p["index"] / lead * 100, 0.6)
        out.append(f"""          <tr>
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape(p['pad'].lower())}">
              <span class="sym-name">{escape(p['pad'])}{' <span class="badge warn">inferred</span>' if p.get('inferred') else ''}</span>
              <span class="sym-sub">{names}</span>
            </td>
            <td class="n strong" data-v="{p['index']}">{escape(usd(p['index']))}
              <div class="idxbar"><i style="width:{pct:.1f}%"></i></div></td>
          </tr>""")
    return "\n".join(out)


def launchpad_index(tokens=None, top_n=10, tvl=None, min_coins=3, **_):
    """Combined FDV of each launchpad's ten biggest live coins.

    Reads LIVE token records (GeckoTerminal, every dex on the chain) rather than
    replaying the observation ledger. The ledger was only ever supplying FDV
    here, and it supplied a stale one over a window bounded by retention -- the
    same growth that took it past GitHub's 100 MB limit and blocked every push.

    Attribution is by the token's EARLIEST pool across all dexes, which does the
    work the ledger's first-sight FDV used to do: an established token opening a
    new pool has an older pool elsewhere, so it credits the pad it actually
    launched on instead of flattering the new venue. PIPEDOG no longer lands on
    Uniswap v3 for opening a v3 pool.

    ⚠️ Known limit: only each dex's busiest pools are fetched, so a token whose
    original pool has gone quiet may be attributed to a later pad.
    """
    cap = tvl or 500_000_000
    equities = _equity_addresses()
    by = {}
    for tk in (tokens or []):
        fdv = tk.get("fdv") or 0
        liq = tk.get("liq") or 0
        if fdv <= 0 or fdv > cap:
            continue
        if liq < MIN_DEPTH_USD:            # dead pools keep quoting a frozen price
            continue
        sym = tk.get("sym") or "?"
        if (sym.upper() in INFRA_SYMBOLS or sym.upper() in WRAPPED_MAJORS
                or STABLE_RE.search(sym)):
            continue
        if (tk.get("addr") or "").lower() in equities:
            continue
        pad = launchpad_of(tk.get("dex"))
        by.setdefault(pad, []).append(
            {"sym": sym[:14], "fdv": fdv, "liq": liq,
             "pool": tk.get("addr") or "", "born": tk.get("born") or ""})

    out = []
    for pad, coins in by.items():
        coins.sort(key=lambda c: -c["fdv"])
        if len(coins) < min_coins:
            continue
        top = coins[:top_n]
        out.append({"pad": pad, "coins": len(coins),
                    "index": sum(c["fdv"] for c in top),
                    "liq": sum(c["liq"] for c in top), "top": top,
                    "launcher": pad in LAUNCHER_PADS,
                    "inferred": pad in INFERRED_PADS})
    out.sort(key=lambda x: -x["index"])
    return {"pads": out, "top_n": top_n, "total": sum(x["index"] for x in out)}


def pad_split(pads):
    """Launchers and AMM venues, each ranked within its own group."""
    return ([p for p in pads if p.get("launcher")],
            [p for p in pads if not p.get("launcher")])


def pad_rows(pads, top_n=10):
    """One row per launchpad, with a proportional bar for the index.

    The bar is LINEAR against the leader, not logarithmic. The spread is the
    finding -- the busiest launchpad by coin count carries the smallest top-ten
    value on the chain -- and a log scale would flatter the tail into looking
    comparable when it is two orders of magnitude behind.
    """
    if not pads:
        return ""
    lead = max(p["index"] for p in pads) or 1   # scaled within the group
    out = []
    for i, p in enumerate(pads, 1):
        names = " &middot; ".join(escape(c["sym"]) for c in p["top"][:3])
        pct = max(p["index"] / lead * 100, 0.6)
        out.append(f"""          <tr>
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape(p['pad'].lower())}">
              <span class="sym-name">{escape(p['pad'])}{' <span class="badge warn">inferred</span>' if p.get('inferred') else ''}</span>
              <span class="sym-sub">{names}</span>
            </td>
            <td class="n strong" data-v="{p['index']}">{escape(usd(p['index']))}
              <div class="idxbar"><i style="width:{pct:.1f}%"></i></div></td>
          </tr>""")
    return "\n".join(out)


def nav_cards(s, l, n, m, rw, lx, nftl, padx=None):
    """A floating jump control, pinned to the right edge.

    Was a row of eight cards under the hero. Navigation earned a full band of
    vertical space above the first section and pushed the actual content down,
    which is a poor trade for something a reader uses once or twice. Collapsed
    to a single button that opens the list on demand: the same eight targets,
    almost no page real estate, and it stays reachable after scrolling instead
    of being stranded at the top.

    Self-contained markup + behaviour so the page template stays a template.
    The JS is a plain string, not an f-string -- its braces are code, not
    placeholders, and doubling them here would be noise.
    """
    # ⚠️ Must match the order the sections appear in the page. Stablecoins sits
    # fourth in the document but was listed last here, so the menu disagreed
    # with the scroll it drives.
    items = [
        ("vitals",          "Chain vitals"),
        ("memecoin-vitals", "Memecoin vitals"),
        ("nft-launches",    "NFT launches"),
        ("stablecoins",     "Stablecoins"),
        ("top-memecoins",   "Top memecoins"),
        ("top-nfts",        "Top NFTs"),
        ("launchpads",      "Launchpads"),
        ("payouts",         "Holder payouts"),
    ]
    links = "".join(
        f'<a href="#{a}" role="menuitem">{escape(lab)}</a>' for a, lab in items)
    js = """
(function(){
  var w=document.querySelector('.jumpdock'); if(!w) return;
  var b=w.querySelector('.jump-btn'), p=w.querySelector('.jump-menu');
  function set(open){
    w.classList.toggle('open', open);
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  b.addEventListener('click', function(e){
    e.stopPropagation(); set(!w.classList.contains('open'));
  });
  p.addEventListener('click', function(e){
    if(e.target.tagName === 'A') set(false);
  });
  document.addEventListener('click', function(e){
    if(!w.contains(e.target)) set(false);
  });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape') set(false);
  });
  var top=w.querySelector('.jump-top');
  top.addEventListener('click', function(){
    set(false);
    window.scrollTo({top:0, left:0});
    // Nothing above the first section is focusable, so send the keyboard back
    // to the start of the document rather than leaving it stranded down-page.
    var h=document.querySelector('h1');
    if(h){ h.setAttribute('tabindex','-1'); h.focus({preventScroll:true}); }
  });
  // Only useful once there is something to go back up to.
  function reveal(){ w.classList.toggle('scrolled', window.scrollY > 500); }
  addEventListener('scroll', reveal, {passive:true});
  reveal();

  // Mark the section currently in view so the open menu says where you are.
  var ids = [].map.call(document.querySelectorAll('.jump-menu a'), function(a){
    return a.getAttribute('href').slice(1);
  });
  var obs = new IntersectionObserver(function(entries){
    entries.forEach(function(en){
      if(!en.isIntersecting) return;
      p.querySelectorAll('a').forEach(function(a){
        a.classList.toggle('here', a.getAttribute('href') === '#' + en.target.id);
      });
    });
  }, {rootMargin: '-45% 0px -50% 0px'});
  ids.forEach(function(id){
    var el = document.getElementById(id); if(el) obs.observe(el);
  });
})();
"""
    return ('<div class="jumpdock">'
            f'<nav class="jump-menu pixel" role="menu" aria-label="Sections">{links}</nav>'
            '<div class="jump-stack">'
            '<button class="jump-top pixel" type="button" aria-label="Back to top">'
            '<span aria-hidden="true">\u2191</span></button>'
            '<button class="jump-btn pixel" type="button" aria-expanded="false"'
            ' aria-haspopup="true" aria-label="Jump to section">'
            '<span aria-hidden="true">\u2261</span></button>'
            '</div></div>\n<script>' + js + "</script>")


def chain_summary(s, l, n, m, rw, lx, tvl):
    """One paragraph tying the page's headline numbers into a reading.

    Every figure here is rendered elsewhere on the page; the value added is the
    RELATIONSHIP between them, which a grid of tiles cannot state -- that more
    dollars sit parked in stablecoins than in TVL, or that the launch count and
    the death count belong in the same sentence. Written so each claim survives
    on its own if a section is missing, because sections do go missing.
    """
    bits = []
    stables = l.get("stables_current") or 0
    if tvl and stables:
        rel = ("more stablecoin float than value locked"
               if stables > tvl else "more value locked than stablecoin float")
        bits.append(
            f"<b>{usd(tvl)}</b> of value locked sits against <b>{usd(stables)}</b> "
            f"of stablecoins &mdash; {rel}.")
    dau, fees = s.get("dau_current"), s.get("gas_fees_usd_current")
    app = l.get("app_fees_24h")
    if dau:
        line = f"<b>{num(dau)}</b> addresses transacted in the last complete day"
        if fees:
            line += f", paying <b>{usd(fees)}</b> in gas"
        if app:
            line += f" while apps earned <b>{usd(app)}</b> on top"
        bits.append(line + ".")
    nvol = (n or {}).get("total_volume_usd")
    if nvol:
        bits.append(f"NFTs cleared <b>{usd(nvol)}</b> in paid Seaport fills.")
    c = (lx or {}).get("counts") or {}
    launched = (lx or {}).get("total_pools")
    if launched:
        line = f"<b>{num(launched)}</b> memecoin pools launched in 24 hours"
        dead, rug = c.get("dead", 0), c.get("rug", 0)
        if dead:
            line += f", <b>{num(dead)}</b> of them already dead"
        if rug:
            line += f" and <b>{num(rug)}</b> drained of a real market"
        bits.append(line + ".")
    payers = (rw.get("projects") or []) + ((rw.get("boosters") or {}).get("projects") or [])
    paid = (rw.get("total_distributed_usd") or 0) + \
           ((rw.get("boosters") or {}).get("total_distributed_usd") or 0)
    if payers and paid:
        bits.append(f"And <b>{len(payers)}</b> projects route fees back to the people "
                    f"holding them, <b>{usd(paid)}</b> paid out so far.")
    if not bits:
        return ""
    return ('<div class="summary pixel">'
            '<span class="summary-label">The short version</span>'
            '<p>' + " ".join(bits) + "</p></div>")


def launch_cards(lx):
    """The 24h launch summary as four counts, each naming the pool behind it.

    A count alone is a number nobody can check. Naming the best and worst pool
    under each one turns it into something a reader can click through to
    DexScreener and verify, which is the standard the rest of the page is held
    to. Rugs get their own callout rather than a fifth card: a drained pool is
    a kind of death, not a parallel outcome, and it is the one worth naming
    even when the count is zero."""
    c = lx.get("counts") or {}
    launched = lx.get("total_pools", 0)
    judged = lx.get("judged", 0)

    def callout(t, label, pct=True):
        if not t:
            return '<div class="c none">—</div>'
        sym = escape(str(t.get("s") or "?"))
        if pct:
            detail = f"{t['chg']:+,.0f}%"
        else:
            detail = f"${t.get('liq', 0):,.0f} left"
        return (f'<div class="c">{label} <a class="ext" href="{escape(t["url"])}"'
                f' target="_blank" rel="noopener"><b>{sym}</b></a> {detail}</div>')

    cards = [
        ("", "Launched", launched,
         f'<div class="c">{num(judged)} with enough history to judge</div>'),
        ("win", "Winners", c.get("winner", 0), callout(lx.get("top_winner"), "top")),
        ("lose", "Losers", c.get("loser", 0), callout(lx.get("top_loser"), "worst")),
        ("dead", "Dead", c.get("dead", 0),
         '<div class="c">liquidity gone</div>'),
    ]
    html = ['<div class="lx-cards">']
    for cls, k, v, foot in cards:
        html.append(
            f'<div class="lx-card {cls} pixel"><span class="k">{k}</span>'
            f'<span class="v">{num(v)}</span>{foot}</div>')
    html.append("</div>")

    rug = lx.get("top_rug")
    n_rug = c.get("rug", 0)
    if rug:
        fell = 100 - (rug.get("liq", 0) / rug["peak_liq"] * 100) if rug.get("peak_liq") else 0
        html.append(
            f'<p class="lx-rug"><b>{num(n_rug)} rugged</b> — pools that held a real '
            f'market and lost it. Biggest: <a class="ext" href="{escape(rug["url"])}" '
            f'target="_blank" rel="noopener"><b>{escape(str(rug.get("s") or "?"))}</b></a> '
            f'took ${rug["peak_liq"]:,.0f} of liquidity down {fell:.0f}% to '
            f'${rug.get("liq", 0):,.0f}.</p>')
    else:
        html.append('<p class="lx-rug">No pool in the window drained a market of '
                    '$5,000 or more.</p>')
    return "\n".join(html)


NFT_LEDGER = OUT_DIR / "nft_launches.jsonl"



def load_nft_launches(path=None, hours=6, top_n=12):
    """New ERC-721 collections from the incremental mint scan.

    A longer window than the memecoin feed (6h vs 24h of trajectory) because
    mints play out slower than pool trading, and a collection's shape is only
    legible once a few hundred wallets have had the chance to mint.

    Ranked by DISTINCT MINTERS, never mint count. Measured separation is wide:
    genuine public mints run 2.6-5.1 mints per minter across 537-945 minters,
    while self-mint farms run 20-150 across 1-7 wallets. Mint count alone cannot
    tell those apart; distinct minters can.
    """
    p = Path(path) if path else NFT_LEDGER
    if not p.exists():
        return {"collections": [], "window_hours": hours}
    latest = {}
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        c = r.get("c")
        if not c:
            continue
        prev = latest.get(c)
        if not prev or r["ts"] > prev["ts"]:
            latest[c] = r

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    out = []
    for c, r in latest.items():
        try:
            born = dt.datetime.fromisoformat(r["first_seen"])
        except (ValueError, KeyError):
            continue
        if born < cutoff:
            continue
        minters = r.get("minters") or 0
        mints = r.get("mints") or 0
        per = mints / minters if minters else 0
        # Same shape of judgement as the token side: a farm is not "small", it
        # is concentrated. Stated as a fact, not an accusation.
        if minters <= 2:
            verdict, why = "farm", "one wallet took every mint"
        elif per >= 15 and minters < 50:
            verdict, why = "farm", f"{per:.0f} mints per wallet across {minters}"
        elif minters < 25:
            verdict, why = "thin", f"only {minters} wallets minted"
        else:
            verdict, why = "public", f"{minters} wallets, {per:.1f} each"
        out.append({"address": c, "mints": mints, "minters": minters,
                    "per": round(per, 2), "verdict": verdict, "why": why,
                    "age_min": round((now - born).total_seconds() / 60),
                    "explorer_url": f"https://robinhoodchain.blockscout.com/token/{c}"})
    out.sort(key=lambda x: (-x["minters"], -x["mints"]))
    counts = {}
    for o in out:
        counts[o["verdict"]] = counts.get(o["verdict"], 0) + 1
    return {"collections": out[:top_n], "total": len(out),
            "counts": counts, "window_hours": hours}


NFT_NAMES = OUT_DIR / "nft_names.json"


def resolve_nft_meta(addrs):
    """Name, symbol and collection size read straight off each contract.

    Blockscout returns nothing for a contract minutes old, which is exactly the
    age this section covers -- every row rendered as a bare 0x address. eth_call
    answers immediately because the data lives in the contract, not an index.

    maxSupply() is the collection size, and with totalSupply() it gives real
    mint progress ("7,000 of 9,999"). Three spellings are tried because there is
    no standard: maxSupply, MAX_SUPPLY, collectionSize.

    Cached on disk. Name and size never change, so a contract is resolved once;
    only the minted count is re-read.
    """
    import urllib.request
    from eth_hash.auto import keccak

    def call(to, sig, kind="str"):
        sel = "0x" + keccak(sig.encode()).hex()[:8]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [{"to": to, "data": sel}, "latest"]}).encode()
        try:
            # The RPC 403s Python-urllib's default User-Agent. It answers fine
            # to anything else, which is why the same call worked under
            # requests and returned nothing here.
            req = urllib.request.Request(
                "https://rpc.mainnet.chain.robinhood.com", body,
                {"Content-Type": "application/json",
                 "User-Agent": "hoodscout/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                res = json.loads(r.read().decode()).get("result")
        except Exception:
            return None
        if not res or res == "0x":
            return None
        if kind == "uint":
            try:
                return int(res, 16)
            except ValueError:
                return None
        b = bytes.fromhex(res[2:])
        if len(b) >= 64:
            n = int.from_bytes(b[32:64], "big")
            return b[64:64 + n].decode("utf8", "replace").rstrip("\0") or None
        return b.decode("utf8", "replace").rstrip("\0") or None

    cache = {}
    if NFT_NAMES.exists():
        try:
            cache = json.loads(NFT_NAMES.read_text())
        except json.JSONDecodeError:
            cache = {}

    dirty = False
    for a in addrs:
        e = cache.get(a) or {}
        if not isinstance(e, dict):
            e = {"name": e or ""}
        if not e.get("name"):
            e["name"] = (call(a, "name()") or "")[:30]
            e["symbol"] = (call(a, "symbol()") or "")[:12]
            e["max"] = (call(a, "maxSupply()", "uint") or call(a, "MAX_SUPPLY()", "uint")
                        or call(a, "collectionSize()", "uint"))
            dirty = True
        minted = call(a, "totalSupply()", "uint")
        if minted is not None and minted != e.get("minted"):
            e["minted"] = minted
            dirty = True
        cache[a] = e
    if dirty:
        NFT_NAMES.parent.mkdir(parents=True, exist_ok=True)
        NFT_NAMES.write_text(json.dumps(cache, indent=0))
    return cache


NFT_VERDICT = {
    "public": ("ok", "many separate wallets minted"),
    "thin": ("warn", "few wallets have minted so far"),
    "farm": ("flag", "mints concentrated in very few wallets"),
}


HEALTH_STATE = {
    "alive": ("ok", "trading, holding its launch value, liquidity intact"),
    "quiet": ("warn", "liquidity intact but nobody is trading it"),
    "fading": ("warn", "market still there, but well under launch value"),
    "dead": ("flag", "liquidity gone"),
    "early": ("warn", "under 30 minutes old"),
}


def nft_launch_rows(cols):
    meta = resolve_nft_meta([c["address"] for c in cols])
    out = []
    for i, c in enumerate(cols, 1):
        m = meta.get(c["address"]) or {}
        nm = m.get("name") or short_addr(c["address"])
        mx, mint = m.get("max"), m.get("minted")
        size = (f"{num(mint)} of {num(mx)} minted" if mx and mint
                else f"{num(mint)} minted" if mint else "")
        cls, tip = NFT_VERDICT.get(c["verdict"], ("warn", ""))
        age = c["age_min"]
        age_s = f"{age}m" if age < 90 else f"{age // 60}h{age % 60:02d}"
        out.append(f"""          <tr class="{'over' if i > 10 else ''}">
            <td class="rank">{i}</td>
            <td class="sym" data-v="{escape(nm.lower())}">
              <span class="sym-name">{link(c['explorer_url'], nm, 'ext strong')}<span
                class="badge {cls}" title="{escape(tip)}">{escape(c['verdict'])}</span></span>
              <span class="sym-sub">{(escape(size) + " &middot; ") if size else ""}first seen {escape(age_s)} ago</span>
            </td>
            <td class="n" data-v="{c['minters']}">{escape(num(c['minters']))}
              <span class="alt">wallets</span></td>
            <td class="n strong" data-v="{c['mints']}">{escape(num(c['mints']))}
              <span class="alt">{c['per']:.1f} each</span></td>
          </tr>""")
    return "\n".join(out)


def byline(path=None):
    """Dave's 0xdgw wordmark, linked to his X account.

    Rendered as a CSS mask rather than an <img>: the source is solid black, so
    an image would vanish against the near-black dark theme. As a mask it takes
    background:currentColor and inherits whichever ink the theme is using.
    """
    import base64
    p = Path(path) if path else (OUT_DIR.parent / "0xdgw_mark.png")
    if not p.exists():
        return ""
    b64 = base64.b64encode(p.read_bytes()).decode()
    return (f'<a class="byline" href="https://x.com/0xdgw" target="_blank" '
            f'rel="noopener noreferrer" aria-label="Built by 0xdgw on X" '
            f'title="Built by 0xdgw">'
            f'<span class="byline-label">Built by</span>'
            f'<span class="byline-mark" style="-webkit-mask-image:url(data:image/png;base64,{b64});'
            f'mask-image:url(data:image/png;base64,{b64})"></span></a>')


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
