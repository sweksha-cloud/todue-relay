from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal: sessionmaker[Session] | None = (
    sessionmaker(bind=engine, expire_on_commit=False) if engine else None
)


def get_session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not set (locally: backend/.env; deployed: the environment or secret)")
    return SessionLocal()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request."""
    session = get_session()
    try:
        yield session
    finally:
        session.close()
