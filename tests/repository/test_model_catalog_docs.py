"""README 的模型目录与网关配置互钉。

营销页承诺"注册即送 500 积分, 以下模型开箱可用" —— 承诺的清单必须与
server/config/models.json 逐一对得上: 目录加了模型而 README 没跟上, 或者
README 还挂着已下架的模型, 这里都要红。清单读的是两份 README 里
model-catalog 标记块之间的内容。
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

START = "<!-- model-catalog:start -->"
END = "<!-- model-catalog:end -->"


def catalog_names() -> set[str]:
    data = json.loads((ROOT / "server/config/models.json").read_text(encoding="utf-8"))
    models = data["models"] if isinstance(data, dict) else data
    return {m["display_name"] for m in models}


def readme_block_names(path: str) -> set[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert START in text and END in text, f"{path} 缺 model-catalog 标记块"
    block = text.split(START, 1)[1].split(END, 1)[0]
    return set(re.findall(r"`([^`]+)`", block))


def test_readme_model_tables_match_the_gateway_catalog():
    expected = catalog_names()
    assert expected, "models.json 是空的?"
    for path in ("README.md", "README.zh-CN.md"):
        listed = readme_block_names(path)
        assert listed == expected, (
            f"{path} 的模型目录与 models.json 不一致: "
            f"缺 {sorted(expected - listed)}; 多 {sorted(listed - expected)}"
        )


def test_readme_model_count_claims_stay_honest():
    n = len(catalog_names())
    for path, phrase in (("README.md", f"{n} models"), ("README.zh-CN.md", f"{n} 个模型")):
        assert phrase in (ROOT / path).read_text(encoding="utf-8"), (path, phrase)
