#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merged PR Observatory — 静态资源生成器

只统计**已合并**的 Pull Request，生成 **亮 / 暗两版** 极简 SVG 卡片
（README 用 <picture> + prefers-color-scheme 按 GitHub 主题自动切换），
并把一行统计小字回写 README.md 的标记区间。

设计原则（用户反馈驱动：2026-10-01 二次精简，2026-10-10 补 PR 计数，
2026-10-16 加组织 Logo 与细节升级）：
- 一个焦点数字（已合并 PR 总数）+ 一条支撑链：每个上游仓库一行 ——
  行首为**组织 Logo**（GitHub 官方头像，base64 内嵌、不依赖外部图床），
  随后是仓库名、该仓库已合并 PR 数、★ 星级（官方 octicon 图标）。
  PR 标题 / 行数 / 文件数 / 合并日期一律不上卡片。
- 排序按仓库含金量：上游仓库以 stars 降序（公开可验证、每日随数据刷新）。
- 贡献贪吃蛇（Platane/snk）由 .github/workflows/snake.yml 独立生成，
  输出在 output 分支，README 顶部引用。

用法：
    python scripts/generate_stats.py                  # 本地（走 gh CLI 鉴权）
    GITHUB_TOKEN=xxx python scripts/generate_stats.py # CI（Actions 自动注入）
    python scripts/generate_stats.py --offline        # 读 assets/stats.json 缓存

只依赖标准库，无需 pip install。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER = "shi-felix"
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
TZ8 = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- GitHub Primer 色值
# 亮色（GitHub light default）
LIGHT = {
    "bg": "#FFFFFF", "border": "#D1D9E0", "hair": "#EAEEF2", "chip": "#F6F8FA",
    "text": "#1F2328", "text2": "#59636E", "text3": "#818B98",
    "accent": "#8250DF",   # GitHub 的 merged 紫
    "add": "#1A7F37",      # GitHub 的 additions 绿
}
# 暗色（GitHub dark default）
DARK = {
    "bg": "#0D1117", "border": "#30363D", "hair": "#21262D", "chip": "#161B22",
    "text": "#F0F6FC", "text2": "#9198A1", "text3": "#6E7681",
    "accent": "#A371F7",
    "add": "#3FB950",
}
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
        "'Hiragino Sans GB','Microsoft YaHei',sans-serif")
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"

# GitHub 官方 octicon star-16（MIT），行内星标图标（16×16 视图框）
STAR16 = ("M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 "
          ".416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 "
          "12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 "
          "6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 "
          ".25Zm0 2.445L6.615 5.5a.75.75 0 0 1-.564.41l-3.097.45 2.24 "
          "2.184a.75.75 0 0 1 .216.664l-.528 3.084 2.769-1.456a.75.75 0 0 1 "
          ".698 0l2.77 1.456-.53-3.084a.75.75 0 0 1 .216-.664l2.24-2.183-"
          "3.096-.45a.75.75 0 0 1-.564-.41L8 2.694Z")


