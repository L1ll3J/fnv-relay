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

from . import __version__, authentication, compiler, tools
from .engine import BANDS, Engine
from .notion import Notion, NotionError, page_id
from .project import FNV, ProjectError
from .store import AlreadyRunning, Store, UncertainOperation

DEFAULT_STATE = "~/.local/share/fnv-relay"
PATHS = ("game_exe", "checker_dir", "compiler_dir", "function_index", "symbols", "compiler_tips", "wine_prefix", "python")


def parser():
    root = argparse.ArgumentParser(prog="fnv-relay", description="Local FNV/Notion relay (preview)")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state-dir", default=DEFAULT_STATE)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize private local state")
    init.add_argument("--state-dir", default=argparse.SUPPRESS)
    config = commands.add_parser("config", help="Set local input paths and bounded runtime options")
    for name in PATHS:
        config.add_argument("--" + name.replace("_", "-"))
    config.add_argument("--max-candidate-bytes", type=int)
    config.add_argument("--check-timeout-seconds", type=float)
    config.add_argument("--poll-seconds", type=float)
    auth = commands.add_parser("auth").add_subparsers(dest="service", required=True)
    for service in ("fnv", "notion"):
        actions = auth.add_parser(service).add_subparsers(dest="auth_action", required=True)
        actions.add_parser("login")
        if service == "fnv":
            actions.add_parser("connect", help="Approve with GitHub; no existing API key needed").add_argument("--label", default="fnv-relay")
    fetch = commands.add_parser("tools").add_subparsers(dest="tools_action", required=True).add_parser("fetch", help="Download official checker and shared files into a new directory")
    fetch.add_argument("--directory", required=True)
    doctor = commands.add_parser("doctor", help="Read-only checks; no compilation or claims")
    doctor.add_argument("--prepare-wine", action="store_true", help="Explicitly initialize the configured dedicated Wine prefix")
    lane = commands.add_parser("lane").add_subparsers(dest="lane_action", required=True)
    create = lane.add_parser("create", help="Create private Notion lane pages")
    create.add_argument("--parent-page", required=True)
    create.add_argument("--order", choices=("largest", "smallest"), default="largest")
    create.add_argument("--band", choices=BANDS)
    create.add_argument("--address")
    create.add_argument("--min-bytes", type=int, default=0)
    create.add_argument("--max-bytes", type=int, default=2**31 - 1)
    create.add_argument("--label", default="Pro lane")
    lane.add_parser("list")
    lane.add_parser("prompt").add_argument("route")
    serve = commands.add_parser("serve", help="Run live work under your FNV account")
    serve.add_argument("--once", action="store_true")
    commands.add_parser("status", help="Summarize local state without secrets")
    commands.add_parser("stop", help="Drain: stop assigning, finish or release existing work")
    commands.add_parser("reconcile", help="List uncertain writes; never retry or clear them")
    return root


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def clients(store):
    secrets = store.secrets()
    return FNV(secrets.get("fnv_api_key", "")), Notion(secrets.get("notion_token", ""))


def policy(args):
    if not 0 <= args.min_bytes <= args.max_bytes <= 2**31 - 1:
        raise ValueError("Invalid byte bounds")
    if args.address and not re.fullmatch(r"0x[0-9a-fA-F]{1,8}", args.address):
        raise ValueError("Invalid address")
    if not args.label.strip() or len(args.label) > 100 or any(ord(c) < 32 for c in args.label):
        raise ValueError("Invalid label")
    result = {"order": args.order, "min_bytes": args.min_bytes, "max_bytes": args.max_bytes}
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
            paths[name] = str(Path(value).expanduser().resolve())
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
    print("Credential validated and stored privately.")


def host_checks(paths):
    checks = {}
    try:
        compiler._config(paths)
        checks["toolchain_files"] = True
    except Exception:
        checks["toolchain_files"] = False
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
        if secrets.get(secret):
            try:
                operation(factory(secrets[secret]))
                checks[label] = True
            except Exception:
                pass
    output({"checks": checks, "ok": all(checks.values()), "compile_smoke_performed": False})
    return 0 if all(checks.values()) else 1


