from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "audiobooks.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _migrate(conn) -> None:
    existing_columns = {col["name"] for col in inspect(conn).get_columns("books")}
    if "cover_image" not in existing_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN cover_image VARCHAR"))


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)


def get_session() -> Session:
    return SessionLocal()
