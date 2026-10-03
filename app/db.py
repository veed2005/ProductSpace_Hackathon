from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (registers tables on SQLModel.metadata)
from app.config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite:///"):
            settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session
