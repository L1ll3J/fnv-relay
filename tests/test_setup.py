"""Guided setup uses only fake services in tests."""
from unittest.mock import Mock

import pytest

from fnv_relay import setup
from fnv_relay.store import Store

PARENT = "12345678-1234-1234-1234-123456789abc"
LANDING = "22345678-1234-1234-1234-123456789abc"


def test_setup_connects_missing_accounts_verifies_page_and_creates_first_lane(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    connect = Mock(side_effect=lambda store, *args, **kwargs: store.set_secret("fnv_api_key", "fnv-secret"))
    monkeypatch.setattr(setup.authentication, "connect", connect)
    project, notion, engine = Mock(), Mock(), Mock()
    monkeypatch.setattr(setup, "FNV", Mock(return_value=project))
    monkeypatch.setattr(setup, "Notion", Mock(return_value=notion))
    engine.create_lane.return_value = {"landing": LANDING, "route_token": "route"}
    monkeypatch.setattr(setup, "Engine", Mock(return_value=engine))
    answers = iter([PARENT, "y", "My lane"])
    shown = []
    try:
        setup.wizard(store, ask=lambda prompt: next(answers), hidden=lambda prompt: "notion-secret", show=shown.append)
        connect.assert_called_once()
        notion.page.assert_called_once_with(PARENT)
        assert store.config()["notion_parent_page"] == PARENT
        assert store.secrets()["notion_token"] == "notion-secret"
        engine.create_lane.assert_called_once_with(PARENT, {"order": "largest", "auto_resume_unfinished": True, "model_label": "ChatGPT Web via Notion"}, "My lane")
        text = "\n".join(shown)
        assert "fnv-secret" not in text and "notion-secret" not in text
        assert "Welcome samurai o7" in text and " start" in text and " stop" in text
    finally:
        store.close()


def test_setup_reuses_saved_credentials_and_parent_without_creating_lane(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set_secret("fnv_api_key", "fnv-secret")
    store.set_secret("notion_token", "notion-secret")
    store.set_config({"paths": {}, "notion_parent_page": PARENT})
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    connect = Mock(side_effect=AssertionError("Credentials already exist"))
    monkeypatch.setattr(setup.authentication, "connect", connect)
    monkeypatch.setattr(setup, "FNV", Mock())
    notion, engine = Mock(), Mock()
    monkeypatch.setattr(setup, "Notion", Mock(return_value=notion))
    monkeypatch.setattr(setup, "Engine", engine)
    answers = iter(["", "n"])
    try:
        setup.wizard(store, ask=lambda prompt: next(answers), hidden=lambda prompt: pytest.fail("Should reuse credentials"), show=lambda text: None)
        connect.assert_not_called()
        engine.assert_not_called()
        notion.page.assert_called_once_with(PARENT)
    finally:
        store.close()


def test_setup_refuses_noninteractive_input_without_network(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    connect = Mock()
    monkeypatch.setattr(setup.authentication, "connect", connect)
    try:
        with pytest.raises(ValueError):
            setup.wizard(store)
        connect.assert_not_called()
        assert store.secrets() == {}
    finally:
        store.close()


def test_setup_does_not_save_inaccessible_parent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set_secret("fnv_api_key", "fnv-secret")
    store.set_secret("notion_token", "notion-secret")
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(setup, "FNV", Mock())
    notion = Mock()
    notion.page.side_effect = RuntimeError("unshared page")
    monkeypatch.setattr(setup, "Notion", Mock(return_value=notion))
    try:
        with pytest.raises(RuntimeError):
            setup.wizard(store, ask=lambda prompt: PARENT, show=lambda text: None)
        assert not store.config().get("notion_parent_page")
    finally:
        store.close()
