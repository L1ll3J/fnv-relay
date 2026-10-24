"""Interactive account/page setup after the local toolchain bootstrap."""
from __future__ import annotations

import getpass
from pathlib import Path
import sys

from . import authentication, connections, preferences
from .engine import Engine
from .notion import Notion, page_id
from .presentation import starter_prompt
from .project import FNV



def chatgpt_connection_step(store, *, ask=None, show=print):
    """Record an operator acknowledgement, not a verified ChatGPT capability."""
    if store.get("chatgpt_notion_setup_ack"):
        return
    if not sys.stdin.isatty():
        raise ValueError("ChatGPT connection setup needs an interactive terminal")
    ask = ask or input
    show("First, connect Notion manually in the ChatGPT account that will solve functions:")
    show("")
    show("In ChatGPT: Account > Settings > Integrations > Plugins > Browse directory > search Notion.")
    show("")
    show("Connect and sign in or create your Notion account. You do not need to do anything in Notion yet.")
    show("")
    show("Then, in ChatGPT (not Notion): Settings > Integrations > Plugins > Notion > Allow all tools.")
    show("")
    show("Next, this CLI will ask you to authorize the same Notion account/workspace in your browser.")
    show("")
    ask("Press Enter when the ChatGPT Notion connection and tool permissions are ready: ")
    store.set("chatgpt_notion_setup_ack", True)
    show("Setup acknowledged. We will still verify actual read/write access before claiming work.")


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
        chatgpt_connection_step(store, ask=ask, show=show)
        if not connections.configured(store):
            from . import notion_oauth
            show("Connect Notion in your browser. Choose the workspace for this relay.")
            notion_oauth.connect(store, show=show)
        notion = connections.notion_client(store, rest_factory=Notion)
        identity = notion.whoami()
        config = store.config()
        previous = config.get("notion_parent_page")
        browser_connection = config.get("notion_transport") == "mcp"
        if browser_connection and not previous:
            entered = ask("Press Enter to create your private FNV Relay page (recommended), or paste an existing page URL: ").strip()
            if entered:
                parent = page_id(entered)
            else:
                workspace = identity.get("workspace", {}).get("id")
                if not isinstance(workspace, str) or not workspace:
                    raise ValueError("Connected workspace identity is required")
                parent = store.operation("setup-parent:" + workspace, {"workspace": workspace, "title": "FNV Relay"}, lambda: notion.create_workspace_page("FNV Relay"))
                parent = page_id(parent)
                show("Created private relay page: https://www.notion.so/" + parent.replace("-", ""))
        else:
            if not browser_connection:
                show("Use a normal Notion page, not developer settings. Share it through ... > Add connections first.")
            entered = ask("Private Notion parent page URL or UUID" + (" [Enter keeps saved page]" if previous else "") + ": ").strip()
            parent = page_id(entered or previous or "")
        notion.page(parent)
        config["notion_parent_page"] = parent
        with store.lock("config"):
            store.set_config(config)
        show("Private parent verified and saved.")
        order = preferences.choose_order(store, ask=ask, show=show)
        lanes = store.lanes()
        create = ask("Create " + ("another" if lanes else "your first") + " lane now? [Y/n]: ").strip().lower()
        if create in ("", "y", "yes"):
            label = "Lane " + str(len(lanes) + 1)
            lane = Engine(store, project, notion).create_lane(parent, {"order": order, "auto_resume_unfinished": True, "model_label": "ChatGPT Web via Notion"}, label)
            show("")
            show("Copy the text between the markers into your ChatGPT conversation:")
            show("")
            show("----- BEGIN CHATGPT PROMPT -----")
            show(starter_prompt(lane))
            show("----- END CHATGPT PROMPT -----")
            show("")
        elif create not in ("n", "no"):
            raise ValueError("Please answer yes or no")
    state = str(store.root)
    import shlex
    executable = Path(sys.executable).with_name("fnv-relay")
    command = str(executable) if executable.is_file() else "fnv-relay"
    prefix = shlex.quote(command) + " --state-dir " + shlex.quote(state)
    show("Setup saved. Back in this terminal:")
    show("")
    show("Check readiness:")
    show("  " + prefix + " doctor")
    show("")
    show("Start the relay:")
    show("  " + prefix + " start")
    show("")
    show("While it is running, use another terminal to inspect or stop it:")
    show("  " + prefix + " status")
    show("  " + prefix + " stop")
    show("")
    show("Start only after the readiness checks pass.")
