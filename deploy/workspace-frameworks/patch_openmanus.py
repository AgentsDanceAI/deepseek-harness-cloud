"""构建期给 OpenManus 打一个补丁: **模型直接作答 (没调工具) 就算这一轮结束**。

上游的 ReAct 循环里, AUTO 模式下模型回了正文却没选任何工具时, think() 返回
True、act() 原样返回正文, 然后**继续下一步** —— 直到模型主动调 `terminate`
或者跑满 max_steps。可对一句"你好啊"它永远不会去调 terminate: 于是第 1 步答
"你好! 有什么可以帮你?", 第 2 步看着自己那句话再答"明白了, 请告诉我你的需求",
第 3 步……一路自言自语到 20 步。老板 2026-09-02 在终端里撞到的就是这个:
"OpenManus 一直回复什么乱七八糟的"。

补在这里而不是 runner 里: 终端标签页跑的是它自己的 main.py, 只改 runner 的话
对话框好了、终端还在自言自语。构建期打, 打不上就**构建失败** —— 上游改了这段
代码时我们当场知道, 而不是上线后用户先发现。
"""

from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("/opt/openmanus/app/agent/toolcall.py")

OLD = '''            # For 'auto' mode, continue with content if no commands but content exists
            if self.tool_choices == ToolChoice.AUTO and not self.tool_calls:
                return bool(content)
'''
NEW = '''            # For 'auto' mode, continue with content if no commands but content exists
            if self.tool_choices == ToolChoice.AUTO and not self.tool_calls:
                # [dsh] 模型直接作答、一个工具都没选 = 它认为这一轮答完了。
                # 不收口的话它会看着自己的回答继续"下一步", 直到跑满 max_steps
                # —— 对一句问候能自言自语 20 步。act() 仍会把这段正文返回。
                if content:
                    self.state = AgentState.FINISHED
                return bool(content)
'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    if NEW in src:
        print("[patch] OpenManus 已打过, 跳过")
        return 0
    if OLD not in src:
        print("[patch] !! OpenManus 的 toolcall.py 里找不到要改的那段 —— 上游改了, 补丁作废", file=sys.stderr)
        return 1
    if "AgentState" not in src.split("class ToolCallAgent")[0]:
        print("[patch] !! toolcall.py 没有导入 AgentState, 补丁引用不到它", file=sys.stderr)
        return 1
    TARGET.write_text(src.replace(OLD, NEW, 1), encoding="utf-8")
    print("[patch] OpenManus: 直接作答即收口 —— 已打上")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
