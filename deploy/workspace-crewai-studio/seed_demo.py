"""开机给 CrewAI-Studio 种一支示例船员队 —— 用户进来就能点「运行团队」。

空的 Studio 首屏是 "No crews defined yet" + 一个 Create crew 按钮: 要先建两个
Agent、两个 Task、再组一个 Crew, 才能跑第一次。这个产品卖的是"开箱即用", 所以
开机时如果库里一支队伍都没有, 就种一支: 研究员 + 撰写, 任务里带 {question}
占位符 —— Kickoff 页会自动为它生成一个输入框。
只在**一支都没有**时种 (库落在 NAS 上跨实例留着, 每次开机都种会攒一堆)。
实体形状照抄 app/db_utils.py 的 save_agent/save_task/save_crew。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, "/opt/cs/app")
import db_utils  # noqa: E402  (它在裸模式下会打一行 streamlit 警告, 无害)

MODEL = os.environ.get("DSH_MODEL", "gpt-5.6-luna")
PROVIDER_MODEL = f"DSH: {MODEL}"


def main() -> int:
    db_utils.initialize_db()
    if db_utils.load_crews():
        print("[seed] 已有队伍, 不种")
        return 0
    now = datetime.now().isoformat()
    agents = {
        "A_dsh_researcher": {
            "created_at": now,
            "role": "资深研究员",
            "backstory": "你擅长快速抓住问题的要害, 分清什么是事实、什么是推测。你只整理要点, 不写长篇 —— 成文是同事的活。",
            "goal": "把用户的问题查清楚, 列出回答所需的要点",
            "allow_delegation": False,
            "verbose": True,
            "cache": True,
            "llm_provider_model": PROVIDER_MODEL,
            "temperature": 0.1,
            "max_iter": 25,
            "tool_ids": [],
            "knowledge_source_ids": [],
        },
        "A_dsh_writer": {
            "created_at": now,
            "role": "答复撰写",
            "backstory": "你写东西简洁、可读, 该短就短。一句话能答完的问题就只写一句话, 复杂问题才展开。",
            "goal": "用中文把答案讲清楚, 直接回答用户问的那件事",
            "allow_delegation": False,
            "verbose": True,
            "cache": True,
            "llm_provider_model": PROVIDER_MODEL,
            "temperature": 0.2,
            "max_iter": 25,
            "tool_ids": [],
            "knowledge_source_ids": [],
        },
    }
    tasks = {
        "T_dsh_research": {
            "description": "针对用户的问题做必要的梳理: {question}\n只列要点, 不要展开成文章。",
            "expected_output": "回答这个问题所需的要点, 最多 5 条。问题很简单时只给 1 条。",
            "async_execution": False,
            "agent_id": "A_dsh_researcher",
            "context_from_async_tasks_ids": None,
            "context_from_sync_tasks_ids": None,
            "created_at": now,
        },
        "T_dsh_answer": {
            "description": "根据上一步的要点, 直接回答用户的问题: {question}",
            "expected_output": "对用户问题的直接回答。长度随问题走 —— 一句话能答完的就只写一句话, 不要凑字数。用 markdown, 不要包在 ``` 里。",
            "async_execution": False,
            "agent_id": "A_dsh_writer",
            "context_from_async_tasks_ids": None,
            "context_from_sync_tasks_ids": ["T_dsh_research"],
            "created_at": now,
        },
    }
    crew = {
        "name": "示例: 研究并作答",
        "process": "sequential",
        "verbose": True,
        "agent_ids": list(agents),
        "task_ids": list(tasks),
        "memory": False,
        "cache": True,
        "planning": False,
        "planning_llm": None,
        "max_rpm": 1000,
        "manager_llm": None,
        "manager_agent_id": None,
        "created_at": now,
        "knowledge_source_ids": [],
    }
    for aid, a in agents.items():
        db_utils.save_entity("agent", aid, a)
    for tid, t in tasks.items():
        db_utils.save_entity("task", tid, t)
    db_utils.save_entity("crew", "C_dsh_demo", crew)
    print("[seed] 种了示例队伍: 研究员 + 撰写, 2 个任务, 占位符 {question}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
