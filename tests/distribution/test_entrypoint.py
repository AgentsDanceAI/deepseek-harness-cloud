import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def entrypoint_module():
    path = ROOT / "release/docker_entrypoint.py"
    assert path.is_file(), "release/docker_entrypoint.py must provide the safe UID migration"
    spec = importlib.util.spec_from_file_location("dsh_cloud_docker_entrypoint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_old_data_tree_is_migrated_without_following_symlinks(tmp_path: Path):
    docker_entrypoint = entrypoint_module()
    data = tmp_path / "data"
    data.mkdir()
    database = data / "dhc.db"
    database.write_text("old sqlite volume", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "must-not-be-chowned"
    outside_file.write_text("host-like target", encoding="utf-8")
    link = data / "outside-link"
    link.symlink_to(outside, target_is_directory=True)

    changed: list[Path] = []
    docker_entrypoint.migrate_data_dir(data, lchown=lambda path, _uid, _gid: changed.append(Path(path)))

    assert data in changed
    assert database in changed
    assert link in changed
    assert outside not in changed
    assert outside_file not in changed


def test_data_root_symlink_is_rejected(tmp_path: Path):
    docker_entrypoint = entrypoint_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "data"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        docker_entrypoint.migrate_data_dir(link, lchown=lambda *_args: None)


def test_failed_data_migration_leaves_root_marker_for_retry(tmp_path: Path):
    docker_entrypoint = entrypoint_module()
    data = tmp_path / "data"
    nested = data / "nested"
    nested.mkdir(parents=True)
    first_file = data / "first.db"
    remaining_file = nested / "remaining.db"
    first_file.write_text("first", encoding="utf-8")
    remaining_file.write_text("remaining", encoding="utf-8")
    first_attempt: list[Path] = []

    def fail_partway(path, _uid, _gid):
        candidate = Path(path)
        first_attempt.append(candidate)
        if candidate == remaining_file:
            raise OSError("simulated interruption")

    with pytest.raises(OSError, match="simulated interruption"):
        docker_entrypoint.migrate_data_dir(data, lchown=fail_partway)

    assert data not in first_attempt, "volume root is the completion marker and must be changed last"
    retry: list[Path] = []
    docker_entrypoint.migrate_data_dir(data, lchown=lambda path, _uid, _gid: retry.append(Path(path)))
    assert remaining_file in retry
    assert retry[-1] == data


def test_root_entrypoint_reads_mode_0600_bind_secret_before_uid_drop(tmp_path: Path):
    docker_entrypoint = entrypoint_module()
    secret_file = tmp_path / "auth_secret"
    secret_file.write_text("test-secret-value\n", encoding="utf-8")
    secret_file.chmod(0o600)
    environment = {"AUTH_SECRET_FILE": str(secret_file)}

    docker_entrypoint.prepare_auth_secret(environment)

    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert environment == {"AUTH_SECRET": "test-secret-value"}


def test_root_entrypoint_refuses_symlinked_auth_secret(tmp_path: Path):
    docker_entrypoint = entrypoint_module()
    real_secret = tmp_path / "real-secret"
    real_secret.write_text("must-not-follow\n", encoding="utf-8")
    link = tmp_path / "auth_secret"
    link.symlink_to(real_secret)

    with pytest.raises(RuntimeError, match="auth secret"):
        docker_entrypoint.prepare_auth_secret({"AUTH_SECRET_FILE": os.fspath(link)})
