"""Small, source-free status and starter text for the human operator."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .notion import page_id
from .store import AlreadyRunning


def clean(value, limit=80):
    text = str(value if value is not None else "-")
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def starter_prompt(lane):
    url = "https://app.notion.com/p/" + page_id(lane["landing"]).replace("-", "")
    return ("Welcome courier o7\n\n"
            "Today we're going to be doing some decompiling work, it's hard, but it's honest work. "
            "You might find it difficult but that's okay, this is largely a game of creative trial-and-error. "
            "Persistence and clever thinking will pay dividends here.\n\n"
            + url + "\n\n"
            "Read that landing page and register using route UUID `" + lane["route_token"] + "`, "
            "following its HELLO instructions. Read future assignments and results from that lane’s outbox.\n\n"
            "You have my explicit approval to read, create and edit pages within this private Notion relay for this project, "
            "including uploading candidate source code and relevant project context, posting HELLO registrations and BATCH messages, "
            "and recording findings and results.")


def resolve_lane(store, value):
    lanes = store.lanes()
    exact = [row for row in lanes if row["route_token"] == value]
    if exact:
        return exact[0]
    labels = [row for row in lanes if row.get("label") == value]
    candidates = labels or [row for row in lanes if len(value) >= 4 and row["route_token"].startswith(value)]
    if len(candidates) != 1:
        raise ValueError("Lane name or route is missing or ambiguous")
    return candidates[0]


def snapshot(store, version):
    rows = []
    lanes = store.lanes()
    for lane in lanes:
        lease = lane.get("lease") or {}
        claim = lease.get("claim_id")
        best = None
        if claim and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", claim):
            path = store.root / "claims" / claim / "checked-best.json"
            try:
                if path.resolve().is_relative_to(store.root):
                    best = json.loads(path.read_text()).get("rank", [None])[0]
            except (OSError, ValueError, IndexError, TypeError):
                pass
        error = store.get("last_error:" + lane["route_token"]) or {}
        error_type = error.get("type")
        if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z_]{1,80}", error_type):
            error_type = None
        rows.append({"route_token": lane["route_token"], "label": clean(lane.get("label", "Lane")), "state": clean(lane.get("state")), "paused": bool(lane.get("paused")), "auto_resume": bool(lane.get("policy", {}).get("auto_resume_unfinished", False)), "model_label": clean(lane.get("policy", {}).get("model_label", "ChatGPT Web via Notion")), "claim_id": claim, "address": clean(lease.get("address") or (lane.get("resume") or {}).get("address")), "size_bytes": lease.get("size"), "expires_at": lease.get("expires"), "best_diff": best, "checks": store.get("checks:" + claim) or 0 if claim else 0, "error_type": error_type, "continuation": bool(lane.get("resume")), "parked_functions": len(lane.get("parked", [])), "cooldown_until": (lane.get("resume") or {}).get("not_before"), "has_active_lease": bool(lease), "landing": lane.get("landing")})
    from .attempts import current
    for row,lane in zip(rows,lanes):
        row['attempts']=current(store,lane)
        row['max_attempts']=lane.get('policy',{}).get('max_attempts') or 0
        row['awaiting_handoff']=bool(lane.get('attempt_handoff'))
    secrets = store.secrets()
    oauth = secrets.get("notion_oauth") or {}
    oauth_tokens = oauth.get("tokens") or {}
    notion_configured = bool(oauth_tokens.get("access_token")) if store.config().get("notion_transport") == "mcp" else bool(secrets.get("notion_token"))
    worker = dict(store.get("worker") or {})
    try:
        with store.lock("serve"):
            worker["running"] = False
    except AlreadyRunning:
        worker["running"] = bool(worker.get("running"))
    return {"schema_version": 1, "version": version, "drain": bool(store.get("drain")), "shutdown_requested": bool(store.get("shutdown_requested")), "worker": worker, "credentials_configured": {"fnv": bool(secrets.get("fnv_api_key")), "notion": notion_configured}, "lanes": rows, "uncertain_operations": len(store.pending_operations())}


def human_status(data):
    worker = data.get("worker") or {}
    mode = "shutdown requested" if data["shutdown_requested"] else "draining" if data["drain"] else "accepting work" if worker.get("running") else "ready to start"
    lines = ["Relay " + ("running" if worker.get("running") else "stopped") + " | " + mode + " | uncertain writes: " + str(data["uncertain_operations"])]
    if data.get("account"):
        account = data["account"]
        lines.append("FNV account (refreshed): " + ", ".join(key + "=" + clean(value) for key, value in account.items()) + "; tracked lanes=" + str(len(data["lanes"])))
    if not data["lanes"]:
        return "\n".join(lines + ["No lanes yet. Run fnv-relay setup."])
    headers = ("LANE / ROUTE", "STATE", "FUNCTION", "SIZE", "EXPIRES UTC", "BEST", "CHECKS", "ATTEMPTS", "DETAILS")
    table = []
    for row in data["lanes"]:
        expires = "-"
        if row["expires_at"]:
            try:
                expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M:%S")
            except (ValueError, TypeError, AttributeError):
                expires = "unknown"
        details = ["paused" if row["paused"] else "resume on" if row["auto_resume"] else "resume off"]
        if row["cooldown_until"]:
            try:
                retry = datetime.fromtimestamp(float(row["cooldown_until"]), timezone.utc).strftime("%H:%M:%S UTC")
                details.append("retry after " + retry)
            except (ValueError,TypeError,OverflowError,OSError):
                details.append("cooldown")
        if row["continuation"]:
            details.append("saved function")
        if row.get("parked_functions"):
            details.append(str(row["parked_functions"]) + " parked function(s)")
        if row["error_type"]:
            details.append("last error: " + row["error_type"])
        table.append((row["label"] + " / " + row["route_token"][:8], {"waiting_hello":"awaiting web agent", "claim_intent":"claim needs review", "preparing":"sending assignment", "assigned":"working", "resume_pending":"waiting to reclaim", "closed_requires_reconciliation":"needs review"}.get(row["state"],row["state"]), row["address"], str(row["size_bytes"] or "-"), expires, str(row["best_diff"] if row["best_diff"] is not None else "-"), str(row["checks"]), str(row.get("attempts",0))+" / "+(str(row.get("max_attempts")) if row.get("max_attempts") else "off"), ", ".join(details)))
    widths = [max(len(headers[i]), *(len(clean(row[i], 100)) for row in table)) for i in range(len(headers))]
    lines.append("  ".join(value.ljust(widths[i]) for i, value in enumerate(headers)))
    lines.extend("  ".join(clean(value, 100).ljust(widths[i]) for i, value in enumerate(row)) for row in table)
    return "\n".join(lines)
