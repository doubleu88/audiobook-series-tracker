import secrets

import bcrypt
from fastapi import HTTPException, Request

from app.db import DATA_DIR, get_session
from app.models import User

SECRET_PATH = DATA_DIR / ".session_secret"


def get_or_create_session_secret() -> str:
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text().strip()
    secret = secrets.token_hex(32)
    SECRET_PATH.write_text(secret)
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def get_current_user(request: Request) -> User:
    user_id = request.session.get("user_id")
    session = get_session()
    try:
        user = session.get(User, user_id) if user_id else None
    finally:
        session.close()

    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def get_optional_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    session = get_session()
    try:
        return session.get(User, user_id)
    finally:
        session.close()
