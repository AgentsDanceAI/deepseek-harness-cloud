import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from packaging.metadata import Metadata


ROOT = Path(__file__).resolve().parents[2]
NODE = ROOT / "packages/cli-npm/bin/dsh-cloud.mjs"

def _release_version() -> str:
    """当前发行版本。测试里凡是"跟着版本走"的值都从这里取, 免得发版时四处手改。"""
    return json.loads((ROOT / "release/release.json").read_text(encoding="utf-8"))["version"]

PYTHON_SRC = ROOT / "packages/cli-python/src"


def run_node(*args: str):
    return subprocess.run(["node", str(NODE), *args], cwd=ROOT, text=True, capture_output=True)


def run_python(*args: str):
    env = {**os.environ, "PYTHONPATH": str(PYTHON_SRC)}
    return subprocess.run(
        [sys.executable, "-m", "dsh_cloud_cli", *args], cwd=ROOT, env=env, text=True, capture_output=True
    )


def test_node_and_python_expose_help_version_and_equivalent_dry_run():
    for runner in (run_node, run_python):
        help_result = runner("--help")
        assert help_result.returncode == 0, help_result.stderr
        assert "start" in help_result.stdout and "--dry-run" in help_result.stdout
        version_result = runner("--version")
        assert version_result.returncode == 0, version_result.stderr
        assert version_result.stdout.strip() == _release_version()
        dry_run = runner("start", "--dry-run", "--json")
        assert dry_run.returncode == 0, dry_run.stderr
        value = json.loads(dry_run.stdout)
        assert value["bindAddress"] == "127.0.0.1"
        assert value["url"] == "http://localhost:8787"
        assert value["publicBaseUrl"] == value["url"]
        assert value["dockerArgv"][-3:] == ["up", "-d", "--wait"]


def test_node_and_python_init_generate_safe_secret_free_stacks(tmp_path: Path):
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / name
        result = runner("init", str(target), "--mode", "trial", "--yes", "--json")
        assert result.returncode == 0, result.stderr
        state = json.loads((target / ".dsh-cloud/state.json").read_text(encoding="utf-8"))
        assert "auth" not in json.dumps(state).lower()
        assert (target / "secrets/auth_secret").stat().st_mode & 0o777 == 0o600
        env_text = (target / ".env").read_text(encoding="utf-8")
        assert "BIND_ADDRESS=127.0.0.1" in env_text
        assert "PUBLIC_BASE=http://localhost:8787" in env_text
        assert "DSH_SITE=http://localhost" in env_text
        assert "AUTH_SECRET=" not in env_text
        for identity_setting in (
            "MAIL_SMTP_HOST=",
            "MAIL_SMTP_USER=",
            "MAIL_SMTP_PASS=",
            "MAIL_FROM=",
            "GOOGLE_LOGIN_CLIENT_ID=",
            "GOOGLE_LOGIN_CLIENT_SECRET=",
            "GITHUB_LOGIN_CLIENT_ID=",
            "GITHUB_LOGIN_CLIENT_SECRET=",
        ):
            assert identity_setting in env_text
        assert f"WORK_IMAGE=ghcr.io/agentsdanceai/dsh-cloud-workspace:{_release_version()}" in env_text
        assert (target / "docker-compose.yml").is_file()
        for config_path in (
            "config/models.json",
            "config/i18n/en.json",
            "config/i18n/zh.json",
            "config/pricing.cny.json",
            "config/pricing.eur.json",
            "config/pricing.usd.json",
        ):
            assert (target / config_path).is_file(), config_path
        compose = subprocess.run(
            ["docker", "compose", "--env-file", str(target / ".env"), "-f", str(target / "docker-compose.yml"), "config", "--quiet"],
            cwd=target,
            text=True,
            capture_output=True,
        )
        assert compose.returncode == 0, compose.stderr


def test_node_and_python_public_start_stop_before_docker_until_identity_is_configured(tmp_path: Path):
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / f"public-{name}"
        result = runner(
            "start",
            str(target),
            "--mode",
            "selfhost",
            "--domain",
            "cloud.example.com",
            "--admin-email",
            "admin@example.com",
            "--yes",
            "--json",
        )

        assert result.returncode == 2
        assert "SMTP or OAuth" in result.stdout + result.stderr
        assert (target / ".env").is_file()
        assert (target / ".dsh-cloud/state.json").is_file()


