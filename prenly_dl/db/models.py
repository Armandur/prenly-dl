"""SQLAlchemy-modeller för prenly-dl.

Databasen är sanningen: publikationer med sina inloggningsuppgifter,
kända utgåvor och jobbhistorik. Uppgifterna ligger i klartext, så
databasfilen ska ligga på ett ställe bara du kommer åt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class AuthStatus(str, Enum):
    """Resultatet av senaste anropet med publikationens uppgifter."""

    UNKNOWN = "unknown"
    OK = "ok"
    AUTH_ERROR = "auth_error"
    SUBSCRIPTION_ERROR = "subscription_error"
    ERROR = "error"


class IssueStatus(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class JobType(str, Enum):
    POLL = "poll"
    DOWNLOAD = "download"
    CATALOGUE = "catalogue"
    SITE_SYNC = "site_sync"


class Site(Base):
    """En tidningssajt och dess inloggningsuppgifter.

    Uppgifterna hör till sajten, inte till en enskild publikation: ett
    förnyat token gäller omedelbart för alla titlar sajten exponerar.
    """

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")

    textalk_auth: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    cdn: Mapped[str] = mapped_column(String(255), default="")

    # Gäller uppgifterna? Om en enskild titel går att läsa är en annan
    # fråga, och bor på publikationen.
    auth_status: Mapped[str] = mapped_column(String(32), default=AuthStatus.UNKNOWN.value)
    auth_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    publications: Mapped[list["Publication"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    title_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Kan kontot läsa titeln? Sätts från sajtens titellista.
    readable: Mapped[bool] = mapped_column(Boolean, default=True)
    # Aktiverad = ingår i den schemalagda katalogkontrollen och nya utgåvor
    # köas för nedladdning. Inaktiva titlar går ändå att bläddra i och hämta
    # enstaka utgåvor ur.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    prefix: Mapped[str] = mapped_column(String(255), default="")
    folder: Mapped[str] = mapped_column(String(255), default="")
    watch_limit: Mapped[int] = mapped_column(Integer, default=1)

    # Hur många katalogposter som hämtats, och om katalogen tagit slut.
    # Äldre utgåvor ändras inte, så en hämtad sida behöver aldrig hämtas om.
    catalogue_offset: Mapped[int] = mapped_column(Integer, default=0)
    catalogue_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Uppgifterna behövs varje gång publikationen används, och en lazy
    # relation dör med sessionen som hämtade raden.
    site: Mapped[Site] = relationship(back_populates="publications", lazy="joined")
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("site_id", "title_id", name="uq_site_title"),)


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=False
    )
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    publication_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cover_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=IssueStatus.NEW.value)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    calibre_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    publication: Mapped[Publication] = relationship(back_populates="issues")
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("publication_id", "uid", name="uq_pub_issue"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value)

    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=True
    )
    publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=True
    )
    issue_id: Mapped[int | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), nullable=True
    )

    force: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    issue: Mapped[Issue | None] = relationship(back_populates="jobs")


class Setting(Base):
    """Nyckel/värde för inställningar som ändras i webbui:t."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
