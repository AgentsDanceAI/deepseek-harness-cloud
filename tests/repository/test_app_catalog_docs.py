"""README 的产品表与站内产品网格互钉。

两份 README 的开场白都承诺"16 个开源 AI 产品", 正文里那张表就得是**那 16 个**:
产品与顺序取 server/app/apps_catalog.py 的 CATALOG, 一句话与说明取 i18n 的
apps.tag.* / apps.d.* —— 和 /apps 那张 4x4 网格读的是同一份数据。

漂了会怎样: 下架一个产品 (Coze 已经下架过一次) 而 README 还挂着, 读者按图索骥
点进去是空的; 新接一格而 README 没跟上, 等于白做。表由 scripts/render_app_catalog.py
生成, 这里逐字核对生成结果 —— 改完产品跑一次 --write 即可。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from app.apps_catalog import CATALOG  # noqa: E402

START = "<!-- app-catalog:start -->"
END = "<!-- app-catalog:end -->"

READMES = {"en": "README.md", "zh": "README.zh-CN.md"}


def readme_block(path: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert START in text and END in text, f"{path} 缺 {START} 标记块"
    return text.split(START, 1)[1].split(END, 1)[0].strip()


def rendered(lang: str) -> str:
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_app_catalog.py"), "--lang", lang],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_readme_product_tables_match_the_app_catalog():
    for lang, path in READMES.items():
        assert readme_block(path) == rendered(lang), (
            f"{path} 的产品表与目录不一致 —— 跑 "
            f"`python3 scripts/render_app_catalog.py --lang {lang} --write` 重新生成"
        )


def test_every_product_is_listed():
    """逐个 id 断言, 这样漏了哪一格报错里点得出名字 (整表比对只会说"不一致")。"""
    for path in READMES.values():
        block = readme_block(path)
        for app in CATALOG:
            assert app.name in block, f"{path} 少了 {app.name} ({app.id})"


def test_readme_product_count_claims_stay_honest():
    n = len(CATALOG)
    for path, phrase in (("README.md", "Sixteen"), ("README.zh-CN.md", f"{n} 个开源 AI 产品")):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert phrase in text, f"{path} 开场白的数量说法要跟着 CATALOG ({n} 个) 改: 找不到 {phrase!r}"
    assert n == 16, f"CATALOG 现在有 {n} 个 —— README 开场白写的是十六个, 两边一起改"
