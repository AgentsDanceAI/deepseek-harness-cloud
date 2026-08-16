#!/usr/bin/env python3
"""生成 DSH Cloud App 图标 (Android mipmap 全尺寸 + iOS AppIcon).

品牌图形与站点 logo 同一母题: 圆角方形蓝色渐变底 (#3574de → #1d52ad),
白色终端提示符 ">" + 下划线 "_"。

依赖: Pillow (pip install pillow)。
用法: python3 scripts/gen_icons.py   (需先 npx cap add android / ios)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MOBILE = Path(__file__).resolve().parents[1]
ANDROID_RES = MOBILE / "android" / "app" / "src" / "main" / "res"
IOS_APPICON = MOBILE / "ios" / "App" / "App" / "Assets.xcassets" / "AppIcon.appiconset"

BLUE_TOP = (0x35, 0x74, 0xDE)  # #3574de
BLUE_BOT = (0x1D, 0x52, 0xAD)  # #1d52ad
WHITE = (255, 255, 255, 255)

MASTER = 1024  # 母版画布边长
SS = 4         # 超采样倍数, 保证小尺寸边缘平滑

# 传统启动图标 (48dp 基准)
ANDROID_LAUNCHER_SIZES = {
    "mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192,
}
# 自适应图标图层 (108dp 基准)
ANDROID_ADAPTIVE_SIZES = {
    "mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432,
}


def gradient(size: int) -> Image.Image:
    """对角线性渐变 (左上 → 右下)。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    span = 2 * (size - 1) or 1
    for y in range(size):
        for x in range(size):
            t = (x + y) / span
            px[x, y] = tuple(
                round(a + (b - a) * t) for a, b in zip(BLUE_TOP, BLUE_BOT)
            )
    return img


def glyph_layer(size: int, scale: float = 1.0) -> Image.Image:
    """透明画布上的白色 ">_" 图形, scale 控制相对整个画布的缩放。"""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c = big / 2

    def pt(nx: float, ny: float) -> tuple[float, float]:
        # 归一化坐标 → 像素坐标, 围绕中心按 scale 缩放
        return (c + (nx - 0.5) * big * scale, c + (ny - 0.5) * big * scale)

    w = 0.085 * big * scale  # 笔画宽度

    # ">" 折线 (带圆头端点)
    p1, p2, p3 = pt(0.29, 0.36), pt(0.47, 0.50), pt(0.29, 0.64)
    draw.line([p1, p2, p3], fill=WHITE, width=round(w), joint="curve")
    for p in (p1, p2, p3):
        draw.ellipse(
            [p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=WHITE
        )

    # "_" 下划线 (圆角矩形, 底边与 ">" 底端对齐)
    ux0, uy1 = pt(0.53, 0.64)
    ux1, _ = pt(0.73, 0.64)
    draw.rounded_rectangle([ux0, uy1 - w, ux1, uy1], radius=w / 2, fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def full_icon(size: int) -> Image.Image:
    """渐变底 + 白色图形 (未裁形状的正方形母版)。"""
    img = gradient(size).convert("RGBA")
    img.alpha_composite(glyph_layer(size))
    return img


def masked(img: Image.Image, kind: str) -> Image.Image:
    """按圆角方形 / 圆形裁切 (蒙版超采样绘制)。"""
    size = img.width
    big = size * SS
    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    if kind == "rounded":
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=round(big * 0.225), fill=255)
    else:  # circle
        d.ellipse([0, 0, big - 1, big - 1], fill=255)
    out = img.copy()
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def main() -> None:
    master = full_icon(MASTER)

    # ---------- Android ----------
    if ANDROID_RES.is_dir():
        for dpi, size in ANDROID_LAUNCHER_SIZES.items():
            d = ANDROID_RES / f"mipmap-{dpi}"
            d.mkdir(parents=True, exist_ok=True)
            base = master.resize((size, size), Image.LANCZOS)
            masked(base, "rounded").save(d / "ic_launcher.png")
            masked(base, "circle").save(d / "ic_launcher_round.png")

        for dpi, size in ANDROID_ADAPTIVE_SIZES.items():
            d = ANDROID_RES / f"mipmap-{dpi}"
            # 前景: 图形缩至 2/3 (108dp 画布仅中央 ~72dp 可见)
            glyph_layer(size, scale=2 / 3).save(d / "ic_launcher_foreground.png")
            # 背景: 全幅渐变
            gradient(size).save(d / "ic_launcher_background.png")
        print(f"[ok] Android mipmap icons -> {ANDROID_RES}")
    else:
        print(f"[skip] Android res dir not found: {ANDROID_RES} (run `npx cap add android` first)")

    # ---------- iOS ----------
    if IOS_APPICON.is_dir():
        # iOS 商店图标: 1024x1024, 不透明, 直角 (系统自动加圆角蒙版)
        master.convert("RGB").save(IOS_APPICON / "AppIcon-512@2x.png")
        print(f"[ok] iOS AppIcon -> {IOS_APPICON / 'AppIcon-512@2x.png'}")
    else:
        print(f"[skip] iOS AppIcon dir not found: {IOS_APPICON} (run `npx cap add ios` first)")


if __name__ == "__main__":
    main()
