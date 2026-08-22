"""Tiny PEP 517 backend for the dependency-free installer package.

Keeping the backend in-tree makes `uv build` work offline. It intentionally
supports only the pure-Python wheel and sdist shapes this package needs.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _dist_info() -> tuple[str, str, str]:
    project = _project()
    version = project["version"]
    normalized = project["name"].replace("-", "_")
    return normalized, version, f"{normalized}-{version}.dist-info"


def _metadata() -> bytes:
    project = _project()
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {project['name']}\n"
        f"Version: {project['version']}\n"
        f"Summary: {project['description']}\n"
        f"Requires-Python: {project['requires-python']}\n"
        "License-Expression: AGPL-3.0-only\n"
        "License-File: LICENSE\n\n"
    ).encode()


def _license_bytes() -> bytes:
    """Read the license from either a staged package or the repository root."""
    candidates = (ROOT / "LICENSE", ROOT.parents[1] / "LICENSE")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError("LICENSE is required to build dsh-cloud")


def _wheel_files() -> dict[str, bytes]:
    _normalized, _version, dist_info = _dist_info()
    files: dict[str, bytes] = {}
    package = ROOT / "src/dsh_cloud_cli"
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[path.relative_to(ROOT / "src").as_posix()] = path.read_bytes()
    files[f"{dist_info}/METADATA"] = _metadata()
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: dsh-cloud-build-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\ndsh-cloud = dsh_cloud_cli.__main__:main\n"
    files[f"{dist_info}/licenses/LICENSE"] = _license_bytes()
    return files


def _record(files: dict[str, bytes], record_path: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, value in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(value)))
    writer.writerow((record_path, "", ""))
    return output.getvalue().encode()


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_editable(config_settings=None) -> list[str]:
    return []


def _write_wheel(wheel_directory, files: dict[str, bytes]) -> str:
    normalized, version, dist_info = _dist_info()
    filename = f"{normalized}-{version}-py3-none-any.whl"
    destination = Path(wheel_directory)
    destination.mkdir(parents=True, exist_ok=True)
    record_path = f"{dist_info}/RECORD"
    files[record_path] = _record(files, record_path)
    with zipfile.ZipFile(destination / filename, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            archive.writestr(name, value)
    return filename


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, _wheel_files())


def build_editable(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    """Build the ephemeral PEP 660 wheel used by `uv run --project`.

    The `.pth` points Python at this checkout's `src/`; dist metadata and the
    console-script entry point remain ordinary wheel records.
    """
    normalized, _version, dist_info = _dist_info()
    files = {
        f"{dist_info}/METADATA": _metadata(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: dsh-cloud-build-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": b"[console_scripts]\ndsh-cloud = dsh_cloud_cli.__main__:main\n",
        f"{dist_info}/licenses/LICENSE": _license_bytes(),
        f"_{normalized}_editable.pth": f"{(ROOT / 'src').resolve()}\n".encode(),
    }
    return _write_wheel(wheel_directory, files)


def _prepare_metadata(metadata_directory) -> str:
    _normalized, _version, dist_info = _dist_info()
    destination = Path(metadata_directory) / dist_info
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "METADATA").write_bytes(_metadata())
    (destination / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: dsh-cloud-build-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    (destination / "entry_points.txt").write_text(
        "[console_scripts]\ndsh-cloud = dsh_cloud_cli.__main__:main\n",
        encoding="utf-8",
    )
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    (licenses / "LICENSE").write_bytes(_license_bytes())
    return dist_info


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    return _prepare_metadata(metadata_directory)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None) -> str:
    return _prepare_metadata(metadata_directory)


def build_sdist(sdist_directory, config_settings=None) -> str:
    normalized, version, _dist_info_name = _dist_info()
    filename = f"{normalized}-{version}.tar.gz"
    prefix = f"{normalized}-{version}"
    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    included = [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "build_backend.py"]
    included.extend(path for path in sorted((ROOT / "src").rglob("*")) if path.is_file() and "__pycache__" not in path.parts)
    with tarfile.open(destination / filename, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in included:
            archive.add(path, arcname=f"{prefix}/{path.relative_to(ROOT).as_posix()}", recursive=False)
        license_text = _license_bytes()
        license_info = tarfile.TarInfo(f"{prefix}/LICENSE")
        license_info.size = len(license_text)
        archive.addfile(license_info, io.BytesIO(license_text))
        metadata = _metadata()
        info = tarfile.TarInfo(f"{prefix}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    return filename
