import os
from typing import Annotated
from fastapi import Depends
from sqlmodel import SQLModel, create_engine, Session
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
assert DATABASE_URL is not None, "DATABASE_URL is not set"


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


# Each uvicorn worker holds up to DB_POOL_SIZE + DB_MAX_OVERFLOW connections;
# keep workers x that sum under the database's connection limit (Supabase's
# direct port allows few; its pooler on port 6543 allows many more).
# pre_ping drops a connection the server or pooler closed instead of failing
# the first request after an idle spell; recycle retires them before that.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=_int("DB_POOL_SIZE", 5),
    max_overflow=_int("DB_MAX_OVERFLOW", 10),
    pool_recycle=_int("DB_POOL_RECYCLE_SECONDS", 1800),
    pool_timeout=_int("DB_POOL_TIMEOUT_SECONDS", 30),
)


def create_db_and_tables():
    # Onto tables are created by Alembic only (migrations must work on a fresh DB).
    from app.db.onto import ONTO_TABLES

    SQLModel.metadata.create_all(
        engine, tables=[t for t in SQLModel.metadata.sorted_tables if t.name not in ONTO_TABLES]
    )


def get_session():
    with Session(engine) as session:
        yield session


SessionType = Annotated[Session, Depends(get_session)]
