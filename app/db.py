import logging
import secrets
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

logger = logging.getLogger(__name__)

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
    if "updated_at" not in book_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN updated_at DATETIME"))
        # Stamp "now", not created_at. Older feeds put DTSTAMP at request time,
        # so a revision clock in the past would be older than Google's copy
        # and the next sync would be discarded.
        conn.execute(text("UPDATE books SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
    if "ics_sequence" not in book_columns:
        conn.execute(text("ALTER TABLE books ADD COLUMN ics_sequence INTEGER DEFAULT 1"))
        conn.execute(text("UPDATE books SET ics_sequence = 1 WHERE ics_sequence IS NULL OR ics_sequence < 1"))

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

    if "user_book_status" in inspect(conn).get_table_names():
        ubs_columns = {col["name"] for col in inspect(conn).get_columns("user_book_status")}
        if "acknowledged" not in ubs_columns:
            conn.execute(text("ALTER TABLE user_book_status ADD COLUMN acknowledged BOOLEAN DEFAULT 0"))
        if "acknowledged_at" not in ubs_columns:
            conn.execute(text("ALTER TABLE user_book_status ADD COLUMN acknowledged_at DATETIME"))
        if "matched_asin" not in ubs_columns:
            conn.execute(text("ALTER TABLE user_book_status ADD COLUMN matched_asin VARCHAR"))

    if "book_editions" in inspect(conn).get_table_names():
        be_columns = {col["name"] for col in inspect(conn).get_columns("book_editions")}
        if "sku" not in be_columns:
            conn.execute(text("ALTER TABLE book_editions ADD COLUMN sku VARCHAR"))
        if "format_type" not in be_columns:
            conn.execute(text("ALTER TABLE book_editions ADD COLUMN format_type VARCHAR"))
        # Seed existing books as primary editions if table is empty
        edition_count = conn.execute(text("SELECT COUNT(*) FROM book_editions")).scalar()
        if edition_count == 0:
            conn.execute(text(
                "INSERT OR IGNORE INTO book_editions (book_id, asin, title, is_primary, created_at) "
                "SELECT id, asin, title, 1, CURRENT_TIMESTAMP FROM books WHERE asin IS NOT NULL"
            ))

    if "users" in inspect(conn).get_table_names():
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
    logger.info("Initializing database at %s", DB_PATH)
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            _migrate(conn)
    except Exception:
        logger.exception("Database migration failed")
        raise
    logger.info("Database ready")


def get_session() -> Session:
    return SessionLocal()
