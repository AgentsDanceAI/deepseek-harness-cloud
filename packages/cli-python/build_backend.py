"""Tiny PEP 517 backend for the dependency-free installer package.

Keeping the backend in-tree makes `uv build` work offline. It intentionally
supports only the pure-Python wheel and sdist shapes this package needs.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import os
import tarfile
import time
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent
ZIP_MIN_EPOCH = 315532800  # 1980-01-01, the earliest timestamp supported by ZIP.
ZIP_MAX_EPOCH = 4354819199  # 2107-12-31 23:59:59.


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def _dist_info() -> tuple[str, str, str]:
    project = _project()
    version = project["version"]
    normalized = project["name"].replace("-", "_")
    return normalized, version, f"{normalized}-{version}.dist-info"


def _metadata() -> bytes:
    project = _project()
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
        f"Summary: {project['description']}",
        f"Requires-Python: {project['requires-python']}",
        "License-Expression: LicenseRef-DSH-Cloud-Community-1.0",
        "License-File: LICENSE",
        "Description-Content-Type: text/markdown; charset=UTF-8",
    ]
    for author in project.get("authors", []):
        if author.get("name"):
            lines.append(f"Author: {author['name']}")
    for name, url in sorted(project.get("urls", {}).items()):
        lines.append(f"Project-URL: {name}, {url}")
    for classifier in project.get("classifiers", []):
        lines.append(f"Classifier: {classifier}")
    return ("\n".join(lines) + "\n\n").encode() + (ROOT / "README.md").read_bytes()


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        epoch = int(raw)
    except ValueError as error:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from error
    if epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must not be negative")
    return epoch


def _zip_info(name: str) -> zipfile.ZipInfo:
    epoch = min(max(_source_date_epoch(), ZIP_MIN_EPOCH), ZIP_MAX_EPOCH)
    info = zipfile.ZipInfo(name, time.gmtime(epoch)[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _tar_info(name: str, value: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = _source_date_epoch()
    return info


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
    files[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: dsh-cloud-build-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    files[f"{dist_info}/entry_points.txt"] = (
        b"[console_scripts]\ndsh-cloud = dsh_cloud_cli.__main__:main\n"
    )
    files[f"{dist_info}/licenses/LICENSE"] = _license_bytes()
    return files


def _record(files: dict[str, bytes], record_path: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, value in sorted(files.items()):
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(value).digest())
            .rstrip(b"=")
            .decode()
        )
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
    with zipfile.ZipFile(
        destination / filename,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name, value in sorted(files.items()):
            archive.writestr(_zip_info(name), value)
    return filename


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, _wheel_files())


def build_editable(
    wheel_directory, config_settings=None, metadata_directory=None
) -> str:
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


def prepare_metadata_for_build_editable(
    metadata_directory, config_settings=None
) -> str:
    return _prepare_metadata(metadata_directory)


def build_sdist(sdist_directory, config_settings=None) -> str:
    normalized, version, _dist_info_name = _dist_info()
    filename = f"{normalized}-{version}.tar.gz"
    prefix = f"{normalized}-{version}"
    destination = Path(sdist_directory)
    destination.mkdir(parents=True, exist_ok=True)
    included = [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "build_backend.py"]
    included.extend(
        path
        for path in sorted((ROOT / "src").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    )
    members = {
        f"{prefix}/{path.relative_to(ROOT).as_posix()}": path.read_bytes()
        for path in included
    }
    members[f"{prefix}/LICENSE"] = _license_bytes()
    members[f"{prefix}/PKG-INFO"] = _metadata()
    with (
        (destination / filename).open("wb") as output,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=output, mtime=_source_date_epoch()
        ) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for name, value in sorted(members.items()):
            archive.addfile(_tar_info(name, value), io.BytesIO(value))
    return filename
