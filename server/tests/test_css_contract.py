"""调色板的几条硬规矩。

这些不是风格偏好, 是 2026-09-11 真踩出来的三个洞 —— 共同点是**它们都只在
某一种模式下现形, 而没人用那种模式打开过页面**:

* 站点原先有明暗两副长相 (`:root` 浅色 + `prefers-color-scheme: dark` 覆盖)。
  我自己浏览器常年深色, 从头到尾只见过深色那版; 老板是浅色, 看到的是首页深蓝、
  其余页纯白, 断成两截。**一套皮肤两种长相, 就等于有一半你从来没验收过。**
* 定价页的月付/年付开关写的是 `background: var(--fg); color: #fff` ——
  深色方案里 --fg 是近白, 白底白字。这个洞在"跟随系统深色"时期就已经在了。
* /download 那条提示条内联写死 `background:#fff7ed`, 配 --muted 的浅字。

所以这里钉的是: **只许有一套调色板**, 且不许再出现"拿前景色当背景还配白字"
和"内联写死浅色底"这两种写法。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "app" / "static" / "app.css"
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))


def css() -> str:
    return CSS.read_text(encoding="utf-8")


def test_only_one_palette():
    """不许再靠 prefers-color-scheme 分出第二套长相。

    想换主题就整体换 :root, 别再让站点在不同人眼里长得不一样。
    """
    assert "prefers-color-scheme" not in css(), (
        "app.css 里又出现了 prefers-color-scheme —— 站点会重新分裂成两副长相, 而你只会验收自己那一副"
    )


def test_no_foreground_colour_used_as_a_background_with_white_text():
    """`background: var(--fg)` 配浅色文字 = 深底方案下的隐形文字。

    要做反色块 (浅底深字) 就配 `color: var(--bg)`, 像 .toast 那样。
    """
    bad = []
    for rule in re.findall(r"\{[^{}]*\}", css()):
        if "background: var(--fg)" not in rule.replace("background:var(--fg)", "background: var(--fg)"):
            continue
        m = re.search(r"color:\s*([^;}]+)", rule)
        if m and m.group(1).strip().lower() in ("#fff", "#ffffff", "white"):
            bad.append(rule.strip()[:90])
    assert not bad, f"拿 --fg 当底还配白字, 深底下就是白底白字: {bad}"


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_no_hardcoded_light_background_inline(tpl):
    """模板里内联写死的浅色底不跟调色板走 —— 换底色那天它原地变成刺眼的白块。

    要用就用 --warn-weak / --brand-weak 这些令牌。
    """
    hits = re.findall(r'style="[^"]*background:\s*(#[fFeE][0-9a-fA-F]{2,5})', tpl.read_text(encoding="utf-8"))
    assert not hits, f"{tpl.name} 内联写死了浅色底 {hits} —— 改用调色板令牌"
