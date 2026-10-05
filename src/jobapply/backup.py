"""Backups of data/: a consistent SQLite snapshot plus the profile, the LLM log and the
generated documents, in one .tar.gz (SPEC section 12)."""

from __future__ import annotations

import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from jobapply.config import Paths

PREFIX = "jobapply-"


def backups_dir(paths: Paths) -> Path:
    return paths.data_dir / "backups"


def _snapshot_db(source: Path, target: Path) -> None:
    """sqlite3 online backup: consistent even while the platform is writing."""
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def create_backup(paths: Paths, now: datetime, keep: int = 14) -> Path:
    folder = backups_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{PREFIX}{now:%Y%m%d-%H%M%S}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(archive, "w:gz") as tar:
        if paths.db.is_file():
            snapshot = Path(tmp) / "app.db"
            _snapshot_db(paths.db, snapshot)
            tar.add(snapshot, arcname="app.db")
        for item in (paths.profile, paths.llm_log, paths.output_dir):
            if item.exists():
                tar.add(item, arcname=item.relative_to(paths.data_dir).as_posix())
    prune_backups(paths, keep)
    return archive


def prune_backups(paths: Paths, keep: int) -> list[Path]:
    """Keep the `keep` most recent archives; returns the removed ones."""
    archives = sorted(backups_dir(paths).glob(f"{PREFIX}*.tar.gz"))
    removed = archives[:-keep] if keep > 0 else archives
    for archive in removed:
        archive.unlink()
    return removed
