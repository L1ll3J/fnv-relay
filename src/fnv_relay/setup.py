"""Interactive account/page setup after the local toolchain bootstrap."""
from __future__ import annotations

import getpass
import sys

from . import authentication
from .engine import Engine
from .notion import Notion, page_id
from .presentation import starter_prompt
from .project import FNV


def wizard(store, *, ask=None, hidden=None, show=print):
    if not sys.stdin.isatty():
        raise ValueError("Setup needs an interactive terminal")
    ask = ask or input
    hidden = hidden or getpass.getpass
    show("Welcome to fnv-relay. This connects your accounts and private Notion page.")
    show("Install the local game/compiler/checker first using docs/BOOTSTRAP.md.")
    with store.lock("serve"), store.lock("setup"):
        if any(row.get("lease") for row in store.lanes()):
            raise ValueError("Resolve active claims before account setup")
        secrets = store.secrets()
        if not secrets.get("fnv_api_key"):
            show("FNV will show a link for GitHub approval; no API key purchase is needed.")
            authentication.connect(store, "fnv-relay", show=show)
        project = FNV(store.secrets().get("fnv_api_key", ""))
        project.call("status", {})
        secrets = store.secrets()
        if not secrets.get("notion_token"):
            show("Notion needs a private internal connection token, not ChatGPT/Codex OAuth.")
            show("As a workspace owner, open https://www.notion.so/profile/integrations and create an internal connection.")
            show("Enable read, insert and update content. Copy its token from Configuration.")
            show("On your private parent page, use the ... menu > Add connections to share it with this connection.")
            show("Keep the page private. Separately connect your Notion account in ChatGPT.")
            token = hidden("Notion internal connection token (hidden): ").strip()
            Notion(token).whoami()
            store.set_secret("notion_token", token)
        notion = Notion(store.secrets().get("notion_token", ""))
        notion.whoami()
        config = store.config()
        previous = config.get("notion_parent_page")
        entered = ask("Private Notion parent page URL or UUID" + (" [Enter keeps saved page]" if previous else "") + ": ").strip()
        parent = page_id(entered or previous or "")
        notion.page(parent)
        config["notion_parent_page"] = parent
        with store.lock("config"):
            store.set_config(config)
        show("Private parent verified and saved.")
        lanes = store.lanes()
        create = ask("Create " + ("another" if lanes else "your first") + " lane now? [Y/n]: ").strip().lower()
        if create in ("", "y", "yes"):
            label = ask("Lane label [My lane]: ").strip() or "My lane"
            if len(label) > 100 or any(ord(c) < 32 for c in label):
                raise ValueError("Invalid label")
            lane = Engine(store, project, notion).create_lane(parent, {"order": "largest", "auto_resume_unfinished": True, "model_label": "ChatGPT Web via Notion"}, label)
            show("Paste this starter into a ChatGPT conversation with Notion access:")
            show(starter_prompt(lane))
        elif create not in ("n", "no"):
            raise ValueError("Please answer yes or no")
    state = str(store.root)
    import shlex
    prefix = "fnv-relay --state-dir " + shlex.quote(state)
    show("Next: " + prefix + " doctor")
    show("Start: " + prefix + " start")
    show("Inspect: " + prefix + " status")
    show("Exit safely: " + prefix + " stop")
    show("Setup is saved. Readiness still depends on doctor and the official checker image/version checks.")
