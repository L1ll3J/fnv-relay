import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fnv_relay import connections, cli
from fnv_relay.store import Store


def test_rest_and_oauth_selection_is_explicit(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set_secret("notion_token", "private-token")
    rest = Mock()
    try:
        connections.notion_client(store, rest_factory=rest)
        rest.assert_called_once_with("private-token")
        mcp = Mock()
        monkeypatch.setitem(sys.modules, "fnv_relay.notion_mcp", SimpleNamespace(NotionMCP=mcp))
        store.set_config({"notion_transport": "mcp"})
        assert connections.notion_client(store) is mcp.return_value
        mcp.assert_called_once_with(store)
        store.set_config({"notion_transport": "unknown"})
        with pytest.raises(ValueError): connections.notion_client(store)
    finally:
        store.close()


def test_notion_connect_command_refuses_to_rebind_existing_lanes(tmp_path):
    store = Store(tmp_path)
    store.put_lane({"route_token": "route", "state": "idle"})
    try:
        args = cli.parser().parse_args(["auth", "notion", "connect"])
        with pytest.raises(ValueError, match="same-workspace browser reconnection"):
            cli.run(args, store)
    finally:
        store.close()


def test_status_recognizes_the_selected_oauth_credentials(tmp_path):
    from fnv_relay.presentation import snapshot
    store = Store(tmp_path)
    try:
        store.set_config({"notion_transport": "mcp"})
        store.set_secret("notion_token", "old-api-token")
        assert snapshot(store, "test")["credentials_configured"]["notion"] is False
        store.set_secret("notion_oauth", {"tokens": {"access_token": "oauth-token"}})
        assert snapshot(store, "test")["credentials_configured"]["notion"] is True
    finally:
        store.close()
