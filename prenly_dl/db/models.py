"""SQLAlchemy-modeller för prenly-dl.

Databasen är sanningen: sajter med sina inloggningsuppgifter, globala titlar,
sajtbunden åtkomst, bevakningar, kända utgåvor och jobbhistorik. Uppgifterna
ligger i klartext, så databasfilen ska ligga på ett ställe bara du kommer åt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class AuthStatus(str, Enum):
    """Resultatet av senaste anropet med sajtens uppgifter."""

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
    """En tidningssajt och dess inloggningsuppgifter."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    textalk_auth: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    cdn: Mapped[str] = mapped_column(String(255), default="")
    auth_status: Mapped[str] = mapped_column(String(32), default=AuthStatus.UNKNOWN.value)
    auth_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    title_accesses: Mapped[list["TitleAccess"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Title(Base):
    """En global Prenly-titel med en gemensam katalog."""

    __tablename__ = "titles"

    title_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    catalogue_offset: Mapped[int] = mapped_column(Integer, default=0)
    catalogue_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    accesses: Mapped[list["TitleAccess"]] = relationship(
        back_populates="title", cascade="all, delete-orphan", lazy="joined"
    )
    publication: Mapped["Publication | None"] = relationship(
        back_populates="title", cascade="all, delete-orphan", uselist=False
    )
    issues: Mapped[list["Issue"]] = relationship(
        back_populates="title", cascade="all, delete-orphan"
    )


class TitleAccess(Base):
    """En sajts åtkomst och läsrätt till en global titel."""

    __tablename__ = "title_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.title_id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    readable: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationerna används efter att webbrutternas session har stängts.
    title: Mapped[Title] = relationship(back_populates="accesses", lazy="joined")
    site: Mapped[Site] = relationship(back_populates="title_accesses", lazy="joined")

    __table_args__ = (UniqueConstraint("title_id", "site_id", name="uq_title_site"),)


class Publication(Base):
    """Inställningar för en titel som användaren har valt att bevaka."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.title_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    watch_limit: Mapped[int] = mapped_column(Integer, default=1)
    prefix: Mapped[str] = mapped_column(String(255), default="")
    folder: Mapped[str] = mapped_column(String(255), default="")
    # Titelmönstret i Calibre. Tomt betyder standarden i calibre-modulen.
    calibre_title: Mapped[str] = mapped_column(String(255), default="")
    main_publication_id: Mapped[int | None] = mapped_column(
        ForeignKey("publications.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Titeluppgifterna behövs varje gång bevakningen används.
    title: Mapped[Title] = relationship(back_populates="publication", lazy="joined")
    # Huvudpublikationen behövs efter att webbrutternas session har stängts.
    main_publication: Mapped["Publication | None"] = relationship(
        remote_side="Publication.id",
        back_populates="attachments",
        foreign_keys=[main_publication_id],
        lazy="joined",
        join_depth=1,
    )
    attachments: Mapped[list["Publication"]] = relationship(
        back_populates="main_publication", foreign_keys=[main_publication_id]
    )

    @property
    def name(self) -> str:
        return self.title.name

    @name.setter
    def name(self, value: str) -> None:
        self.title.name = value

    @property
    def slug(self) -> str:
        return self.title.slug

    @property
    def catalogue_offset(self) -> int:
        return self.title.catalogue_offset

    @catalogue_offset.setter
    def catalogue_offset(self, value: int) -> None:
        self.title.catalogue_offset = value

    @property
    def catalogue_complete(self) -> bool:
        return self.title.catalogue_complete

    @catalogue_complete.setter
    def catalogue_complete(self, value: bool) -> None:
        self.title.catalogue_complete = value

    @property
    def cover_checksum(self) -> str | None:
        return self.title.cover_checksum

    @cover_checksum.setter
    def cover_checksum(self, value: str | None) -> None:
        self.title.cover_checksum = value

    @property
    def readable(self) -> bool:
        return any(access.readable for access in self.title.accesses)

    @property
    def accesses(self) -> list[TitleAccess]:
        return self.title.accesses


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.title_id", ondelete="CASCADE"), nullable=False
    )
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    publication_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cover_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Utgåvans tema, alltså förstasidesartikelns rubrik. Katalogen ger det
    # inte, så det fylls i vid nedladdning.
    theme: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default=IssueStatus.NEW.value)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # En fil som ligger på disk utan motsvarighet i Prenlys katalog. Den går
    # inte att hämta igen, så den räknas inte som en katalogutgåva.
    disk_only: Mapped[bool] = mapped_column(Boolean, default=False)
    calibre_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Utgåvan har granskats i matchningsvyn utan att kopplas. Utan flaggan
    # dyker samma förslag upp vid varje genomgång.
    calibre_ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    title: Mapped[Title] = relationship(back_populates="issues", lazy="joined")
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("title_id", "uid", name="uq_title_issue"),)


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