def test_staged_packages_ship_templates_and_release_manifest(tmp_path: Path):
    stage = tmp_path / "stage"
    result = subprocess.run(
        ["node", "scripts/release/build-packages.mjs", str(stage)], cwd=ROOT, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    npm = stage / "npm"
    py = stage / "python"
    for root in (npm, py / "src/dsh_cloud_cli"):
        assert (root / "release-manifest.json").is_file()
        assert (root / "templates/docker-compose.yml").is_file()
        assert (root / "templates/config/pricing.cny.json").is_file()
        assert (root / "templates/config/pricing.eur.json").is_file()
        assert (root / "templates/config/pricing.usd.json").is_file()
        assert (root / "templates/config/i18n/en.json").is_file()
        assert (root / "templates/config/i18n/zh.json").is_file()
    packed = subprocess.run(
        ["npm", "pack", "--json", "--dry-run"],
        cwd=npm,
        env={**os.environ, "npm_config_cache": str(tmp_path / "npm-cache")},
        text=True,
        capture_output=True,
    )
    assert packed.returncode == 0, packed.stderr
    files = {item["path"] for item in json.loads(packed.stdout)[0]["files"]}
    assert "templates/docker-compose.yml" in files
    assert "release-manifest.json" in files
    package = json.loads((npm / "package.json").read_text(encoding="utf-8"))
    assert package["license"] == "SEE LICENSE IN LICENSE"
    assert package["homepage"].startswith("https://github.com/AgentsDanceAI/")
    assert package["bugs"].endswith("/issues")
    assert package["author"] == "AgentsDance AI"
    assert package["publishConfig"] == {
        "access": "public",
        "registry": "https://registry.npmjs.org/",
    }


def test_package_staging_excludes_local_caches_and_virtualenvs(tmp_path: Path):
    stage = tmp_path / "stage"
    result = subprocess.run(["node", "scripts/release/build-packages.mjs", str(stage)], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    forbidden = {".venv", "__pycache__", ".pytest_cache", "node_modules"}
    assert not [path for path in stage.rglob("*") if path.name in forbidden]


def test_python_wheel_contains_deployment_assets(tmp_path: Path):
    stage = tmp_path / "stage"
    subprocess.run(["node", "scripts/release/build-packages.mjs", str(stage)], cwd=ROOT, check=True)
    output = tmp_path / "wheel"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--project", str(stage / "python"), "--out-dir", str(output)],
        cwd=ROOT,
        env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Metadata.from_email(archive.read(metadata_path), validate=True)
    assert "dsh_cloud_cli/templates/docker-compose.yml" in names
    assert "dsh_cloud_cli/release-manifest.json" in names
    assert "dsh_cloud_cli/templates/config/i18n/en.json" in names
    assert "dsh_cloud_cli/templates/config/pricing.eur.json" in names
    assert metadata.metadata_version == "2.4"
    assert metadata.license_expression == "LicenseRef-DSH-Cloud-Community-1.0"
    assert "LICENSE" in metadata.license_files
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)


def test_python_release_artifacts_are_reproducible_and_publishable(tmp_path: Path):
    stage = tmp_path / "stage"
    subprocess.run(["node", "scripts/release/build-packages.mjs", str(stage)], cwd=ROOT, check=True)
    hashes = []
    outputs = []
    for build_number in (1, 2):
        output = tmp_path / f"build-{build_number}"
        env = {
            **os.environ,
            "SOURCE_DATE_EPOCH": "0",
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        }
        result = subprocess.run(
            ["uv", "build", "--sdist", "--wheel", "--project", str(stage / "python"), "--out-dir", str(output)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        artifacts = sorted(output.iterdir())
        outputs.append(artifacts)
        hashes.append([hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts])

    assert hashes[0] == hashes[1]
    wheel = next(path for path in outputs[0] if path.suffix == ".whl")
    sdist = next(path for path in outputs[0] if path.name.endswith(".tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata_text = archive.read(metadata_name).decode()
    assert "Description-Content-Type: text/markdown" in metadata_text
    assert "Author: AgentsDance AI" in metadata_text
    assert "Project-URL: Homepage," in metadata_text
    assert "# `dsh-cloud`" in metadata_text
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(not member.uname and not member.gname for member in members)
    assert all(member.mtime == 0 for member in members)


def test_install_documentation_distinguishes_source_and_registry_commands():
    text = (ROOT / "docs/install.md").read_text(encoding="utf-8")
    assert f"@agentsdanceai/dsh-cloud@{_release_version()}" in text
    assert f"dsh-cloud=={_release_version()}" in text
    assert "npm --prefix packages/cli-npm" in text
    assert "uv run --project packages/cli-python" in text
    assert "npm pack dist/packages/npm --dry-run --json" in text
    assert "npm pack --dry-run --json --prefix" not in text
    assert "not published" not in text.lower()
    assert "after publication" not in text.lower()
    assert "http://localhost:8787" in text
    assert "http://127.0.0.1:8787" not in text


def test_documented_uv_run_source_entrypoint_is_executable(tmp_path: Path):
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "uv-env"),
    }
    result = subprocess.run(
        ["uv", "run", "--project", "packages/cli-python", "dsh-cloud", "--version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _release_version()


def test_node_and_python_refuse_reinit_without_mutating_existing_install(tmp_path: Path):
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / f"reinit-{name}"
        first = runner("init", str(target), "--yes", "--json")
        assert first.returncode == 0, first.stderr
        sentinels = {
            target / "docker-compose.yml": "sentinel compose\n",
            target / ".env": "sentinel env\n",
            target / "secrets/auth_secret": "sentinel secret\n",
            target / ".dsh-cloud/state.json": "sentinel state\n",
        }
        for path, value in sentinels.items():
            path.write_text(value, encoding="utf-8")

        second = runner("init", str(target), "--yes", "--json")

        assert second.returncode == 2
        assert "already initialized" in second.stdout + second.stderr
        for path, value in sentinels.items():
            assert path.read_text(encoding="utf-8") == value


def test_node_and_python_lifecycle_use_persisted_project_name(tmp_path: Path):
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / f"project-{name}"
        initialized = runner("init", str(target), "--project-name", f"custom-{name}", "--yes", "--json")
        assert initialized.returncode == 0, initialized.stderr
        env = {**os.environ, "DSH_CLOUD_TEST_COMMAND_JSON": json.dumps(["true"])}
        for command in ("up", "down", "status", "logs", "doctor"):
            if name == "node":
                result = subprocess.run(
                    ["node", str(NODE), command, str(target), "--json"],
                    cwd=ROOT, env=env, text=True, capture_output=True,
                )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "dsh_cloud_cli", command, str(target), "--json"],
                    cwd=ROOT, env={**env, "PYTHONPATH": str(PYTHON_SRC)}, text=True, capture_output=True,
                )
            assert result.returncode == 0, result.stderr
            value = json.loads(result.stdout)
            assert value["projectName"] == f"custom-{name}"
            project_index = value["dockerArgv"].index("--project-name")
            assert value["dockerArgv"][project_index + 1] == f"custom-{name}"


def test_staged_node_and_python_artifacts_initialize_complete_runtime_config(tmp_path: Path):
    stage = tmp_path / "stage"
    built = subprocess.run(
        ["node", "scripts/release/build-packages.mjs", str(stage)], cwd=ROOT, text=True, capture_output=True
    )
    assert built.returncode == 0, built.stderr
    runners = (
        ("node", ["node", str(stage / "npm/bin/dsh-cloud.mjs")], os.environ),
        (
            "python",
            [sys.executable, "-m", "dsh_cloud_cli"],
            {**os.environ, "PYTHONPATH": str(stage / "python/src")},
        ),
    )
    for name, prefix, environment in runners:
        target = tmp_path / f"artifact-{name}"
        result = subprocess.run(
            [*prefix, "init", str(target), "--yes", "--json"],
            cwd=tmp_path, env=environment, text=True, capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert (target / "config/i18n/en.json").is_file()
        assert (target / "config/i18n/zh.json").is_file()
        for currency in ("cny", "eur", "gbp", "hkd", "jpy", "usd"):
            path = target / f"config/pricing.{currency}.json"
            assert path.is_file(), path
            json.loads(path.read_text(encoding="utf-8"))


def test_node_and_python_restore_selfhost_url_and_exact_dry_run_from_state(tmp_path: Path):
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / f"restore-{name}"
        initialized = runner(
            "init", str(target), "--mode", "selfhost", "--domain", "cloud.example.com",
            "--admin-email", "admin@example.com", "--project-name", f"restore-{name}", "--yes", "--json",
        )
        assert initialized.returncode == 0, initialized.stderr

        dry_run = runner("down", str(target), "--dry-run", "--json")

        assert dry_run.returncode == 0, dry_run.stderr
        value = json.loads(dry_run.stdout)
        assert value["mode"] == "selfhost"
        assert value["projectName"] == f"restore-{name}"
        assert value["url"] == "https://cloud.example.com"
        assert value["publicBaseUrl"] == value["url"]
        assert value["bindAddress"] == "0.0.0.0"
        assert value["dockerArgv"][-1] == "down"


def test_node_and_python_json_mode_contains_child_output_in_one_document(tmp_path: Path):
    child_script = "process.stdout.write(JSON.stringify([{Name:'dhc-server',State:'running'}]))"
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / f"json-{name}"
        initialized = runner("init", str(target), "--yes", "--json")
        assert initialized.returncode == 0, initialized.stderr
        environment = {
            **os.environ,
            "DSH_CLOUD_TEST_COMMAND_JSON": json.dumps(["node", "-e", child_script]),
        }
        if name == "node":
            result = subprocess.run(
                ["node", str(NODE), "status", str(target), "--json"],
                cwd=ROOT, env=environment, text=True, capture_output=True,
            )
        else:
            result = subprocess.run(
                [sys.executable, "-m", "dsh_cloud_cli", "status", str(target), "--json"],
                cwd=ROOT, env={**environment, "PYTHONPATH": str(PYTHON_SRC)}, text=True, capture_output=True,
            )
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value["composeOutput"] == [{"Name": "dhc-server", "State": "running"}]


def test_node_and_python_reject_unsafe_selfhost_env_values_before_writing(tmp_path: Path):
    invalid_cases = (
        ("--domain", "https://cloud.example.com"),
        ("--domain", "cloud.example.com:8443"),
        ("--domain", "cloud.example.com\nINJECTED=yes"),
        ("--admin-email", "admin@example.com\nINJECTED=yes"),
        ("--project-name", "Bad Project"),
    )
    for name, runner in (("node", run_node), ("python", run_python)):
        for index, (option, unsafe_value) in enumerate(invalid_cases):
            target = tmp_path / f"unsafe-{name}-{index}"
            arguments = [
                "init", str(target), "--mode", "selfhost", "--domain", "cloud.example.com",
                "--admin-email", "admin@example.com", "--project-name", "safe-project", option, unsafe_value,
                "--yes", "--json",
            ]
            result = runner(*arguments)
            assert result.returncode == 2
            assert not target.exists()


def test_node_and_python_write_byte_identical_env(tmp_path: Path):
    """两个安装器宣称同一套栈契约, .env 就必须一模一样。

    2026-08-25 给两边都加了首次运行引导与搜索配置位 —— 同时改两处正是漂移最
    容易发生的时刻, 这条把它钉死: 少改一边、键序不同、值不同, 都会红。
    """
    envs = {}
    for name, runner in (("node", run_node), ("python", run_python)):
        target = tmp_path / name
        result = runner("init", str(target), "--mode", "trial", "--yes", "--json")
        assert result.returncode == 0, result.stderr
        envs[name] = (target / ".env").read_text(encoding="utf-8")
    assert envs["node"] == envs["python"]
    # 搜索是"自带 API"叙事的一部分, 配置位必须露出来而不是只活在源码里
    assert "SEARCH_PROVIDER=zhipu" in envs["node"]
    assert "ZHIPU_SEARCH_API_KEY=" in envs["node"]
