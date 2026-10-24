"""Offline onboarding tests; never contact FNV or consume model API credits."""
import io
import json
from pathlib import Path
import tarfile
from urllib.error import URLError

import pytest

from fnv_relay import authentication, cli, tools
from fnv_relay.engine import sha
from fnv_relay.store import Store


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def initial(**changes):
    return {"device_code": "secret-device-123456", "user_code": "ABCD-1234", "approve_url": "https://fnvdecomp.com/account?code=ABCD-1234", "interval": 5, "expires_in": 900, **changes}


def test_device_flow_polls_at_interval_and_stores_without_exposing_secrets(tmp_path):
    store = Store(tmp_path)
    clock, shown, requests = Clock(), [], []
    responses = [initial(), URLError("secret-device-123456"), {"status": "pending"}, {"status": "approved", "api_key": "secret-api-key-123456"}]

    def opener(request, timeout):
        requests.append((request.full_url, json.loads(request.data), timeout))
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())

    try:
        authentication.connect(store, "my relay", opener=opener, sleep=clock.sleep, clock=clock, show=shown.append)
        assert store.secrets()["fnv_api_key"] == "secret-api-key-123456"
        assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
        assert clock.sleeps == [5, 5, 5]
        assert requests[0][1] == {"agent_label": "my relay"}
        assert all(row[1] == {"device_code": "secret-device-123456"} for row in requests[1:])
        assert "ABCD-1234" in "\n".join(shown)
        assert "secret-device" not in "\n".join(shown)
        assert "secret-api-key" not in "\n".join(shown)
        assert all(0 < row[2] <= 30 for row in requests)
    finally:
        store.close()


def test_device_flow_rejects_unsafe_or_secret_bearing_approval_urls(tmp_path):
    store = Store(tmp_path)
    try:
        for url in ("https://evil.example/approve", "http://fnvdecomp.com/approve", "https://fnvdecomp.com/approve?device=secret-device-123456", "https://fnvdecomp.com/approve\nsecret"):
            shown = []
            with pytest.raises(authentication.ConnectionError):
                authentication.connect(store, opener=lambda *args, **kwargs: io.BytesIO(json.dumps(initial(approve_url=url)).encode()), show=shown.append)
            assert shown == []
        assert store.secrets() == {}
    finally:
        store.close()


def test_initial_connect_failure_is_never_retried_and_expiry_stops_polling(tmp_path):
    store = Store(tmp_path)
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        raise URLError("secret-text")

    try:
        with pytest.raises(authentication.ConnectionError, match="not automatically retried"):
            authentication.connect(store, opener=failed)
        assert len(calls) == 1
        clock = Clock()
        with pytest.raises(authentication.ConnectionError, match="expired"):
            authentication.connect(store, opener=lambda *args, **kwargs: io.BytesIO(json.dumps(initial(expires_in=4)).encode()), sleep=clock.sleep, clock=clock, show=lambda text: None)
        assert clock.sleeps == []
        assert store.secrets() == {}
    finally:
        store.close()


class Downloads:
    def __init__(self, active=False):
        self.downloads = []
        self.active = active

    def call(self, tool, args):
        assert tool == "status"
        return {"open_claims": [{"this_key": True}] if self.active else []}

    def download_pack(self, url, dest):
        self.downloads.append(url)
        if dest.name == "fnvcheck.tar.gz":
            with tarfile.open(dest, "w:gz") as archive:
                raw = b"# official checker test stand-in"
                member = tarfile.TarInfo("fnvcheck/fnvcheck.py")
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        else:
            dest.write_text("{}")
        return sha(dest.read_bytes())


def test_tools_fetch_preserves_nested_layout_and_configuration(tmp_path):
    store = Store(tmp_path / "state")
    config = {"schema": 1, "paths": {"checker_dir": "/existing/checker"}}
    store.set_config(config)
    project = Downloads()
    try:
        result = tools.fetch(store, tmp_path / "new-tools", project=project)
        assert len(project.downloads) == 4
        assert all(url.startswith("https://fnvdecomp.com/pack-data/") for url in project.downloads)
        assert Path(result["suggested_paths"]["checker_dir"], "fnvcheck.py").is_file()
        assert len(result["sha256"]) == 4
        assert store.config() == config
        assert result["configuration_changed"] is False
        assert json.loads((tmp_path / "new-tools/download-manifest.json").read_text()) == result["sha256"]
        assert cli.parser().parse_args(["auth", "fnv", "connect", "--label", "newcomer"]).label == "newcomer"
        assert cli.parser().parse_args(["tools", "fetch", "--directory", "new"]).directory == "new"
    finally:
        store.close()


def test_tools_fetch_refuses_existing_files_and_active_owner_claims(tmp_path):
    store = Store(tmp_path / "state")
    project = Downloads()
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "user-file").write_text("keep")
    try:
        with pytest.raises(ValueError):
            tools.fetch(store, existing, project=project)
        assert (existing / "user-file").read_text() == "keep"
        assert project.downloads == []
        active = Downloads(active=True)
        with pytest.raises(ValueError):
            tools.fetch(store, tmp_path / "active-refused", project=active)
        assert active.downloads == []
        assert not (tmp_path / "active-refused").exists()
        store.put_lane({"route_token": "route", "lease": {"claim_id": "local-active"}})
        with pytest.raises(ValueError):
            tools.fetch(store, tmp_path / "local-refused", project=project)
        assert project.downloads == []
    finally:
        store.close()
