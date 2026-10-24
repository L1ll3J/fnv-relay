import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fnv_relay import context, watch
from fnv_relay.project import ProjectError
from fnv_relay.store import Store


def lane(store, expiry):
    directory = store.root / 'claim'
    directory.mkdir()
    value = {'route_token': 'route', 'state': 'assigned', 'lease': {'claim_id': 'claim', 'directory': str(directory), 'expires': dt.datetime.fromtimestamp(expiry, dt.timezone.utc).isoformat()}}
    store.put_lane(value)
    store.set_secret('fnv_api_key', 'private_test_key')
    return value


class Project:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == 'status':
            return {'open_claims': [{'claim_id': 'claim', 'this_key': True}]}
        assert tool == 'giveup'
        if self.fail:
            raise ProjectError(uncertain=True)
        return {'released': True}


def test_guardian_waits_then_releases_owned_lease(tmp_path):
    store = Store(tmp_path)
    lane(store, 1700)
    now = [1000]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    project = Project()
    with patch.object(watch, 'FNV', return_value=project) as factory:
        watch.run(store, 'claim', sleep=sleep, clock=lambda: now[0])
    assert waits == [30, 30, 30, 10]
    assert [call[0] for call in project.calls] == ['status', 'giveup']
    assert store.lane('route')['state'] == 'idle'
    assert factory.call_args.kwargs['base'] == 'https://fnvdecomp.com'
    assert store.pending_operations() == []
    store.close()


def test_guardian_ambiguous_giveup_is_never_repeated(tmp_path):
    store = Store(tmp_path)
    lane(store, 1020)
    now = [1000]
    def sleep(seconds):
        now[0] += seconds
    project = Project(fail=True)
    with patch.object(watch, 'FNV', return_value=project):
        watch.run(store, 'claim', sleep=sleep, clock=lambda: now[0])
    assert len([call for call in project.calls if call[0] == 'giveup']) == 1
    assert store.pending_operations()[0]['status'] == 'uncertain'
    assert now[0] == 1020
    store.close()


@pytest.mark.parametrize('receipt', ['submission-response.json', 'giveup-response.json'])
def test_guardian_exits_on_recorded_completion_without_network(tmp_path, receipt):
    store = Store(tmp_path)
    value = lane(store, 1020)
    (Path(value['lease']['directory']) / receipt).write_text('{}')
    with patch.object(watch, 'FNV') as factory:
        watch.run(store, 'claim', sleep=lambda _: pytest.fail('unexpected wait'), clock=lambda: 1000)
    factory.assert_not_called()
    store.close()


def test_guardian_exits_when_claim_is_no_longer_registered(tmp_path):
    store = Store(tmp_path)
    with patch.object(watch, 'FNV') as factory:
        watch.run(store, 'missing', sleep=lambda _: pytest.fail('unexpected wait'))
    factory.assert_not_called()
    store.close()


def paths(tmp_path):
    return {key: str(tmp_path / key) for key in ['checker_dir', 'function_index', 'symbols', 'game_exe']}


def test_context_uses_fixed_official_exporter_and_isolated_environment(tmp_path, monkeypatch):
    pack = tmp_path / 'pack'
    pack.mkdir()
    malicious = 'REMOTE_INPUT_MUST_NEVER_EXECUTE'
    (pack / 'candidate.cpp').write_text(malicious)
    (pack / 'pack.json').write_text('{}')
    monkeypatch.setenv('PRIVATE_CREDENTIAL', 'must_not_inherit')
    config = paths(tmp_path)
    with patch.object(context.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'complete evidence\r\n', stderr=b'')) as run:
        target = context.export(config, pack, tmp_path / 'evidence')
    args = run.call_args.args[0]
    assert '-I' in args and '-B' in args
    assert args[args.index('-c') + 1] == context._SCRIPT
    assert malicious not in repr(run.call_args)
    env = run.call_args.kwargs['env']
    assert 'PRIVATE_CREDENTIAL' not in env
    assert env['FNVCHECK_TOOLS'] == config['checker_dir']
    assert env['FNVCHECK_TREE'] == str(pack)
    assert target.read_bytes() == b'complete evidence\r\n'
    assert run.call_args.kwargs['timeout'] == 90


def test_context_failure_keeps_stderr_without_publishing_partial_output(tmp_path):
    with patch.object(context.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'partial', stderr=b'tool failed')):
        with pytest.raises(RuntimeError, match='Target export failed'):
            context.export(paths(tmp_path), tmp_path / 'pack', tmp_path / 'evidence')
    assert (tmp_path / 'evidence/target-export.stderr.log').read_bytes() == b'tool failed'
    assert not (tmp_path / 'evidence/target-disassembly.txt').exists()
