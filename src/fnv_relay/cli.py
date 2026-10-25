"""Owner-operated command line; never prints credentials or remote error bodies."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import os
import ctypes
import getpass
import json
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import signal
import threading

from . import __version__, authentication, compiler, connections, preferences, presentation, setup, tools
from .engine import BANDS, Engine
from .notion import Notion, NotionError, page_id
from .notion_oauth import OAuthError
from .project import FNV, ProjectError
from .store import AlreadyRunning, Store, UncertainOperation, OperationNotStarted
from .lifecycle import process_identity, owner_alive

DEFAULT_STATE = "~/.local/share/fnv-relay"
PATHS = ("game_exe", "checker_dir", "compiler_dir", "function_index", "symbols", "compiler_tips", "wine_prefix", "python")


def parser():
    root = argparse.ArgumentParser(prog="fnv-relay", description="Local FNV/Notion relay (preview)")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state-dir", default=DEFAULT_STATE)
    commands = root.add_subparsers(dest="command")
    root.set_defaults(command="menu")
    commands.add_parser("menu", help="Open the full-screen terminal application")
    commands.add_parser("text-menu", help="Use the legacy numbered menu")
    init = commands.add_parser("init", help="Initialize private local state")
    init.add_argument("--state-dir", default=argparse.SUPPRESS)
    commands.add_parser("setup", help="Guided account and private Notion page setup")
    config = commands.add_parser("config", help="Set local input paths and bounded runtime options")
    for name in PATHS:
        config.add_argument("--" + name.replace("_", "-"))
    config.add_argument("--max-candidate-bytes", type=int)
    config.add_argument("--check-timeout-seconds", type=float)
    config.add_argument("--poll-seconds", type=float)
    auth = commands.add_parser("auth").add_subparsers(dest="service", required=True)
    for service in ("fnv", "notion"):
        actions = auth.add_parser(service).add_subparsers(dest="auth_action", required=True)
        actions.add_parser("login", help="Advanced: enter an existing API credential")
        if service == "notion":
            actions.add_parser("connect", help="Sign in to Notion in your browser; no API token needed")
        if service == "fnv":
            actions.add_parser("connect", help="Approve with GitHub; no existing API key needed").add_argument("--label", default="fnv-relay")
    fetch = commands.add_parser("tools").add_subparsers(dest="tools_action", required=True).add_parser("fetch", help="Download official checker and shared files into a new directory")
    fetch.add_argument("--directory", required=True)
    doctor = commands.add_parser("doctor", help="Read-only checks; no compilation or claims")
    doctor.add_argument("--prepare-wine", action="store_true", help="Explicitly initialize the configured dedicated Wine prefix")
    lane = commands.add_parser("lane").add_subparsers(dest="lane_action", required=True)
    create = lane.add_parser("create", help="Create private Notion lane pages")
    create.add_argument("--parent-page", help="Default: parent saved by setup")
    create.add_argument("--auto-resume", action=argparse.BooleanOptionalAction, default=True, help="Continue unfinished function after release when agent activity is recent")
    create.add_argument("--order", choices=preferences.ORDERS, help="Default: task order saved during setup")
    create.add_argument("--allow-attempted", action=argparse.BooleanOptionalAction, default=None, help="Allow previously attempted functions in new selections")
    create.add_argument("--band", choices=BANDS)
    create.add_argument("--address")
    create.add_argument("--min-bytes", type=int, default=0)
    create.add_argument("--max-bytes", type=int, default=2**31 - 1)
    create.add_argument("--label", default="Pro lane")
    create.add_argument("--model-label", default="ChatGPT Web via Notion", help="Human-confirmed model attribution; otherwise use the truthful route label")
    settings = lane.add_parser("configure", help="Change future selection; optionally park current work and switch now")
    settings.add_argument("route")
    settings.add_argument("--order", choices=preferences.ORDERS)
    settings.add_argument("--allow-attempted", action=argparse.BooleanOptionalAction, default=None)
    settings.add_argument("--paused", action=argparse.BooleanOptionalAction, default=None, help="Pause or unpause future selections")
    settings.add_argument("--switch-now", action="store_true", help="Release and preserve current work, then select a different task")
    recover = lane.add_parser("recover", help="Finish an incomplete lane setup using Notion readback")
    recover.add_argument("route")
    recover.add_argument("--retry-missing", action="store_true", help="Authorize a fresh attempt only after readback finds no matching page")
    lane.add_parser("list")
    lane.add_parser("prompt").add_argument("route")
    lane.add_parser("parked", help="List preserved unfinished functions").add_argument("route")
    for action in ("pause", "resume"):
        control = lane.add_parser(action)
        control.add_argument("route", help="Full route, unique prefix (4+ characters), or exact label")
        if action == "resume":
            control.add_argument("--address", help="Resume a parked function by address")
        if action == "pause":
            control.add_argument("--release", action="store_true", help="Also release the current claim, retaining continuation")
    serve = commands.add_parser("serve", aliases=["start"], help="Run live work under your FNV account")
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--menu-owner-pid", type=int, help=argparse.SUPPRESS)
    serve.add_argument("--menu-owner-start-time", help=argparse.SUPPRESS)
    serve.add_argument("--order", choices=preferences.ORDERS, help="Save and apply an order to future tasks in unpaused lanes")
    status = commands.add_parser("status", help="Human-readable local status")
    status.add_argument("--json", action="store_true", help="Stable machine-readable status")
    status.add_argument("--refresh", action="store_true", help="Also fetch read-only FNV account totals")
    commands.add_parser("logs", help="Recent sanitized event summaries").add_argument("--tail", type=int, default=20)
    stop = commands.add_parser("stop", aliases=["shutdown"], help="Release tracked claims and stop the worker safely")
    stop.add_argument("--timeout", type=float, default=15, help="Wait at most this many seconds for a running worker (0–120)")
    commands.add_parser("drain", help="Finish current claims without assigning more")
    reconcile = commands.add_parser("reconcile", help="Inspect uncertain writes; optionally verify a claim is no longer active")
    reconcile.add_argument("--claim", metavar="ROUTE", help="Verify no claims belong to this key; preserve the intent and rearm this lane locally")
    return root


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def clients(store):
    secrets = store.secrets()
    return FNV(secrets.get("fnv_api_key", "")), connections.notion_client(store, rest_factory=Notion)


def policy(args):
    if not 0 <= args.min_bytes <= args.max_bytes <= 2**31 - 1:
        raise ValueError("Invalid byte bounds")
    if args.address and not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", args.address):
        raise ValueError("Invalid address")
    if not args.label.strip() or len(args.label) > 100 or any(ord(c) < 32 for c in args.label):
        raise ValueError("Invalid label")
    if not args.model_label.strip() or len(args.model_label) > 100 or any(ord(c) < 32 for c in args.model_label):
        raise ValueError("Invalid model label")
    result = {"model_label": args.model_label.strip(), "order": args.order, "min_bytes": args.min_bytes, "max_bytes": args.max_bytes, "auto_resume_unfinished": args.auto_resume}
    if getattr(args,"allow_attempted",None) is not None:
        result["allow_attempted"] = args.allow_attempted
    if args.band:
        result["band"] = args.band
    if args.address:
        result["address"] = args.address
    return result


def configure(store, args):
    config = store.config()
    paths = dict(config.get("paths", {}))
    for name in PATHS:
        value = getattr(args, name)
        if value is not None:
            path = Path(value).expanduser()
            paths[name] = str(path.absolute() if name == "python" else path.resolve())
    if args.max_candidate_bytes is not None:
        if not 0 < args.max_candidate_bytes <= 16 * 1024 * 1024:
            raise ValueError("Invalid source byte limit")
        config["max_candidate_bytes"] = args.max_candidate_bytes
    if args.check_timeout_seconds is not None:
        if not 0 < args.check_timeout_seconds <= 3600:
            raise ValueError("Invalid checker timeout")
        paths["check_timeout_seconds"] = args.check_timeout_seconds
    if args.poll_seconds is not None:
        if not 1 <= args.poll_seconds <= 60:
            raise ValueError("Invalid poll interval")
        config["poll_seconds"] = args.poll_seconds
    config["paths"] = paths
    with store.lock("config"):
        store.set_config(config)
    print("Local configuration saved.")


def login(store, service):
    # Fail rather than falling back to an echoed credential on non-interactive input.
    if not sys.stdin.isatty():
        raise ValueError("Login needs an interactive terminal")
    value = getpass.getpass("FNV project API key: " if service == "fnv" else "Notion internal connection token: ").strip()
    if service == "fnv":
        FNV(value).call("status", {})
        name = "fnv_api_key"
    else:
        Notion(value).whoami()
        name = "notion_token"
    store.set_secret(name, value)
    if service == "notion":
        with store.lock("config"):
            config = store.config()
            config["notion_transport"] = "api"
            store.set_config(config)
    print("Credential validated and stored privately.")


def host_checks(paths):
    checks = {}
    validated = None
    try:
        validated = compiler._config(paths)
        checks["toolchain_files"] = True
    except Exception:
        checks["toolchain_files"] = False
    checks["checker_python_dependencies"] = False
    if validated is not None:
        try:
            result = subprocess.run([validated["python"], "-I", "-B", "-c", "import capstone"], capture_output=True, timeout=10, check=False)
            checks["checker_python_dependencies"] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks["linux"] = platform.system() == "Linux"
    for name in ("wine", "wineboot", "wineserver", "unshare"):
        checks[name] = shutil.which(name) is not None
    checks["user_and_network_namespaces"] = False
    if checks["unshare"]:
        try:
            result = subprocess.run([shutil.which("unshare"), "--user", "--map-root-user", "--net", "--", "/usr/bin/true"], capture_output=True, timeout=10, check=False)
            checks["user_and_network_namespaces"] = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks["landlock_abi_3"] = False
    if checks["linux"] and platform.machine() in ("x86_64", "aarch64"):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        checks["landlock_abi_3"] = libc.syscall(444, 0, 0, 1) >= 3
    prefix = Path(paths.get("wine_prefix", "/nonexistent"))
    checks["dedicated_wine_prefix"] = (prefix / compiler._MARKER).is_file()
    checks["wine_server"] = checks["dedicated_wine_prefix"] and any(path.is_dir() for path in compiler._wine_servers(prefix))
    return checks


def doctor(store, prepare=False):
    paths = store.config().get("paths", {})
    if prepare:
        compiler.prepare(paths)
    checks = host_checks(paths)
    secrets = store.secrets()
    for label, secret, factory, operation in (
        ("fnv_auth", "fnv_api_key", FNV, lambda client: client.call("status", {})),
        ("notion_auth", "notion_token", Notion, lambda client: client.whoami()),
    ):
        checks[label] = False
        if secrets.get(secret) and not (label == "notion_auth" and store.config().get("notion_transport") == "mcp"):
            try:
                operation(factory(secrets[secret]))
                checks[label] = True
            except Exception:
                pass
    if store.config().get("notion_transport") == "mcp":
        try:
            connections.notion_client(store).whoami()
            checks["notion_auth"] = True
        except Exception:
            checks["notion_auth"] = False
    output({"checks": checks, "ok": all(checks.values()), "compile_smoke_performed": False})
    return 0 if all(checks.values()) else 1


def summary(lane):
    result = {key: lane[key] for key in ("route_token", "state", "landing") if key in lane}
    result["has_active_lease"] = bool(lane.get("lease"))
    return result


def lane_command(store, args):
    if args.lane_action == "configure":
        from .lane_policy import configure as configure_lane
        lane = presentation.resolve_lane(store,args.route)
        project = FNV(store.secrets().get('fnv_api_key','')) if args.switch_now and lane.get('lease') else None
        engine = Engine(store,project,None,watchdog=False)
        options=dict(order=args.order,allow_attempted=args.allow_attempted,switch_now=args.switch_now,paused=args.paused)
        try:
            with store.lock('serve'):
                lane=configure_lane(engine,lane['route_token'],**options)
        except AlreadyRunning:
            from .lane_policy import queue
            queue(store,lane['route_token'],**options)
            print('Change queued. The relay will apply it after the current batch finishes; saved work is preserved.')
            return
        output(summary(lane))
    elif args.lane_action == "list":
        print(presentation.human_status(presentation.snapshot(store, __version__)))
    elif args.lane_action == "prompt":
        lane = presentation.resolve_lane(store, args.route)
        if lane.get('state') == 'setup' or not lane.get('landing'):
            print('Lane setup is incomplete. Use Add lane in the menu to recover it before copying a starter.')
            return
        print(presentation.starter_prompt(lane))
    elif args.lane_action == "recover":
        lane = presentation.resolve_lane(store,args.route)
        project,notion = clients(store)
        with store.lock("lane-create"):
            lane = Engine(store,project,notion).finish_lane_setup(lane,retry_missing=args.retry_missing)
        output(summary(lane))
    elif args.lane_action == "parked":
        lane = presentation.resolve_lane(store, args.route)
        output([{'address':entry['continuation']['address'], 'previous_claim_id':entry['continuation']['previous_claim_id'], 'reason':entry['reason']} for entry in lane.get('parked',[])])
    elif args.lane_action in ("pause", "resume"):
        lane = presentation.resolve_lane(store, args.route)
        key = store.secrets().get("fnv_api_key", "")
        project = FNV(key) if args.lane_action == "pause" and args.release else None
        engine = Engine(store, project, None, watchdog=False)
        if args.lane_action == "pause":
            engine.pause_lane(lane["route_token"], release=args.release)
            print("Lane paused." + (" Current claim release requested." if args.release else " Current claim may finish; no new claim will be assigned."))
        else:
            if args.address:
                engine.resume_parked(lane["route_token"], args.address)
            else:
                engine.resume_lane(lane["route_token"])
            print("Lane resumed. Start the worker if it is stopped.")
    else:
        if args.order is None: args.order = store.config().get("default_task_order", "largest")
        selected = policy(args)
        parent = page_id(args.parent_page or store.config().get("notion_parent_page", ""))
        project, notion = clients(store)
        with store.lock("lane-create"):
            lane = Engine(store, project, notion).create_lane(parent, selected, args.label)
        output(summary(lane))


@contextmanager
def account_lock(key):
    """One owner key may run in only one state directory at a time."""
    root = Path.home() / ".local/share/fnv-relay-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise ValueError("Unsafe global lock directory")
    root.chmod(0o700)
    name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
    fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("This owner key is already serving") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def shutdown_signals(event):
    previous = {}
    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.getsignal(signum)
                def request_stop(*_):
                    if not event.is_set():
                        try: os.write(2, b"\nStopping safely; finishing the current operation and releasing tracked claims...\n")
                        except OSError: pass
                    event.set()
                signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def release_report(store, engine, reason):
    try:
        result = engine.release_all(reason)
        if not isinstance(result, dict) or not isinstance(result.get("counts"), dict):
            raise ValueError("Invalid release summary")
    except Exception:
        result = {"counts": {"released": 0, "pending": max(1, sum(bool(row.get("lease")) for row in store.lanes())), "uncertain": len(store.pending_operations())}}
    store.set("last_shutdown", result)
    counts = result["counts"]
    released, pending, uncertain = (counts.get(key, 0) for key in ("released", "pending", "uncertain"))
    expired = counts.get("expired", 0)
    print(f"Shutdown: {released} released, {pending} pending, {uncertain} uncertain, {expired} already expired.")
    if pending or uncertain:
        print("Unresolved operations remain recorded. An uncertain claim request does not prove an active claim. Inspect status --refresh and reconcile; tracked claims retain their guardians.")
    return 1 if pending or uncertain else 0


def reconcile_claim(store, route):
    with store.lock("serve"), account_lock(store.secrets().get("fnv_api_key", "")):
        lane = presentation.resolve_lane(store, route)
        if lane.get("lease") or lane.get("state") != "claim_intent":
            raise ValueError("Lane does not have an unresolved claim intent")
        op = "claim:"+lane['route_token']+":"+str(lane['sequence'])
        record = store.operation_record(op)
        if not record or record['status'] == 'response':
            raise ValueError("Claim operation is not uncertain")
        status = FNV(store.secrets().get("fnv_api_key", "")).call("status", {})
        if not isinstance(status.get('open_claims'), list) or any(not isinstance(row,dict) or type(row.get('this_key')) is not bool for row in status['open_claims']):
            raise ValueError("Official ownership status was not confirmed")
        if any(row.get('this_key') for row in status['open_claims']):
            raise UncertainOperation("This key still owns a claim; inspect official status before reconciliation")
        receipt = {'args_hash':record['args_hash'], 'observed_at':time.time(), 'resolution':'no_active_claim_for_key', 'original_status':record['status']}
        store.set('claim-resolution:'+op, receipt)
        engine = Engine(store, None, None, watchdog=False)
        engine.recover_claim_intent(lane)
        engine.resume_lane(lane['route_token'])
        store.event('claim_reconciled_no_active_ownership', {'route_token':lane['route_token'], 'operation':op})
        print("No active claim belongs to this key. Original intent retained; unfinished work is ready to resume on start.")



def stop_unstarted_worker(store, project):
    engine = Engine(store,project,None,watchdog=False)
    return release_report(store,engine,'Menu closed before worker startup completed')


def serve(store, once=False, order=None, menu_owner=None):
    project, notion = clients(store)
    if not all(host_checks(store.config().get("paths", {})).values()):
        raise ValueError("Toolchain or isolation health check failed")
    project.call("status", {})
    notion.whoami()
    interval = store.config().get("poll_seconds", 5)
    if not isinstance(interval, (int, float)) or not 1 <= interval <= 60:
        raise ValueError("Invalid poll interval")
    event = threading.Event()
    project.set_stop_event(event)
    with store.lock("serve"), account_lock(store.secrets().get("fnv_api_key", "")):
        if menu_owner is not None and (not owner_alive(menu_owner) or store.get('shutdown_requested')):
            return stop_unstarted_worker(store,project)
        if store.pending_operations():
            raise UncertainOperation("Reconciliation required")
        stopping = lambda: event.is_set() or menu_owner is not None and not owner_alive(menu_owner)
        engine = Engine(store, project, notion, should_stop=stopping)
        selected,apply_order = preferences.start_order(store,order)
        if apply_order:
            for row in store.lanes():
                if row.get('paused'): continue
                with engine.lane_lock(row['route_token']):
                    current=store.lane(row['route_token'])
                    if current and not current.get('paused'):
                        current.setdefault('policy',{})['order']=selected; store.put_lane(current)
        store.set("drain", False)
        store.set("shutdown_requested", False)
        for lane in store.lanes():
            if lane.get("resume") and (not lane.get("paused") or lane.get("blocked_reason") == "function_paused"):
                engine.resume_lane(lane["route_token"])
        store.set("worker", {**(process_identity() or {"pid":os.getpid()}), "running": True, "started_at": time.time()})
        code = 0
        with shutdown_signals(event):
            try:
                print("Relay running. Leave this terminal open. A running relay does not yet mean a claim is assigned.", flush=True)
                for row in store.lanes():
                    if row.get("landing"):
                        print("\nOpen a NEW ChatGPT chat after configuring Notion. Paste this starter (also available with lane prompt " + row["route_token"] + "):")
                        print("----- BEGIN CHATGPT PROMPT -----")
                        print(presentation.starter_prompt(row))
                        print("----- END CHATGPT PROMPT -----", flush=True)
                previous_status = None
                while not stopping() and not store.get("shutdown_requested"):
                    engine.poll()
                    current_status = presentation.human_status(presentation.snapshot(store, __version__))
                    if current_status != previous_status:
                        print(current_status, flush=True)
                        for row in store.lanes():
                            error = store.get('last_error:'+row['route_token']) or {}
                            if error.get('type') == 'OAuthError' or error.get('type') == 'NotionError' and error.get('http_status') == 401:
                                print("Notion sign-in needs attention. Stop the relay, then run auth notion connect with this state directory and existing workspace.", flush=True)
                            if row.get('blocked_reason') == 'function_paused':
                                print("FNV paused this function for problem-report review. Unfinished work is saved; reconnecting will not unblock it. Resume this lane after the project reopens the function.", flush=True)
                            if row.get('blocked_reason') == 'fnv_access_denied':
                                print("FNV refused a claim (401/403). Unfinished work is saved. Check FNV access, then run lane resume " + row['route_token'] + ".", flush=True)
                        previous_status = current_status
                    if once or store.get("drain") and not any(row.get("lease") for row in store.lanes()):
                        break
                    event.wait(interval)
            except KeyboardInterrupt:
                event.set()
                code = 130
            finally:
                project.set_stop_event(None)
                store.set("shutdown_requested", True)
                store.set("drain", True)
                cleanup_code = release_report(store, engine, "Owner stopped relay; preserve checked work for continuation")
                store.set("worker", {"pid": os.getpid(), "running": False, "stopped_at": time.time()})
        return code or cleanup_code


def stop(store, timeout=15):
    if not 0 <= timeout <= 120:
        raise ValueError("Invalid stop timeout")
    store.set("shutdown_requested", True)
    store.set("drain", True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            with store.lock("serve"):
                previous = store.get("worker") or {}
                if previous.get("running") is False and store.get("last_shutdown"):
                    counts = store.get("last_shutdown")["counts"]
                    if not any(row.get("lease") for row in store.lanes()) and not store.pending_operations():
                        print("Worker stopped; no tracked active claims remain.")
                        return 1 if counts.get("pending") or counts.get("uncertain") else 0
                if not any(row.get("lease") for row in store.lanes()) and not store.pending_operations():
                    store.set("worker", {"running": False, "stopped_at": time.time()})
                    print("Stopped; no tracked active claims remain.")
                    return 0
                key = store.secrets().get("fnv_api_key", "")
                with account_lock(key):
                    result = release_report(store, Engine(store, FNV(key), None, watchdog=False), "Owner requested offline shutdown")
                    store.set("worker", {"running": False, "stopped_at": time.time()})
                    return result
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                pending = sum(bool(row.get("lease")) for row in store.lanes())
                print(f"Shutdown requested; worker is still stopping. Tracked active claims: {pending}. Check status shortly.")
                return 1
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))


def run(args, store):
    if args.command in ("menu","text-menu"):
        if not sys.stdin.isatty():
            raise ValueError("Menu needs an interactive terminal")
        from . import interactive
        from .managed import menu_signals
        with menu_signals():
            dispatch = lambda command: main(["--state-dir",str(store.root),*command])
            if args.command == "text-menu": return interactive.run(store,dispatch)
            from .tui import run as run_tui
            return run_tui(store,dispatch)
    elif args.command == "init":
        if not (store.root / "config.json").exists():
            store.set_config(store.config())
        print("Private local state initialized.")
    elif args.command == "setup":
        setup.wizard(store)
    elif args.command == "config":
        configure(store, args)
    elif args.command == "auth":
        with store.lock("serve"):
            if any(row.get("lease") for row in store.lanes()):
                raise ValueError("Resolve active claims before replacing service credentials")
            if args.service == "notion" and store.lanes() and (args.auth_action != "connect" or store.config().get("notion_transport") != "mcp"):
                raise ValueError("Existing lanes require same-workspace browser reconnection")
            if args.service == "fnv" and args.auth_action == "connect":
                authentication.connect(store, args.label)
            elif args.service == "notion" and args.auth_action == "connect":
                from . import notion_oauth
                setup.chatgpt_connection_step(store)
                notion_oauth.connect(store)
            else:
                login(store, args.service)
    elif args.command == "tools":
        output(tools.fetch(store, args.directory))
    elif args.command == "doctor":
        return doctor(store, args.prepare_wine)
    elif args.command == "lane":
        lane_command(store, args)
    elif args.command in ("serve", "start"):
        if (args.menu_owner_pid is None) != (args.menu_owner_start_time is None):
            raise ValueError("Menu owner identity is incomplete")
        owner = {"pid":args.menu_owner_pid,"start_time":args.menu_owner_start_time} if args.menu_owner_pid is not None else None
        return serve(store, args.once, order=args.order, menu_owner=owner)
    elif args.command in ("stop", "shutdown"):
        return stop(store, args.timeout)
    elif args.command == "drain":
        store.set("drain", True)
        print("Draining: current claims may finish; no new claims will be assigned.")
    elif args.command == "reconcile":
        if args.claim:
            reconcile_claim(store, args.claim)
        else:
            output({"uncertain_operations": store.pending_operations(), "action": "Use reconcile --claim ROUTE to verify ownership of an uncertain claim request. Other uncertain writes require review."})
    elif args.command == "logs":
        events = store.events(args.tail)
        for event in events:
            data = event.get("data") or {}
            kind = event.get("kind", "event")
            kind = kind if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,60}", kind) else "event"
            fields = []
            for field in ("route_token", "claim_id", "status"):
                value = data.get(field)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", value):
                    fields.append(field + "=" + value[:40])
            print(str(event.get("id", "-")) + " " + kind + (" " + " ".join(fields) if fields else ""))
        if not events:
            print("No recent events.")
    elif args.command == "status":
        data = presentation.snapshot(store, __version__)
        if args.refresh:
            account = FNV(store.secrets().get("fnv_api_key", "")).call("status", {})
            safe_account = {field: account.get(field) if type(account.get(field)) is int else None for field in ("verified_bytes", "submissions_today", "awaiting_verification")}
            safe_account["open_claims"] = len(account.get("open_claims", []))
            safe_account["this_key_open_claims"] = sum(bool(row.get("this_key")) for row in account.get("open_claims", []))
            data["account"] = safe_account
        if args.json:
            output(data)
        else:
            print(presentation.human_status(data))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.state_dir)
        return run(args, store)
    except OAuthError as exc:
        print(str(exc), file=sys.stderr)
    except authentication.ConnectionError as exc:
        print(str(exc), file=sys.stderr)
    except OperationNotStarted as exc:
        print(str(exc), file=sys.stderr)
    except AlreadyRunning as exc:
        if "notion-oauth-session" in str(exc):
            print("The Notion connection is busy. No new connection request was started.", file=sys.stderr)
        else:
            print("Another relay command is running. Use the existing menu; it can manage lanes while the worker runs.", file=sys.stderr)
    except UncertainOperation:
        print("An uncertain external operation requires review; no automatic retry was made. Run reconcile. For an uncertain claim request, stop the relay and run reconcile --claim ROUTE to verify current ownership.", file=sys.stderr)
    except (ProjectError, NotionError) as exc:
        # Codes and text returned by a service are not trusted terminal output.
        if isinstance(exc, ProjectError) and exc.status == 401:
            print("FNV authentication failed. Run fnv-relay auth fnv connect, or auth fnv login with a current key.", file=sys.stderr)
        elif isinstance(exc, NotionError) and exc.status == 401:
            print("Notion sign-in expired. Stop the relay, then run auth notion connect using the same state directory and workspace.", file=sys.stderr)
        elif isinstance(exc, NotionError) and exc.status == 403:
            if store.config().get("notion_transport") == "mcp":
                print("Notion access was refused. Check the authorized workspace, page permissions and workspace limits.", file=sys.stderr)
            else:
                print("Notion access was refused. Share the parent with your internal connection and check workspace block limits.", file=sys.stderr)
        else:
            print("Remote service request failed" + (f" (HTTP {exc.status})" if exc.status else "") + "; inspect authentication, permissions and service status.", file=sys.stderr)
    except ValueError:
        print("Invalid input or configuration. Check --help, paths, byte bounds and credentials.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception:
        print("Command failed; private state was retained. Run doctor and reconcile before resuming live work.", file=sys.stderr)
    finally:
        if store is not None:
            store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
