"""挂在 LangGraph 上的智能体 —— LangChain 那格真正干活的东西。

为什么要我们自己写: agent-chat-ui 只是**前端**, 它必须连一个 LangGraph 服务才
有东西可聊。上游那两个更像成品的自托管仓库 (open-agent-platform /
deep-agents-ui) 今年已经先后归档, 官方引导去 LangSmith 托管版 —— 归档的东西
不能当在售产品挂出去。所以前端用他们的, 智能体这一半是我们的。

模型走 DSH 网关: 型号名必须钉在**在售目录**里 (由 DSH_MODEL 给), 网关只放行
目录内的 —— 写厂商自己的名字就是每次 404。
"""

import os

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


@tool
def calculator(expression: str) -> str:
    """算一个算术表达式, 例如 "(12+5)*3"。只支持数字和 + - * / ( ) 。"""
    allowed = set("0123456789+-*/(). ")
    if not expression or set(expression) - allowed:
        return "只支持数字和 + - * / ( ) "
    try:
        # 字符集已经卡死在数字和四则运算符上, eval 不到别的东西
        return str(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307
    except Exception as e:  # noqa: BLE001
        return f"算不出来: {type(e).__name__}"


def build():
    model = ChatOpenAI(
        model=os.environ.get("DSH_MODEL") or "deepseek-v4-flash",
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY") or "unset",
        temperature=0.7,
    )
    return create_react_agent(
        model,
        tools=[calculator],
        prompt=(
            "你是跑在 LangGraph 上的智能体, 住在 DSH Cloud 里。"
            "回答简洁、直接; 该算数就用 calculator 工具, 别自己心算。"
        ),
    )


graph = build()
