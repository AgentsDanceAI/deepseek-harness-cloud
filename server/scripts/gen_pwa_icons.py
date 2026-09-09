#!/usr/bin/env python3
"""生成站点的 PWA / favicon 图标 (server/app/static/pwa/*.png)。

图形与 base.html 里那段内联 SVG 是**同一个母题**, 改一边就要改另一边:
圆角方形蓝色渐变底 (#4a8cf0 -> #1d52ad, 左上到右下) + 白色购物袋, 袋身里
2x2 四个点。袋子取自站点自己的说法 ("把全世界最好的 AI 产品装进一个货架"),
四个点是货架上的产品。

坐标全部按 32x32 画布写 (与那段 SVG 的 viewBox 一致), 再乘 SCALE 放到母版上,
所以两边的数字可以逐个对着看。

依赖: Pillow。用法: python3 server/scripts/gen_pwa_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "pwa"

MASTER = 1024  # 母版边长; 所有尺寸都从它缩下去 (LANCZOS), 边缘才干净
SCALE = MASTER / 32  # 32x32 设计画布 -> 母版

TOP = (0x4A, 0x8C, 0xF0)  # #4a8cf0
BOT = (0x1D, 0x52, 0xAD)  # #1d52ad
WHITE = (255, 255, 255, 255)


def _u(v: float) -> float:
    """设计画布坐标 -> 母版像素。"""
    return v * SCALE


def _gradient(size: int) -> Image.Image:
    """左上到右下的线性渐变 —— 与 SVG 里 x1,y1=0,0 -> x2,y2=1,1 的 lg 一致。"""
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
    return g


def _mark(size: int, inset: float = 0.0) -> Image.Image:
    """购物袋标记, 返回一张 L 通道蒙版 (255 = 白墨水)。

    袋身是**实心**的, 四个产品点从里面**挖空** (露出底色) —— 描边版在小尺寸
    上会糊成一坨, 而且带竖直提手的空心圆角矩形读起来像挂锁而不是袋子。
    """
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    k = (size / MASTER) * (1 - inset / 16)  # 缩放 + 居中留白
    off = (size - MASTER * k) / 2

    def P(v: float) -> float:
        return off + _u(v) * k

    # 提手: 只有上半弧, 不画竖线 (画了就成锁梁了)。半径明显小于袋身宽度。
    r, cy = 3.4, 12.2
    d.arc(
        [P(16 - r), P(cy - r), P(16 + r), P(cy + r)],
        180,
        360,
        fill=255,
        width=max(1, round(_u(1.8) * k)),
    )
    # 袋身: 宽大于高, 才像袋子而不像盒子
    d.rounded_rectangle([P(8.0), P(12.2), P(24.0), P(24.2)], radius=_u(1.8) * k, fill=255)
    # 袋里的四个产品 —— 挖空
    dot = 1.25
    for cx in (12.7, 19.3):
        for cy2 in (16.6, 20.4):
            d.ellipse([P(cx - dot), P(cy2 - dot), P(cx + dot), P(cy2 + dot)], fill=0)
    return m


def _rounded(size: int, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def icon(size: int, *, maskable: bool = False) -> Image.Image:
    """maskable: 满幅出血 + 标记缩到安全区内 (Android 会自行裁成任意形状)。"""
    base = _gradient(MASTER).convert("RGBA")
    if not maskable:
        base.putalpha(_rounded(MASTER, _u(9)))  # 圆角 9, 与 SVG 的 rx 一致
    base.paste(WHITE, mask=_mark(MASTER, inset=5.0 if maskable else 0.0))
    out = base.resize((size, size), Image.LANCZOS)
    return out.convert("RGB") if maskable else out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, mask in (
        ("icon-192", 192, False),
        ("icon-512", 512, False),
        ("icon-180", 180, False),
        ("icon-maskable-512", 512, True),
    ):
        p = OUT / f"{name}.png"
        icon(size, maskable=mask).save(p)
        print(f"  {p.relative_to(OUT.parents[3])}  {size}x{size}")


if __name__ == "__main__":
    main()
