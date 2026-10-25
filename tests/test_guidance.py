"""Offline durability checks for the individually approved landing addition."""
from unittest.mock import Mock

import pytest

from fnv_relay.engine import Engine
from fnv_relay.guidance import WORK_STYLE, publication, recoverable
from fnv_relay.managed import MenuWorker
from fnv_relay.store import Store, UncertainOperation
from test_engine import FakeNotion


def fixture(tmp_path):
    store=Store(tmp_path)
    lane={'route_token':'route','landing':'landing','state':'assigned','policy':{},'lease':{'claim_id':'keep-current-claim'}}
    store.put_lane(lane)
    notion=FakeNotion();notion.pages['landing']=[{'kind':'LANE','instructions':'Existing approved instructions'}]
    return store,lane,notion,Engine(store,None,notion,watchdog=False)


def test_guidance_appends_once_without_changing_registration_or_current_claim(tmp_path):
    store,lane,notion,engine=fixture(tmp_path)
    original=notion.messages('landing')[0]
    engine.ensure_work_guidance(lane);engine.ensure_work_guidance(lane)
    assert notion.appends==1
    assert notion.messages('landing')==[original,publication(lane)[1]['record']]
    assert notion.messages('landing')[1]['instructions']==WORK_STYLE
    assert 'as a /goal:' in WORK_STYLE and 'time.sleep(30)' in WORK_STYLE
    assert store.lane('route')==lane
    assert store.pending_operations()==[]
    store.close()


def test_guidance_unknown_ack_uses_exact_readback_without_replaying_append(tmp_path):
    store,lane,notion,engine=fixture(tmp_path)
    append=notion.append
    def lost_reply(page,record):
        append(page,record);raise TimeoutError('unknown reply')
    notion.append=lost_reply
    with pytest.raises(TimeoutError):engine.ensure_work_guidance(lane)
    key,_=publication(lane);original=store.operation_record(key)
    assert [op['key'] for op in recoverable(store)]==[key]
    engine.ensure_work_guidance(lane)
    assert notion.appends==1
    assert store.pending_operations()==[]
    assert store.operation_record(key)==original
    assert original['status']=='uncertain'
    store.close()


def test_guidance_absent_after_unknown_write_requires_review_and_never_auto_retries(tmp_path):
    store,lane,notion,engine=fixture(tmp_path)
    notion.append=Mock(side_effect=TimeoutError('unknown reply'))
    with pytest.raises(TimeoutError):engine.ensure_work_guidance(lane)
    key,_=publication(lane)
    with pytest.raises(UncertainOperation):engine.ensure_work_guidance(lane)
    assert notion.append.call_count==1
    assert store.get('guidance-confirmed:'+key) is None
    assert len(store.pending_operations())==1
    store.close()


@pytest.mark.parametrize('unrelated',[False,True])
def test_menu_allows_only_exact_guidance_intent_readback_on_startup(tmp_path,monkeypatch,unrelated):
    from fnv_relay import managed
    store,lane,notion,engine=fixture(tmp_path)
    notion.append=Mock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):engine.ensure_work_guidance(lane)
    lane.pop('lease');store.put_lane(lane)
    if unrelated:
        with pytest.raises(TimeoutError):store.operation('notion-append:other',{},Mock(side_effect=TimeoutError()))
    child=Mock();child.pid=987654321;child.poll.return_value=None
    popen=Mock(return_value=child);monkeypatch.setattr(managed.subprocess,'Popen',popen)
    MenuWorker(store,lambda _:0,lambda _:None).start()
    assert popen.call_count==(0 if unrelated else 1)
    # Changing the recorded arguments invalidates startup's recovery exception.
    key,_=publication(lane)
    store.db.execute('UPDATE operations SET args_hash=? WHERE key=?',('f'*64,key))
    assert recoverable(store)==[]
    store.close()
