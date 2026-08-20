import logging
import os

DEFAULT_LEVEL_NAME = "INFO"
_VALID_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_requested = os.environ.get("LOG_LEVEL", DEFAULT_LEVEL_NAME).upper()
_base_level_name = _requested if _requested in _VALID_LEVEL_NAMES else DEFAULT_LEVEL_NAME
BASE_LEVEL = getattr(logging, _base_level_name)


def configure_logging() -> None:
    """Sets up a root handler with timestamps so every module's logger.*()
    call is actually visible in `docker compose logs`, and not just the
    unformatted, WARNING-and-up-only output Python falls back to when no
    handler is configured at all.

    Level comes from the LOG_LEVEL env var (DEBUG/INFO/WARNING/ERROR/
    CRITICAL), defaulting to INFO — see also set_debug_logging() for a
    runtime toggle that doesn't require restarting the container.
    """
    logging.basicConfig(
        level=BASE_LEVEL,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if _requested != _base_level_name:
        logging.getLogger(__name__).warning(
            "Invalid LOG_LEVEL %r, falling back to %s", _requested, DEFAULT_LEVEL_NAME
        )


def is_debug_enabled() -> bool:
    return logging.getLogger().getEffectiveLevel() <= logging.DEBUG


def set_debug_logging(enabled: bool) -> None:
    """Runtime toggle (e.g. from /admin/health) for verbose logging without
    restarting the container. Falls back to the LOG_LEVEL-configured base
    level when turned off, rather than a hardcoded default."""
    logging.getLogger().setLevel(logging.DEBUG if enabled else BASE_LEVEL)
