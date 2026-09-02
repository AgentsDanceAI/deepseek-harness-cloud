"""构建期给 CrewAI-Studio 打补丁: 提供方下拉只留我们的网关。

它默认把 Groq / Anthropic / LM Studio / xAI 的一串型号也列在下拉里 —— 没配对应
密钥也照列 (探针实测: 只给 OPENAI_* 时下拉里仍有 10 个条目, 8 个是别家的),
用户选了必然报错。托管环境里只有一扇门: 我们的网关。顺手把 "OpenAI" 这个标签
换成 "DSH" —— 它不是 OpenAI, 是我们的网关, 标签说实话。
打不上就构建失败: 上游改了这段我们当场知道。
"""

from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("/opt/cs/app/llms.py")
ANCHOR = 'def llm_providers_and_models():'
NEW = '''# [dsh] 只留网关这一个提供方, 标签改成 DSH (它不是 OpenAI)。
LLM_CONFIG = {"DSH": LLM_CONFIG["OpenAI"]}


def llm_providers_and_models():'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    if "[dsh] 只留网关" in src:
        print("[patch] 已打过, 跳过")
        return 0
    if src.count(ANCHOR) != 1 or 'LLM_CONFIG = {\n    "OpenAI": {' not in src:
        print("[patch] !! llms.py 里找不到锚点 —— 上游改了, 补丁作废", file=sys.stderr)
        return 1
    TARGET.write_text(src.replace(ANCHOR, NEW, 1), encoding="utf-8")
    print("[patch] CrewAI-Studio: 提供方只留 DSH —— 已打上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
