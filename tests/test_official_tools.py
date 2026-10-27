"""No network or compilation: official-data generations and admission preflight."""
import gzip
import io
import json
from pathlib import Path
import tarfile
from contextlib import nullcontext

import pytest

from fnv_relay import cli, official_tools
from fnv_relay.store import Store
from test_engine import env, lane, message, candidate


class Downloads:
    def __init__(self, addresses=('00401000',)):
        self.addresses=list(addresses)
        self.calls=[]
        self.fail=None

    def download_pack(self, url, dest):
        self.calls.append(url)
        if self.fail == dest.name: raise RuntimeError('Download interrupted')
        if dest.name == 'fnvcheck.tar.gz':
            data=io.BytesIO()
            with tarfile.open(fileobj=data,mode='w') as archive:
                raw=b'# official stand-in\n'
                member=tarfile.TarInfo('fnvcheck/fnvcheck.py');member.size=len(raw)
                archive.addfile(member,io.BytesIO(raw))
            dest.write_bytes(gzip.compress(data.getvalue(),mtime=0))
        elif dest.name == 'function_index.json':dest.write_text(json.dumps([{'address':a} for a in self.addresses]))
        elif dest.name == 'fnvdecomp_symbols.json':dest.write_text('{"symbols": []}')
        else:dest.write_text('Official compiler tips\n')


@pytest.fixture
def tool_env(tmp_path):
    store=Store(tmp_path/'state')
    yield store, Downloads(), {'checker_dir':'/old/checker','function_index':'/old/index','symbols':'/old/symbols'}
    store.close()


def test_refresh_validates_and_atomically_publishes_without_changing_config(tool_env):
    store, project, paths=tool_env
    before=store.config()
    result=official_tools.current(store,project,paths,now=100,force=True)
    selected=official_tools.validate(store,result)
    assert official_tools.addresses(selected['function_index'])=={0x401000}
    assert len(project.calls)==5
    assert store.config()==before
    assert store.get(official_tools.CURRENT)==result
    assert not list((store.root/'official-tools').glob('.download-*'))
    # A freshly verified generation is reused for admissions until the refresh deadline.
    assert official_tools.current(store,project,paths,now=101)==result
    assert len(project.calls)==5
    official_tools.current(store,project,paths,now=401)
    assert len(project.calls)==10


def test_failed_refresh_never_replaces_previous_generation(tool_env):
    store, project, paths=tool_env
    first=official_tools.current(store,project,paths,now=100)
    project.fail='fnvdecomp_symbols.json'
    with pytest.raises(RuntimeError):official_tools.current(store,project,paths,now=500)
    assert store.get(official_tools.CURRENT)==first
    official_tools.validate(store,first)
    assert not list((store.root/'official-tools').glob('.download-*'))


@pytest.mark.parametrize('rows',[[],[{}],[{'address':'bad data'}],[{'address':'00401000'},{'address':'0x401000'}]])
def test_invalid_index_is_rejected_before_activation(tool_env,rows):
    store, project, paths=tool_env
    download=project.download_pack
    def malformed(url,dest):
        download(url,dest)
        if dest.name=='function_index.json':dest.write_text(json.dumps(rows))
    project.download_pack=malformed
    with pytest.raises(ValueError):official_tools.current(store,project,paths,now=100)
    assert store.get(official_tools.CURRENT) is None


def test_new_assignment_refreshes_missing_target_and_keeps_old_snapshot_immutable(tool_env):
    store, project, paths=tool_env
    old=official_tools.current(store,project,paths,now=100)
    old_path=Path(official_tools.validate(store,old)['function_index']);raw=old_path.read_bytes()
    project.addresses.append('00b82112')
    selected,new=official_tools.for_target(store,project,paths,{'address':'0x00b82112'},now=101)
    assert new['id']!=old['id']
    assert 0xb82112 in official_tools.addresses(selected['function_index'])
    assert old_path.read_bytes()==raw
    assert len(project.calls)==10


def test_existing_assignment_keeps_snapshot_after_new_activation(tool_env):
    store, project, paths=tool_env
    old=official_tools.current(store,project,paths,now=100)
    project.addresses.append('00b82112')
    current=official_tools.current(store,project,paths,now=101,force=True)
    selected,pinned=official_tools.for_target(store,project,paths,{'address':'0x00401000','tool_snapshot':old},now=1000)
    assert current['id']!=pinned['id'] and pinned==old
    assert 0xb82112 not in official_tools.addresses(selected['function_index'])
    assert len(project.calls)==10


def test_still_missing_target_blocks_and_refresh_is_rate_limited(tool_env):
    store, project, paths=tool_env
    official_tools.current(store,project,paths,now=100)
    for now in (101,102):
        with pytest.raises(official_tools.TargetIndexUnavailable):
            official_tools.for_target(store,project,paths,{'address':'0x00b82112'},now=now)
    assert len(project.calls)==10
    project.addresses.append('00b82112')
    selected,_=official_tools.for_target(store,project,paths,{'address':'0x00b82112'},now=402)
    assert 0xb82112 in official_tools.addresses(selected['function_index'])


def test_tampered_generation_is_not_silently_used(tool_env):
    store,project,paths=tool_env
    snapshot=official_tools.current(store,project,paths,now=100)
    selected=official_tools.validate(store,snapshot)
    Path(selected['function_index']).write_text('[{"address":"00b82112"}]')
    with pytest.raises(ValueError,match='snapshot file changed'):
        official_tools.for_target(store,project,paths,{'address':'0x00401000','tool_snapshot':snapshot},now=101)