# ---------------------------------------------------------------- 取数
def gh_api(path: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req = urllib.request.Request(
            "https://api.github.com" + path,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "merged-pr-observatory"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    # 本地走 gh CLI；偶发网络/代理抖动时重试，避免整条生成链中断
    last = ""
    for attempt in range(3):
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                             encoding="utf-8")
        if out.returncode == 0:
            return json.loads(out.stdout)
        last = (out.stderr or "").strip()[-200:]
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gh api {path} 重试 3 次仍失败: {last}")


def search_prs(user: str) -> list[dict]:
    items, page = [], 1
    while True:
        data = gh_api(f"/search/issues?q=author:{user}+type:pr"
                      f"&per_page=100&page={page}&sort=created&order=desc")
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or page >= 10:
            break
        page += 1
    return items


def parse_ts(s):
    return (datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if s else None)


def fetch_avatar(url: str) -> str | None:
    """把组织头像抓成 base64 data URI：Logo 内嵌进 SVG，不依赖外部图床。"""
    try:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            f"{url}{sep}size=64",
            headers={"User-Agent": "merged-pr-observatory"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        else:
            return None
        if not 0 < len(data) <= 256 * 1024:
            return None
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except Exception as e:  # noqa: BLE001 —— 头像失败不阻塞主流程
        print(f"  ! avatar {url}: {e}", file=sys.stderr)
        return None


def collect(user: str) -> tuple[list[dict], dict[str, str]]:
    cache = ASSETS / "stats.json"
    if "--offline" in sys.argv and cache.exists():
        print(f"  [offline] 读取缓存 {cache.relative_to(ROOT)}")
        d = json.loads(cache.read_text(encoding="utf-8"))
        return d["prs"], d.get("avatars", {})

    raw = search_prs(user)
    repos: dict[str, dict] = {}
    prs: list[dict] = []

    for it in raw:
        repo_api = it["repository_url"].split("/repos/")[-1]
        if repo_api not in repos:
            try:
                d = gh_api(f"/repos/{repo_api}")
                repos[repo_api] = {
                    "language": d.get("language") or "Other",
                    "stars": d.get("stargazers_count") or 0,
                    "avatar_url": (d.get("owner") or {}).get("avatar_url") or "",
                }
            except Exception as e:
                print(f"  ! repo {repo_api}: {e}", file=sys.stderr)
                repos[repo_api] = {"language": "Other", "stars": 0, "avatar_url": ""}

        pr = gh_api(f"/repos/{repo_api}/pulls/{it['number']}")
        merged_at = pr.get("merged_at")
        status = "merged" if merged_at else ("open" if pr.get("state") == "open" else "closed")
        merged_dt = parse_ts(merged_at)
        owner, name = repo_api.split("/")

        prs.append({
            "repo": repo_api, "owner": owner, "name": name,
            "number": it["number"],
            "title": pr.get("title") or it.get("title") or "",
            "status": status,
            "stars": repos[repo_api]["stars"],
            "created": pr.get("created_at"), "merged_at": merged_at,
            "merged_date": merged_dt.astimezone(TZ8).strftime("%m-%d") if merged_dt else "",
            "merged_year": merged_dt.astimezone(TZ8).strftime("%Y") if merged_dt else "",
            "additions": pr.get("additions") or 0,
            "deletions": pr.get("deletions") or 0,
            "changed_files": pr.get("changed_files") or 0,
            "url": pr.get("html_url") or f"https://github.com/{repo_api}/pull/{it['number']}",
        })

    prs.sort(key=lambda p: p["created"] or "")

    # 组织 Logo：按 owner 去重抓取；失败时回退旧缓存，再不行由卡片画字母占位
    old: dict[str, str] = {}
    if cache.exists():
        try:
            old = json.loads(cache.read_text(encoding="utf-8")).get("avatars", {})
        except Exception:  # noqa: BLE001
            old = {}
    avatars: dict[str, str] = {}
    seen: set[str] = set()
    for p in prs:
        owner = p["owner"]
        if p["status"] != "merged" or owner == USER or owner in seen:
            continue
        seen.add(owner)
        url = repos[p["repo"]].get("avatar_url") or f"https://github.com/{owner}.png"
        data_uri = fetch_avatar(url) or old.get(owner)
        if data_uri:
            avatars[owner] = data_uri
    return prs, avatars


# ---------------------------------------------------------------- 工具
def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fmt_stars(n: int) -> str:
    """★ 计数展示：83290 → 83.3k，264 → 264。"""
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def tx(x, y, s, size=12, fill="#000", weight="normal", anchor="start",
       family=None, ls=None):
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    l = f' letter-spacing="{ls}"' if ls is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{family or FONT}" '
            f'font-size="{size}" fill="{fill}" font-weight="{weight}"{a}{l}>{esc(s)}</text>')


# ---------------------------------------------------------------- 卡片（极简）
def card_merged(prs: list[dict], C: dict, avatars: dict[str, str]) -> str:
    """962 宽、高度自适应：焦点数字 + 每上游仓库一行（Logo + 仓库名 + PR 数 + ★）。"""
    W = 962
    L, R = 40, W - 40
    PRC = R - 152          # PR 计数列的右基线（与最右的 ★ 列留出固定间距）
    SZ, RX = 30, 8         # 行首 Logo 的尺寸与圆角
    NX = L + SZ + 16       # 仓库名起始 x（与 Logo 留 16px 间距）

    merged = [p for p in prs if p["status"] == "merged"]
    upstream = [p for p in merged if p["owner"] != USER]
    own = [p for p in merged if p["owner"] == USER]

    groups: dict[str, list[dict]] = {}
    for p in upstream:
        groups.setdefault(p["repo"], []).append(p)
    # 分组按含金量排：仓库 stars 降序，PR 数量为次序
    groups = dict(sorted(
        groups.items(),
        key=lambda kv: (-max(p.get("stars") or 0 for p in kv[1]), -len(kv[1]))))
    TOP = 6
    shown = list(groups.items())[:TOP]
    rest = len(groups) - len(shown)

    b: list[str] = []
    defs: list[str] = []
    # 眉头
    b.append(f'<circle cx="{L + 4}" cy="50" r="3.2" fill="{C["accent"]}"/>')
    b.append(tx(L + 16, 54, "MERGED PULL REQUESTS", 12, C["accent"], "700", ls="2.4"))
    b.append(tx(R, 54, f"github.com/{USER}", 12, C["text2"], "normal", "end", MONO))

    # 焦点数字
    n = str(len(merged))
    b.append(tx(L - 5, 146, n, 84, C["text"], "700", family=MONO, ls="-3.5"))
    b.append(tx(L + 52 * len(n) + 16, 146, "个 PR 已合并", 20, C["text"], "600", ls="-0.2"))
    b.append(tx(L, 174, f"{len(upstream)} 个提给他人仓库 · {len(own)} 个自有项目",
                14.5, C["text2"], "500"))
    b.append(f'<line x1="{L}" y1="202" x2="{R}" y2="202" stroke="{C["hair"]}"/>')

    # 仓库行：组织 Logo + 仓库名 + 该仓库已合并 PR 数 + 星级（列表主体）
    y = 242
    for i, (repo, items) in enumerate(shown):
        stars = max(p.get("stars") or 0 for p in items)
        owner = repo.split("/")[0]
        ly = round(y - SZ / 2 - 6.3, 1)       # Logo 顶边：与文字光学居中对齐
        av = avatars.get(owner)
        if av:
            defs.append(f'<clipPath id="lg{i}"><rect x="{L}" y="{ly}" '
                        f'width="{SZ}" height="{SZ}" rx="{RX}"/></clipPath>')
            b.append(f'<rect x="{L}" y="{ly}" width="{SZ}" height="{SZ}" '
                     f'rx="{RX}" fill="#FFFFFF"/>')
            b.append(f'<image x="{L}" y="{ly}" width="{SZ}" height="{SZ}" '
                     f'href="{av}" clip-path="url(#lg{i})" '
                     f'preserveAspectRatio="xMidYMid slice"/>')
            b.append(f'<rect x="{L}" y="{ly}" width="{SZ}" height="{SZ}" '
                     f'rx="{RX}" fill="none" stroke="{C["border"]}"/>')
        else:
            b.append(f'<rect x="{L}" y="{ly}" width="{SZ}" height="{SZ}" '
                     f'rx="{RX}" fill="{C["chip"]}" stroke="{C["border"]}"/>')
            b.append(tx(L + SZ / 2, round(ly + SZ / 2 + 5.3, 1), owner[0].upper(),
                        15, C["text2"], "700", "middle"))
        b.append(tx(NX, y, repo, 18, C["text"], "700", family=MONO, ls="0.2"))
        b.append(tx(PRC, y, f"{len(items)} 个 PR", 15.5, C["text"], "700", "end"))
        star = fmt_stars(stars)
        sx = R - len(star) * 0.58 * 16 - 6 - 15   # 星标图标放计数左侧（按最宽等宽字宽估算）
        b.append(f'<g transform="translate({sx:.1f} {y - 13}) scale(0.9375)" '
                 f'fill="{C["text2"]}"><path d="{STAR16}"/></g>')
        b.append(tx(R, y, star, 16, C["text2"], "600", "end", MONO))
        y += 46
    if rest > 0:
        b.append(tx(NX, y, f"… 另有 {rest} 个仓库已合并 PR", 15, C["text3"], "500"))
        y += 46

    H = int(y + 26 + 44)
    stamp = datetime.now(TZ8).strftime("%Y-%m-%d %H:%M (UTC+8)")

    head = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" role="img" aria-label="已合并的 Pull Request">',
            f'<title>已合并的 Pull Request · github.com/{USER}</title>',
            '<defs>',
            f'<clipPath id="card"><rect x="0.5" y="0.5" width="{W - 1}" '
            f'height="{H - 1}" rx="12"/></clipPath>',
            *defs,
            '</defs>',
            f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" '
            f'fill="{C["bg"]}" stroke="{C["border"]}"/>',
            f'<rect x="0" y="0" width="{W}" height="3" rx="1.5" fill="{C["accent"]}" '
            f'fill-opacity="0.9" clip-path="url(#card)"/>']
    tail = [f'<line x1="{L}" y1="{H - 44}" x2="{R}" y2="{H - 44}" stroke="{C["hair"]}"/>',
            tx(L, H - 20, "数据来源 GitHub REST API · 每日自动刷新", 13, C["text2"]),
            tx(R, H - 20, f"更新于 {stamp}", 13, C["text2"], "normal", "end", MONO),
            '</svg>']
    return "\n".join(head + b + tail) + "\n"


