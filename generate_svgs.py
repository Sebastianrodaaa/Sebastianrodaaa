#!/usr/bin/env python3
"""Render light_mode.svg / dark_mode.svg from profile.json plus live GitHub stats.

Run locally with a token to preview:

    ACCESS_TOKEN=ghp_xxx python3 generate_svgs.py

Without a token the script falls back to stats_cache.json, so it always
produces valid SVGs and never leaves the profile README broken.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
PROFILE = ROOT / "profile.json"
CACHE = ROOT / "stats_cache.json"
API = "https://api.github.com/graphql"

# --- layout -----------------------------------------------------------------
FONT = "Consolas, 'Cascadia Mono', 'SF Mono', Menlo, 'DejaVu Sans Mono', monospace"
FONT_SIZE = 15
# Widest common monospace advance ratio, so nothing clips on any platform.
CHAR_W = FONT_SIZE * 0.605
LINE_H = 19
# Solid-block glyphs only tile seamlessly when the art's line height matches the
# em box, so the ASCII column gets its own tighter leading.
ART_LINE_H = FONT_SIZE
PAD_X = 26
PAD_Y = 34
LABEL_W = 26  # label + dot leader column, in characters
GUTTER = 40

THEMES = {
    "light_mode": {
        "bg": "#f6f8fa", "border": "#d0d7de", "key": "#953800", "value": "#0a3069",
        "accent": "#8250df", "dim": "#8c959f", "add": "#1a7f37", "del": "#cf222e",
        "art": "#0969da",
    },
    "dark_mode": {
        "bg": "#161b22", "border": "#30363d", "key": "#ffa657", "value": "#a5d6ff",
        "accent": "#d2a8ff", "dim": "#6e7681", "add": "#3fb950", "del": "#f85149",
        "art": "#58a6ff",
    },
}

MARKUP = re.compile(r"\[([+\-*~])(.*?)\]", re.DOTALL)
MARKUP_CLASS = {"+": "add", "-": "del", "*": "accent", "~": "cursor"}


# --- GitHub ------------------------------------------------------------------
def graphql(token: str, query: str, variables: dict) -> dict:
    """POST a GraphQL query, retrying on the transient errors GitHub likes to throw."""
    for attempt in range(5):
        resp = requests.post(
            API,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"bearer {token}"},
            timeout=30,
        )
        if resp.status_code in (502, 503, 504) or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(payload["errors"])
        return payload["data"]
    raise RuntimeError(f"GitHub API kept failing: {resp.status_code} {resp.text[:200]}")


USER_QUERY = """
query($login: String!) {
  user(login: $login) {
    id
    createdAt
    followers { totalCount }
    repositoriesContributedTo(contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {
      totalCount
    }
    contributionsCollection { contributionYears }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
    }
  }
}
"""

REPO_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(first: 100, after: $after, ownerAffiliations: [OWNER], isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        stargazerCount
        forkCount
        defaultBranchRef { name }
      }
    }
  }
}
"""

HISTORY_QUERY = """
query($owner: String!, $name: String!, $branch: String!, $id: ID!, $after: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: 100, after: $after, author: {id: $id}) {
            pageInfo { hasNextPage endCursor }
            nodes { oid additions deletions }
          }
        }
      }
    }
  }
}
"""


