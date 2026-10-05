import tarfile
from datetime import datetime
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from jobapply.backup import create_backup, prune_backups
from jobapply.cli import app
from jobapply.config import AppConfig
from jobapply.models.db import get_engine
from jobapply.tracking import service


def test_backup_contains_data(profile_config: AppConfig) -> None:
    paths = profile_config.paths
    with Session(get_engine(paths.db)) as session:
        service.create_offer(session, raw_text="an offer worth keeping")
    (paths.output_dir / "1").mkdir(parents=True)
    (paths.output_dir / "1" / "cv_v1.pdf").write_bytes(b"%PDF-fake")
    paths.llm_log.write_text("{}\n", encoding="utf-8")

    archive = create_backup(paths, datetime(2026, 10, 5, 3, 0))

    assert archive.name == "jobapply-20261005-030000.tar.gz"
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        assert {"app.db", "profile.yaml", "llm_calls.jsonl", "output/1/cv_v1.pdf"} <= names
        assert not any(n.startswith("backups") for n in names)
        header = tar.extractfile("app.db").read(16)
    assert header.startswith(b"SQLite format 3")


def test_backup_without_database(profile_config: AppConfig) -> None:
    archive = create_backup(profile_config.paths, datetime(2026, 10, 5))
    with tarfile.open(archive) as tar:
        assert tar.getnames() == ["profile.yaml"]


def test_old_backups_are_pruned(profile_config: AppConfig) -> None:
    paths = profile_config.paths
    for day in range(1, 6):
        create_backup(paths, datetime(2026, 10, day), keep=3)
    remaining = sorted(p.name for p in (paths.data_dir / "backups").iterdir())
    assert remaining == [f"jobapply-202610{d:02d}-000000.tar.gz" for d in (3, 4, 5)]
    assert len(prune_backups(paths, 0)) == 3


def test_backup_command(monkeypatch, profile_root: Path) -> None:
    monkeypatch.setenv("JOBAPPLY_ROOT", str(profile_root))
    result = CliRunner().invoke(app, ["backup", "--keep", "2"])
    assert result.exit_code == 0, result.output
    assert "Sauvegarde créée" in result.output
    assert len(list((profile_root / "data" / "backups").glob("*.tar.gz"))) == 1
