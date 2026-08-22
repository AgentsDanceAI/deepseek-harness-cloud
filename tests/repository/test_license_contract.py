from pathlib import Path
import json
import tomllib


ROOT = Path(__file__).resolve().parents[2]
LICENSE_ID = "LicenseRef-DSH-Cloud-Community-1.0"


def test_active_license_is_the_dsh_cloud_community_license():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("DSH Cloud Community License\nVersion 1.0")
    assert "Managed Multi-Tenant Service" in text
    assert "Official Frontend" in text
    assert "Beijing AgentsDance AI Technology Co., Ltd." in text

    state = tomllib.loads((ROOT / "legal/licensing/LICENSE_STATE.toml").read_text(encoding="utf-8"))
    assert state["active_license"] == LICENSE_ID
    assert state["effective_release"] == "0.2.0"


def test_release_and_runtime_metadata_use_the_custom_license_reference():
    release = json.loads((ROOT / "release/release.json").read_text(encoding="utf-8"))
    assert release["license"] == LICENSE_ID
    assert f'org.opencontainers.image.licenses="{LICENSE_ID}"' in (
        ROOT / "server/Dockerfile"
    ).read_text(encoding="utf-8")
    assert f'org.opencontainers.image.licenses="{LICENSE_ID}"' in (
        ROOT / "deploy/workspace/Dockerfile"
    ).read_text(encoding="utf-8")


def test_public_copy_does_not_use_stale_license_positioning():
    active = (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "LICENSING.md",
        ROOT / "docs/editions.md",
    )
    joined = "\n".join(path.read_text(encoding="utf-8") for path in active)
    assert "AGPL-3.0-only" not in joined