def count_commits(token: str, login: str, years: list[int]) -> int:
    """Sum commit contributions across every year the account has been active."""
    total = 0
    now = datetime.now(timezone.utc)
    for year in years:
        # The API rejects ranges longer than a year, so query one year at a
        # time and clamp the current year's window to now.
        end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
        data = graphql(token, YEAR_QUERY, {
            "login": login,
            "from": f"{year}-01-01T00:00:00Z",
            "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        c = data["user"]["contributionsCollection"]
        total += c["totalCommitContributions"] + c["restrictedContributionsCount"]
    return total


def walk_loc(token: str, repo: dict, user_id: str, cache: dict) -> dict:
    """Additions/deletions this user authored in one repo.

    Walks newest-to-oldest and stops at the last commit seen on a previous run,
    so a daily job only ever pays for the new commits.
    """
    name = repo["nameWithOwner"]
    branch_ref = repo.get("defaultBranchRef")
    if not branch_ref:  # empty repo
        return {"additions": 0, "deletions": 0, "oid": None}

    owner, repo_name = name.split("/", 1)
    seen = cache.get(name, {})
    stop_at = seen.get("oid")

    additions = deletions = 0
    newest_oid = None
    cursor = None
    hit_cache = False

    while not hit_cache:
        data = graphql(token, HISTORY_QUERY, {
            "owner": owner, "name": repo_name, "branch": branch_ref["name"],
            "id": user_id, "after": cursor,
        })
        target = (data["repository"].get("ref") or {}).get("target") or {}
        history = target.get("history")
        if not history:
            break
        for node in history["nodes"]:
            if newest_oid is None:
                newest_oid = node["oid"]
            if stop_at and node["oid"] == stop_at:
                hit_cache = True
                break
            additions += node["additions"]
            deletions += node["deletions"]
        if hit_cache or not history["pageInfo"]["hasNextPage"]:
            break
        cursor = history["pageInfo"]["endCursor"]

    if hit_cache:
        additions += seen.get("additions", 0)
        deletions += seen.get("deletions", 0)

    return {
        "additions": additions,
        "deletions": deletions,
        "oid": newest_oid or stop_at,
    }


def fetch_stats(token: str, login: str, cache: dict) -> dict:
    user = graphql(token, USER_QUERY, {"login": login})["user"]
    user_id = user["id"]

    repos, stars, forks = [], 0, 0
    cursor = None
    while True:
        page = graphql(token, REPO_QUERY, {"login": login, "after": cursor})
        block = page["user"]["repositories"]
        for node in block["nodes"]:
            repos.append(node)
            stars += node["stargazerCount"]
            forks += node["forkCount"]
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]

    loc_cache = cache.get("loc", {})
    additions = deletions = 0
    for repo in repos:
        result = walk_loc(token, repo, user_id, loc_cache)
        loc_cache[repo["nameWithOwner"]] = result
        additions += result["additions"]
        deletions += result["deletions"]

    return {
        "account_created": user["createdAt"],
        "followers": user["followers"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "repos": len(repos),
        "stars": stars,
        "forks": forks,
        "commits": count_commits(
            token, login, user["contributionsCollection"]["contributionYears"]
        ),
        "loc_add": additions,
        "loc_del": deletions,
        "loc": loc_cache,
    }


# --- formatting --------------------------------------------------------------
def humanize_uptime(created_iso: str) -> str:
    created = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    years = now.year - created.year
    months = now.month - created.month
    days = now.day - created.day
    if days < 0:
        months -= 1
        days += 30
    if months < 0:
        years -= 1
        months += 12

    def plural(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")

    parts = [plural(years, "year"), plural(months, "month"), plural(days, "day")]
    return ", ".join(p for p, n in zip(parts, (years, months, days)) if n) or "today"


def placeholders(stats: dict) -> dict:
    created = datetime.fromisoformat(stats["account_created"].replace("Z", "+00:00"))
    add, delete = stats["loc_add"], stats["loc_del"]
    return {
        "uptime": humanize_uptime(stats["account_created"]),
        "account_created": created.strftime("%d %b %Y"),
        "repos": f"{stats['repos']:,}",
        "contributed": f"{stats['contributed']:,}",
        "commits": f"{stats['commits']:,}",
        "stars": f"{stats['stars']:,}",
        "forks": f"{stats['forks']:,}",
        "followers": f"{stats['followers']:,}",
        "loc_add": f"{add:,}",
        "loc_del": f"{delete:,}",
        "loc_total": f"{add - delete:,}",
    }


def substitute(text: str, values: dict) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_markup(text: str) -> str:
    return MARKUP.sub(lambda m: m.group(2), text)


def render_markup(text: str, default_class: str) -> str:
    """Turn [+x] / [-x] / [*x] / [~x] into coloured tspans."""
    out, pos = [], 0
    for match in MARKUP.finditer(text):
        if match.start() > pos:
            out.append(f'<tspan class="{default_class}">'
                       f'{xml_escape(text[pos:match.start()])}</tspan>')
        out.append(f'<tspan class="{MARKUP_CLASS[match.group(1)]}">'
                   f'{xml_escape(match.group(2))}</tspan>')
        pos = match.end()
    if pos < len(text):
        out.append(f'<tspan class="{default_class}">{xml_escape(text[pos:])}</tspan>')
    return "".join(out)


# --- SVG ---------------------------------------------------------------------
def build_svg(profile: dict, values: dict, theme: dict) -> str:
    title = substitute(profile["title"], values)
    art = profile["art"]

    rows = []  # (label, value) or None for a blank line
    rows.append(("__title__", title))
    rows.append(("__rule__", "─" * len(title)))
    for entry in profile["info"]:
        rows.append(None if not entry else (entry[0], substitute(entry[1], values)))

    # Width is derived from the content, so edits to profile.json never clip.
    art_cols = max((len(strip_markup(line)) for line in art), default=0)
    info_x = PAD_X + art_cols * CHAR_W + GUTTER
    info_cols = max(
        (len(strip_markup(r[1])) + (0 if r[0].startswith("__") else LABEL_W)
         for r in rows if r),
        default=0,
    )
    body_h = max(len(rows) * LINE_H, len(art) * ART_LINE_H)
    width = round(info_x + info_cols * CHAR_W + PAD_X)
    height = round(body_h + PAD_Y * 2)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}" font-size="{FONT_SIZE}">',
        "<style>",
        "text { white-space: pre; dominant-baseline: middle; }",
        f'.key {{ fill: {theme["key"]}; font-weight: 600; }}',
        f'.value {{ fill: {theme["value"]}; }}',
        f'.dim {{ fill: {theme["dim"]}; }}',
        f'.accent {{ fill: {theme["accent"]}; font-weight: 600; }}',
        f'.add {{ fill: {theme["add"]}; }}',
        f'.del {{ fill: {theme["del"]}; }}',
        f'.art {{ fill: {theme["art"]}; }}',
        f'.cursor {{ fill: {theme["accent"]}; animation: blink 1.06s steps(1) infinite; }}',
        "@keyframes blink { 0%, 49% { opacity: 1 } 50%, 100% { opacity: 0 } }",
        "@media (prefers-reduced-motion: reduce) { .cursor { animation: none } }",
        "</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" '
        f'fill="{theme["bg"]}" stroke="{theme["border"]}"/>',
    ]

    # Left column: ASCII art, vertically centred against the info column.
    art_top = PAD_Y + (body_h - len(art) * ART_LINE_H) / 2
    for i, line in enumerate(art):
        y = art_top + i * ART_LINE_H + ART_LINE_H / 2
        parts.append(f'<text x="{PAD_X}" y="{y:.1f}">{render_markup(line, "art")}</text>')

    # Right column: neofetch-style key / dot-leader / value rows.
    for i, row in enumerate(rows):
        if row is None:
            continue
        label, value = row
        y = PAD_Y + i * LINE_H + LINE_H / 2
        x = f"{info_x:.1f}"

        if label == "__title__":
            parts.append(f'<text x="{x}" y="{y:.1f}">'
                         f'<tspan class="accent">{xml_escape(value)}</tspan></text>')
        elif label == "__rule__":
            parts.append(f'<text x="{x}" y="{y:.1f}">'
                         f'<tspan class="dim">{xml_escape(value)}</tspan></text>')
        else:
            dots = "." * max(1, LABEL_W - len(label) - 2)
            parts.append(
                f'<text x="{x}" y="{y:.1f}">'
                f'<tspan class="key">{xml_escape(label)}</tspan>'
                f'<tspan class="dim"> {dots} </tspan>'
                f'{render_markup(value, "value")}'
                f"</text>"
            )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# --- entry point -------------------------------------------------------------
def main() -> int:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            stats = fetch_stats(token, profile["login"], cache)
            CACHE.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:  # keep the README rendering even if the API is down
            print(f"warning: falling back to cached stats ({exc})", file=sys.stderr)
            stats = cache
    else:
        print("warning: no ACCESS_TOKEN set, using cached stats", file=sys.stderr)
        stats = cache

    if not stats:
        print("error: no token and no stats_cache.json to fall back on", file=sys.stderr)
        return 1

    values = placeholders(stats)
    for name, theme in THEMES.items():
        (ROOT / f"{name}.svg").write_text(build_svg(profile, values, theme), encoding="utf-8")
        print(f"wrote {name}.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
