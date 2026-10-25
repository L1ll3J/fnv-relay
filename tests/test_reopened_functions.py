"""FNV-paused work is excluded by server eligibility, not a permanent local ban."""
import pytest

from fnv_relay import attempts
from test_engine import env, lane


@pytest.mark.parametrize('reason,expected',[
    ('function_paused','0x00401000'),
    ('owner_selected_new_task','0x00402000'),
    ('attempt_limit','0x00402000'),
])
def test_reopened_pool_entries_return_but_operator_parked_work_stays_parked(env,monkeypatch,reason,expected):
    current=lane(env);current['policy'].update(order='smallest',allow_attempted=True)
    current['parked']=[{'reason':reason,'continuation':{'address':'0x00401000','previous_claim_id':'prior'}}]
    monkeypatch.setattr(env.project,'call',lambda tool,args:{'functions':[
        {'address':'0x00401000','size':19,'earlier_claims':2},
        {'address':'0x00402000','size':32,'earlier_claims':0}], 'next':None})
    result=attempts.selection(env.engine,current,'<=255')
    assert result['address']==expected
    assert current['parked'][0]['reason']==reason


def test_reopened_function_still_respects_fresh_only_policy(env,monkeypatch):
    current=lane(env);current['policy'].update(order='smallest',allow_attempted=False)
    current['parked']=[{'reason':'function_paused','continuation':{'address':'0x00401000','previous_claim_id':'prior'}}]
    monkeypatch.setattr(env.project,'call',lambda tool,args:{'functions':[
        {'address':'0x00401000','size':19,'earlier_claims':2},
        {'address':'0x00402000','size':32,'earlier_claims':0}], 'next':None})
    assert attempts.selection(env.engine,current,'<=255')['address']=='0x00402000'


@pytest.mark.parametrize('state,expected',[('paused (under review)',False),('open',True)])
def test_explicit_target_only_returns_when_project_reopens_it(env,monkeypatch,state,expected):
    current=lane(env);current['policy'].update(address='0x00401000')
    current['parked']=[{'reason':'function_paused','continuation':{'address':'0x00401000','previous_claim_id':'prior'}}]
    def response(tool,args):
        if tool=='function_info':return {'address':args['address'],'state':state}
        if tool=='list_bands':return {'bands':[]}
        raise AssertionError(tool)
    monkeypatch.setattr(env.project,'call',response)
    status={'limits':{'claims':4,'subsPerDay':60,'pending':25},'open_claims':[],
            'submissions_today':0,'awaiting_verification':0}
    result=env.engine.select(current,status)
    assert (result is not None)==expected
    if expected:assert result['address']=='0x00401000'