def summary(lane):
    result = {key: lane[key] for key in ("route_token", "state", "landing") if key in lane}
    result["has_active_lease"] = bool(lane.get("lease"))
    return result


def lane_command(store, args):
    if args.lane_action == "list":
        output([summary(lane) for lane in store.lanes()])
    elif args.lane_action == "prompt":
        lane = store.lane(args.route)
        if not lane or not lane.get("landing"):
            raise ValueError("Unknown or incomplete lane")
        print("Welcome samurai o7. Read https://www.notion.so/" + page_id(lane["landing"]).replace("-", "") + ".")
        print("Register using the exact HELLO record on that landing page for route " + lane["route_token"] + ".")
        print("Read future assignments and results from its outbox. Follow the current assignment's schemas and scope. Keep original context private and use Notion for candidates and messages; the local controller checks and submits. Never request or post credentials. If you cannot access the pages, tell your human.")
    else:
        selected = policy(args)
        parent = page_id(args.parent_page)
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


def serve(store, once=False):
    project, notion = clients(store)
    if not all(host_checks(store.config().get("paths", {})).values()):
        raise ValueError("Toolchain or isolation health check failed")
    project.call("status", {})
    notion.whoami()
    engine = Engine(store, project, notion)
    interval = store.config().get("poll_seconds", 5)
    if not isinstance(interval, (int, float)) or not 1 <= interval <= 60:
        raise ValueError("Invalid poll interval")
    with store.lock("serve"), account_lock(store.secrets().get("fnv_api_key", "")):
        if store.pending_operations():
            raise UncertainOperation("Reconciliation required")
        store.set("drain", False)
        try:
            while True:
                engine.poll()
                if once:
                    break
                if store.get("drain") and not any(row.get("lease") for row in store.lanes()):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            store.set("drain", True)
            print("Drain requested. Run serve again to resume unfinished work; claim watchdogs remain responsible for lease deadlines.")
            return 130
    return 0


def run(args, store):
    if args.command == "init":
        if not (store.root / "config.json").exists():
            store.set_config(store.config())
        print("Private local state initialized.")
    elif args.command == "config":
        configure(store, args)
    elif args.command == "auth":
        with store.lock("serve"):
            if args.service == "fnv" and any(row.get("lease") for row in store.lanes()):
                raise ValueError("Resolve active claims before replacing owner credentials")
            if args.service == "fnv" and args.auth_action == "connect":
                authentication.connect(store, args.label)
            else:
                login(store, args.service)
    elif args.command == "tools":
        output(tools.fetch(store, args.directory))
    elif args.command == "doctor":
        return doctor(store, args.prepare_wine)
    elif args.command == "lane":
        lane_command(store, args)
    elif args.command == "serve":
        return serve(store, args.once)
    elif args.command == "stop":
        store.set("drain", True)
        print("Drain requested; inspect status before closing the worker terminal.")
    elif args.command == "reconcile":
        output({"uncertain_operations": store.pending_operations(), "action": "Review service state before any retry; this command does not mutate or retry operations."})
    elif args.command == "status":
        secrets = store.secrets()
        output({"version": __version__, "drain": bool(store.get("drain")), "credentials_configured": {"fnv": bool(secrets.get("fnv_api_key")), "notion": bool(secrets.get("notion_token"))}, "lanes": [summary(lane) for lane in store.lanes()], "uncertain_operations": len(store.pending_operations())})
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        store = Store(args.state_dir)
        return run(args, store)
    except authentication.ConnectionError as exc:
        print(str(exc), file=sys.stderr)
    except authentication.ConnectionError as exc:
        print(str(exc), file=sys.stderr)
    except AlreadyRunning:
        print("Another relay command holds the worker lock.", file=sys.stderr)
    except UncertainOperation:
        print("An uncertain external operation requires review. Run reconcile; no automatic retry was made.", file=sys.stderr)
    except (ProjectError, NotionError) as exc:
        # Codes and text returned by a service are not trusted terminal output.
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
