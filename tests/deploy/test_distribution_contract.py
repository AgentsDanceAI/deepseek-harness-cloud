import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_legacy_compose_builds_from_repository_root():
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"build:\s*\n\s+context: \.\.\s*\n\s+dockerfile: server/Dockerfile", compose)


def test_canonical_selfhost_default_pricing_file_exists():
    env_text = (ROOT / "deploy/selfhost/.env.example").read_text(encoding="utf-8")
    match = re.search(r"^PRICING_FILE=([^\s#]+)$", env_text, re.MULTILINE)
    assert match, "PRICING_FILE must have a checked-in default"
    assert (ROOT / "server/config" / match.group(1)).is_file()


def test_runtime_image_uses_only_operator_neutral_legal_assets():
    dockerfile = (ROOT / "server/Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=0:0 server/legal/ /srv/legal/" in dockerfile
    assert not re.search(r"^COPY\s+(?:\./)?legal(?:/|\s)", dockerfile, re.MULTILINE)
    assert (ROOT / "server/legal/README.md").is_file()


def test_hosted_production_mounts_its_legal_documents_explicitly():
    production = (ROOT / "deploy/prod/compose.yml").read_text(encoding="utf-8")
    selfhost = (ROOT / "deploy/selfhost/docker-compose.yml").read_text(encoding="utf-8")

    assert "DHC_LEGAL_DIR: /srv/operator-legal" in production
    assert "../../legal:/srv/operator-legal:ro" in production
    assert "../../legal:/srv/operator-legal:ro" not in selfhost


def test_hosted_safe_deploy_stamps_the_server_image_with_the_clean_git_head():
    compose = (ROOT / "deploy/prod/compose.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/safe_deploy.sh").read_text(encoding="utf-8")

    assert "REVISION: ${REVISION:-unknown}" in compose
    dirty_gate = script.index('if [ -n "$dirty" ]')
    resolve_head = script.index('revision="$(git rev-parse --verify HEAD^{commit})"')
    deploy = script.index('REVISION="$revision" docker compose')
    assert dirty_gate < resolve_head < deploy


def test_selfhost_compose_defaults_are_safe_and_image_first():
    compose = (ROOT / "deploy/selfhost/docker-compose.yml").read_text(encoding="utf-8")
    assert "image: ${DHC_SERVER_IMAGE" in compose
    assert "${BIND_ADDRESS:-127.0.0.1}:${HTTP_PORT:-8787}:80" in compose
    assert "context: ../.." not in compose.split("image:", 1)[0]


def test_compose_healthchecks_use_readiness_not_liveness():
    for relative in ("deploy/docker-compose.yml", "deploy/selfhost/docker-compose.yml"):
        compose = (ROOT / relative).read_text(encoding="utf-8")
        assert "http://127.0.0.1:8100/readyz" in compose
        healthcheck = compose.split("healthcheck:", 1)[1].split("interval:", 1)[0]
        assert "/api/health" not in healthcheck
        assert '["CMD", "/usr/local/bin/python3", "-I", "-c"' in healthcheck


def test_source_quickstart_is_loopback_safe_and_waits_for_readiness():
    script = (ROOT / "scripts/quickstart.sh").read_text(encoding="utf-8")
    assert 'compose.build.yml' in script
    assert 'set_kv "$ENV_FILE" BIND_ADDRESS "127.0.0.1"' in script
    assert 'set_kv "$ENV_FILE" HTTP_PORT "$LOCAL_HTTP_PORT"' in script
    assert 'PUBLIC_BASE "http://localhost:$LOCAL_HTTP_PORT"' in script
    assert "http://127.0.0.1:8100/readyz" in script
    assert "http://127.0.0.1:8100/api/health" not in script
    assert "public mode requires SMTP or Google/GitHub OAuth" in script
    assert 'get_kv "$ENV_FILE" MAIL_SMTP_HOST' in script


def test_source_quickstart_preserves_an_existing_postgres_overlay():
    script = (ROOT / "scripts/quickstart.sh").read_text(encoding="utf-8")
    assert "com.docker.compose.service=postgres" in script
    assert 'STACK_FILES+=("-f" "$STACK_DIR/compose.postgres.yml")' in script
    assert "--remove-orphans" not in script
    assert "10#$LOCAL_HTTP_PORT > 65535" in script
    assert 'COMPOSE_HINT="$(compose_hint)"' in script
    assert 'echo "  Logs           $COMPOSE_HINT logs -f dhc-server"' in script


def test_no_distribution_runtime_version_drift():
    release = json.loads((ROOT / "release/release.json").read_text(encoding="utf-8"))
    assert release["harnessRuntime"] == "0.1.0-rc.8"
    assert release["desktopRuntime"] == "0.1.0-rc.6"
    for directory in (ROOT / "deploy", ROOT / "packages"):
        for path in directory.rglob("*"):
            if path.is_file() and "prod" not in path.parts and path.suffix != ".swp" and path.name != ".env":
                assert not re.search(r"rc(?:\.|)6\b", path.read_text(encoding="utf-8", errors="ignore")), path
                assert not re.search(r"(?:0\.1\.0-)?rc(?:\.|)8\b", path.read_text(encoding="utf-8", errors="ignore")), path


def test_selfhost_docs_use_the_verified_account_creation_flow():
    readme = (ROOT / "deploy/selfhost/README.md").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy/selfhost/.env.example").read_text(encoding="utf-8")
    assert "/api/auth/register" not in readme
    assert "/api/auth/email/send" in readme
    assert "/api/auth/email/login" in readme
    assert "needs SMTP or OAuth" in env_example
    assert "# -> http://localhost:8787" in readme

    deployment = (ROOT / "docs/deploy.md").read_text(encoding="utf-8")
    assert "MAIL_SMTP_HOST" in deployment
    assert "new accounts require verified email" in deployment


def test_readme_single_container_does_not_reuse_compose_only_environment():
    for name in ("README.md", "README.zh-CN.md"):
        readme = (ROOT / name).read_text(encoding="utf-8")
        section = readme.split("docker run --rm --name dsh-cloud", 1)[1].split("```", 1)[0]
        assert "--env-file .dsh-cloud/docker.env" in section
        assert "deploy/selfhost/.env" not in section

    deployment = (ROOT / "docs/deploy.md").read_text(encoding="utf-8")
    docker_section = deployment.split("## Docker single-container path", 1)[1].split(
        "## Configuration boundaries", 1
    )[0]
    assert "--env-file deploy/selfhost/.env" not in docker_section
    assert docker_section.count("--env-file .dsh-cloud/docker.env") == 2
    assert "@sha256:<digest>" in docker_section
