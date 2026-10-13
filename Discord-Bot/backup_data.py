"""Back up Discord-Bot/data to a verified ZIP without changing live data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BACKUPS = ROOT / "backups"
KEEP = 7


def main() -> None:
    if not DATA.is_dir():
        raise SystemExit(f"Data directory missing: {DATA}")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUPS / f"dmd-data-{stamp}.zip"
    if target.exists():
        raise SystemExit("Backup already exists; retry after one second")

    files = sorted(p for p in DATA.rglob("*") if p.is_file() and not p.is_symlink())
    if not files:
        raise SystemExit("Data directory contains no files; backup aborted")
    manifest = {"created_utc": stamp, "files": {}}
    fd, tempname = tempfile.mkstemp(prefix=".dmd-backup-", suffix=".zip", dir=BACKUPS)
    os.close(fd)
    temp = Path(tempname)
    try:
        with ZipFile(temp, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                relative = path.relative_to(DATA).as_posix()
                if relative.startswith("backups/"):
                    continue
                # Data may contain player IDs and redemption history; never publish this ZIP.
                contents = path.read_bytes()
                archive.writestr(f"data/{relative}", contents)
                manifest["files"][relative] = hashlib.sha256(contents).hexdigest()
            archive.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, indent=2))
        with ZipFile(temp) as archive:
            assert archive.testzip() is None, "ZIP checksum failure"
            for relative, digest in manifest["files"].items():
                actual = hashlib.sha256(archive.read(f"data/{relative}")).hexdigest()
                if actual != digest:
                    raise ValueError(f"Content verification failed: {relative}")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    existing = sorted(BACKUPS.glob("dmd-data-*.zip"), reverse=True)
    for old in existing[KEEP:]:
        old.unlink()
    print(f"Backup verified: {target} ({len(manifest['files'])} files)")
    print("Store a copy outside Bot-Hosting to survive hosting account or disk loss.")

if __name__ == "__main__":
    main()
