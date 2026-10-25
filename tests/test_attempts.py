"""Batch-round limits with fake Notion/FNV/checker and real durable state."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from unittest.mock import Mock

import pytest

from fnv_relay import attempts
from fnv_relay.lane_policy import configure, queue
from fnv_relay.presentation import snapshot
from fnv_relay.store import UncertainOperation
from test_engine import env,assign,candidate,lane,message


def batch(e,mid,texts=('int f(){return 1;}',)):
    pointers=[candidate(e,mid+'-'+str(i),text) for i,text in enumerate(texts)]
    record=message(e,'BATCH',mid,candidates=pointers)
    e.notion.append(lane(e)['inbox'],record)
    return record


def limit(e,maximum):
    configure(e.engine,e.route,max_attempts=maximum)


def test_single_candidate_is_valid_and_batch_replay_is_one_attempt_with_default_off(env):
    assign(env);record=batch(env,'one')
    env.engine.poll()
    assert attempts.current(env.store,lane(env))==1
    assert len(env.checker.calls)==1
    env.engine.batch(lane(env),record)
    assert attempts.current(env.store,lane(env))==1
    assert len(env.checker.calls)==1
    assert not lane(env).get('attempt_handoff')
    row=snapshot(env.store,'test')['lanes'][0]
    assert row['attempts']==1 and row['max_attempts']==0


def test_rejected_metadata_does_not_count_as_an_attempt(env):
    assign(env)
    pointer=candidate(env,'bad','int f(){return 1;}',file_override={'source_bytes':999})
    env.notion.append(lane(env)['inbox'],message(env,'BATCH','bad',candidates=[pointer]))
    env.engine.poll()
    assert attempts.current(env.store,lane(env))==0
    assert len(env.checker.calls)==0


def test_limit_counts_rounds_not_candidates_and_accepts_the_last_full_batch(env):
    assign(env);limit(env,1)
    assert 'giveup' not in json.dumps(env.notion.pages)
    batch(env,'round1',('int f(){return 1;}','int f(){return 2;}','int f(){return 3;}'))
    env.engine.poll()
    assert len(env.checker.calls)==3
    current=lane(env)
    assert current['attempt_handoff']['attempts']==1
    notices=[r['payload'] for r in env.notion.messages(current['outbox']) if r.get('kind')=='RESULT']
    cap=next(x for x in notices if x.get('status')=='ATTEMPT_LIMIT_REACHED')
    assert cap['instructions']==attempts.HANDOFF_INSTRUCTIONS
    batch(env,'round2',('int f(){return 4;}',))
    env.engine.poll()
    assert len(env.checker.calls)==3
    assert attempts.current(env.store,lane(env))==1
    assert len([x for x in env.notion.messages(current['outbox']) if x.get('payload',{}).get('status')=='ATTEMPT_LIMIT_REACHED'])==1


def test_retroactive_limit_uses_old_batch_journal_without_new_ledger(env):
    assign(env)
    for n in (1,2):batch(env,'round'+str(n),('int f(){return '+str(n)+';}',));env.engine.poll()
    env.store.db.execute("DELETE FROM kv WHERE key LIKE 'attempt-round:%'")
    assert attempts.current(env.store,lane(env))==2
    limit(env,1);env.engine.poll()
    assert lane(env)['attempt_handoff']['attempts']==2
    assert len(env.checker.calls)==2


def test_attempt_count_survives_release_and_reclaim_same_function(env):
    assign(env);batch(env,'round1');env.engine.poll()
    current=lane(env)
    env.engine.release(current,'test reconnect',resume=True)
    # Reassignment without an enabled limit follows the original continuation path.
    env.engine.resume_lane(env.route);env.engine.poll()
    assert lane(env)['state']=='assigned'
    assert attempts.current(env.store,lane(env))==1
    batch(env,'round2',('int f(){return 2;}',));env.engine.poll()
    assert attempts.current(env.store,lane(env))==2


def test_handoff_preserves_summary_and_checked_best_before_release_and_rotation(env):
    assign(env);limit(env,1);batch(env,'round1');env.engine.poll()
    current=lane(env);old=deepcopy(current['lease'])
    waiting=current['attempt_handoff']
    handoff=message(env,'HANDOFF','handoff1',request_id=waiting['request_id'],summary='best checked candidate is saved',
                    remaining_mismatches='stack allocation differs',approaches_tried='compared three frame layouts',next_steps='inspect register allocation')
    attempts.receive(env.engine,current,handoff)
    attempts.tick(env.engine,current)
    current=lane(env)
    assert 'lease' not in current and 'attempt_handoff' not in current
    assert current['parked'][0]['reason']=='attempt_limit'
    assert current['ready'] and current['explicit_resume']
    from pathlib import Path
    assert json.loads((Path(old['directory'])/'attempt-handoff.json').read_text())==handoff
    released=[args for name,args in env.project.calls if name=='giveup'][-1]
    assert 'stack allocation differs' in released['reason']
    assert 'best_code' in released
    assert 'best checked candidate is saved' in released['reason']


def test_handoff_timeout_releases_preserved_best_without_waiting_forever(env):
    assign(env);limit(env,1);batch(env,'round1');env.engine.poll()
    current=lane(env);deadline=current['attempt_handoff']['deadline']
    env.engine.clock=lambda:deadline+1
    attempts.tick(env.engine,current)
    assert 'lease' not in lane(env)
    assert lane(env)['parked'][0]['reason']=='attempt_limit'
    assert any(name=='giveup' and 'best_code' in args for name,args in env.project.calls)


def test_handoff_wrong_scope_cannot_release_another_claim(env):
    assign(env);limit(env,1);batch(env,'round1');env.engine.poll()
    current=lane(env)
    handoff=message(env,'HANDOFF','bad-handoff',request_id=current['attempt_handoff']['request_id'],summary='summary',
                    remaining_mismatches='issues',approaches_tried='attempts',next_steps='next')
    handoff['claim_id']='some-other-claim'
    with pytest.raises(ValueError):attempts.receive(env.engine,current,handoff)
    assert not any(name=='giveup' for name,_ in env.project.calls)


def test_uncertain_release_prevents_rotation_and_new_claims(env):
    assign(env);limit(env,1);batch(env,'round1');env.engine.poll()
    current=lane(env)
    env.engine.clock=lambda:current['attempt_handoff']['deadline']+1
    original=env.project.call
    def failed(name,args):
        if name=='giveup':raise TimeoutError('uncertain release')
        return original(name,args)
    env.project.call=failed
    with pytest.raises(TimeoutError):attempts.tick(env.engine,current)
    assert lane(env).get('lease') and not lane(env).get('parked')
    with pytest.raises(UncertainOperation):attempts.tick(env.engine,lane(env))
    assert env.project.claims==1


@pytest.mark.parametrize('maximum',[-1,True,1.5,1000001])
def test_invalid_limits_are_rejected_for_direct_and_queued_controls(env,maximum):
    with pytest.raises(ValueError):limit(env,maximum)
    with pytest.raises(ValueError):queue(env.store,env.route,max_attempts=maximum)


def test_selection_pages_skip_capped_and_previously_attempted_functions(env):
    assign(env);batch(env,'round1');env.engine.poll();limit(env,1)
    current=lane(env);current['policy']['allow_attempted']=False
    call=Mock(side_effect=[{'functions':[{'address':'0x00401000','size':32,'earlier_claims':1}], 'next':'page2'},
                           {'functions':[{'address':'0x00402000','size':35,'earlier_claims':0}, {'address':'0x00403000','size':34,'earlier_claims':2}], 'next':None}])
    env.project.call=call
    result=attempts.selection(env.engine,current,'<=255')
    assert result['address']=='0x00402000'
    assert call.call_args_list[1].args[1]['cursor']=='page2'


def test_largest_selection_uses_all_pages_and_rejects_cursor_loops(env):
    assign(env);current=lane(env);current['policy'].update(order='largest',max_attempts=1)
    env.project.call=Mock(side_effect=[{'functions':[{'address':'0x00402000','size':34}], 'next':'page2'},
                                      {'functions':[{'address':'0x00403000','size':90}], 'next':None}])
    assert attempts.selection(env.engine,current,'<=255')['address']=='0x00403000'
    env.project.call=Mock(return_value={'functions':[],'next':'loop'})
    with pytest.raises(ValueError):attempts.selection(env.engine,current,'<=255')


def test_handoff_message_through_poll_rotates_and_claims_a_different_function(env):
    assign(env);limit(env,1);batch(env,'round1');env.engine.poll()
    old=lane(env)['lease']['claim_id'];waiting=lane(env)['attempt_handoff']
    handoff=message(env,'HANDOFF','handoff',request_id=waiting['request_id'],summary='best is saved',
                    remaining_mismatches='frame layout',approaches_tried='tested one targeted candidate',next_steps='inspect stack')
    env.notion.append(lane(env)['inbox'],handoff)
    original=env.project.call
    def pool(name,args):
        if name=='list_functions':return {'functions':[{'address':'0x00401000','size':32},{'address':'0x00402000','size':40}],'next':None}
        if name=='claim' and args.get('address'):env.project.address=args['address']
        return original(name,args)
    env.project.call=pool
    env.engine.poll()
    current=lane(env)
    assert current['state']=='assigned' and current['lease']['claim_id']!=old
    assert current['lease']['address']=='0x00402000'
    assert attempts.current(env.store,current)==0
    assert env.project.claims==2


def test_match_on_the_last_allowed_round_submits_without_handoff(env):
    assign(env);limit(env,1)
    # Prevent next admission so this assertion concerns only the completed function.
    env.engine.ready_for_claim=lambda _:False
    batch(env,'round1',('int f(){return 42;}',))
    env.engine.poll()
    assert any(name=='submit' for name,_ in env.project.calls)
    assert not any(name=='giveup' for name,_ in env.project.calls)
    assert not any(r.get('payload',{}).get('status')=='ATTEMPT_LIMIT_REACHED' for r in env.notion.messages(lane(env)['outbox']))


def test_disabling_limit_restores_unlimited_rounds_before_handoff_started(env):
    assign(env);limit(env,2);batch(env,'round1');env.engine.poll()
    limit(env,0)
    for n in (2,3):batch(env,'round'+str(n),('int f(){return '+str(n)+';}',));env.engine.poll()
    assert attempts.current(env.store,lane(env))==3
    assert not lane(env).get('attempt_handoff')


def test_exhausted_eligible_pool_waits_without_reclaiming_capped_work(env):
    assign(env);batch(env,'round1');env.engine.poll();limit(env,1)
    current=lane(env)
    env.engine.release(current,'test saved work',resume=True)
    current=lane(env);attempts.tick(env.engine,current)
    original=env.project.call
    env.project.call=lambda name,args:({'functions':[{'address':'0x00401000','size':32}],'next':None} if name=='list_functions' else original(name,args))
    env.engine.assign(lane(env))
    assert env.project.claims==1
    assert lane(env)['attempt_pool_empty']
    assert lane(env)['admission_not_before']>env.engine.clock()


def test_handoff_deadline_never_crosses_lease_safety_cutoff(env):
    assign(env);batch(env,'round1');env.engine.poll();limit(env,1)
    current=lane(env);current['lease']['expires']=datetime.fromtimestamp(env.engine.clock()+650,timezone.utc).isoformat()
    env.store.put_lane(current)
    attempts.tick(env.engine,current)
    assert lane(env)['attempt_handoff']['deadline']==env.engine.clock()+50


def test_shutdown_does_not_publish_new_handoff(env):
    assign(env);batch(env,'round1');env.engine.poll();limit(env,1)
    env.store.set('shutdown_requested',True)
    attempts.tick(env.engine,lane(env))
    assert not lane(env).get('attempt_handoff')


def test_limit_change_queued_during_last_batch_applies_before_handoff(env):
    assign(env);limit(env,1)
    check=env.checker
    def disable(*args,**kwargs):
        queue(env.store,env.route,max_attempts=0)
        return check(*args,**kwargs)
    env.engine.checker=disable
    batch(env,'round1');env.engine.poll()
    assert lane(env)['policy']['max_attempts']==0
    assert not lane(env).get('attempt_handoff')
