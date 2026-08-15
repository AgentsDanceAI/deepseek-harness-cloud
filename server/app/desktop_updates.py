"""Desktop auto-update endpoints (the 0002 patch points the app here).

Contract (verified against upstream update-checker/update-download):
  GET /api/desktop/version    -> {"version": "x.y.z"}  strict stable SemVer,
                                 body <= 4KiB, anything else is ignored client-side
  GET /api/downloads/mac      -> the DMG (redirect ok; client validates magic)
  GET /api/downloads/windows  -> the NSIS installer

Release procedure: upload installers under Caddy /releases/, then
  POST /api/admin/desktop-version {"version": "2.1.0"}
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from . import db
from .admin import require_admin

router = APIRouter(tags=["desktop-updates"])

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@router.get("/api/desktop/version")
def desktop_version():
    row = db.query_one("SELECT v FROM kv WHERE k='desktop_version'")
    version = row["v"] if row else os.environ.get("DESKTOP_VERSION", "")
    if not _SEMVER.match(version):
        raise HTTPException(404, "no_release")
    return {"version": version}


@router.post("/api/admin/desktop-version")
def set_desktop_version(body: dict, _: dict = Depends(require_admin)):
    version = str(body.get("version", "")).strip()
    if not _SEMVER.match(version):
        raise HTTPException(400, "version_must_be_stable_semver")
    with db.tx() as conn:
        conn.execute("DELETE FROM kv WHERE k='desktop_version'")
        conn.execute("INSERT INTO kv (k, v) VALUES ('desktop_version', ?)", (version,))
    return {"ok": True, "version": version}


@router.get("/api/downloads/{platform}")
def download(platform: str):
    url = {
        "mac": os.environ.get("DOWNLOAD_URL_MAC", ""),
        "windows": os.environ.get("DOWNLOAD_URL_WIN", ""),
    }.get(platform, "")
    if not url:
        raise HTTPException(404, "no_installer")
    return RedirectResponse(url, status_code=302)