def install_downloads(env,monkeypatch,addresses):
    paths={'checker_dir':'/old/checker','function_index':'/old/index','symbols':'/old/symbols'}
    config=env.store.config();config['paths']=paths;env.store.set_config(config)
    project=Downloads(addresses)
    pack=env.project.download_pack
    def download(url,dest):
        if dest.name=='pack.tar.gz':return pack(url,dest)
        return project.download_pack(url,dest)
    monkeypatch.setattr(env.project,'download_pack',download)
    return project


def test_missing_target_is_blocked_before_assignment_or_attempt_count(env,monkeypatch):
    project=install_downloads(env,monkeypatch,['00402000'])
    env.notion.append(lane(env)['inbox'],message(env,'HELLO','hello'))
    env.engine.poll()
    current=lane(env)
    assert current['state']=='preparing'
    assert env.checker.calls==[]
    assert not env.store.get('checks:'+current['lease']['claim_id'])
    assert env.store.get('last_error:'+env.route)['type']=='TargetIndexUnavailable'
    project.addresses.append('00401000')
    env.engine.clock=lambda:env.project.now+301
    env.engine.poll()
    current=lane(env)
    assert current['state']=='assigned' and current['lease']['tool_snapshot']
    assert env.project.claims==1


def test_preflight_handles_old_running_assignment_before_any_compilation(env,monkeypatch):
    from test_engine import assign
    current=assign(env)
    install_downloads(env,monkeypatch,['00402000'])
    pointer=candidate(env,'blocked','int target() { return 1; }')
    env.notion.append(current['inbox'],message(env,'BATCH','blocked-batch',candidates=[pointer]))
    env.engine.poll()
    assert not env.checker.calls
    assert not env.store.get('checks:'+current['lease']['claim_id'])
    assert not any(e['kind']=='batch_attempt_started' for e in env.store.events())


def test_download_failure_prevents_claim_acquisition(env,monkeypatch):
    project=install_downloads(env,monkeypatch,['00401000']);project.fail='function_index.json'
    env.notion.append(lane(env)['inbox'],message(env,'HELLO','hello'))
    env.engine.poll()
    assert env.project.claims==0 and not lane(env).get('lease')


def test_diagnostic_and_plain_checks_use_same_pinned_generation(env,monkeypatch):
    install_downloads(env,monkeypatch,['00401000'])
    env.notion.append(lane(env)['inbox'],message(env,'HELLO','hello'));env.engine.poll()
    current=lane(env);snapshot=current['lease']['tool_snapshot']
    selected=official_tools.validate(env.store,snapshot)
    calls=[];checker=env.checker
    def check(config,*args,**kwargs):
        calls.append(config)
        return checker(config,*args,**kwargs)
    env.engine.checker=check
    pointer=candidate(env,'winner','int target() { return 42; }')
    env.notion.append(current['inbox'],message(env,'BATCH','winner-batch',candidates=[pointer]));env.engine.poll()
    assert len(calls)==2
    assert all(c['function_index']==selected['function_index'] for c in calls)
    assert all(c['checker_dir']==selected['checker_dir'] for c in calls)


def test_start_refreshes_even_with_a_recent_saved_snapshot(env,monkeypatch):
    project=install_downloads(env,monkeypatch,['00401000'])
    official_tools.current(env.store,env.project,env.store.config()['paths'])
    monkeypatch.setattr(cli,'clients',lambda store:(env.project,env.notion))
    monkeypatch.setattr(cli,'host_checks',lambda paths:{'offline':True})
    monkeypatch.setattr(cli,'account_lock',lambda key:nullcontext())
    monkeypatch.setattr(env.project,'set_stop_event',lambda event:None,raising=False)
    monkeypatch.setattr(env.notion,'whoami',lambda:{},raising=False)
    assert cli.serve(env.store,once=True)==0
    assert len(project.calls)==10
    assert env.project.claims==0


def test_old_generation_cache_is_preserved_but_not_reused_for_new_batch(env,monkeypatch):
    from test_engine import assign
    from fnv_relay.engine import sha
    from fnv_relay.dashboard import Dashboard
    current=assign(env)
    raw='int target() { return 1; }'
    pointer=candidate(env,'first',raw)
    env.notion.append(current['inbox'],message(env,'BATCH','first-batch',candidates=[pointer]))
    env.engine.poll()
    base='check:'+current['lease']['claim_id']+':'+sha(raw.encode())
    original=env.store.get(base)
    assert len(env.checker.calls)==1
    install_downloads(env,monkeypatch,['00401000'])
    pointer=candidate(env,'second',raw)
    env.notion.append(current['inbox'],message(env,'BATCH','new-generation-batch',candidates=[pointer]))
    env.engine.poll()
    assert len(env.checker.calls)==2
    assert env.store.get(base+':legacy')==original
    result=env.store.get(base)['result']
    assert result['tool_snapshot_id']==lane(env)['lease']['tool_snapshot']['id']
    assert env.store.get(base+':tools:'+result['tool_snapshot_id'])['result']==result
    pointer=candidate(env,'third',raw)
    env.notion.append(current['inbox'],message(env,'BATCH','same-generation-batch',candidates=[pointer]))
    env.engine.poll()
    assert len(env.checker.calls)==2
    model=Dashboard(env.store);model.update()
    assert len(model.checks)==2
