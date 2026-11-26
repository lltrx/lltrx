#!/usr/bin/env python3
"""Build the SVG cards shown on the profile README.

Writes light and dark versions of:
  assets/header-{light,dark}.svg   the banner at the top
  assets/stats-{light,dark}.svg    contributions, streaks, monthly activity, languages
  assets/stats.json                the numbers the stats card was drawn from

Usage:
  GITHUB_TOKEN=... python3 scripts/build_cards.py --user lltrx   # fetch from the GitHub API
  python3 scripts/build_cards.py --data assets/stats.json        # redraw from saved numbers

Uses only the standard library, so the workflow needs no install step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"
TOP_LANGUAGES = 6  # the rest fold into "Other"

FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'

# Surfaces and inks follow GitHub's own light and dark themes so the cards sit
# naturally on the profile page. Series colors are a fixed categorical order,
# validated for color-vision deficiencies against these two surfaces.
THEMES = {
    "light": {
        "bg": "#ffffff",
        "raised": "#f6f8fa",
        "border": "#d1d9e0",
        "text": "#1f2328",
        "muted": "#59636e",
        "grid": "#eaeef2",
        "axis": "#c8d1da",
        "accent": "#257de0",
        "glow": 0.10,
        "dots": 0.45,
        "series": ["#257de0", "#f47230", "#00b894", "#f2a600", "#ee7fac", "#008a17"],
        "other": "#afb8c1",
    },
    "dark": {
        "bg": "#0d1117",
        "raised": "#151b23",
        "border": "#3d444d",
        "text": "#f0f6fc",
        "muted": "#9198a1",
        "grid": "#21262d",
        "axis": "#3d444d",
        "accent": "#338ced",
        "glow": 0.22,
        "dots": 0.35,
        "series": ["#338ced", "#e76624", "#00a888", "#d28e05", "#e24d8b", "#0aac23"],
        "other": "#656c76",
    },
}


# --------------------------------------------------------------------------- fetch

def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-cards",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


def fetch(login: str, token: str, now: dt.datetime) -> dict:
    """Return per-day contribution counts and language sizes for public repos."""
    profile = graphql(
        token,
        "query($login: String!) { user(login: $login) { name createdAt } }",
        {"login": login},
    )["user"]
    first_year = int(profile["createdAt"][:4])

    # One aliased contributionsCollection per calendar year (the API caps a
    # collection at one year).
    parts = []
    for year in range(first_year, now.year + 1):
        end = now.isoformat() if year == now.year else f"{year}-12-31T23:59:59Z"
        parts.append(
            f'y{year}: contributionsCollection(from: "{year}-01-01T00:00:00Z", to: "{end}") '
            "{ contributionCalendar { weeks { contributionDays { date contributionCount } } } }"
        )
    years = graphql(
        token,
        "query($login: String!) { user(login: $login) { " + " ".join(parts) + " } }",
        {"login": login},
    )["user"]
    days: dict[str, int] = {}
    for alias, collection in years.items():
        for week in collection["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                # Edge weeks can spill into the neighbouring year; keep each day once.
                if day["date"].startswith(alias[1:]):
                    days[day["date"]] = day["contributionCount"]

    languages: dict[str, int] = defaultdict(int)
    cursor = None
    while True:
        page = graphql(
            token,
            """
            query($login: String!, $cursor: String) {
              user(login: $login) {
                repositories(first: 100, after: $cursor, ownerAffiliations: OWNER,
                             isFork: false, privacy: PUBLIC) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
                      edges { size node { name } }
                    }
                  }
                }
              }
            }
            """,
            {"login": login, "cursor": cursor},
        )["user"]["repositories"]
        for repo in page["nodes"]:
            for edge in repo["languages"]["edges"]:
                languages[edge["node"]["name"]] += edge["size"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    return {"login": login, "name": profile["name"] or login, "days": days, "languages": languages}


# ----------------------------------------------------------------------- summarize

def streaks(days: dict[str, int], today: dt.date) -> tuple[dict, dict]:
    """Current and longest runs of days with at least one contribution.

    A streak that ended yesterday still counts as current, since today may
    not have any contributions yet when the workflow runs.
    """
    active = {dt.date.fromisoformat(d) for d, n in days.items() if n > 0}
    active = {d for d in active if d <= today}

    longest = {"days": 0, "start": None, "end": None}
    run_start = None
    for d in sorted(active):
        if run_start is None or d - prev != dt.timedelta(days=1):
            run_start = d
        prev = d
        length = (d - run_start).days + 1
        if length > longest["days"]:
            longest = {"days": length, "start": run_start.isoformat(), "end": d.isoformat()}

    current = {"days": 0, "start": None, "end": None}
    end = today if today in active else today - dt.timedelta(days=1)
    if end in active:
        start = end
        while start - dt.timedelta(days=1) in active:
            start -= dt.timedelta(days=1)
        current = {"days": (end - start).days + 1, "start": start.isoformat(), "end": end.isoformat()}
    return current, longest


def summarize(raw: dict, today: dt.date) -> dict:
    days = {d: n for d, n in raw["days"].items() if dt.date.fromisoformat(d) <= today}

    per_year: dict[str, int] = defaultdict(int)
    per_month: dict[str, int] = defaultdict(int)
    for d, n in days.items():
        per_year[d[:4]] += n
        per_month[d[:7]] += n

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()

    current, longest = streaks(days, today)
    active_years = sorted(y for y, n in per_year.items() if n > 0)

    total_bytes = sum(raw["languages"].values()) or 1
    ranked = sorted(raw["languages"].items(), key=lambda kv: kv[1], reverse=True)
    languages = [[name, round(100 * size / total_bytes, 2)] for name, size in ranked[:TOP_LANGUAGES]]
    rest = sum(size for _, size in ranked[TOP_LANGUAGES:])
    if rest:
        languages.append(["Other", round(100 * rest / total_bytes, 2)])

    return {
        "login": raw["login"],
        "name": raw["name"],
        "updated": today.isoformat(),
        "total": sum(days.values()),
        "first_year": int(active_years[0]) if active_years else today.year,
        "this_year": per_year.get(str(today.year), 0),
        "per_year": dict(sorted(per_year.items())),
        "months": [[k, per_month.get(k, 0)] for k in months],
        "current_streak": current,
        "longest_streak": longest,
        "languages": languages,
    }


# -------------------------------------------------------------------------- render

def fmt_day(iso: str, with_year: bool) -> str:
    d = dt.date.fromisoformat(iso)
    text = f"{d:%b} {d.day}"
    return f"{text}, {d.year}" if with_year else text


def nice_step(top: float) -> float:
    """Gridline step so that two steps cover `top` with a round number."""
    raw = max(top, 1) / 2
    magnitude = 10 ** (len(str(int(raw))) - 1)
    for factor in (1, 1.5, 2, 2.5, 5, 10):
        if factor * magnitude >= raw:
            return factor * magnitude
    return 10 * magnitude


def column(x: float, base: float, w: float, h: float, r: float = 4) -> str:
    """A bar rounded at its data end and square at the baseline."""
    r = min(r, h, w / 2)
    top = base - h
    return (
        f"M{x:.1f},{base:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{base:.1f} Z"
    )


def svg_open(width: int, height: int, title: str, desc: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">',
        f'<title id="t">{escape(title)}</title>',
        f'<desc id="d">{escape(desc)}</desc>',
    ]


def render_stats(s: dict, theme: str) -> str:
    c = THEMES[theme]
    W, H = 840, 372
    today = dt.date.fromisoformat(s["updated"])
    cur, best = s["current_streak"], s["longest_streak"]

    def streak_sub(streak: dict, ongoing: bool) -> str:
        if not streak["days"]:
            return "No active streak"
        if ongoing:
            return f"since {fmt_day(streak['start'], streak['start'][:4] != str(today.year))}"
        with_year = streak["end"][:4] != str(today.year)
        return f"{fmt_day(streak['start'], False)} – {fmt_day(streak['end'], with_year)}"

    tiles = [
        ("All-time contributions", f"{s['total']:,}", "", f"since {s['first_year']}"),
        (f"Contributions in {today.year}", f"{s['this_year']:,}", "", "so far this year"),
        ("Current streak", str(cur["days"]), " day" if cur["days"] == 1 else " days", streak_sub(cur, True)),
        ("Longest streak", str(best["days"]), " day" if best["days"] == 1 else " days", streak_sub(best, False)),
    ]
    desc = (
        f"{s['total']:,} contributions since {s['first_year']}, {s['this_year']:,} in {today.year}. "
        f"Current streak {cur['days']} days, longest {best['days']} days. "
        "Contributions per month: "
        + ", ".join(f"{dt.date.fromisoformat(k + '-01'):%b %Y} {n:,}" for k, n in s["months"])
        + ". Languages: "
        + ", ".join(f"{name} {pct:.1f}%" for name, pct in s["languages"])
        + "."
    )

    out = svg_open(W, H, f"GitHub activity for {s['login']}", desc)
    out.append(
        "<style>"
        f"text{{font-family:{FONT};fill:{c['text']}}}"
        f".m{{fill:{c['muted']}}}"
        ".num{font-variant-numeric:tabular-nums}"
        "</style>"
    )
    out.append(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" '
        f'fill="{c["bg"]}" stroke="{c["border"]}"/>'
    )

    # Stat tiles
    tile_w = (W - 56) / 4
    for i, (label, value, unit, sub) in enumerate(tiles):
        x = 28 + i * tile_w + (0 if i == 0 else 20)
        if i:
            out.append(
                f'<line x1="{28 + i * tile_w:.1f}" y1="32" x2="{28 + i * tile_w:.1f}" y2="112" '
                f'stroke="{c["grid"]}"/>'
            )
        out.append(f'<text x="{x:.1f}" y="50" font-size="13" class="m">{escape(label)}</text>')
        unit_span = f'<tspan font-size="16" font-weight="400" class="m">{escape(unit)}</tspan>' if unit else ""
        out.append(
            f'<text x="{x:.1f}" y="88" font-size="32" font-weight="600" letter-spacing="-0.5">'
            f"{escape(value)}{unit_span}</text>"
        )
        out.append(f'<text x="{x:.1f}" y="110" font-size="12" class="m">{escape(sub)}</text>')

    out.append(f'<line x1="28" y1="136" x2="{W - 28}" y2="136" stroke="{c["grid"]}"/>')

    # Contributions per month: one series, so the title names it and no legend.
    out.append('<text x="28" y="168" font-size="14" font-weight="600">Contributions per month</text>')
    out.append('<text x="28" y="186" font-size="12" class="m">Last 12 months</text>')
    left, right, top, base = 72, 480, 214, 318
    values = [n for _, n in s["months"]]
    step = nice_step(max(values))
    scale_top = 2 * step
    for k in range(3):
        y = base - (base - top) * k / 2
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
            f'stroke="{c["axis"] if k == 0 else c["grid"]}"/>'
        )
        out.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" class="m num">'
            f"{int(step * k):,}</text>"
        )
    band = (right - left) / len(values)
    bar_w = min(20, band - 8)
    peak = max(range(len(values)), key=lambda i: values[i])
    labelled = {peak}
    if abs(len(values) - 1 - peak) > 1:
        labelled.add(len(values) - 1)
    for i, ((key, n), v) in enumerate(zip(s["months"], values)):
        cx = left + band * i + band / 2
        h = (base - top) * v / scale_top
        if v:
            out.append(f'<path d="{column(cx - bar_w / 2, base, bar_w, max(h, 2))}" fill="{c["accent"]}"/>')
        month = dt.date.fromisoformat(key + "-01")
        out.append(
            f'<text x="{cx:.1f}" y="{base + 18}" font-size="11" text-anchor="middle" class="m">'
            f"{month:%b}</text>"
        )
        if i in labelled and v:
            out.append(
                f'<text x="{cx:.1f}" y="{base - max(h, 2) - 7:.1f}" font-size="11" font-weight="600" '
                f'text-anchor="middle" class="num">{v:,}</text>'
            )

    # Languages: a thin segmented bar plus a legend that names every part.
    lx, lw = 528, W - 28 - 528
    out.append(f'<text x="{lx}" y="168" font-size="14" font-weight="600">Languages</text>')
    out.append(f'<text x="{lx}" y="186" font-size="12" class="m">By code size in public repos</text>')
    langs = s["languages"]
    colors = [c["other"] if name == "Other" else c["series"][i] for i, (name, _) in enumerate(langs)]
    gap = 2
    usable = lw - gap * (len(langs) - 1)
    total_pct = sum(p for _, p in langs) or 1
    out.append(f'<clipPath id="bar"><rect x="{lx}" y="206" width="{lw}" height="8" rx="4"/></clipPath>')
    out.append('<g clip-path="url(#bar)">')
    x = float(lx)
    for (name, pct), color in zip(langs, colors):
        w = max(2.0, usable * pct / total_pct)
        out.append(f'<rect x="{x:.1f}" y="206" width="{w:.1f}" height="8" fill="{color}"/>')
        x += w + gap
    out.append("</g>")
    col_w, per_col = (lw - 16) / 2, (len(langs) + 1) // 2
    for i, ((name, pct), color) in enumerate(zip(langs, colors)):
        col, row = divmod(i, per_col)
        x0 = lx + col * (col_w + 16)
        y0 = 244 + row * 26
        share = "<1%" if pct < 1 else f"{round(pct)}%"
        out.append(f'<circle cx="{x0 + 5}" cy="{y0 - 4.5}" r="4.5" fill="{color}"/>')
        out.append(f'<text x="{x0 + 17}" y="{y0}" font-size="13">{escape(name)}</text>')
        out.append(
            f'<text x="{x0 + col_w:.1f}" y="{y0}" font-size="12" text-anchor="end" class="m num">'
            f"{share}</text>"
        )

    out.append(
        f'<text x="{W - 28}" y="{H - 18}" font-size="11" text-anchor="end" class="m">'
        f"Updated {fmt_day(s['updated'], True)}</text>"
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


# A row of clipboard cards, a nod to Shelf, drifting along the bottom of the banner.
SHELF_CARDS = [
    ("Text", "Ship Shelf 2.0", False, None),
    ("Code", "func paste() {}", True, None),
    ("Color", "#2F6FEB", True, "#2F6FEB"),
    ("Link", "github.com/lltrx", False, None),
    ("Snippet", "Thanks! {{date}}", True, None),
    ("API key", "••••••••0042", True, None),
    ("Text", "Hello from Riyadh", False, None),
    ("Code", "npm run dev", True, None),
]


def render_header(theme: str, name: str, words: list[str]) -> str:
    c = THEMES[theme]
    W, H = 840, 248
    pitch, card_w, card_h, card_y = 140, 128, 54, 166
    loop = pitch * len(SHELF_CARDS)
    cycle = 3 * len(words)

    out = svg_open(W, H, name, f"{name}. I build apps for {', '.join(words[:-1])} and {words[-1]}.")
    keyframes = (
        f"@keyframes drift{{from{{transform:translateX(0)}}to{{transform:translateX(-{loop}px)}}}}"
        "@keyframes word{0%{opacity:0;transform:translateY(8px)}"
        f"{100 * 0.4 / cycle:.2f}%,{100 * 2.6 / cycle:.2f}%{{opacity:1;transform:translateY(0)}}"
        f"{100 * 3 / cycle:.2f}%,100%{{opacity:0;transform:translateY(-8px)}}}}"
    )
    out.append(
        "<style>"
        f"text{{font-family:{FONT};fill:{c['text']}}}"
        f".m{{fill:{c['muted']}}}.a{{fill:{c['accent']}}}"
        f".mono{{font-family:{MONO}}}"
        f".drift{{animation:drift 70s linear infinite}}"
        f".w{{opacity:0;animation:word {cycle}s ease-in-out infinite backwards}}"
        ".w0{opacity:1}"
        + "".join(f".w{i}{{animation-delay:{3 * i}s}}" for i in range(1, len(words)))
        + keyframes
        + "@media (prefers-reduced-motion:reduce){.drift,.w{animation:none}}"
        "</style>"
    )
    out.append(
        "<defs>"
        '<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        f'<circle cx="2" cy="2" r="1.1" fill="{c["muted"]}" opacity="{c["dots"]}"/></pattern>'
        '<radialGradient id="fade" cx="50%" cy="38%" r="62%">'
        '<stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>'
        "</radialGradient>"
        f'<mask id="dotmask"><rect width="{W}" height="{H}" fill="url(#fade)"/></mask>'
        '<radialGradient id="glow" cx="50%" cy="50%" r="50%">'
        f'<stop offset="0" stop-color="{c["accent"]}" stop-opacity="{c["glow"]}"/>'
        f'<stop offset="1" stop-color="{c["accent"]}" stop-opacity="0"/></radialGradient>'
        '<linearGradient id="edges" x1="0" x2="1">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        '<stop offset="0.16" stop-color="#fff"/><stop offset="0.84" stop-color="#fff"/>'
        '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
        f'<mask id="edgemask"><rect width="{W}" height="{H}" fill="url(#edges)"/></mask>'
        f'<clipPath id="frame"><rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="11"/></clipPath>'
        "</defs>"
    )
    out.append(f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="{c["bg"]}"/>')
    out.append('<g clip-path="url(#frame)">')
    out.append(f'<rect width="{W}" height="{H}" fill="url(#dots)" mask="url(#dotmask)"/>')
    out.append(f'<ellipse cx="{W / 2}" cy="96" rx="330" ry="120" fill="url(#glow)"/>')

    out.append(
        f'<text x="{W / 2}" y="88" font-size="46" font-weight="700" text-anchor="middle" '
        f'letter-spacing="-1">{escape(name)}</text>'
    )
    pivot = W / 2 + 38
    out.append(
        f'<text x="{pivot - 4}" y="130" font-size="21" text-anchor="end" class="m">I build apps for</text>'
    )
    for i, word in enumerate(words):
        out.append(
            f'<text x="{pivot + 4}" y="130" font-size="21" font-weight="600" class="a w w{i}">'
            f"{escape(word)}</text>"
        )

    out.append('<g mask="url(#edgemask)"><g class="drift">')
    for copy in range(2):
        for i, (kind, body, mono, swatch) in enumerate(SHELF_CARDS):
            x = 24 + copy * loop + i * pitch
            out.append(
                f'<rect x="{x}" y="{card_y}" width="{card_w}" height="{card_h}" rx="10" '
                f'fill="{c["raised"]}" stroke="{c["border"]}"/>'
            )
            out.append(f'<text x="{x + 12}" y="{card_y + 20}" font-size="10.5" class="m">{kind}</text>')
            tx = x + 12
            if swatch:
                out.append(
                    f'<rect x="{tx}" y="{card_y + 29}" width="14" height="14" rx="3" fill="{swatch}"/>'
                )
                tx += 20
            cls = ' class="mono"' if mono else ""
            out.append(
                f'<text x="{tx}" y="{card_y + 41}" font-size="12"{cls}>{escape(body)}</text>'
            )
    out.append("</g></g></g>")
    out.append(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="none" stroke="{c["border"]}"/>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--user", help="GitHub login to fetch (needs GITHUB_TOKEN)")
    source.add_argument("--data", type=Path, help="redraw from a saved stats.json instead of fetching")
    parser.add_argument("--out", type=Path, default=Path("assets"), help="output folder (default: assets)")
    args = parser.parse_args()

    if args.data:
        summary = json.loads(args.data.read_text())
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            parser.error("set GITHUB_TOKEN to fetch from the GitHub API")
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        summary = summarize(fetch(args.user, token, now), now.date())

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "stats.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    for theme in THEMES:
        (args.out / f"stats-{theme}.svg").write_text(render_stats(summary, theme))
        (args.out / f"header-{theme}.svg").write_text(
            render_header(theme, summary["name"], ["mobile", "the web", "the Mac"])
        )
    print(
        f"{summary['login']}: {summary['total']:,} contributions, "
        f"streak {summary['current_streak']['days']} (best {summary['longest_streak']['days']}), "
        f"{len(summary['languages'])} languages -> {args.out}/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
