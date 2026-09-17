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

# 用 listed(): 未上架的 (unlisted) 不进 README —— 否则文档在宣传一个
# 货架上找不到的东西 (2026-09-17 创始人「前期先不展示」)。
from app.apps_catalog import listed  # noqa: E402

CATALOG = listed()

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


#: 英文开场白把数字写成单词 ("Seventeen open-source AI products")。这里跟着 CATALOG
#: 推导而不是写死 —— 原先中文是推导的、英文却钉着 "Sixteen", 于是加一个产品必然红在
#: 一个跟被测行为无关的字上, 而且报错还指不出该改成什么。
_NUMBER_WORDS = {
    14: "Fourteen",
    15: "Fifteen",
    16: "Sixteen",
    17: "Seventeen",
    18: "Eighteen",
    19: "Nineteen",
    20: "Twenty",
}


def test_readme_product_count_claims_stay_honest():
    n = len(CATALOG)
    word = _NUMBER_WORDS.get(n)
    assert word, f"货架上有 {n} 个产品, 但 _NUMBER_WORDS 里没有这个数的英文写法, 补一个"
    for path, phrase in (
        (("README.md"), word),
        ("README.zh-CN.md", f"{n} 个开源 AI 产品"),
    ):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert phrase in text, (
            f"{path} 开场白的数量说法要跟着 CATALOG ({n} 个) 改: 找不到 {phrase!r}"
        )
    # 上面两条已经把"开场白与 CATALOG 对不对得上"钉死了; 再钉一个具体数字是**重复**,
    # 而且它每加一个产品都会红在一个与被测行为无关的字上 (这次就是它)。删。
