import base64
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from app.db import DATA_DIR
from app.models import PushSubscription

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY_PATH = DATA_DIR / ".vapid_private_key.pem"

# VAPID requires a contact identifier (a mailto: link, or a bare https
# origin) for push services to reach out to if there's a problem. This is a
# generic placeholder rather than a real address, since the source is public
# and self-hosters shouldn't need to edit code to change it.
VAPID_CLAIMS_SUB = "mailto:admin@localhost"


def _ensure_vapid_key() -> Path:
    if not VAPID_PRIVATE_KEY_PATH.exists():
        vapid = Vapid02()
        vapid.generate_keys()
        vapid.save_key(str(VAPID_PRIVATE_KEY_PATH))
    return VAPID_PRIVATE_KEY_PATH


def get_vapid_public_key_b64() -> str:
    path = _ensure_vapid_key()
    with open(path, "rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)
    raw = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def send_push(
    subscription: PushSubscription,
    title: str,
    body: str,
    url: str = "/",
    icon: str | None = None,
) -> bool:
    """Sends a push notification. Returns False if the subscription is dead and should be removed."""
    payload = {"title": title, "body": body, "url": url}
    if icon:
        payload["icon"] = icon
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=str(_ensure_vapid_key()),
            vapid_claims={"sub": VAPID_CLAIMS_SUB},
        )
        return True
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            logger.debug("Push subscription %s is dead (HTTP %s), removing it", subscription.endpoint, status)
            return False
        logger.warning("Push to %s failed: %s", subscription.endpoint, exc)
        return True
