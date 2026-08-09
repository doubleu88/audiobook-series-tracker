import secrets
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
    if "created_at" not in book_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN created_at DATETIME"))
        conn.execute(text("UPDATE books SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))

    series_columns = {col["name"] for col in inspect(conn).get_columns("series")}
    if "consecutive_failures" not in series_columns:
        conn.execute(text("ALTER TABLE series ADD COLUMN consecutive_failures INTEGER DEFAULT 0"))
    if "last_failure_at" not in series_columns:
        conn.execute(text("ALTER TABLE series ADD COLUMN last_failure_at DATETIME"))
    if "last_failure_reason" not in series_columns:
        conn.execute(text("ALTER TABLE series ADD COLUMN last_failure_reason VARCHAR"))

    if "subscriptions" in inspect(conn).get_table_names():
        sub_columns = {col["name"] for col in inspect(conn).get_columns("subscriptions")}
        if "muted" not in sub_columns:
            conn.execute(text("ALTER TABLE subscriptions ADD COLUMN muted BOOLEAN DEFAULT 0"))

    if "users" in inspect(conn).get_table_names():
        user_columns = {col["name"] for col in inspect(conn).get_columns("users")}
        if "is_admin" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0"))
        if "last_login" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN last_login DATETIME"))
        if "digest_enabled" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN digest_enabled BOOLEAN DEFAULT 0"))
        if "calendar_token" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN calendar_token VARCHAR"))
            for row in conn.execute(text("SELECT id FROM users WHERE calendar_token IS NULL")).all():
                conn.execute(
                    text("UPDATE users SET calendar_token = :token WHERE id = :id"),
                    {"token": secrets.token_urlsafe(24), "id": row.id},
                )
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_calendar_token ON users (calendar_token)"))
        if "abs_base_url" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN abs_base_url VARCHAR"))
        if "abs_api_key" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN abs_api_key VARCHAR"))
        if "abs_library_id" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN abs_library_id VARCHAR"))
        if "prowlarr_base_url" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN prowlarr_base_url VARCHAR"))
        if "prowlarr_api_key" not in user_columns:
            conn.execute(text("ALTER TABLE users ADD COLUMN prowlarr_api_key VARCHAR"))

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
