"""Explicit, authenticated downloads of the official shared tool files."""
from __future__ import annotations

from pathlib import Path
import shutil

from .engine import extract
from .project import FNV
from .store import write_json

BASE = "https://fnvdecomp.com/pack-data/"
FILES = ("fnvcheck.tar.gz", "fnvdecomp_symbols.json", "function_index.json", "compiler_tips.md")


def fetch(store, directory, *, project=None):
    target = Path(directory).expanduser().absolute()
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Choose a new or empty directory")
    with store.lock("serve"), store.lock("tools-fetch"):
        if any(lane.get("lease") for lane in store.lanes()):
            raise ValueError("Active local claims block tool replacement")
        project = project or FNV(store.secrets().get("fnv_api_key", ""))
        status = project.call("status", {})
        if any(claim.get("this_key") for claim in status["open_claims"]):
            raise ValueError("Active owner-key claims block fetching")
        # Recheck inside the lock; never overwrite a previous or partial download.
        if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Choose a new or empty directory")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = {}
        for name in FILES:
            manifest[name] = project.download_pack(BASE + name, target / name)
        staging = target / ".checker-extract"
        extract(target / "fnvcheck.tar.gz", staging)
        roots = [path.parent for path in staging.rglob("fnvcheck.py") if path.is_file()]
        if len(roots) != 1:
            raise ValueError("Checker archive has an ambiguous layout")
        shutil.move(str(roots[0]), str(target / "fnvcheck"))
        if staging.exists():
            shutil.rmtree(staging)
        suggestions = {"checker_dir": str(target / "fnvcheck"), "symbols": str(target / "fnvdecomp_symbols.json"), "function_index": str(target / "function_index.json"), "compiler_tips": str(target / "compiler_tips.md")}
        write_json(target / "download-manifest.json", manifest)
        return {"sha256": manifest, "suggested_paths": suggestions, "configuration_changed": False}
