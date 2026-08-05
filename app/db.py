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
    book_columns = {col["name"] for col in inspect(conn).get_columns("books")}
    if "cover_image" not in book_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN cover_image VARCHAR"))
    if "release_day_notified" not in book_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN release_day_notified BOOLEAN DEFAULT 0"))

    if "users" in inspect(conn).get_table_names():
        user_columns = {col["name"] for col in inspect(conn).get_columns("users")}
        if "is_admin" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0"))

        has_admin = conn.execute(text("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1")).first()
        if has_admin is None:
            earliest_user_id = conn.execute(
                text("SELECT id FROM users ORDER BY created_at, id LIMIT 1")
            ).scalar()
            if earliest_user_id is not None:
                conn.execute(
                    text("UPDATE users SET is_admin = 1 WHERE id = :id"),
                    {"id": earliest_user_id},
                )


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)


def get_session() -> Session:
    return SessionLocal()
