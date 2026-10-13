from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATABASE_URL

_engine_kwargs: dict = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_engine(DATABASE_URL, **_engine_kwargs)


def configure_sqlite(target):
    if target.dialect.name == "sqlite":
        @event.listens_for(target, "connect")
        def pragmas(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")


configure_sqlite(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


from fastapi import Depends
from .security import Principal, principal


def get_db(identity: Principal = Depends(principal)) -> Generator[Session, None, None]:
    from .projects import project_engine
    db = Session(bind=project_engine(identity.project_id), autoflush=False, expire_on_commit=False)
    db.info["principal"] = identity
    try:
        yield db
    finally:
        db.close()
