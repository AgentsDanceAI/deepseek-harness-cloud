"""把一句话交给用户的 crew 跑一轮, 输出工作台那套统一事件 (每行一个 JSON)。

CrewAI 没有 `--json` 这类流式输出 —— 它是个 Python 库, 正常用法是
`crew.kickoff(inputs=...)`。所以这里直接用它的 API, 事件从**回调**里出,
而不是去刮它那套 rich 画的框 (那等于把排版当协议)。

跑的是 **/workspace 里那个真的 CrewAI 工程** (开机时用 `crewai create crew`
生成), 不是我们另起炉灶捏的东西 —— 用户在终端里 `crewai run`、在文件面板里改
agents.yaml / tasks.yaml, 和这里跑的是同一份。

⚠️ **agents 的 verbose 输出必须挪开 stdout**: 那边打的是给人看的框线, 混进来
一行就把 JSON 流冲断, 而前端只会表现成"这一轮少了半截"。
"""

from __future__ import annotations

import datetime
import importlib
import json
import os
import sys

PROJECT = os.environ.get("DSH_CREW_DIR", "/workspace/crew")
PACKAGE = os.environ.get("DSH_CREW_PACKAGE", "dsh_crew")

_OUT = sys.stdout
sys.stdout = sys.stderr  # 框架的花哨输出去 stderr, 见上面那条警告


def emit(ev: dict) -> None:
    print(json.dumps(ev, ensure_ascii=False), file=_OUT, flush=True)


def _find_crew_class(mod):
    """找出 @CrewBase 那个类。

    按**结构**找而不是按名字: 名字是 `crewai create crew <名字>` 推导出来的,
    用户改个工程名就对不上了。判据是"这个模块里定义的、有 crew 方法的类"。
    """
    for name, obj in vars(mod).items():
        if isinstance(obj, type) and callable(getattr(obj, "crew", None)) and not name.startswith("_"):
            return obj
    raise RuntimeError(f"{PACKAGE}.crew 里没找到 crew 类")


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit({"t": "error", "message": "没有收到问题"})
        return 2

    sys.path.insert(0, os.path.join(PROJECT, "src"))
    try:
        mod = importlib.import_module(f"{PACKAGE}.crew")
    except Exception as e:  # noqa: BLE001
        emit({"t": "error", "message": f"打不开 {PROJECT}: {type(e).__name__}: {e}"[:600]})
        return 1

    crew = _find_crew_class(mod)().crew()

    # 每个任务结束报一次 —— 这支队伍是**多个 agent 依次干活**, 不报的话用户
    # 盯着一个转圈等好几分钟, 不知道到哪一步了。
    def on_task(out) -> None:
        who = getattr(out, "agent", "") or ""
        emit({"t": "tool_end", "id": str(who), "ok": True, "output": str(getattr(out, "raw", out))[:2000]})

    def on_step(step) -> None:
        # 中间步骤 (工具调用/思考) 形状随版本变, 只作调试面板用, 认不出也不丢。
        emit({"t": "raw", "line": str(step)[:400]})

    crew.task_callback = on_task
    crew.step_callback = on_step

    inputs = {"topic": prompt, "current_year": str(datetime.datetime.now().year)}
    try:
        out = crew.kickoff(inputs=inputs)
    except Exception as e:  # noqa: BLE001
        emit({"t": "error", "message": f"{type(e).__name__}: {e}"[:600]})
        return 1

    emit({"t": "text", "text": str(getattr(out, "raw", out))})
    usage = getattr(out, "token_usage", None)
    u = {}
    if usage is not None:
        u = {
            "input_tokens": getattr(usage, "prompt_tokens", 0),
            "output_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    emit({"t": "done", "usage": u})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        emit({"t": "error", "message": f"{type(e).__name__}: {e}"[:600]})
        raise SystemExit(1) from e
