from datetime import datetime
from threading import RLock

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Person(Base):
    __tablename__ = 'persons'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)


class PersonalBaseline(Base):
    __tablename__ = 'personal_baselines'
    person_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    anchor: Mapped[dict] = mapped_column(JSON)
    current: Mapped[dict] = mapped_column(JSON)
    threshold: Mapped[float] = mapped_column(Float, default=3.5)
    trigger_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float)


class ProfileVersion(Base):
    __tablename__ = 'profiles'
    person_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
    author: Mapped[str] = mapped_column(String(80), default='Operator')


class Stream(Base):
    __tablename__ = 'streams'
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float)


class Window(Base):
    __tablename__ = 'pose_windows'
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(64), index=True)
    quality: Mapped[float] = mapped_column(Float)
    features: Mapped[dict] = mapped_column(JSON)
    rule_hits: Mapped[list] = mapped_column(JSON)
    # Fixed-width resampled feature sequences, only retained during explicit calibration.
    calibration: Mapped[bool] = mapped_column(Boolean, default=False)
    sequence: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Event(Base):
    __tablename__ = 'events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(64), index=True)
    stream_id: Mapped[str] = mapped_column(String(200), index=True)
    profile: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(16))
    opened_at: Mapped[float] = mapped_column(Float, index=True)
    observed_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    rule_hits: Mapped[list] = mapped_column(JSON)
    floor_urgency: Mapped[int] = mapped_column(Integer)
    urgency: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='open')
    summary: Mapped[str] = mapped_column(Text)
    features: Mapped[dict] = mapped_column(JSON)
    keyframes: Mapped[list] = mapped_column(JSON)
    verdict_status: Mapped[str] = mapped_column(String(32), default='pending')
    verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    acknowledged_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    acknowledgement_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_escalation_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Notification(Base):
    __tablename__ = 'notifications'
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient: Mapped[str] = mapped_column(String(80))
    contact_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16))
    urgency: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[float] = mapped_column(Float)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class PushSubscription(Base):
    __tablename__ = 'push_subscriptions'
    id: Mapped[str] = mapped_column(String(140), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(64))
    contact_id: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ts: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(40))
    body: Mapped[dict] = mapped_column(JSON)


class Database:
    def __init__(self, url: str):
        url = str(url)
        kwargs = {'pool_pre_ping': True}
        if url.startswith('sqlite'):
            kwargs['connect_args'] = {'check_same_thread': False, 'timeout': 30}
            if ':memory:' in url:
                kwargs['poolclass'] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def sqlite_options(conn, _):
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=30000')
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        # This MVP intentionally runs one API process. PostgreSQL row locks additionally
        # serialize event acknowledgements and sends; SQLite uses this shared lock.
        self.lock = RLock()

    def create(self):
        Base.metadata.create_all(self.engine)
