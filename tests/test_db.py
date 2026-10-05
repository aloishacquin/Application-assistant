import sqlite3
from pathlib import Path

from sqlmodel import Session

from jobapply.models.db import Application, ApplicationStatus, Document, get_engine, upgrade_schema

PHASE1_SCHEMA = """
CREATE TABLE offer (
    id INTEGER PRIMARY KEY, source VARCHAR, status VARCHAR,
    created_at DATETIME, updated_at DATETIME);
CREATE TABLE application (
    id INTEGER PRIMARY KEY, offer_id INTEGER NOT NULL, status VARCHAR NOT NULL,
    applied_at DATETIME, next_follow_up_at DATETIME, notes VARCHAR NOT NULL,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
CREATE TABLE document (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL, kind VARCHAR NOT NULL,
    path VARCHAR NOT NULL, json_payload JSON NOT NULL, model VARCHAR NOT NULL,
    prompt_version INTEGER NOT NULL, sent BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
INSERT INTO offer VALUES (1, 'manual', 'accepted', '2026-10-01', '2026-10-01');
INSERT INTO application
    VALUES (1, 1, 'PREPARING', NULL, NULL, 'keep me', '2026-10-01', '2026-10-01');
INSERT INTO document VALUES (1, 1, 'CV', 'x.pdf', '{}', 'm', 1, 0, '2026-10-01');
"""


def columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as connection:
        return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def test_old_database_is_upgraded_without_data_loss(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(PHASE1_SCHEMA)

    engine = get_engine(db)

    assert {"generating", "generation_error"} <= columns(db, "application")
    assert {"version", "edited"} <= columns(db, "document")
    assert {"raw_text", "payload", "text_hash"} <= columns(db, "offer")
    assert "analysis" in {t for t in engine.dialect.get_table_names(engine.connect())}
    with Session(engine) as session:
        application = session.get(Application, 1)
        assert application.notes == "keep me"
        assert application.status is ApplicationStatus.PREPARING
        assert application.generating is False
        assert application.generation_error is None
        document = session.get(Document, 1)
        assert document.version == 0 and document.edited is False


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    engine = get_engine(tmp_path / "app.db")
    assert upgrade_schema(engine) == []
