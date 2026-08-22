import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def test_release_source_is_complete_and_locked():
    release = json.loads((ROOT / "release/release.json").read_text(encoding="utf-8"))
    assert release["version"] == "0.2.0"
    assert release["license"] == "LicenseRef-DSH-Cloud-Community-1.0"
    assert release["harnessRuntime"] == "0.1.0-rc.8"
    assert release["desktopRuntime"] == "0.1.0-rc.6"
    assert release["minCliVersion"] == "0.2.0"
    assert release["legacyCompatibility"]["supportedThrough"] == "0.4.0"
    assert set(release["productImages"]) == {"server", "workspace"}
    assert "socketProxy" in release["baseImages"]
    for ref in release["baseImages"].values():
        assert DIGEST.fullmatch(ref), ref
    assert release["productImages"] == {
        "server": "ghcr.io/agentsdanceai/dsh-cloud-server:0.2.0",
        "workspace": "ghcr.io/agentsdanceai/dsh-cloud-workspace:0.2.0",
    }

    package_schema = json.loads(
        (ROOT / "release/release-manifest.schema.json").read_text(encoding="utf-8")
    )
    assert "license" in package_schema["required"]
    assert package_schema["properties"]["license"]["const"] == release["license"]


def test_release_validator_rejects_mutable_references():
    result = subprocess.run(
        ["node", "scripts/release/validate-release.mjs", "--self-test-floating"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "rejected mutable image reference" in result.stdout


def test_release_workflow_is_tag_locked_pinned_and_provenanced():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'tags: ["v*"]' in workflow
    assert "environment: release" in workflow
    assert "npm publish" in workflow and "--provenance" in workflow
    assert "NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}" in workflow
    assert "uv publish --trusted-publishing always" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "gh release create" in workflow
    uses = re.findall(r"uses:\s*([^\s]+)", workflow)
    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses), uses


def test_release_sbom_is_valid_spdx_and_uses_the_active_license(tmp_path: Path):
    stage = tmp_path / "stage"
    subprocess.run(
        ["node", "scripts/release/build-packages.mjs", str(stage)],
        cwd=ROOT,
        check=True,
    )
    output = tmp_path / "dsh-cloud-0.2.0.spdx.json"
    subprocess.run(
        ["node", "scripts/release/generate-sbom.mjs", str(stage), str(output)],
        cwd=ROOT,
        check=True,
    )
    sbom = json.loads(output.read_text(encoding="utf-8"))
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert sbom["documentNamespace"].endswith("/0.2.0")
    packages = {package["name"]: package for package in sbom["packages"]}
    assert set(packages) == {"@agentsdanceai/dsh-cloud", "dsh-cloud"}
    assert all(
        package["licenseDeclared"] == "LicenseRef-DSH-Cloud-Community-1.0"
        for package in packages.values()
    )


def test_dockerfile_is_locked_non_root_and_carries_release_source():
    dockerfile = (ROOT / "server/Dockerfile").read_text(encoding="utf-8")
    assert "ARG PYTHON_IMAGE" in dockerfile
    assert "ARG UV_IMAGE" in dockerfile
    assert "USER 10001:10001" not in dockerfile
    assert "COPY --chown=0:0 release/docker_entrypoint.py /usr/local/bin/dsh-cloud-entrypoint" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/python3", "-I", "/usr/local/bin/dsh-cloud-entrypoint"]' in dockerfile
    assert "COPY --chown=0:0 release/release.json /usr/share/dsh-cloud/release.json" in dockerfile
    assert "org.opencontainers.image.source" in dockerfile
    assert "COPY server/pyproject.toml server/uv.lock" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project --extra postgres" in dockerfile
    assert "--extra redis" in dockerfile
    assert "--extra stripe" in dockerfile
    assert "--extra dev" not in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/dsh-cloud/.venv" in dockerfile
    assert "COPY --from=builder --chown=0:0 /opt/dsh-cloud/.venv /opt/dsh-cloud/.venv" in dockerfile
    assert "RELEASE_VERSION=${VERSION}" in dockerfile
    assert "RELEASE_REVISION=${REVISION}" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "http://127.0.0.1:8100/readyz" in dockerfile
    assert 'CMD ["/usr/local/bin/python3", "-I", "-c"' in dockerfile
    assert "COPY --chown=10001:10001" not in dockerfile


def test_all_server_base_image_args_are_global_before_the_first_stage():
    dockerfile = (ROOT / "server/Dockerfile").read_text(encoding="utf-8")
    global_args = dockerfile.split("FROM", 1)[0]

    assert "ARG UV_IMAGE=" in global_args
    assert "ARG PYTHON_IMAGE=" in global_args
    assert dockerfile.count("ARG PYTHON_IMAGE=") == 1


def test_workspace_image_derives_harness_runtime_from_release_build_args():
    dockerfile = (ROOT / "deploy/workspace/Dockerfile").read_text(encoding="utf-8")
    assert "ARG NODE_IMAGE" in dockerfile
    assert "ARG HARNESS_RUNTIME" in dockerfile
    assert 'npm install -g "@deepseek-ai/dsh@${HARNESS_RUNTIME}"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert not re.search(r"rc(?:\.|)\d+", dockerfile)
