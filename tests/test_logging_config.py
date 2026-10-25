import importlib
import logging

import pytest

from app import logging_config as lc


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    importlib.reload(lc)


def reload_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOG_LEVEL", value)
    return importlib.reload(lc)


def test_default_level(monkeypatch):
    m = reload_with(monkeypatch, None)
    assert m.BASE_LEVEL == logging.INFO


def test_env_level_case_insensitive(monkeypatch):
    m = reload_with(monkeypatch, "debug")
    assert m.BASE_LEVEL == logging.DEBUG


def test_invalid_level_falls_back(monkeypatch):
    m = reload_with(monkeypatch, "loud")
    assert m.BASE_LEVEL == logging.INFO


def test_configure_logging_sets_up_root(monkeypatch):
    m = reload_with(monkeypatch, "WARNING")
    root = logging.getLogger()
    root.handlers[:] = []
    m.configure_logging()
    assert root.level == logging.WARNING
    assert len(root.handlers) == 1
    fmt = root.handlers[0].formatter._fmt
    assert "%(asctime)s" in fmt and "%(name)s" in fmt


def test_configure_logging_warns_on_invalid(monkeypatch, capsys):
    m = reload_with(monkeypatch, "loud")
    logging.getLogger().handlers[:] = []
    m.configure_logging()
    assert "Invalid LOG_LEVEL 'LOUD', falling back to INFO" in capsys.readouterr().err


def test_configure_logging_no_warning_when_valid(monkeypatch, capsys):
    m = reload_with(monkeypatch, "INFO")
    logging.getLogger().handlers[:] = []
    m.configure_logging()
    assert "Invalid LOG_LEVEL" not in capsys.readouterr().err


def test_is_debug_enabled():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    assert lc.is_debug_enabled() is True
    root.setLevel(logging.INFO)
    assert lc.is_debug_enabled() is False


def test_set_debug_logging_toggle(monkeypatch):
    m = reload_with(monkeypatch, "ERROR")
    m.set_debug_logging(True)
    assert logging.getLogger().level == logging.DEBUG
    assert m.is_debug_enabled()
    m.set_debug_logging(False)
    assert logging.getLogger().level == logging.ERROR
    assert not m.is_debug_enabled()
