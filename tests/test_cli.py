"""Offline CLI contract tests: no project, Notion or model requests."""
import json
import subprocess
import sys
from unittest.mock import Mock
from contextlib import nullcontext

import pytest

from fnv_relay import cli
from fnv_relay.store import Store


def call(tmp_path, *args):
    return cli.main(["--state-dir", str(tmp_path), *args])


def test_init_subcommand_state_and_permissions(tmp_path):
    state = tmp_path / "private"
    assert cli.main(["init", "--state-dir", str(state)]) == 0
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "config.json").stat().st_mode & 0o777 == 0o600


def test_config_invalid_bounds_do_not_persist(tmp_path):
    assert call(tmp_path, "init") == 0
    before = (tmp_path / "config.json").read_bytes()
    for args in (("--max-candidate-bytes", "0"), ("--check-timeout-seconds", "nan"), ("--poll-seconds", "61")):
        assert call(tmp_path, "config", *args) == 1
        assert (tmp_path / "config.json").read_bytes() == before


def test_config_sets_engine_compatible_options(tmp_path):
    assert call(tmp_path, "config", "--game-exe", str(tmp_path / "game.exe"), "--check-timeout-seconds", "60", "--max-candidate-bytes", "4096") == 0
    store = Store(tmp_path)
    try:
        assert store.config()["paths"]["check_timeout_seconds"] == 60
        assert store.config()["max_candidate_bytes"] == 4096
    finally:
        store.close()


def test_auth_validates_before_persisting(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "secret-token")
    client = Mock()
    monkeypatch.setattr(cli, "FNV", Mock(return_value=client))
    assert call(tmp_path, "auth", "fnv", "login") == 0
    client.call.assert_called_once_with("status", {})
    assert json.loads((tmp_path / "secrets.json").read_text())["fnv_api_key"] == "secret-token"
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    client.call.side_effect = RuntimeError("secret-token")
    assert call(tmp_path, "auth", "fnv", "login") == 1


def test_invalid_lane_never_constructs_clients(tmp_path, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(cli, "clients", factory)
    assert call(tmp_path, "lane", "create", "--parent-page", "not-a-uuid", "--min-bytes", "5", "--max-bytes", "4") == 1
    factory.assert_not_called()
    with pytest.raises(SystemExit):
        call(tmp_path, "lane", "create", "--parent-page", "ignored", "--order", "closest")


def test_lane_create_passes_validated_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "clients", lambda store: (Mock(), Mock()))
    monkeypatch.setattr(cli, "host_checks", lambda paths: {"ready": True})
    monkeypatch.setattr(cli, "account_lock", lambda key: nullcontext())
    engine = Mock()
    engine.create_lane.return_value = {"route_token": "route", "state": "waiting_hello"}
    monkeypatch.setattr(cli, "Engine", Mock(return_value=engine))
    parent = "12345678-1234-1234-1234-123456789abc"
    assert call(tmp_path, "lane", "create", "--parent-page", parent, "--address", "0x1234", "--min-bytes", "8", "--max-bytes", "20") == 0
    engine.create_lane.assert_called_once_with(parent, {"order": "largest", "min_bytes": 8, "max_bytes": 20, "address": "0x1234"}, "Pro lane")


def test_status_and_errors_do_not_echo_credentials(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    store.set_secret("fnv_api_key", "secret-not-for-output")
    store.set_secret("notion_token", "secret-not-for-output")
    store.close()
    assert call(tmp_path, "status") == 0
    assert "secret-not-for-output" not in capsys.readouterr().out
    monkeypatch.setattr(cli, "run", Mock(side_effect=RuntimeError("secret-not-for-output")))
    assert call(tmp_path, "status") == 1
    captured = capsys.readouterr()
    assert "secret-not-for-output" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_serve_once_and_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "clients", lambda store: (Mock(), Mock()))
    monkeypatch.setattr(cli, "host_checks", lambda paths: {"ready": True})
    monkeypatch.setattr(cli, "account_lock", lambda key: nullcontext())
    engine = Mock()
    monkeypatch.setattr(cli, "Engine", Mock(return_value=engine))
    assert call(tmp_path, "serve", "--once") == 0
    engine.poll.assert_called_once_with()
    assert call(tmp_path, "stop") == 0
    store = Store(tmp_path)
    try:
        assert store.get("drain") is True
    finally:
        store.close()


