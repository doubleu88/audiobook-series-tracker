import pytest
from fastapi import HTTPException

from app import auth


class Req:
    def __init__(self, **session):
        self.session = session


def test_hash_and_verify_password():
    h = auth.hash_password("s3cret")
    assert h != "s3cret" and h.startswith("$2")
    assert auth.verify_password("s3cret", h) is True
    assert auth.verify_password("wrong", h) is False


def test_hash_is_salted():
    assert auth.hash_password("same") != auth.hash_password("same")


def test_session_secret_created_and_reused(tmp_path, monkeypatch):
    path = tmp_path / ".session_secret"
    monkeypatch.setattr(auth, "SECRET_PATH", path)
    first = auth.get_or_create_session_secret()
    assert len(first) == 64 and path.read_text() == first
    assert auth.get_or_create_session_secret() == first


def test_session_secret_strips_whitespace(tmp_path, monkeypatch):
    path = tmp_path / ".session_secret"
    path.write_text("abc123\n")
    monkeypatch.setattr(auth, "SECRET_PATH", path)
    assert auth.get_or_create_session_secret() == "abc123"


def test_get_current_user(make_user):
    u = make_user()
    assert auth.get_current_user(Req(user_id=u.id)).id == u.id


@pytest.mark.parametrize("sess", [{}, {"user_id": 99999}, {"user_id": None}])
def test_get_current_user_redirects_to_login(sess):
    with pytest.raises(HTTPException) as exc:
        auth.get_current_user(Req(**sess))
    assert exc.value.status_code == 303
    assert exc.value.headers["Location"] == "/login"


def test_require_admin_allows_admin(make_user):
    u = make_user("root", is_admin=True)
    assert auth.require_admin(Req(user_id=u.id)).id == u.id


def test_require_admin_hides_from_non_admin(make_user):
    u = make_user()
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(Req(user_id=u.id))
    assert exc.value.status_code == 404


def test_require_admin_anonymous_redirects():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(Req())
    assert exc.value.status_code == 303


def test_get_optional_user(make_user):
    u = make_user()
    assert auth.get_optional_user(Req(user_id=u.id)).id == u.id


def test_get_optional_user_none_cases():
    assert auth.get_optional_user(Req()) is None
    assert auth.get_optional_user(Req(user_id=99999)) is None
