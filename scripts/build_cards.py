#!/usr/bin/env python3
"""Build the SVG cards shown on the profile README.

Writes light and dark versions of:
  assets/header-{light,dark}.svg     skyline banner: one tower per week of contributions
  assets/terminal-{light,dark}.svg   terminal that types out whoami, now, uptime and next
  assets/stats-{light,dark}.svg      contributions, streaks, monthly activity, languages
  assets/certs-{light,dark}.svg      certifications as holographic cards (when profile.json lists any)
  assets/stats.json                  the numbers the cards were drawn from

Text on the cards (tagline, terminal lines, projects, certifications) comes from profile.json,
and the README's certifications section is regenerated from it (expired ones drop off).

Usage:
  GITHUB_TOKEN=... python3 scripts/build_cards.py --user lltrx   # fetch from the GitHub API
  python3 scripts/build_cards.py --data assets/stats.json        # redraw from saved numbers

Uses only the standard library, so the workflow needs no install step.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import random
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


def weekly_totals(days: dict[str, int], today: dt.date, count: int = 52) -> list[list]:
    """Contributions in each of the last `count` 7-day windows, oldest first."""
    weeks = []
    for back in range(count - 1, -1, -1):
        start = today - dt.timedelta(days=7 * back + 6)
        total = sum(days.get((start + dt.timedelta(days=k)).isoformat(), 0) for k in range(7))
        weeks.append([start.isoformat(), total])
    return weeks


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
        "weeks": weekly_totals(days, today),
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


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colors; t=0 gives c1, t=1 gives c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def pts(*points: tuple[float, float]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


# The header is a city at night (a city by day in light mode). Each tower is one
# week of contributions, so the skyline is redrawn from real data every day.
# Tower faces blend from the "short" color to the "tall" color with height.
SKYLINE = {
    "dark": {
        "sky": ("#05080f", "#0d1117"),
        "blobs": [("#338ced", 0.30), ("#9887ef", 0.22), ("#00a888", 0.16)],
        "ground": 0.22,
        "top": ("#2b5590", "#a8d4ff"),
        "left": ("#18325a", "#3f8ae6"),
        "right": ("#0e1f3c", "#1d58ad"),
        "edge": 0.35,
        "lot": "#1a2230",
        "window": "#ffd27a",
        "window_opacity": 0.92,
        "lit": 0.42,
        "night": True,
        "name": ("#f0f6fc", "#a5d0ff"),
        "shine": 0.6,
    },
    "light": {
        "sky": ("#e8f1ff", "#ffffff"),
        "blobs": [("#84b9f3", 0.40), ("#c9bdf7", 0.34), ("#8fe0cf", 0.26)],
        "ground": 0.14,
        "top": ("#e9f2fd", "#ffffff"),
        "left": ("#bcd5f2", "#78acea"),
        "right": ("#98bbe4", "#3b82d6"),
        "edge": 0.0,
        "lot": "#dde5ee",
        "window": "#0f4a96",
        "window_opacity": 0.26,
        "lit": 0.55,
        "night": False,
        "name": ("#1f2328", "#1757ac"),
        "shine": 0.85,
    },
}


def render_header(theme: str, s: dict, words: list[str], subtitle: str) -> str:
    c, k = THEMES[theme], SKYLINE[theme]
    W, H = 840, 350
    rng = random.Random(1307)  # fixed seed: same data, same picture
    weeks = s["weeks"]
    counts = [n for _, n in weeks]
    best = max(range(len(counts)), key=lambda i: counts[i])
    top_week = counts[best] or 1
    cycle = 3 * len(words)
    first = dt.date.fromisoformat(weeks[0][0])
    last = dt.date.fromisoformat(s["updated"])
    caption = f"The skyline is my GitHub activity: one tower per week, {first:%b %Y} – {last:%b %Y}"

    out = svg_open(
        W, H, name := s["name"],
        f"{name}. I build apps for {', '.join(words[:-1])} and {words[-1]}. {subtitle}. "
        f"A city skyline where each of {len(weeks)} towers is one week of GitHub contributions; "
        f"the tallest, {counts[best]:,} contributions, is the week of {fmt_day(weeks[best][0], True)}.",
    )

    # ---- styles and animations (all CSS, so prefers-reduced-motion can stop them)
    css = [
        f"text{{font-family:{FONT};fill:{c['text']}}}",
        f".m{{fill:{c['muted']}}}.a{{fill:{c['accent']}}}",
        f".w{{opacity:0;animation:word {cycle}s ease-in-out infinite backwards}}.w0{{opacity:1}}",
        *(f".w{i}{{animation-delay:{3 * i}s}}" for i in range(1, len(words))),
        "@keyframes word{0%{opacity:0;transform:translateY(8px)}"
        f"{100 * 0.4 / cycle:.2f}%,{100 * 2.6 / cycle:.2f}%{{opacity:1;transform:translateY(0)}}"
        f"{100 * 3 / cycle:.2f}%,100%{{opacity:0;transform:translateY(-8px)}}}}",
        # towers rise from the ground one after another
        ".t{transform-box:fill-box;transform-origin:50% 100%;"
        "animation:rise 1.1s cubic-bezier(.2,.9,.25,1) backwards}",
        "@keyframes rise{from{transform:scaleY(0)}}",
        # aurora blobs drift slowly
        ".b{animation:drift 18s ease-in-out infinite alternate}",
        ".b1{animation-duration:23s;animation-direction:alternate-reverse}.b2{animation-duration:29s}",
        "@keyframes drift{to{transform:translate(46px,14px)}}",
        # light sweep across the name
        ".shine{animation:shine 7s ease-in-out 1.6s infinite}",
        "@keyframes shine{0%{transform:translateX(0)}30%,100%{transform:translateX(1150px)}}",
        # windows, stars, aviation light, shooting star
        ".f{animation:flicker 4.3s steps(1) infinite}",
        "@keyframes flicker{0%,100%{opacity:1}40%{opacity:.25}48%{opacity:1}71%{opacity:.4}}",
        ".tw{animation:twinkle 3.1s ease-in-out infinite alternate}",
        "@keyframes twinkle{from{opacity:.15}to{opacity:1}}",
        ".beacon{animation:beacon 1.8s steps(1) infinite}",
        "@keyframes beacon{0%{opacity:1}50%{opacity:.12}}",
        ".meteor{opacity:0;animation:meteor 9s linear 3s infinite}",
        "@keyframes meteor{0%{opacity:0;transform:translate(0,0)}2%{opacity:1}"
        "11%{opacity:0;transform:translate(300px,118px)}100%{opacity:0;transform:translate(300px,118px)}}",
        ".late{animation:appear .6s ease-out backwards}",
        "@keyframes appear{from{opacity:0;transform:translateY(6px)}}",
        "@media (prefers-reduced-motion:reduce){.w,.t,.b,.shine,.f,.tw,.beacon,.meteor,.late{animation:none}}",
    ]
    out.append("<style>" + "".join(css) + "</style>")

    # ---- defs
    defs = [
        f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{k["sky"][0]}"/>'
        f'<stop offset="1" stop-color="{k["sky"][1]}"/></linearGradient>',
        f'<clipPath id="frame"><rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="11"/></clipPath>',
        f'<radialGradient id="ground"><stop offset="0" stop-color="{c["accent"]}" stop-opacity="{k["ground"]}"/>'
        f'<stop offset="1" stop-color="{c["accent"]}" stop-opacity="0"/></radialGradient>',
        f'<linearGradient id="namefill" gradientUnits="userSpaceOnUse" x1="{W / 2 - 170}" x2="{W / 2 + 170}">'
        f'<stop offset="0" stop-color="{k["name"][0]}"/><stop offset="1" stop-color="{k["name"][1]}"/></linearGradient>',
        '<linearGradient id="shinefill"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="#fff" stop-opacity="{k["shine"]}"/>'
        '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>',
        '<linearGradient id="meteorfill" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="70" y2="28">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff"/></linearGradient>',
        f'<radialGradient id="moonglow"><stop offset="0" stop-color="#ffd27a" stop-opacity="{0.22 if k["night"] else 0.5}"/>'
        '<stop offset="1" stop-color="#ffd27a" stop-opacity="0"/></radialGradient>',
        '<clipPath id="nameclip">'
        f'<text x="{W / 2}" y="84" font-size="50" font-weight="800" text-anchor="middle" letter-spacing="-1.2">'
        f"{escape(name)}</text></clipPath>",
    ]
    for i, (color, alpha) in enumerate(k["blobs"]):
        defs.append(
            f'<radialGradient id="blob{i}"><stop offset="0" stop-color="{color}" stop-opacity="{alpha}"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient>'
        )
    out.append("<defs>" + "".join(defs) + "</defs>")

    out.append(f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="url(#sky)"/>')
    out.append('<g clip-path="url(#frame)">')

    # ---- aurora
    for i, (cx, cy, rx, ry) in enumerate([(210, 70, 260, 120), (640, 60, 280, 110), (430, 190, 330, 90)]):
        out.append(f'<ellipse class="b b{i}" cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="url(#blob{i})"/>')

    # ---- stars and a shooting star (night only)
    if k["night"]:
        for i in range(70):
            x, y = rng.uniform(8, W - 8), rng.uniform(8, 210)
            r, o = rng.uniform(0.5, 1.25), rng.uniform(0.25, 0.85)
            twinkle = rng.random() < 0.25
            extra = f' class="tw" style="animation-delay:{rng.uniform(0, 3):.2f}s"' if twinkle else ""
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="#fff" opacity="{o:.2f}"{extra}/>')
        out.append(
            '<g transform="translate(70,22)"><line class="meteor" x1="0" y1="0" x2="70" y2="28" '
            'stroke="url(#meteorfill)" stroke-width="1.6" stroke-linecap="round"/></g>'
        )
        # a crescent moon: a disc with a smaller offset disc cut out of it
        out.append(
            '<mask id="crescent"><circle cx="772" cy="58" r="15" fill="#fff"/>'
            '<circle cx="779" cy="53" r="13" fill="#000"/></mask>'
        )
        out.append('<circle cx="772" cy="58" r="34" fill="url(#moonglow)"/>')
        out.append('<circle cx="772" cy="58" r="15" fill="#fff4d6" mask="url(#crescent)"/>')
    else:
        out.append('<circle cx="772" cy="58" r="46" fill="url(#moonglow)"/>')
        out.append('<circle cx="772" cy="58" r="15" fill="#ffd27a" opacity=".9"/>')

    # ---- the city
    margin, max_h = 26, 118
    step = (W - 2 * margin) / (len(counts) + 1)
    hw = step * 0.86          # half-width of a tower's footprint on screen
    hd = hw * 0.577           # half-depth (isometric: tan 30°)
    row_gap = step * 0.577
    back_y = H - 58
    out.append(f'<ellipse cx="{W / 2}" cy="{back_y + 6}" rx="470" ry="58" fill="url(#ground)"/>')

    towers = []
    for i, n in enumerate(counts):
        cx = margin + step * (i + 1)
        cy = back_y + (row_gap if i % 2 else 0)
        h = 0 if n == 0 else 5 + (max_h - 5) * n / top_week
        towers.append((i, n, cx, cy, h))

    beacon = None
    for i, n, cx, cy, h in sorted(towers, key=lambda t: (t[0] % 2, t[0])):  # back row, then front row
        A, B, C, D = (cx, cy - hd), (cx + hw, cy), (cx, cy + hd), (cx - hw, cy)
        up = lambda p, z=h: (p[0], p[1] - z)  # noqa: E731
        if n == 0:
            out.append(f'<polygon points="{pts(A, B, C, D)}" fill="{k["lot"]}"/>')
            continue
        t = (n / top_week) ** 0.6
        top, left, right = (mix(*k[face], t) for face in ("top", "left", "right"))
        g = [f'<g class="t" style="animation-delay:{0.25 + i * 0.035:.2f}s">']
        g.append(f'<polygon points="{pts(D, C, up(C), up(D))}" fill="{left}"/>')
        g.append(f'<polygon points="{pts(C, B, up(B), up(C))}" fill="{right}"/>')
        edge = f' stroke="{mix(top, "#ffffff", 0.5)}" stroke-opacity="{k["edge"]}" stroke-width="0.6"' if k["edge"] else ""
        g.append(f'<polygon points="{pts(up(A), up(B), up(C), up(D))}" fill="{top}"{edge}/>')

        # windows: two columns per face, a floor every 6.5px
        z = 6.0
        while z + 3 < h - 4:
            for p0, p1 in ((D, C), (C, B)):
                for u in (0.22, 0.58):
                    if rng.random() > k["lit"]:
                        continue
                    du = 0.22
                    q = [
                        (p0[0] + (p1[0] - p0[0]) * uu, p0[1] + (p1[1] - p0[1]) * uu - zz)
                        for uu, zz in ((u, z), (u + du, z), (u + du, z + 3), (u, z + 3))
                    ]
                    flicker = ""
                    if k["night"] and rng.random() < 0.06:
                        flicker = f' class="f" style="animation-delay:{rng.uniform(0, 4):.2f}s"'
                    g.append(
                        f'<polygon points="{pts(*q)}" fill="{k["window"]}" '
                        f'opacity="{k["window_opacity"]}"{flicker}/>'
                    )
            z += 6.5
        g.append("</g>")
        out.extend(g)
        if i == best:
            beacon = (cx, cy - h)

    # ---- best week: antenna, blinking light and a label
    if beacon:
        bx, by = beacon
        # Appears once the city has finished rising.
        out.append(f'<g class="late" style="animation-delay:{0.25 + len(counts) * 0.035 + 0.9:.2f}s">')
        out.append(f'<line x1="{bx}" y1="{by:.1f}" x2="{bx}" y2="{by - 11:.1f}" stroke="{c["muted"]}" stroke-width="1"/>')
        out.append(f'<circle class="beacon" cx="{bx}" cy="{by - 12:.1f}" r="5" fill="#ff5f57" opacity=".25"/>')
        out.append(f'<circle class="beacon" cx="{bx}" cy="{by - 12:.1f}" r="2.2" fill="#ff5f57"/>')
        if abs(bx - W / 2) < 240:  # under the title: put the label beside the light instead
            anchor, lx, ly = "start", bx + 10, by - 8
        elif bx > W - 140:
            anchor, lx, ly = "end", bx + 8, by - 24
        else:
            anchor, lx, ly = "middle", bx, by - 24
        out.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11.5" font-weight="600" text-anchor="{anchor}">'
            f'Best week <tspan class="m" font-weight="400">· {counts[best]:,} contributions</tspan></text>'
        )
        out.append("</g>")

    # ---- name with a light sweep, rotating tagline, subtitle
    out.append('<g clip-path="url(#nameclip)">')
    out.append(f'<rect x="0" y="20" width="{W}" height="80" fill="url(#namefill)"/>')
    out.append('<g transform="skewX(-22)"><rect class="shine" x="-120" y="20" width="110" height="80" fill="url(#shinefill)"/></g>')
    out.append("</g>")
    pivot = W / 2 + 38
    out.append(f'<text x="{pivot - 4}" y="124" font-size="21" text-anchor="end" class="m">I build apps for</text>')
    for i, word in enumerate(words):
        out.append(
            f'<text x="{pivot + 4}" y="124" font-size="21" font-weight="600" class="a w w{i}">{escape(word)}</text>'
        )
    out.append(f'<text x="{W / 2}" y="154" font-size="14" text-anchor="middle" class="m">{escape(subtitle)}</text>')
    out.append(f'<text x="{W / 2}" y="{H - 14}" font-size="11" text-anchor="middle" class="m">{escape(caption)}</text>')
    out.append("</g>")
    out.append(
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="none" stroke="{c["border"]}"/>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- terminal card

TERMINAL = {
    "dark": {"bg": "#0d1117", "bar": "#161b22", "border": "#30363d", "title": "#9198a1", "prompt": "#3fb950",
             "dir": "#39c5cf", "cmd": "#e6edf3", "out": "#9198a1", "dim": "#6e7681", "hl": "#79c0ff",
             "hl2": "#d2a8ff", "num": "#e3b341", "cursor": "#58a6ff"},
    "light": {"bg": "#ffffff", "bar": "#f6f8fa", "border": "#d1d9e0", "title": "#59636e", "prompt": "#1a7f37",
              "dir": "#1b7c83", "cmd": "#1f2328", "out": "#59636e", "dim": "#8c959f", "hl": "#0550ae",
              "hl2": "#8250df", "num": "#9a6700", "cursor": "#0969da"},
}
CHAR_W = 8.45  # approximate advance of a 14px monospace glyph


def render_terminal(s: dict, profile: dict, theme: str) -> str:
    """A terminal that types out whoami, now, uptime (live numbers) and next."""
    k, term = TERMINAL[theme], profile["terminal"]
    cur, best = s["current_streak"]["days"], s["longest_streak"]["days"]
    today = dt.date.fromisoformat(s["updated"])
    pad = max(len(name) for name, _ in term["now"]) + 3
    lines = [("cmd", "whoami"), ("out", [("hl", s["name"]), ("out", f" · {term['whoami']}")]), ("cmd", "cat now.md")]
    lines += [("out", [("dim", "• "), ("hl", name.ljust(pad)), ("out", what)]) for name, what in term["now"]]
    lines += [("cmd", "ls ~/projects"), ("out", [("hl", "  ".join(term["projects"]))])]
    lines += [
        ("cmd", "uptime"),
        ("out", [("out", "streak "), ("num", f"{cur} day" + ("" if cur == 1 else "s")), ("out", " · best "),
                 ("num", str(best)), ("out", " · "), ("num", f"{s['this_year']:,}"),
                 ("out", f" contributions in {today.year}")]),
        ("cmd", "next"),
        ("out", [("hl2", term["next"]), ("out", f" · {term['status']}")]),
    ]
    W, top, step = 840, 78, 25
    H = top + step * (len(lines) - 1) + 30
    desc = " ".join(
        (f"$ {c}" if kind == "cmd" else "".join(text for _, text in c)) for kind, c in lines
    )
    out = svg_open(W, H, f"{s['name']} in a terminal", desc)
    out.append(
        "<style>"
        f"text{{font-family:{MONO};font-size:14px;fill:{k['out']}}}"
        f".title{{font-family:{FONT};font-size:12px;fill:{k['title']}}}"
        + "".join(f".{name}{{fill:{k[name]}}}" for name in ("prompt", "dir", "cmd", "out", "dim", "num"))
        + f".hl{{fill:{k['hl']};font-weight:600}}.hl2{{fill:{k['hl2']};font-weight:600}}"
        ".line{animation:show .05s linear backwards}"
        ".type{transform-box:fill-box;transform-origin:left center;animation:typing 1s backwards}"
        ".cursor{animation:show .05s linear backwards,blink 1s steps(1) infinite}"
        "@keyframes show{from{opacity:0}to{opacity:1}}"
        "@keyframes typing{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        "@keyframes blink{0%{fill-opacity:1}50%{fill-opacity:0}}"
        "@media (prefers-reduced-motion:reduce){.line,.type,.cursor{animation:none}}"
        "</style>"
    )
    t, y, clips, body = 0.4, top, [], []
    for i, (kind, content) in enumerate(lines):
        if kind == "cmd":
            n, dur = len(content), 0.055 * len(content)
            clips.append(
                f'<clipPath id="c{i}"><rect class="type" x="62" y="{y - 17}" width="{n * CHAR_W + 6:.0f}" height="24" '
                f'style="animation-duration:{dur:.2f}s;animation-delay:{t + 0.35:.2f}s;'
                f'animation-timing-function:steps({n})"/></clipPath>'
            )
            body.append(
                f'<g class="line" style="animation-delay:{t:.2f}s">'
                f'<text x="24" y="{y}"><tspan class="prompt">$</tspan> <tspan class="dir">~</tspan></text>'
                f'<text x="62" y="{y}" class="cmd" clip-path="url(#c{i})">{escape(content)}</text></g>'
            )
            t += 0.35 + dur + 0.25
        else:
            spans = "".join(f'<tspan class="{cls}">{escape(text)}</tspan>' for cls, text in content)
            body.append(
                f'<text x="24" y="{y}" class="line" style="animation-delay:{t:.2f}s;white-space:pre" '
                f'xml:space="preserve">{spans}</text>'
            )
            t += 0.12
        y += step
    last_len = sum(len(text) for _, text in lines[-1][1])
    out.append("<defs>" + "".join(clips) + "</defs>")
    out.append(f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="{k["bg"]}" stroke="{k["border"]}"/>')
    out.append(f'<path d="M0.5,12.5 a12,12 0 0 1 12,-12 h{W - 25} a12,12 0 0 1 12,12 v24 h-{W - 1}z" fill="{k["bar"]}"/>')
    out.append(f'<line x1="0.5" y1="36.5" x2="{W - 0.5}" y2="36.5" stroke="{k["border"]}"/>')
    for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append(f'<circle cx="{22 + i * 20}" cy="18.5" r="6" fill="{color}"/>')
    out.append(f'<text x="{W / 2}" y="23" text-anchor="middle" class="title">{escape(term["host"])}: ~</text>')
    out += body
    out.append(
        f'<rect class="cursor" style="animation-delay:{t:.2f}s,{t:.2f}s" x="{24 + CHAR_W * (last_len + 1):.0f}" '
        f'y="{y - step - 15}" width="9" height="19" fill="{k["cursor"]}"/>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ certification cards

HOLO = ["#ff6ec7", "#7afcff", "#fdff8f", "#8affb0", "#a58bff", "#ff6ec7", "#7afcff"]


def wrap(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line] if line else lines


def active_certs(certs: list[dict], today: str) -> list[dict]:
    """Certifications that have not expired yet, oldest first."""
    live = [c for c in certs if c.get("expires", "9999-12-31") >= today]
    return sorted(live, key=lambda c: c["issued"])


def render_certs(certs: list[dict], theme: str, base: Path = Path(".")) -> str:
    """Certifications as holographic trading cards in a fanned row."""
    c = THEMES[theme]
    W, H = 840, 330
    n = len(certs)
    spread = min(186, (W - 200) / max(n - 1, 1))
    names = "; ".join(
        f"{x['name']}, {x['issuer']}, issued {fmt_day(x['issued'], True)}"
        + (f", valid until {fmt_day(x['expires'], True)}" if x.get("expires") else "")
        for x in certs
    )
    out = svg_open(W, H, "Certifications", f"Certifications: {names}.")
    out.append(
        "<style>"
        f"text{{font-family:{FONT};fill:#fff}}"
        ".kick{font-size:10px;font-weight:700;fill-opacity:.75}"
        ".name{font-weight:700}.sub{font-size:11.5px;fill-opacity:.75}"
        ".meta{font-size:9.5px;font-weight:600;letter-spacing:1px;fill-opacity:.6}"
        ".float{animation:float 5s ease-in-out infinite alternate}"
        ".holo{animation:holo 9s ease-in-out infinite alternate}"
        ".shine{animation:sweep 5.5s ease-in-out infinite}"
        "@keyframes float{to{transform:translateY(-7px)}}"
        "@keyframes holo{to{transform:translateX(176px)}}"
        "@keyframes sweep{0%{transform:translateX(0)}30%,100%{transform:translateX(440px)}}"
        "@media (prefers-reduced-motion:reduce){.float,.holo,.shine{animation:none}}"
        "</style>"
    )
    defs = [
        f'<radialGradient id="glow"><stop offset="0" stop-color="{c["accent"]}" stop-opacity="{0.22 if theme == "dark" else 0.14}"/>'
        f'<stop offset="1" stop-color="{c["accent"]}" stop-opacity="0"/></radialGradient>',
        '<linearGradient id="body" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1b2338"/>'
        '<stop offset="1" stop-color="#090c14"/></linearGradient>',
        '<linearGradient id="rainbow">'
        + "".join(f'<stop offset="{i / 6:.3f}" stop-color="{col}"/>' for i, col in enumerate(HOLO))
        + "</linearGradient>",
        '<linearGradient id="shinefill"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        '<stop offset=".5" stop-color="#fff" stop-opacity=".55"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>',
        '<pattern id="dots" width="6" height="6" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".7" fill="#fff" opacity=".18"/></pattern>',
        '<clipPath id="card"><rect x="-84" y="-118" width="168" height="236" rx="14"/></clipPath>',
        '<filter id="shadow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#000" flood-opacity=".45"/></filter>',
    ]
    for i, cert in enumerate(certs):
        tint = cert.get("color", c["accent"])
        defs.append(
            f'<radialGradient id="tint{i}" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="{tint}" stop-opacity=".55"/>'
            f'<stop offset="1" stop-color="{tint}" stop-opacity="0"/></radialGradient>'
        )
    out.append("<defs>" + "".join(defs) + "</defs>")
    out.append(f'<ellipse cx="{W / 2}" cy="{H / 2}" rx="390" ry="145" fill="url(#glow)"/>')
    hexagon = " ".join(
        f"{20 * math.cos(math.radians(60 * k - 90)):.1f},{-70 + 20 * math.sin(math.radians(60 * k - 90)):.1f}"
        for k in range(6)
    )
    for i, cert in enumerate(certs):
        tint = cert.get("color", c["accent"])
        offset = i - (n - 1) / 2
        cx, cy, rot = W / 2 + offset * spread, H / 2 + 4 + abs(offset) * 6, offset * 4.5
        lines = wrap(cert.get("title", cert["name"]), 18)[:4]
        size = 15 if len(lines) <= 3 and max(map(len, lines)) <= 18 else 13.5
        kicker = cert.get("kicker", "CERTIFIED")
        spacing = 3 if len(kicker) <= 10 else 1.5
        years = cert["issued"][:4] + (f"–{cert['expires'][:4]}" if cert.get("expires") else "")
        out.append(f'<g transform="translate({cx:.1f},{cy:.1f}) rotate({rot:.1f})">')
        out.append(f'<g class="float" style="animation-delay:-{i * 0.9:.1f}s">')
        out.append('<rect x="-84" y="-118" width="168" height="236" rx="14" fill="url(#body)"/>')
        out.append('<g clip-path="url(#card)">')
        out.append(f'<rect x="-84" y="-118" width="168" height="150" fill="url(#tint{i})"/>')
        out.append(
            f'<rect class="holo" style="animation-delay:-{i * 2}s" x="-260" y="-118" width="520" height="236" '
            'fill="url(#rainbow)" opacity=".22"/>'
        )
        out.append('<rect x="-84" y="44" width="168" height="74" fill="url(#dots)"/>')
        out.append(
            f'<g transform="skewX(-18)"><rect class="shine" style="animation-delay:{1 + i * 0.35:.2f}s" '
            'x="-200" y="-118" width="60" height="236" fill="url(#shinefill)"/></g>'
        )
        out.append("</g>")
        out.append('<rect x="-84" y="-118" width="168" height="236" rx="14" fill="none" stroke="#fff" stroke-opacity=".22"/>')
        if cert.get("badge"):  # the issuer's official badge image, embedded so it shows inside an <img>
            data = base64.b64encode((base / cert["badge"]).read_bytes()).decode()
            out.append(
                f'<image href="data:image/png;base64,{data}" x="-38" y="-108" width="76" height="76" filter="url(#shadow)"/>'
            )
        else:
            out.append(f'<polygon points="{hexagon}" fill="{tint}" fill-opacity=".18" stroke="{tint}" stroke-width="1.8"/>')
            out.append('<path d="M-8,-70 l5,5 l11,-11" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>')
        out.append(
            f'<text x="0" y="-12" text-anchor="middle" class="kick" letter-spacing="{spacing}">{escape(kicker)}</text>'
        )
        for j, line in enumerate(lines):
            out.append(
                f'<text x="0" y="{11 + j * (size + 4)}" text-anchor="middle" class="name" font-size="{size}">{escape(line)}</text>'
            )
        out.append(
            f'<text x="0" y="{17 + len(lines) * (size + 4)}" text-anchor="middle" class="sub">{escape(cert["issuer"])}</text>'
        )
        out.append(f'<text x="-68" y="102" class="meta">{escape(years)}</text>')
        out.append('<text x="68" y="102" text-anchor="end" class="meta">VERIFIED</text>')
        out.append("</g></g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


CERTS_START, CERTS_END = "<!-- certs:start", "<!-- certs:end -->"


def update_readme_certs(readme: Path, certs: list[dict]) -> None:
    """Rewrite the certifications block in the README so it matches profile.json."""
    text = readme.read_text()
    if CERTS_START not in text or CERTS_END not in text:
        return
    start = text.index(CERTS_START)
    end = text.index(CERTS_END) + len(CERTS_END)
    block = [f"{CERTS_START} (generated from profile.json by scripts/build_cards.py; edit profile.json) -->"]
    if certs:
        alt = "Certifications: " + "; ".join(f"{c['name']} ({c['issuer']})" for c in certs)
        links = " · ".join(f'<a href="{escape(c["url"])}">{escape(c["name"])}</a>' for c in certs if c.get("url"))
        block += [
            "## Certifications",
            "",
            "<picture>",
            '  <source media="(prefers-color-scheme: dark)" srcset="assets/certs-dark.svg">',
            f'  <img src="assets/certs-light.svg" alt="{escape(alt, {chr(34): "&quot;"})}" width="100%">',
            "</picture>",
            "",
            f'<p align="center"><sub>Verify on Credly: {links}</sub></p>' if links else "",
        ]
    block.append(CERTS_END)
    new = text[:start] + "\n".join(line for line in block) + text[end:]
    if new != text:
        readme.write_text(new)


# ---------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--user", help="GitHub login to fetch (needs GITHUB_TOKEN)")
    source.add_argument("--data", type=Path, help="redraw from a saved stats.json instead of fetching")
    parser.add_argument("--out", type=Path, default=Path("assets"), help="output folder (default: assets)")
    parser.add_argument("--profile", type=Path, default=Path("profile.json"), help="text shown on the cards")
    parser.add_argument("--readme", type=Path, default=Path("README.md"), help="README whose certifications block to update")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text())

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
    certs = active_certs(profile.get("certifications", []), summary["updated"])
    for theme in THEMES:
        (args.out / f"stats-{theme}.svg").write_text(render_stats(summary, theme))
        (args.out / f"header-{theme}.svg").write_text(
            render_header(theme, summary, profile["tagline"], profile["subtitle"])
        )
        (args.out / f"terminal-{theme}.svg").write_text(render_terminal(summary, profile, theme))
        cert_file = args.out / f"certs-{theme}.svg"
        if certs:
            cert_file.write_text(render_certs(certs, theme, args.profile.parent))
        elif cert_file.exists():
            cert_file.unlink()
    if args.readme.exists():
        update_readme_certs(args.readme, certs)
    print(
        f"{summary['login']}: {summary['total']:,} contributions, "
        f"streak {summary['current_streak']['days']} (best {summary['longest_streak']['days']}), "
        f"{len(summary['languages'])} languages -> {args.out}/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