def test_uncertain_operation_prevents_serve_retry(tmp_path, monkeypatch):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError):
        store.operation("claim:test", {}, Mock(side_effect=RuntimeError()))
    store.close()
    engine = Mock()
    monkeypatch.setattr(cli, "Engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "clients", lambda store: (Mock(), Mock()))
    monkeypatch.setattr(cli, "host_checks", lambda paths: {"ready": True})
    monkeypatch.setattr(cli, "account_lock", lambda key: nullcontext())
    assert call(tmp_path, "serve", "--once") == 1
    engine.poll.assert_not_called()
    assert call(tmp_path, "reconcile") == 0
    store = Store(tmp_path)
    try:
        assert store.operation_record("claim:test")["status"] == "uncertain"
    finally:
        store.close()


def test_doctor_without_prepare_never_initializes_wine(tmp_path, monkeypatch):
    prepare = Mock()
    monkeypatch.setattr(cli.compiler, "prepare", prepare)
    monkeypatch.setattr(cli, "host_checks", lambda paths: {"toolchain_files": True})
    monkeypatch.setattr(cli, "FNV", Mock())
    monkeypatch.setattr(cli, "Notion", Mock())
    assert call(tmp_path, "doctor") == 1  # Missing stored credentials.
    prepare.assert_not_called()
    assert call(tmp_path, "doctor", "--prepare-wine") == 1
    prepare.assert_called_once_with({})


def test_module_help():
    result = subprocess.run([sys.executable, "-m", "fnv_relay.cli", "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "reconcile" in result.stdout


def test_failed_preflight_prevents_any_live_project_request(tmp_path, monkeypatch):
    project, notion = Mock(), Mock()
    monkeypatch.setattr(cli, "clients", lambda store: (project, notion))
    monkeypatch.setattr(cli, "host_checks", lambda paths: {"dedicated_wine_prefix": False})
    assert call(tmp_path, "serve", "--once") == 1
    project.call.assert_not_called()
    notion.whoami.assert_not_called()


def test_lane_create_allowed_while_daemon_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "clients", lambda store: (Mock(), Mock()))
    engine = Mock()
    engine.create_lane.return_value = {"route_token": "route", "state": "waiting_hello"}
    monkeypatch.setattr(cli, "Engine", Mock(return_value=engine))
    store = Store(tmp_path)
    try:
        with store.lock("serve"):
            assert call(tmp_path, "lane", "create", "--parent-page", "12345678-1234-1234-1234-123456789abc") == 0
    finally:
        store.close()


def test_global_account_lock_blocks_same_key_across_states(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    with cli.account_lock("private-owner-key"):
        with pytest.raises(cli.AlreadyRunning):
            with cli.account_lock("private-owner-key"):
                pytest.fail("Second worker acquired the same account key")
        with cli.account_lock("different-key"):
            pass
    assert not any("private-owner-key" in path.name for path in tmp_path.rglob("*"))


def test_owner_credential_replacement_refused_while_lease_exists(tmp_path, monkeypatch):
    from fnv_relay.store import Store
    from fnv_relay import cli
    store = Store(tmp_path/'state')
    store.put_lane({'route_token':'lane-1','state':'assigned','lease':{'claim_id':'claim-1'}})
    monkeypatch.setattr(cli.authentication,'connect',lambda *args:pytest.fail('must not rotate owner'))
    with pytest.raises(ValueError):
        cli.run(cli.parser().parse_args(['auth','fnv','connect']),store)
    store.close()
