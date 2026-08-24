from pathlib import Path
import json
import re
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_root_dockerignore_excludes_generated_and_sensitive_trees():
    rules = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {
        ".git",
        "**/.venv",
        "**/__pycache__",
        "**/node_modules",
        "desktop/build",
        "desktop/.cache-electron",
        "**/.env",
        "**/.env.*",
        "deploy/prod",
    } <= rules


def test_postgres_extra_includes_connection_pool():
    project = tomllib.loads((ROOT / "server/pyproject.toml").read_text())
    postgres = project["project"]["optional-dependencies"]["postgres"]
    assert any(dependency.startswith("psycopg[") for dependency in postgres)
    assert any(dependency.startswith("psycopg-pool") for dependency in postgres)


def test_installer_workflow_never_copies_a_missing_literal_file():
    workflow = (ROOT / ".github/workflows/desktop-installers.yml").read_text()
    literal_sources = re.findall(r'^\s*cp\s+"?\$GITHUB_WORKSPACE/([^" ]+)', workflow, re.MULTILINE)
    assert literal_sources
    assert all((ROOT / source).is_file() for source in literal_sources)


def test_mobile_workflow_is_reproducible_and_validates_before_building():
    workflow = (ROOT / ".github/workflows/mobile-android.yml").read_text()
    assert "npm ci || npm install" not in workflow
    assert "run: npm ci" in workflow
    assert "run: npm run check" in workflow
    assert workflow.index("run: npm run check") < workflow.index("./gradlew")


def test_mobile_package_exposes_a_real_check_command():
    package = json.loads((ROOT / "mobile/package.json").read_text())
    assert package["scripts"]["check"] == "npm run typecheck && npm run verify:webview"
    assert (ROOT / "mobile/tsconfig.json").is_file()
    assert (ROOT / "mobile/scripts/verify_webview.mjs").is_file()
    assert package["scripts"]["postinstall"] == "node scripts/patch-capacitor-tar.mjs"
    assert (ROOT / "mobile/scripts/patch-capacitor-tar.mjs").is_file()
    assert package["overrides"]["tar"] == "7.5.22"


def test_makefile_exposes_non_publishing_quality_targets():
    makefile = (ROOT / "Makefile").read_text()
    for target in ("check:", "check-server:", "check-desktop:", "check-mobile:", "check-deploy:"):
        assert target in makefile
    assert "publish" not in makefile.lower()


def test_desktop_runtime_matches_the_release_manifest():
    release = json.loads((ROOT / "release/release.json").read_text())
    upstream = json.loads((ROOT / "desktop/upstream.json").read_text())
    assert upstream["runtimePackageVersion"] == release["desktopRuntime"]
    assert (ROOT / "desktop/scripts/verify-version.mjs").is_file()


def test_ci_runs_on_pull_requests_and_pushes_without_publish_permissions():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert re.search(r"(?m)^\s*pull_request:\s*$", workflow)
    assert re.search(r"(?m)^\s*push:\s*$", workflow)
    assert re.search(r"(?ms)^permissions:\s*\n\s+contents:\s*read\s*$", workflow)
    assert "packages: write" not in workflow
    assert "contents: write" not in workflow
    for job in ("server:", "desktop:", "mobile:", "deploy:"):
        assert job in workflow


def test_repository_has_editor_and_dependency_update_policy():
    editorconfig = (ROOT / ".editorconfig").read_text()
    assert "root = true" in editorconfig
    assert "end_of_line = lf" in editorconfig
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    for ecosystem in ("github-actions", "pip", "npm", "docker"):
        assert f"package-ecosystem: {ecosystem}" in dependabot


def test_manual_local_compose_docs_use_the_published_port_as_public_base():
    # 2026-08-23 起手动 Compose 步骤只住 docs/deploy.md; README 保留两行式
    # quickstart, 不再携带 PUBLIC_BASE 配置块。
    for path in ("docs/deploy.md",):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "PUBLIC_BASE=http://localhost:8787" in text, path


def test_edition_docs_and_licensing_files_share_the_public_baseline_commit():
    baseline = "945439f346a291722f8f0883e3c3c789d3c0463c"
    for path in (
        "docs/editions.md",
        "legal/licensing/LICENSE_STATE.toml",
        "legal/licensing/COMMUNITY_BASELINE.manifest",
    ):
        assert baseline in (ROOT / path).read_text(encoding="utf-8"), path


def test_github_actions_are_pinned_to_immutable_commits():
    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        text = workflow.read_text()
        for action, revision in re.findall(r"uses:\s*([^@\s]+)@([^\s#]+)", text):
            assert re.fullmatch(r"[0-9a-f]{40}", revision), f"{workflow}: {action}@{revision}"
