"""Private local state and durable, fail-closed mutation journal."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def write_json(path, data, mode=0o600):
    path = Path(path)
    raw = (_json(data) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".state-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class UncertainOperation(RuntimeError):
    """An earlier attempt may have reached the service; reconcile before any retry."""


class AlreadyRunning(RuntimeError):
    pass


class OperationNotStarted(RuntimeError):
    """A local prerequisite failed before an external operation was sent."""



class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._locks = self.root / "locks"
        self._locks.mkdir(mode=0o700, exist_ok=True)
        self._locks.chmod(0o700)
        database = self.root / "state.sqlite3"
        fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(database, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS lanes(route_token TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, args_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        """)

    def close(self):
        self.db.close()

    def config(self) -> dict:
        value = read_json(self.root / "config.json", {"schema": 1, "paths": {}, "policy": {}})
        if not isinstance(value, dict):
            raise ValueError("Configuration must be a JSON object")
        return value

    def set_config(self, config: dict):
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a JSON object")
        write_json(self.root / "config.json", config)

    def secrets(self) -> dict:
        value = read_json(self.root / "secrets.json", {})
        if not isinstance(value, dict):
            raise ValueError("Secrets must be a JSON object")
        return value

    def set_secret(self, name, value):
        with self.lock("secrets"):
            secrets = self.secrets()
            secrets[name] = value
            write_json(self.root / "secrets.json", secrets)

    def lanes(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM lanes ORDER BY route_token")]

    def lane(self, token):
        row = self.db.execute("SELECT data FROM lanes WHERE route_token=?", (token,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_lane(self, row):
        token = row["route_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("A route token is required")
        self.db.execute("INSERT INTO lanes VALUES (?,?) ON CONFLICT(route_token) DO UPDATE SET data=excluded.data", (token, _json(row)))

    def get(self, key):
        row = self.db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key, value):
        self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, _json(value)))

    def event(self, kind, data):
        self.db.execute("INSERT INTO events(created,kind,data) VALUES (?,?,?)", (time.time(), kind, _json(data)))

    def events(self, limit=20):
        """Recent local events; callers choose which fields to display publicly."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Event limit must be 1..200")
        rows = self.db.execute("SELECT id,created,kind,data FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id":row[0], "created":row[1], "kind":row[2], "data":json.loads(row[3])} for row in reversed(rows)]

    def operation_record(self, key):
        row = self.db.execute("SELECT key,args_hash,status,result,created,updated FROM operations WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        record = dict(zip(("key", "args_hash", "status", "result", "created", "updated"), row))
        if record["result"] is not None:
            record["result"] = json.loads(record["result"])
        return record

    def pending_operations(self):
        pending = []
        for row in self.db.execute("SELECT key,args_hash,status,created,updated FROM operations WHERE status NOT IN ('response','not_sent') ORDER BY created"):
            key, digest = row[:2]
            if key.startswith('claim:') and (self.get('claim-resolution:'+key) or {}).get('args_hash') == digest:
                continue
            if key.startswith(('create:','landing:')) and (self.get('lane-setup-resolution:'+key) or {}).get('args_hash') == digest:
                continue
            pending.append(dict(zip(('key','args_hash','status','created','updated'), row)))
        return pending

    def operation(self, key, args, fn):
        args_hash = hashlib.sha256(_json(args).encode("utf-8")).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.operation_record(key)
            if existing:
                if existing["args_hash"] != args_hash:
                    raise ValueError("Operation key is already bound to different arguments")
                if existing["status"] == "response":
                    self.db.execute("COMMIT")
                    return existing["result"]
                if existing['status'] != 'not_sent':
                    raise UncertainOperation(f"Operation {key!r} requires reconciliation")
            now = time.time()
            if existing:
                self.db.execute("UPDATE operations SET status='intent',updated=? WHERE key=?", (now,key))
            else:
                self.db.execute("INSERT INTO operations VALUES (?,?, 'intent', NULL,?,?)", (key, args_hash, now, now))
            # FULL synchronous commit durably writes intent before the network request.
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        try:
            result = fn()
            encoded = _json(result)
            self.db.execute("UPDATE operations SET status='response',result=?,updated=? WHERE key=?", (encoded, time.time(), key))
            return result
        except OperationNotStarted:
            self.db.execute("UPDATE operations SET status='not_sent',updated=? WHERE key=?", (time.time(), key))
            raise
        except BaseException:
            self.db.execute("UPDATE operations SET status='uncertain',updated=? WHERE key=?", (time.time(), key))
            raise

    @contextmanager
    def lock(self, name, *, timeout=0):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name):
            raise ValueError("Invalid lock name")
        fd = os.open(self._locks / (name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AlreadyRunning(f"Another relay process holds {name!r}") from None
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
