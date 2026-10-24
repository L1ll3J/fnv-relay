import hashlib
import io
import json
import os
from pathlib import Path
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from fnv_relay.project import FNV, ProjectError, USER_AGENT, _SameOrigin
from fnv_relay.store import AlreadyRunning, Store, UncertainOperation, read_json, write_json


class Reply(io.BytesIO):
    def __init__(self, content, url="https://fnvdecomp.com/pack.zip", headers=None):
        super().__init__(content)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class Server:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.sleeps = []

    def __call__(self, request, timeout):
        assert request.get_header("User-agent") == USER_AGENT
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return Reply(result)
        return Reply(json.dumps(result).encode())

    def client(self):
        return FNV("private_key", opener=self, sleep=self.sleeps.append)


def error(status, body=None, headers=None):
    return HTTPError("https://fnvdecomp.com/api/v1/status", status, "private_key", headers or {}, Reply(json.dumps(body or {}).encode()))


def test_read_retries_rate_limit_and_uses_auth():
    server = Server(error(429, headers={"Retry-After": "8"}), {"claimed": []})
    assert server.client().call("status", {}) == {"claimed": []}
    assert server.sleeps == [8]
    assert server.calls[0].get_header("Authorization") == "Bearer private_key"
    assert server.calls[0].data == server.calls[1].data


@pytest.mark.parametrize("failure", [error(503), URLError("private_key"), TimeoutError("private_key")])
def test_mutations_never_retry_ambiguous_failure(failure):
    server = Server(failure)
    with pytest.raises(ProjectError) as caught:
        server.client().call("submit", {"claim_id": "mine"})
    assert caught.value.uncertain
    assert "private_key" not in str(caught.value)
    assert len(server.calls) == 1


def test_definite_mutation_rate_rejection_not_auto_retried():
    server = Server(error(429, {"code": "rate_limited"}, {"Retry-After": "12"}))
    with pytest.raises(ProjectError) as caught:
        server.client().call("claim", {"band": "A"})
    assert not caught.value.uncertain
    assert caught.value.retry_after == 12
    assert len(server.calls) == 1


def test_error_body_cannot_echo_credentials():
    server = Server(error(401, {"message": "private_key", "code": "private_key"}))
    with pytest.raises(ProjectError) as caught:
        server.client().call("status")
    assert "private_key" not in str(caught.value)


def test_read_retries_bounded():
    server = Server(*(error(503) for _ in range(4)))
    with pytest.raises(ProjectError):
        server.client().call("packet", {"function": "f"})
    assert len(server.calls) == 4


def test_pack_atomic_hash_relative_url(tmp_path):
    content = b"pack contents\x00"
    server = Server(content)
    dest = tmp_path / "pack.zip"
    dest.write_bytes(b"old")
    digest = server.client().download_pack("/pack.zip", dest)
    assert digest == hashlib.sha256(content).hexdigest()
    assert dest.read_bytes() == content
    assert server.calls[0].full_url == "https://fnvdecomp.com/pack.zip"
    assert server.calls[0].get_header("Authorization") == "Bearer private_key"
    assert not list(tmp_path.glob(".pack-*"))


def test_pack_rejects_foreign_origin_before_sending_key(tmp_path):
    server = Server()
    with pytest.raises(ValueError):
        server.client().download_pack("https://foreign.example/pack.zip", tmp_path / "pack")
    assert server.calls == []
    handler = _SameOrigin(("https", "fnvdecomp.com", 443))
    request = Request("https://fnvdecomp.com/pack.zip", headers={"Authorization": "Bearer private_key"})
    with pytest.raises(HTTPError):
        handler.redirect_request(request, None, 302, "Moved", {}, "https://foreign.example/pack")


def test_pack_limit_and_incomplete_leave_existing_file(tmp_path, monkeypatch):
    import fnv_relay.project as project
    monkeypatch.setattr(project, "MAX_PACK", 5)
    dest = tmp_path / "pack"
    dest.write_bytes(b"old")
    with pytest.raises(ProjectError, match="pack_too_large"):
        Server(b"too large").client().download_pack("/pack", dest)
    assert dest.read_bytes() == b"old"
    assert not list(tmp_path.glob(".pack-*"))
    response = Reply(b"a", headers={"Content-Length": "2"})
    with pytest.raises(ProjectError, match="incomplete_pack"):
        FNV("private_key", opener=lambda *a, **kw: response).download_pack("/pack", dest)
    assert dest.read_bytes() == b"old"


