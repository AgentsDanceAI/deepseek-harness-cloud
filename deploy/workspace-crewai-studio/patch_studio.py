"""构建期给 CrewAI-Studio 打两处补丁。打不上就构建失败: 上游改了这段我们当场知道。

1. **提供方下拉只留我们的网关。** 它默认把 Groq / Anthropic / LM Studio / xAI 的一串
   型号也列在下拉里 —— 没配对应密钥也照列 (探针实测: 只给 OPENAI_* 时下拉里仍有
   10 个条目, 8 个是别家的), 用户选了必然报错。托管环境里只有一扇门: 我们的网关。
   顺手把 "OpenAI" 这个标签换成 "DSH" —— 它不是 OpenAI, 标签说实话。
2. **运行中别把页面钉死成英文 "Kickoff!"。** 上游想在运行时把用户留在执行页, 写的
   是 ss.page = "Kickoff!"; 可页面键是**翻译后的标签**, 中文界面里那页叫「执行!」,
   对不上任何页 -> 每次重绘都落回第一页「团队」; 而执行页是靠自己每秒 rerun 轮询
   来收结果的, 它不画, 结果队列没人消费, running 永远是 True —— 队伍在后台跑完了,
   用户被钉在「团队」页什么也看不到 (2026-09-02 中文界面实测)。作者用英文界面,
   所以从没撞到。改成 t("page.kickoff")。
"""

from __future__ import annotations

import pathlib
import sys

LLMS = pathlib.Path("/opt/cs/app/llms.py")
LLMS_ANCHOR = "def llm_providers_and_models():"
LLMS_NEW = (
    "# [dsh] 只留网关这一个提供方, 标签改成 DSH (它不是 OpenAI)。\n"
    'LLM_CONFIG = {"DSH": LLM_CONFIG["OpenAI"]}\n\n\n'
    "def llm_providers_and_models():"
)

RUN = pathlib.Path("/opt/cs/app/pg_crew_run.py")
RUN_OLD = '        if ss.running and ss.page != "Kickoff!":\n            ss.page = "Kickoff!"\n'
RUN_NEW = (
    "        # [dsh] 页面键是翻译后的标签 (中文界面里是「执行!」), 写死英文对不上任何页,\n"
    "        # 每次重绘都落回「团队」, 执行页不画就没人收结果 -> running 永远清不掉。\n"
    '        if ss.running and ss.page != t("page.kickoff"):\n'
    '            ss.page = t("page.kickoff")\n'
)


def main() -> int:
    rc = 0
    src = LLMS.read_text(encoding="utf-8")
    if "[dsh] 只留网关" in src:
        print("[patch] llms.py 已打过, 跳过")
    elif src.count(LLMS_ANCHOR) != 1 or 'LLM_CONFIG = {\n    "OpenAI": {' not in src:
        print("[patch] !! llms.py 里找不到锚点 —— 上游改了, 补丁作废", file=sys.stderr)
        rc = 1
    else:
        LLMS.write_text(src.replace(LLMS_ANCHOR, LLMS_NEW, 1), encoding="utf-8")
        print("[patch] 提供方只留 DSH —— 已打上")

    run = RUN.read_text(encoding="utf-8")
    if "[dsh] 页面键是" in run:
        print("[patch] pg_crew_run.py 已打过, 跳过")
    elif run.count(RUN_OLD) != 1 or 'from i18n import t' not in run and 't(' not in run:
        print("[patch] !! pg_crew_run.py 里找不到 Kickoff! 那两行 —— 上游改了, 补丁作废", file=sys.stderr)
        rc = 1
    else:
        RUN.write_text(run.replace(RUN_OLD, RUN_NEW, 1), encoding="utf-8")
        print("[patch] 运行中不再把页面钉死成英文 Kickoff! —— 已打上")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
