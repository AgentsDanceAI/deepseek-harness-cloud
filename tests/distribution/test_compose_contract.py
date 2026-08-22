import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def compose_config(*files: str):
    argv = ["docker", "compose", "--env-file", "tests/distribution/compose.env"]
    for file in files:
        argv += ["-f", file]
    argv += ["config", "--format", "json"]
    return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)


def test_image_first_base_and_source_build_overlay_resolve():
    base = compose_config("deploy/selfhost/docker-compose.yml")
    assert base.returncode == 0, base.stderr
    value = json.loads(base.stdout)
    assert value["name"] == "dsh-selfhost"
    assert "build" not in value["services"]["dhc-server"]
    assert value["services"]["dhc-server"]["image"].endswith(":0.2.0")
    assert value["services"]["dhc-server"]["environment"]["PUBLIC_BASE"] == "http://localhost:8787"
    assert value["services"]["dhc-caddy"]["environment"]["DSH_SITE"] == "http://localhost"
    assert any(port["published"] == "8787" and port["target"] == 80 for port in value["services"]["dhc-caddy"]["ports"])
    compose_text = (ROOT / "deploy/selfhost/docker-compose.yml").read_text(encoding="utf-8")
    assert "image: ${SOCKET_PROXY_IMAGE:-tecnativa/docker-socket-proxy@sha256:" in compose_text

    build = compose_config("deploy/selfhost/docker-compose.yml", "deploy/selfhost/compose.build.yml")
    assert build.returncode == 0, build.stderr
    built = json.loads(build.stdout)["services"]["dhc-server"]["build"]
    assert Path(built["context"]) == ROOT
    # Compose resolves the context to an absolute path but keeps Dockerfile
    # relative to that context; this is the portable canonical representation.
    assert built["dockerfile"] == "server/Dockerfile"


def test_postgres_overlay_resolves_with_locked_image():
    result = compose_config("deploy/selfhost/docker-compose.yml", "deploy/selfhost/compose.postgres.yml")
    assert result.returncode == 0, result.stderr
    postgres = json.loads(result.stdout)["services"]["postgres"]
    assert "@sha256:" in postgres["image"]
    assert any(volume["target"] == "/var/lib/postgresql" and volume["source"] == "dhc-pgdata" for volume in postgres["volumes"])
    assert not any(volume["target"] == "/var/lib/postgresql/data" for volume in postgres["volumes"])
