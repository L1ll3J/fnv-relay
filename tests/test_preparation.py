"""Real threads and SQLite, fake services: overlap, isolation and ordered shutdown."""
import threading
import time

import pytest

from fnv_relay import preparation
from fnv_relay.engine import Engine, PROTOCOL, sha
from fnv_relay.store import Store
from test_engine import env, lane, message


def preparing(store,route):
    record={'route_token':route,'state':'preparing','policy':{},'lease':{'claim_id':'claim-'+route}}
    store.put_lane(record)
    return record


def finish(pool,timeout=5):
    deadline=time.monotonic()+timeout
    while pool.jobs and time.monotonic()<deadline:
        pool.reap()
        if pool.jobs:threading.Event().wait(.01)
    assert not pool.jobs


def test_preparations_overlap_with_private_connections_and_bounded_admission(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');rows=[preparing(store,str(i)) for i in range(3)]
    barrier=threading.Barrier(2);release=threading.Event();started=threading.Event()
    threads=[];connections=[];clients=[];errors=[]
    def make_clients(state):
        clients.append(object())
        return clients[-1],None
    def prepare(engine,row):
        threads.append(threading.get_ident());connections.append(engine.store.db)
        engine.store.set('thread:'+row['route_token'],threading.get_ident())
        barrier.wait(timeout=5);started.set()
        assert release.wait(5)
    monkeypatch.setattr(Engine,'prepare_assignment',prepare)
    pool=preparation.PreparationPool(store.root,make_clients,lambda:False,lambda *e:errors.append(e),limit=2)
    try:
        assert pool.submit(rows[0]) and pool.submit(rows[1])
        assert started.wait(5), 'Two preparation jobs must reach the barrier concurrently'
        assert not pool.submit(rows[0])  # No duplicate job for a lane.
        assert not pool.submit(rows[2])  # No unbounded queue of owned claims.
        assert not pool.available(rows[2]['route_token'])
        assert len(set(threads))==2 and threading.get_ident() not in threads
        assert len({id(c) for c in connections})==2
        assert store.get('thread:0') in threads and store.get('thread:1') in threads
        assert len(clients)==2
        release.set();finish(pool)
        assert errors==[]
    finally:release.set();pool.close();store.close()


def test_default_prepares_all_lanes_and_new_lanes_without_waiting_for_a_slot(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');rows=[preparing(store,str(i)) for i in range(12)]
    started=threading.Barrier(13);added_started=threading.Event();release=threading.Event();errors=[]
    def prepare(engine,row):
        if row['route_token']=='added':added_started.set()
        else:started.wait(timeout=10)
        assert release.wait(10)
    monkeypatch.setattr(Engine,'prepare_assignment',prepare)
    pool=preparation.PreparationPool(store.root,lambda s:(None,None),lambda:False,lambda *e:errors.append(e))
    try:
        assert all(pool.submit(row) for row in rows)
        started.wait(timeout=10)
        assert len(pool.jobs)==12 and not pool.submit(rows[0])
        added=preparing(store,'added')
        assert pool.submit(added) and added_started.wait(5)
        assert len(pool.jobs)==13
        release.set();finish(pool)
        assert not errors and not pool.lane_executors
    finally:release.set();pool.close();store.close()


def test_errors_are_reaped_on_controller_thread_and_retry_same_scope(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');row=preparing(store,'one')
    parent=Engine(store,None,None,clock=lambda:100,watchdog=False)
    callbacks=[]
    def failed(*args):raise RuntimeError('Readback failed')
    def record(*args):
        callbacks.append(threading.get_ident());parent.preparation_failed(*args)
    monkeypatch.setattr(Engine,'prepare_assignment',failed)
    pool=preparation.PreparationPool(store.root,lambda s:(None,None),lambda:False,record)
    try:
        pool.submit(row);finish(pool)
        assert callbacks==[threading.get_ident()]
        assert store.lane('one')['preparation_retry_at']==130
        assert store.lane('one')['lease']['claim_id']=='claim-one'
        assert store.get('last_error:one')['message']=='Readback failed'
    finally:pool.close();store.close()


def test_late_failure_does_not_overwrite_replacement_scope(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');row=preparing(store,'one')
    parent=Engine(store,None,None,watchdog=False);entered=threading.Event();release=threading.Event()
    def fail(*args):
        entered.set();assert release.wait(5);raise RuntimeError('Old request failed')
    monkeypatch.setattr(Engine,'prepare_assignment',fail)
    pool=preparation.PreparationPool(store.root,lambda s:(None,None),lambda:False,parent.preparation_failed)
    try:
        pool.submit(row);assert entered.wait(5)
        replacement=dict(row,lease={'claim_id':'replacement'},state='assigned');store.put_lane(replacement)
        release.set();finish(pool)
        assert store.lane('one')==replacement
        assert not store.get('last_error:one')
    finally:release.set();pool.close();store.close()


def test_close_waits_for_running_preparation_and_refuses_more_jobs(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');row=preparing(store,'one');entered=threading.Event();done=threading.Event()
    def prepare(engine,row):
        entered.set()
        while not engine.stop_requested():threading.Event().wait(.01)
        engine.store.set('finished',True);done.set()
    monkeypatch.setattr(Engine,'prepare_assignment',prepare)
    pool=preparation.PreparationPool(store.root,lambda s:(None,None),lambda:False,lambda *a:None)
    try:
        pool.submit(row);assert entered.wait(5)
        pool.close()
        assert done.is_set() and store.get('finished')
        assert not pool.submit(row)
        assert not pool.jobs
    finally:pool.close();store.close()


def test_preflight_never_restores_a_lease_released_during_download(env,monkeypatch):
    current=lane(env);current.update(state='preparing',lease={'claim_id':'old','address':'0x401000'})
    env.store.put_lane(current)
    def changed(*args,**kwargs):
        saved=env.store.lane(env.route);saved.pop('lease');saved['state']='idle';env.store.put_lane(saved)
        return {},{'id':'snapshot'}
    monkeypatch.setattr('fnv_relay.official_tools.for_target',changed)
    with pytest.raises(ValueError,match='Claim changed during tool preflight'):env.engine.tool_paths(current)
    assert 'lease' not in lane(env)


def test_full_preparation_pool_prevents_another_claim(env):
    class Full:
        def available(self,route):return False
    current=lane(env);current.update(state='idle',ready=True);env.store.put_lane(current)
    env.engine.preparation=Full()
    env.engine.assign(current)
    assert env.project.claims==0 and env.project.calls==[]


def test_working_lane_is_checked_on_controller_while_another_prepares(env,monkeypatch):
    second=env.engine.create_lane('parent',{'order':'smallest'},'Working')
    env.notion.append(second['inbox'],{'protocol':PROTOCOL,'kind':'HELLO','sender':'chatgpt','route_token':second['route_token'],'message_id':'hello-second'})
    env.engine.poll();second=env.store.lane(second['route_token']);assert second['state']=='assigned'
    entered=threading.Event();release=threading.Event()
    def slow(*args):entered.set();assert release.wait(5)
    monkeypatch.setattr(Engine,'prepare_assignment',slow)
    pool=preparation.PreparationPool(env.store.root,lambda s:(None,None),lambda:False,env.engine.preparation_failed)
    env.engine.preparation=pool
    try:
        env.notion.append(lane(env)['inbox'],message(env,'HELLO','hello-first'));env.engine.poll()
        assert entered.wait(5) and lane(env)['state']=='preparing'
        raw=b'int target() { return 1; }';lease=second['lease']
        scope={'protocol':PROTOCOL,'sender':'chatgpt','route_token':second['route_token'],'claim_id':lease['claim_id'],'pack_sha256':lease['pack_sha256']}
        record={**scope,'kind':'CANDIDATE','candidate_id':'candidate','files':[{'filename':'candidate.cpp','source_utf8':raw.decode(),'source_sha256':sha(raw),'source_bytes':len(raw)}]}
        page=env.notion.create_page(second['data'],'candidate',[record])
        env.notion.append(second['inbox'],{**scope,'kind':'BATCH','message_id':'working-batch','candidates':[{'candidate_id':'candidate','page':page}]})
        env.engine.poll()
        assert len(env.checker.calls)==1
        assert lane(env)['state']=='preparing'
    finally:release.set();pool.close();env.engine.preparation=None
