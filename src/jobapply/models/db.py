"""SQLModel tables: Offer, Analysis, Application, Document, Event (SPEC section 7.4).

Schema changes are applied at startup by `upgrade_schema`: new tables are created and new
columns added to existing tables, so data survives upgrades. Renaming or removing a column
is not supported automatically.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Boolean, Column, Float, Integer, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, create_engine

from jobapply.models.offer import JobOffer

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class OfferSource(StrEnum):
    MANUAL = "manual"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    RSS = "rss"
    EMAIL = "email"


class OfferStatus(StrEnum):
    NEW = "new"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    FILTERED = "filtered"
    ERROR = "error"


class ApplicationStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    GHOSTED = "ghosted"
    WITHDRAWN = "withdrawn"


class DocumentKind(StrEnum):
    CV = "cv"
    LETTER = "letter"


class Offer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: OfferSource = OfferSource.MANUAL
    external_id: str | None = Field(default=None, index=True)
    url: str | None = Field(default=None, index=True)
    raw_text: str | None = None
    text_hash: str | None = Field(default=None, index=True, unique=True)
    title: str | None = None
    company: str | None = None
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # JobOffer
    model: str | None = None
    prompt_version: int | None = None
    status: OfferStatus = Field(default=OfferStatus.NEW, index=True)
    status_message: str | None = None  # error or filter reason
    duplicate_of: int | None = Field(default=None, foreign_key="offer.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def to_job_offer(self) -> JobOffer | None:
        return JobOffer.model_validate(self.payload) if self.payload else None

    @property
    def label(self) -> str:
        if self.title and self.company:
            return f"{self.title} — {self.company}"
        return self.url or f"Offre #{self.id}"


class Analysis(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    offer_id: int = Field(foreign_key="offer.id", index=True)
    visa: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))  # VisaCheck
    match: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))  # MatchScore
    score: int
    visa_verdict: str
    stop_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    model: str | None = None
    prompt_versions: dict[str, int] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    cost_usd: float | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def recommended(self) -> bool:
        return not self.stop_reasons


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    offer_id: int = Field(foreign_key="offer.id", index=True, unique=True)
    status: ApplicationStatus = ApplicationStatus.PREPARING
    generating: bool = False  # a CV/letter generation is running in the background
    generation_error: str | None = None
    applied_at: datetime | None = None
    next_follow_up_at: datetime | None = None
    last_follow_up_at: datetime | None = None
    follow_up_count: int = 0
    interview_at: datetime | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: DocumentKind
    path: str
    json_payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    version: int
    model: str
    prompt_version: int
    edited: bool = False  # manual edit of a previous version
    sent: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    offer_id: int = Field(foreign_key="offer.id", index=True)
    application_id: int | None = Field(default=None, foreign_key="application.id")
    kind: str  # "status", "analysis", "note", "generation"...
    message: str
    created_at: datetime = Field(default_factory=utcnow)


class SourceRun(SQLModel, table=True):
    """One collection of one source (SPEC section 6)."""

    id: int | None = Field(default=None, primary_key=True)
    source_name: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    fetched: int = 0
    new: int = 0
    filtered: int = 0
    out_of_area: int = 0
    duplicates: int = 0
    error: str | None = None


def get_engine(db_path: Path) -> Engine:
    """Create the SQLite engine (shared by web threads) and the tables if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False}
    )
    upgrade_schema(engine)
    return engine


def _default_sql(column: Column[Any]) -> str:
    """SQL literal used to fill existing rows when a NOT NULL column is added."""
    default = column.default.arg if column.default is not None else None  # type: ignore[attr-defined]
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, int | float):
        return str(default)
    if isinstance(default, str):  # includes StrEnum members
        return "'" + default.replace("'", "''") + "'"
    if isinstance(column.type, Boolean | Integer | Float):
        return "0"
    return "''"


def upgrade_schema(engine: Engine) -> list[str]:
    """Create missing tables and add missing columns. Returns the columns added."""
    SQLModel.metadata.create_all(engine)
    inspector = inspect(engine)
    added = []
    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = (
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                    f"{column.type.compile(dialect=engine.dialect)}"
                )
                if not column.nullable:
                    ddl += f" NOT NULL DEFAULT {_default_sql(column)}"
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    if added:
        logger.info("Database schema upgraded, columns added: %s", ", ".join(added))
    return added
