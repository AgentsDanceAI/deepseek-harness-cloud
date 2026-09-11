"""镜像可见性的合同用例 —— 探针与生产脚本共用一份实现。

**覆盖面是有边界的, 别当成全量**: 有五格的镜像引用来自生产 `.env`
(WORK_IMAGE_REF、COMFY_IMAGE_REF 这些), 仓库里根本没有, CI 看不见。这里钉的是
**代码里写死的那几个**; 全量要在生产机上跑
`python server/scripts/check_image_visibility.py`。

2026-09-10: 四个包 (pi-web-ui / langchain-agent / agent-frameworks / openmausbot)
一直是私有的, 靠人工探测才发现 —— 而 README 上那张手写清单里有一个写错了。
设成公开之后这条用例接手代码里那部分。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ.setdefault("DHC_DATA_DIR", tempfile.mkdtemp(prefix="dhc-img-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
from check_image_visibility import ghcr_refs, probe  # noqa: E402

#: 代码里写死的 ghcr 引用至少有这么多。低于它说明探针范围塌了 (比如 registry()
#: 空了、或者引用都挪进了 env), 这条用例会静默变成"零覆盖的绿"。
#: 2026-09-11 从 5 提到 6: 工作台外壳开关的**另一侧** (workspace-cli) 也纳入了扫描。
_FLOOR = 6


def test_hardcoded_images_are_anonymously_pullable():
    refs = ghcr_refs()
    assert len(refs) >= _FLOOR, f"只扫到 {len(refs)} 个 ghcr 引用, 比下限 {_FLOOR} 还少 —— 探针范围塌了"
    results = probe(refs)

    unreachable = [r for r, s in results.items() if isinstance(s, str)]
    if len(unreachable) == len(refs):
        pytest.skip(f"ghcr 整个连不上, 不是可见性问题: {results[unreachable[0]]}")

    denied = sorted(r for r, s in results.items() if s in (401, 403))
    missing = sorted(r for r, s in results.items() if s == 404)
    assert not denied, (
        "这些镜像陌生人拉不动 —— 去 github.com/orgs/agentsdancepro/packages "
        f"把包的可见性改成 Public: {denied}"
    )
    assert not missing, f"这些 tag 在 ghcr 上根本不存在 (拼错了? 还没推?): {missing}"
