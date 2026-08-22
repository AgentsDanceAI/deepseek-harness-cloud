import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_maintained_documentation_has_english_and_simplified_chinese_pairs():
    manifest = json.loads((ROOT / "docs/documentation-pairs.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 1
    assert len(manifest["pairs"]) >= 25
    for english_name, chinese_name in manifest["pairs"]:
        english = ROOT / english_name
        chinese = ROOT / chinese_name
        assert english.is_file(), english_name
        assert chinese.is_file(), chinese_name
        english_text = english.read_text(encoding="utf-8")
        chinese_text = chinese.read_text(encoding="utf-8")
        assert Path(chinese_name).name in english_text, english_name
        assert Path(english_name).name in chinese_text, chinese_name
