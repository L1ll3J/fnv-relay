"""Select the explicitly configured Notion transport without borrowing other clients' credentials."""
from .notion import Notion


def notion_client(store, *, rest_factory=None):
    transport = store.config().get("notion_transport", "api")
    if transport == "mcp":
        from .notion_mcp import NotionMCP
        return NotionMCP(store)
    if transport != "api":
        raise ValueError("Unknown Notion transport")
    return (rest_factory or Notion)(store.secrets().get("notion_token", ""))


def configured(store):
    return store.config().get("notion_transport") == "mcp" or bool(store.secrets().get("notion_token"))