# ---------------------------------------------------------------- README
def sync(md: str, prs: list[dict]) -> str:
    """只同步徽章数字与一行统计小字；明细表格已按 2026-10-01 反馈移除。"""
    merged = [p for p in prs if p["status"] == "merged"]
    upstream = [p for p in merged if p["owner"] != USER]
    own = [p for p in merged if p["owner"] == USER]
    md = re.sub(r"(badge/Merged%20PRs-)\d+(-)", rf"\g<1>{len(merged)}\g<2>", md)

    lines = ["<!-- STATS:BEGIN -->",
             f"**{len(merged)}** 个 PR 已被合并"
             f"（{len(upstream)} 个提给他人仓库 · {len(own)} 个自有项目）· 数据由 "
             "[`generate_stats.py`](./scripts/generate_stats.py) 每日直连 GitHub API 刷新",
             "<!-- STATS:END -->"]
    return re.sub(r"<!-- STATS:BEGIN -->.*?<!-- STATS:END -->", "\n".join(lines), md, flags=re.S)


# ---------------------------------------------------------------- main
def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    print(f"抓取 @{USER} 的 Pull Request ...")
    prs, avatars = collect(USER)
    merged = [p for p in prs if p["status"] == "merged"]
    print(f"  共 {len(prs)} 个 PR，其中已合并 {len(merged)} 个；组织 Logo {len(avatars)} 枚")

    for name, palette in (("light", LIGHT), ("dark", DARK)):
        (ASSETS / f"merged-prs-{name}.svg").write_text(
            card_merged(prs, palette, avatars), encoding="utf-8")
        print(f"  assets/merged-prs-{name}.svg")

    if README.exists():
        README.write_text(sync(README.read_text(encoding="utf-8"), prs), encoding="utf-8")
        print("  README.md 统计区间 / 徽章已更新")

    if "--offline" not in sys.argv:
        (ASSETS / "stats.json").write_text(
            json.dumps({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "user": USER, "avatars": avatars, "prs": prs},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        print("  assets/stats.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
