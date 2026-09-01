"""把 AutoGen Studio 里写死的模型名换成读环境变量。

为什么在**构建时**改而不是运行时灌: 它的默认图库和默认模型客户端是在首次访问
时按用户建进 SQLite 的, 灌得比那更早才有用; 而且这两处是纯常量, 改一次镜像里
就对了 —— 比起启动脚本去 POST 它的接口, 少一条会静默失败的链路。

改的是两个地方:
  · types.py 的 default_model_client —— 新建队伍时的默认模型;
  · gallery/builder.py 的 base_model —— 自带那几支示例队伍用的模型。
两处都写死了 gpt-4o-mini, 而我们的网关只放行在售目录里的型号, 不改就是每次 404。
"""
import os
import re

P = "/usr/local/lib/python3.12/site-packages/autogenstudio"
HEAD = (
    "import os as _dsh_os\n"
    "_DSH_MODEL = _dsh_os.environ.get('DSH_MODEL') or 'gpt-4o-mini'\n"
    "_DSH_BASE = _dsh_os.environ.get('OPENAI_BASE_URL') or None\n"
    "_DSH_KEY = _dsh_os.environ.get('OPENAI_API_KEY') or 'your-api-key'\n"
    # 非 OpenAI 的型号名**必须显式给能力声明**, 否则 OpenAIChatCompletionClient
    # 构造就抛 ValueError("model_info is required when model name is not a valid
    # OpenAI model") —— 而抛的位置在建默认图库那一步, 表现是整个应用起不来。
    "_DSH_INFO = None if not _dsh_os.environ.get('DSH_MODEL') else {\n"
    "    'vision': False, 'function_calling': True, 'json_output': True,\n"
    "    'family': 'unknown', 'structured_output': True,\n"
    "}\n"
)

edits = [
    (f"{P}/datamodel/types.py",
     'OpenAIChatCompletionClient(\n        model="gpt-4o-mini", api_key="your-api-key"\n    )',
     "OpenAIChatCompletionClient(\n"
     "        model=_DSH_MODEL, api_key=_DSH_KEY, base_url=_DSH_BASE, model_info=_DSH_INFO\n"
     "    )"),
    (f"{P}/gallery/builder.py",
     'OpenAIChatCompletionClient(model="gpt-4o-mini")',
     "OpenAIChatCompletionClient(model=_DSH_MODEL, api_key=_DSH_KEY, base_url=_DSH_BASE, model_info=_DSH_INFO)"),
    (f"{P}/gallery/builder.py",
     'OpenAIChatCompletionClient(model="gpt-4o", temperature=0.7)',
     "OpenAIChatCompletionClient(model=_DSH_MODEL, api_key=_DSH_KEY, base_url=_DSH_BASE, model_info=_DSH_INFO, temperature=0.7)"),
]

touched = set()
for path, old, new in edits:
    s = open(path, encoding="utf-8").read()
    if old not in s:
        raise SystemExit(f"!! 锚点不在了, 上游改过版: {path}\n   {old[:60]}")
    s = s.replace(old, new, 1)
    if path not in touched:
        # 头部常量插在最后一个 import 之后
        m = list(re.finditer(r"^(from .+|import .+)$", s, re.M))
        s = s[: m[-1].end()] + "\n" + HEAD + s[m[-1].end():]
        touched.add(path)
    open(path, "w", encoding="utf-8").write(s)
    print(f"改好 {path}")

# 立刻自证: 导入一遍, 看默认模型是不是环境里那个
os.environ.setdefault("DSH_MODEL", "补丁自检用的型号")
import subprocess
out = subprocess.run(
    ["python", "-c",
     "import json;from autogenstudio.datamodel.types import SettingsConfig;"
     "print(json.dumps(SettingsConfig().default_model_client.config))"],
    capture_output=True, text=True, env={**os.environ})
print("自检:", (out.stdout or out.stderr).strip()[:200])
