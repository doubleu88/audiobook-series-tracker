import base64
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pywebpush import WebPushException

from app import push
from app.models import PushSubscription


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    p = tmp_path / "vapid.pem"
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY_PATH", p)
    return p


@pytest.fixture
def sub():
    return PushSubscription(user_id=1, endpoint="https://push.example/abc", p256dh="P", auth="A")


def test_default_key_path_in_data_dir():
    from app.db import DATA_DIR

    assert push.VAPID_PRIVATE_KEY_PATH == DATA_DIR / ".vapid_private_key.pem"


class TestEnsureVapidKey:
    def test_creates_key(self, key_path):
        assert not key_path.exists()
        assert push._ensure_vapid_key() == key_path
        assert key_path.exists()
        key = load_pem_private_key(key_path.read_bytes(), password=None)
        assert key.curve.name == "secp256r1"

    def test_reuses_existing_key(self, key_path):
        push._ensure_vapid_key()
        first = key_path.read_bytes()
        assert push._ensure_vapid_key() == key_path
        assert key_path.read_bytes() == first

    def test_does_not_regenerate(self, key_path, monkeypatch):
        key_path.write_text("existing")
        monkeypatch.setattr(push, "Vapid02", lambda: pytest.fail("should not generate"))
        assert push._ensure_vapid_key() == key_path
        assert key_path.read_text() == "existing"


class TestPublicKey:
    def test_format(self, key_path):
        b64 = push.get_vapid_public_key_b64()
        assert "=" not in b64 and "+" not in b64 and "/" not in b64
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        assert len(raw) == 65
        assert raw[0] == 4  # uncompressed point

    def test_stable_across_calls(self, key_path):
        assert push.get_vapid_public_key_b64() == push.get_vapid_public_key_b64()

    def test_creates_key_if_missing(self, key_path):
        push.get_vapid_public_key_b64()
        assert key_path.exists()

    def test_matches_private_key(self, key_path):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        b64 = push.get_vapid_public_key_b64()
        key = load_pem_private_key(key_path.read_bytes(), password=None)
        raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        assert base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)) == raw


class TestSendPush:
    def test_success(self, key_path, sub, monkeypatch):
        calls = []
        monkeypatch.setattr(push, "webpush", lambda **kw: calls.append(kw))
        assert push.send_push(sub, "Title", "Body") is True
        kw = calls[0]
        assert kw["subscription_info"] == {
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "P", "auth": "A"},
        }
        assert json.loads(kw["data"]) == {"title": "Title", "body": "Body", "url": "/"}
        assert kw["vapid_private_key"] == str(key_path)
        assert kw["vapid_claims"] == {"sub": push.VAPID_CLAIMS_SUB}
        assert key_path.exists()

    def test_custom_url_and_icon(self, key_path, sub, monkeypatch):
        calls = []
        monkeypatch.setattr(push, "webpush", lambda **kw: calls.append(kw))
        assert push.send_push(sub, "T", "B", url="/series/1", icon="/i.png") is True
        assert json.loads(calls[0]["data"]) == {
            "title": "T", "body": "B", "url": "/series/1", "icon": "/i.png"
        }

    @pytest.mark.parametrize("status", [404, 410])
    def test_expired_returns_false(self, key_path, sub, monkeypatch, status):
        def boom(**kw):
            raise WebPushException("gone", response=SimpleNamespace(status_code=status))

        monkeypatch.setattr(push, "webpush", boom)
        assert push.send_push(sub, "T", "B") is False

    @pytest.mark.parametrize("status", [400, 429, 500])
    def test_other_http_error_keeps_subscription(self, key_path, sub, monkeypatch, status, caplog):
        def boom(**kw):
            raise WebPushException("bad", response=SimpleNamespace(status_code=status))

        monkeypatch.setattr(push, "webpush", boom)
        with caplog.at_level("WARNING", logger=push.logger.name):
            assert push.send_push(sub, "T", "B") is True
        assert "Push to https://push.example/abc failed" in caplog.text

    def test_no_response_keeps_subscription(self, key_path, sub, monkeypatch, caplog):
        def boom(**kw):
            raise WebPushException("network", response=None)

        monkeypatch.setattr(push, "webpush", boom)
        with caplog.at_level("WARNING", logger=push.logger.name):
            assert push.send_push(sub, "T", "B") is True
        assert "failed" in caplog.text

    def test_unexpected_exception_propagates(self, key_path, sub, monkeypatch):
        def boom(**kw):
            raise RuntimeError("bug")

        monkeypatch.setattr(push, "webpush", boom)
        with pytest.raises(RuntimeError):
            push.send_push(sub, "T", "B")