def test_store_paths_permissions_and_secret_separation(tmp_path):
    root = tmp_path / "custom-root"
    store = Store(root)
    assert store.config() == {"schema": 1, "paths": {}, "policy": {}}
    store.set_config({"schema": 1, "paths": {"checkout": "/custom/repo"}})
    store.set_secret("project_key", "never_in_sqlite")
    assert store.secrets() == {"project_key": "never_in_sqlite"}
    assert store.config()["paths"]["checkout"] == "/custom/repo"
    assert root.stat().st_mode & 0o777 == 0o700
    for name in ("config.json", "secrets.json", "state.sqlite3"):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    store.close()
    assert b"never_in_sqlite" not in (root / "state.sqlite3").read_bytes()


def test_lane_kv_events_and_json_unicode(tmp_path):
    store = Store(tmp_path)
    row = {"route_token": "lane-a", "data": "é\r\n"}
    store.put_lane(row)
    assert store.lane("lane-a") == row
    assert store.lanes() == [row]
    row["data"] = "new"
    store.put_lane(row)
    assert store.lanes() == [row]
    store.set("pending", {"a": 1})
    assert store.get("pending") == {"a": 1}
    assert store.get("missing") is None
    store.event("test", {"safe": True})
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    path = tmp_path / "separate.json"
    write_json(path, {"unicode": "😀\r\n"})
    assert read_json(path) == {"unicode": "😀\r\n"}
    store.close()


def test_operation_replay_and_argument_binding(tmp_path):
    store = Store(tmp_path)
    calls = []
    result = store.operation("submit-one", {"b": 2, "a": 1}, lambda: calls.append(1) or {"ok": True})
    assert result == {"ok": True}
    store.close()
    store = Store(tmp_path)
    assert store.operation("submit-one", {"a": 1, "b": 2}, lambda: calls.append(2)) == result
    assert calls == [1]
    with pytest.raises(ValueError, match="different arguments"):
        store.operation("submit-one", {"a": 3}, lambda: None)
    assert store.pending_operations() == []
    assert "args" not in store.operation_record("submit-one")
    store.close()


def test_operation_intent_visible_before_call_and_failure_never_replays(tmp_path):
    store = Store(tmp_path)
    other = Store(tmp_path)
    def failed():
        assert other.operation_record("claim") ["status"] == "intent"
        with pytest.raises(UncertainOperation):
            other.operation("claim", {"id": 1}, lambda: pytest.fail("duplicate write"))
        raise TimeoutError("service may have accepted")
    with pytest.raises(TimeoutError):
        store.operation("claim", {"id": 1}, failed)
    assert store.pending_operations()[0]["status"] == "uncertain"
    with pytest.raises(UncertainOperation):
        other.operation("claim", {"id": 1}, lambda: pytest.fail("duplicate write"))
    store.close()
    other.close()


def test_same_operation_race_sends_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    errors = []
    def worker():
        store = Store(tmp_path)
        try:
            store.operation("one", {}, lambda: entered.set() or release.wait(3) or {})
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()
    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(3)
    other = Store(tmp_path)
    try:
        with pytest.raises(UncertainOperation):
            other.operation("one", {}, lambda: pytest.fail("duplicate write"))
    finally:
        release.set()
        thread.join(3)
        other.close()
    assert not thread.is_alive()
    assert errors == []


def test_nonblocking_process_lock(tmp_path):
    one, two = Store(tmp_path), Store(tmp_path)
    with one.lock("serve"):
        with pytest.raises(AlreadyRunning):
            with two.lock("serve"):
                pytest.fail("second owner")
    with two.lock("serve"):
        pass
    with pytest.raises(ValueError):
        with one.lock("../escape"):
            pass
    one.close()
    two.close()


def test_event_log_is_bounded_and_chronological(tmp_path):
    from fnv_relay.store import Store
    store = Store(tmp_path/'events')
    for i in range(5):
        store.event('fixture', {'value':i})
    assert [row['data']['value'] for row in store.events(2)] == [3,4]
    with pytest.raises(ValueError):
        store.events(201)
    store.close()
