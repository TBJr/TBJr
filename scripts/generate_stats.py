#!/usr/bin/env python3
"""
Self-contained GitHub stats + top-languages SVG generator.

Replaces github-readme-stats.vercel.app. Instead of a live request to a shared
service on every profile view, this queries the GitHub GraphQL API once (in a
scheduled Action), then writes two static SVGs into the repo. The README points
at those files, so rendering never depends on an external service and can never
be rate-limited.

Usage:
    GITHUB_TOKEN=xxx python generate_stats.py --user TBJr --out assets
    python generate_stats.py --demo --out assets        # no token, fake data

Env:
    GITHUB_TOKEN   a token with public repo read scope (the Actions GITHUB_TOKEN
                   works for public data; use a PAT if you want private counts)
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html import escape

GRAPHQL = "https://api.github.com/graphql"

# ---- Theme -----------------------------------------------------------------
# One place to restyle everything. These defaults are a calm dark theme.
THEME = {
    "bg":       "#0d1117",
    "border":   "#30363d",
    "title":    "#58a6ff",
    "text":     "#c9d1d9",
    "muted":    "#8b949e",
    "icon":     "#58a6ff",
    "accent":   "#58a6ff",
    "ring_bg":  "#21262d",
    "radius":   "10",
}

# Fallback colours for languages GitHub doesn't return a colour for.
LANG_FALLBACK = "#858585"


# ---- Data fetching ---------------------------------------------------------
def gql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "self-hosted-readme-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API HTTP {e.code}: {e.read().decode()[:400]}")
    if "errors" in payload:
        sys.exit(f"GitHub API errors: {payload['errors']}")
    return payload["data"]


STATS_Q = """
query($login:String!){
  user(login:$login){
    name login
    contributionsCollection{
      totalCommitContributions
      restrictedContributionsCount
    }
    repositoriesContributedTo(first:1, contributionTypes:[COMMIT,ISSUE,PULL_REQUEST,REPOSITORY]){
      totalCount
    }
    pullRequests(first:1){ totalCount }
    mergedPRs: pullRequests(states:MERGED){ totalCount }
    openIssues: issues(states:OPEN){ totalCount }
    closedIssues: issues(states:CLOSED){ totalCount }
    followers{ totalCount }
    repositories(first:100, ownerAffiliations:OWNER, isFork:false,
                 orderBy:{field:STARGAZERS, direction:DESC}){
      totalCount
      nodes{ stargazerCount }
    }
  }
}
"""

LANGS_Q = """
query($login:String!, $after:String){
  user(login:$login){
    repositories(ownerAffiliations:OWNER, isFork:false, first:100, after:$after){
      pageInfo{ hasNextPage endCursor }
      nodes{
        languages(first:100, orderBy:{field:SIZE, direction:DESC}){
          edges{ size node{ name color } }
        }
      }
    }
  }
}
"""


def fetch_stats(user, token):
    d = gql(STATS_Q, {"login": user}, token)["user"]
    stars = sum(n["stargazerCount"] for n in d["repositories"]["nodes"])
    commits = (d["contributionsCollection"]["totalCommitContributions"]
               + d["contributionsCollection"]["restrictedContributionsCount"])
    return {
        "name": d["name"] or d["login"],
        "stars": stars,
        "commits": commits,
        "prs": d["pullRequests"]["totalCount"],
        "issues": d["openIssues"]["totalCount"] + d["closedIssues"]["totalCount"],
        "contributed": d["repositoriesContributedTo"]["totalCount"],
        "followers": d["followers"]["totalCount"],
        "repos": d["repositories"]["totalCount"],
    }


def fetch_languages(user, token, top_n=8):
    sizes, colors = {}, {}
    after = None
    while True:
        d = gql(LANGS_Q, {"login": user, "after": after}, token)["user"]["repositories"]
        for repo in d["nodes"]:
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                sizes[name] = sizes.get(name, 0) + edge["size"]
                colors[name] = edge["node"]["color"] or LANG_FALLBACK
        if d["pageInfo"]["hasNextPage"]:
            after = d["pageInfo"]["endCursor"]
        else:
            break
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        {"name": n, "pct": round(v / total * 100, 1), "color": colors[n]}
        for n, v in ranked
    ]


# ---- Rank (rough GRS-style, S..C) ------------------------------------------
def compute_rank(s):
    # Weighted, normalised into a 0..1 "percentile-ish" score, then bucketed.
    def norm(v, med):  # logistic-ish curve, higher = better
        return 1 / (1 + pow(2.718281828, -(v - med) / (med + 1)))
    score = (
        norm(s["commits"], 250) * 0.30 +
        norm(s["prs"], 50) * 0.20 +
        norm(s["issues"], 25) * 0.15 +
        norm(s["stars"], 50) * 0.20 +
        norm(s["followers"], 25) * 0.15
    )
    pct = round((1 - score) * 100, 1)  # lower = better rank
    for level, cutoff in [("S", 1), ("A+", 12.5), ("A", 25), ("A-", 37.5),
                          ("B+", 50), ("B", 62.5), ("B-", 75), ("C+", 87.5), ("C", 100)]:
        if pct <= cutoff:
            return level, min(score, 1.0)
    return "C", score


# ---- SVG helpers -----------------------------------------------------------
def human(n):
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


ICONS = {
    "star": "M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z",
    "commit": "M10.5 7.75a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0ZM1 7.75a.75.75 0 0 1 .75-.75h2.879a3.5 3.5 0 0 1 6.742 0h2.879a.75.75 0 0 1 0 1.5h-2.879a3.5 3.5 0 0 1-6.742 0H1.75A.75.75 0 0 1 1 7.75Z",
    "pr": "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354Z",
    "issue": "M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z",
    "contrib": "M2 5.5a3.5 3.5 0 1 1 5.898 2.549 5.508 5.508 0 0 1 3.034 4.084.75.75 0 1 1-1.482.235 4 4 0 0 0-7.9 0 .75.75 0 0 1-1.482-.236A5.507 5.507 0 0 1 3.102 8.05 3.493 3.493 0 0 1 2 5.5ZM11 4a3.001 3.001 0 0 1 2.22 5.018 5.01 5.01 0 0 1 2.56 3.012.749.749 0 0 1-.885.954.752.752 0 0 1-.549-.514 3.507 3.507 0 0 0-2.522-2.372.75.75 0 0 1-.574-.73v-.352a.75.75 0 0 1 .416-.672A1.5 1.5 0 0 0 11 5.5.75.75 0 0 1 11 4Z",
}


def stat_row(y, icon, label, value):
    return f'''
    <g transform="translate(0,{y})">
      <svg x="0" y="0" width="16" height="16" viewBox="0 0 16 16" fill="{THEME['icon']}">
        <path d="{ICONS[icon]}"/>
      </svg>
      <text x="26" y="12" class="stat">{escape(label)}</text>
      <text x="195" y="12" class="num">{escape(value)}</text>
    </g>'''


def build_stats_svg(s):
    rank, prog = compute_rank(s)
    W, H = 470, 200
    rows = [
        ("star", "Total Stars Earned", human(s["stars"])),
        ("commit", "Total Commits", human(s["commits"])),
        ("pr", "Total PRs", human(s["prs"])),
        ("issue", "Total Issues", human(s["issues"])),
        ("contrib", "Contributed to", human(s["contributed"])),
    ]
    body = "".join(stat_row(48 + i * 26, ic, lb, vl) for i, (ic, lb, vl) in enumerate(rows))

    # rank ring
    r = 40
    circ = 2 * 3.1415926 * r
    dash = circ * min(prog, 1.0)
    ring_cx, ring_cy = 370, 100
    ring = f'''
    <g transform="translate({ring_cx},{ring_cy})">
      <circle r="{r}" fill="none" stroke="{THEME['ring_bg']}" stroke-width="6"/>
      <circle r="{r}" fill="none" stroke="{THEME['accent']}" stroke-width="6"
              stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}"
              transform="rotate(-90)"/>
      <text text-anchor="middle" y="7" class="rank">{rank}</text>
    </g>'''

    return f'''<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
     xmlns="http://www.w3.org/2000/svg" role="img"
     aria-label="{escape(s['name'])} GitHub stats">
  <style>
    .title {{ font: 600 18px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['title']}; }}
    .stat  {{ font: 400 14px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['text']}; }}
    .num   {{ font: 700 14px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['text']}; }}
    .rank  {{ font: 700 20px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['title']}; }}
  </style>
  <rect x="0.5" y="0.5" rx="{THEME['radius']}" width="{W-1}" height="{H-1}"
        fill="{THEME['bg']}" stroke="{THEME['border']}"/>
  <text x="25" y="35" class="title">{escape(s['name'])}'s GitHub Stats</text>
  <g transform="translate(25,0)">{body}</g>
  {ring}
</svg>'''


def build_langs_svg(langs):
    W = 345
    row_h = 26
    H = 70 + row_h * len(langs)
    bar_w = W - 50
    bar_y = 55

    # stacked bar
    x = 25
    segs = ""
    for l in langs:
        seg_w = bar_w * l["pct"] / 100
        segs += f'<rect x="{x:.1f}" y="{bar_y}" width="{seg_w:.1f}" height="8" fill="{l["color"]}"/>'
        x += seg_w

    # legend (two columns)
    legend = ""
    col_x = [25, 188]
    for i, l in enumerate(langs):
        cx = col_x[i % 2]
        cy = 90 + (i // 2) * row_h
        legend += f'''
      <circle cx="{cx+5}" cy="{cy-4}" r="5" fill="{l['color']}"/>
      <text x="{cx+16}" y="{cy}" class="lg">{escape(l['name'])} {l['pct']}%</text>'''

    return f'''<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
     xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Most used languages">
  <style>
    .title {{ font: 600 18px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['title']}; }}
    .lg    {{ font: 400 12px 'Segoe UI',Ubuntu,Sans-Serif; fill:{THEME['text']}; }}
  </style>
  <rect x="0.5" y="0.5" rx="{THEME['radius']}" width="{W-1}" height="{H-1}"
        fill="{THEME['bg']}" stroke="{THEME['border']}"/>
  <text x="25" y="35" class="title">Most Used Languages</text>
  <rect x="25" y="{bar_y}" width="{bar_w}" height="8" rx="4" fill="{THEME['ring_bg']}"/>
  {segs}
  {legend}
</svg>'''


# ---- Demo data (for testing without a token) -------------------------------
DEMO_STATS = {"name": "Thomas Brown", "stars": 128, "commits": 1342, "prs": 214,
              "issues": 96, "contributed": 37, "followers": 88, "repos": 41}
DEMO_LANGS = [
    {"name": "PHP", "pct": 22.1, "color": "#4F5D95"},
    {"name": "JavaScript", "pct": 18.9, "color": "#f1e05a"},
    {"name": "TypeScript", "pct": 14.2, "color": "#3178c6"},
    {"name": "Dart", "pct": 11.0, "color": "#00B4AB"},
    {"name": "Python", "pct": 8.8, "color": "#3572A5"},
    {"name": "Vue", "pct": 6.7, "color": "#41b883"},
    {"name": "Java", "pct": 5.1, "color": "#b07219"},
    {"name": "HTML", "pct": 4.3, "color": "#e34c26"},
    {"name": "CSS", "pct": 3.6, "color": "#563d7c"},
    {"name": "Shell", "pct": 2.7, "color": "#89e051"},
    {"name": "Dockerfile", "pct": 1.6, "color": "#384d54"},
    {"name": "C++", "pct": 1.0, "color": "#f34b7d"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("GH_USER", "TBJr"))
    ap.add_argument("--out", default="assets")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--demo", action="store_true", help="use fake data, no API call")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.demo:
        stats, langs = DEMO_STATS, DEMO_LANGS
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            sys.exit("Set GITHUB_TOKEN (or use --demo).")
        stats = fetch_stats(args.user, token)
        langs = fetch_languages(args.user, token, args.top)

    with open(os.path.join(args.out, "github-stats.svg"), "w") as f:
        f.write(build_stats_svg(stats))
    with open(os.path.join(args.out, "top-langs.svg"), "w") as f:
        f.write(build_langs_svg(langs))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Wrote assets/github-stats.svg and assets/top-langs.svg ({stamp})")
    print(f"Stats: {stats}")


if __name__ == "__main__":
    main()
