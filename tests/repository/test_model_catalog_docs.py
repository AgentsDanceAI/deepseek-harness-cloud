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
EMBED_START = "<!-- embedding-catalog:start -->"
EMBED_END = "<!-- embedding-catalog:end -->"


def catalog_names() -> set[str]:
    data = json.loads((ROOT / "server/config/models.json").read_text(encoding="utf-8"))
    models = data["models"] if isinstance(data, dict) else data
    return {m["display_name"] for m in models}


def embedding_models() -> list[dict]:
    data = json.loads((ROOT / "server/config/models.json").read_text(encoding="utf-8"))
    return data["embedding_models"]


def readme_block_names(path: str, start: str = START, end: str = END) -> set[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert start in text and end in text, f"{path} 缺 {start} 标记块"
    block = text.split(start, 1)[1].split(end, 1)[0]
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


def test_readme_embedding_tables_match_the_gateway_catalog():
    """向量化目录同理 —— 而且它比对话目录更容易漂: 知识库的模型名是用户**抄进
    Coze/Dify 配置里**的, README 上多一个已下架的型号, 换来的是他建完知识库才
    发现每一条都 404。"""
    expected = {m["display_name"] for m in embedding_models()}
    assert expected, "models.json 没有 embedding_models"
    for path in ("README.md", "README.zh-CN.md"):
        listed = readme_block_names(path, EMBED_START, EMBED_END)
        assert listed == expected, (
            f"{path} 的向量化目录与 models.json 不一致: "
            f"缺 {sorted(expected - listed)}; 多 {sorted(listed - expected)}"
        )


def test_readme_embedding_dimensions_match_the_catalog():
    """维度写错比漏写更坏: 用户照着 README 把 DIMS 填进向量库, 建好的集合与实际
    向量对不上, 报错发生在写入那一刻, 跟"维度"两个字毫无关系。"""
    for path in ("README.md", "README.zh-CN.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        block = text.split(EMBED_START, 1)[1].split(EMBED_END, 1)[0]
        listed = dict(re.findall(r"\| `([^`]+)` \| (\d+) \|", block))
        assert listed == {m["display_name"]: str(m["dimensions"]) for m in embedding_models()}, path
