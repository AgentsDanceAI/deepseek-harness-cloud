#!/usr/bin/env python3
"""把 /apps 那张 4x4 网格渲染成 README 里的产品表。

README 的开场白承诺"16 个开源 AI 产品", 却从来没列出是哪 16 个 —— 这个脚本
把清单从**站内同一个数据源**生成出来: 产品与顺序取 server/app/apps_catalog.py
的 CATALOG, 一句话与说明取 i18n 的 apps.tag.* / apps.d.* (就是卡片上那两行字)。

手抄一份到 README 里必然漂: 加一格、改一句文案、下架一个产品 (Coze 已经下架过
一次), README 就悄悄成了假的。所以这里生成、tests/repository/test_app_catalog_docs.py
逐字核对。改完产品跑:

    python3 scripts/render_app_catalog.py --lang en --write
    python3 scripts/render_app_catalog.py --lang zh --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.apps_catalog import CATALOG  # noqa: E402

START = "<!-- app-catalog:start -->"
END = "<!-- app-catalog:end -->"

README = {"en": "README.md", "zh": "README.zh-CN.md"}
HEADER = {
    "en": ("Product", "In one line", "What you get"),
    "zh": ("产品", "一句话", "能拿到什么"),
}
# 有 href 的不是云工作台: 它住在主站上 (数字人共用 GPU 节点, 开不出每用户容器)。
ON_SITE = {"en": "on the main site", "zh": "主站页面"}


def render(lang: str) -> str:
    i18n = json.loads((ROOT / f"server/config/i18n/{lang}.json").read_text(encoding="utf-8"))
    head = HEADER[lang]
    rows = [f"| {head[0]} | {head[1]} | {head[2]} |", "| --- | --- | --- |"]
    for app in CATALOG:
        slot = f"`{app.href}` · {ON_SITE[lang]}" if app.href else f"`{app.id}`"
        tag = i18n[f"apps.tag.{app.tag}"]
        desc = i18n[f"apps.d.{app.id}"]
        rows.append(f"| **{app.name}**<br>{slot} | {tag} | {desc} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=sorted(README), required=True)
    ap.add_argument("--write", action="store_true", help="改写 README 里的标记块")
    args = ap.parse_args()

    block = render(args.lang)
    if not args.write:
        print(block)
        return 0

    path = ROOT / README[args.lang]
    text = path.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print(f"{path.name} 里没有 {START} 标记块", file=sys.stderr)
        return 1
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    path.write_text(f"{before}{START}\n{block}\n{END}{after}", encoding="utf-8")
    print(f"已更新 {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
