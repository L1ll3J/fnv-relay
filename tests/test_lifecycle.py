"""Offline lifecycle integration: durable real state, fake services and compiler."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from fnv_relay.engine import Engine, epoch, sha
from fnv_relay.project import ProjectError
from fnv_relay.store import Store, write_json
from fnv_relay import lifecycle, watch
from test_engine import env, assign, lane, message, candidate, post_batch


def automatic(env):
    row=lane(env); row['policy']['auto_resume_unfinished']=True; env.store.put_lane(row)
    env.engine.clock=lambda:env.project.now
    return assign(env)


def checked(env, text='int target(){return 41;}'):
    post_batch(env,[candidate(env,'best',text)])
    env.engine.poll()
    return lane(env)


def test_expiry_release_queues_same_function_and_new_scope_with_prior_work(env):
    old=automatic(env); checked(env)
    env.notion.append(old['inbox'],message(env,'FINDING','finding',note='First difference remains at the allocation.'))
    env.engine.poll()
    env.project.now=epoch(old['lease']['expires'])-600
    env.engine.poll()
    queued=lane(env)
    assert queued['state']=='resume_pending' and 'lease' not in queued
    assert queued['resume']['address']==old['lease']['address']
    assert env.project.claims==1  # Original HELLO is too old to allocate again.
    env.notion.append(old['inbox'],message(env,'READY','fresh-ready'))
    env.engine.poll()
    current=lane(env)
    assert current['lease']['claim_id']=='claim-2' and current['state']=='assigned'
    calls=[args for tool,args in env.project.calls if tool=='claim']
    assert calls[-1]=={'address':old['lease']['address'],'context_mode':'resume'}
    assert current['inbox']==old['inbox'] and current['outbox']==old['outbox']
    directory=Path(current['lease']['directory'])
    prior=__import__('json').loads((directory/'continuation.json').read_text())
    assert prior['checked_for_current_lease'] is False
    assert prior['previous_claim_id']=='claim-1'
    assert (directory/'prior-notes.json').exists()
    assert len(list((directory/'sources').glob('*.cpp')))==1
    stale={'claim_id':old['lease']['claim_id'],'pack_sha256':old['lease']['pack_sha256']}
    env.notion.append(current['inbox'],message(env,'BATCH','stale-after-resume',candidates=[],**stale))
    env.engine.poll()
    assert len(env.checker.calls)==1


def test_manual_resume_is_one_admission_grant_not_an_idle_hour_loop(env):
    automatic(env); checked(env); env.project.now+=1800
    env.engine.release(lane(env),'Time to preserve unfinished work')
    env.engine.assign(lane(env)); assert env.project.claims==1
    env.engine.resume_lane(env.route); env.engine.assign(lane(env))
    assert env.project.claims==2 and not lane(env)['explicit_resume']
    env.engine.release(lane(env),'No newer heartbeat')
    env.engine.assign(lane(env)); assert env.project.claims==2


@pytest.mark.parametrize('status,retry_after',[(404,None),(409,20),(429,700),(503,800)])
def test_definite_claim_denial_persists_receipt_and_waits_without_fallback(env,monkeypatch,status,retry_after):
    automatic(env); env.engine.release(lane(env),'Keep this function')
    original=env.project.call; denials=[]
    def call(tool,args):
        if tool=='claim':
            denials.append(deepcopy(args)); raise ProjectError(status,retry_after,False,'capacity_limit')
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',call)
    env.engine.assign(lane(env)); current=lane(env)
    assert current['state']=='resume_pending'
    assert current['last_claim_denial']['status']==status
    assert env.store.operation_record('claim:'+env.route+':2')['status']=='response'
    assert env.store.pending_operations()==[]
    assert denials==[{'address':'0x401000','context_mode':'resume'}]
    wait=max(300,retry_after or 0)
    env.project.now+=wait-1
    before=len(env.project.calls)
    env.engine.assign(lane(env)); env.engine.assign(lane(env))
    assert len(env.project.calls)==before and len(denials)==1
    env.project.now+=1; env.engine.assign(lane(env))
    assert len(denials)==2 and all('address' in args and 'band' not in args for args in denials)


@pytest.mark.parametrize('failure',[ProjectError(503,600,True),ProjectError(500,None,True),TimeoutError('unknown claim outcome')])
def test_ambiguous_claim_stays_intent_and_is_never_retried(env,monkeypatch,failure):
    automatic(env); env.engine.release(lane(env),'Keep address')
    original=env.project.call; calls=[]
    def call(tool,args):
        if tool=='claim': calls.append(args); raise failure
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',call)
    with pytest.raises(type(failure)): env.engine.assign(lane(env))
    assert lane(env)['state']=='claim_intent'
    env.project.now+=10000; env.engine.assign(lane(env)); env.engine.poll()
    assert len(calls)==1
    assert env.store.operation_record('claim:'+env.route+':2')['status']=='uncertain'


def test_stale_release_snapshot_never_removes_new_lease(env):
    stale=automatic(env); checked(env)
    other=Store(env.store.root)
    watcher=Engine(other,env.project,None,clock=lambda:env.project.now,watchdog=False)
    try:
        watcher.release(deepcopy(stale),'Old owner finished')
        env.engine.assign(lane(env)); replacement=lane(env)
        assert replacement['lease']['claim_id']=='claim-2'
        outcome=watcher.release(stale,'Stale watcher snapshot')
        assert outcome['status']=='stale_lease_ignored'
        assert lane(env)['lease']['claim_id']=='claim-2'
        assert len([1 for tool,_ in env.project.calls if tool=='giveup'])==1
    finally: other.close()


def test_guardian_release_reloads_and_preserves_latest_pause(env):
    stale=automatic(env)
    env.engine.pause_lane(env.route)
    env.engine.release(stale,'Expiry')
    current=lane(env)
    assert current['paused'] is True and current['state']=='resume_pending'
    env.engine.assign(current)
    assert env.project.claims==1


def test_release_all_preserves_best_and_disables_all_new_admissions(env):
    automatic(env); checked(env)
    env.engine._should_stop=lambda:True
    result=env.engine.release_all('Owner stopped controller')
    assert result['counts']=={'released':1,'expired':0,'pending':0,'uncertain':0,'remaining':0}
    assert env.store.get('shutdown_requested') and env.store.get('drain')
    current=lane(env)
    assert not current.get('paused') and current['resume']['address']=='0x401000'
    gives=[args for tool,args in env.project.calls if tool=='giveup']
    assert gives[0]['best_code']=='int target(){return 41;}'
    env.engine.resume_lane(env.route); env.engine.assign(lane(env))
    assert env.project.claims==1


def test_release_all_continues_other_lanes_and_never_releases_untracked_claim(env,monkeypatch):
    automatic(env); checked(env)
    second=env.engine.create_lane('parent',{'order':'smallest','auto_resume_unfinished':True})
    second.update(state='idle',ready=True,explicit_resume=True); env.store.put_lane(second); env.engine.assign(second)
    env.project.open.append({'claim_id':'foreign','this_key':True,'expires':'2099-01-01T00:00:00Z'})
    original=env.project.call
    def call(tool,args):
        if tool=='giveup' and args['claim_id']=='claim-1':
            env.project.calls.append((tool,deepcopy(args))); raise ProjectError(503,10,True)
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',call)
    result=env.engine.release_all('Stop tracked work')
    assert result['counts']['released']==1 and result['counts']['uncertain']==1 and result['counts']['remaining']==1
    assert 'foreign' not in [args['claim_id'] for tool,args in env.project.calls if tool=='giveup']
    assert not env.store.lane(second['route_token']).get('paused')


def test_unknown_submit_blocks_giveup_but_is_reported_uncertain(env):
    automatic(env)
    with pytest.raises(ProjectError):
        env.store.operation('submit:claim-1',{},lambda:(_ for _ in ()).throw(ProjectError(503,1,True)))
    result=env.engine.release_all('Stop')
    assert result['counts']['uncertain']==1 and result['counts']['released']==0
    assert not any(tool=='giveup' for tool,_ in env.project.calls)


def test_release_all_reports_busy_lane_as_pending_and_sets_flags_first(env):
    automatic(env); other=Store(env.store.root)
    try:
        holder=Engine(other,env.project,None,watchdog=False)
        with holder.lane_lock(env.route):
            result=env.engine.release_all('Stop during active check')
            assert env.store.get('shutdown_requested') is True
            assert result['counts']['pending']==1 and result['counts']['remaining']==1
        assert env.engine.release_all('Retry cleanup')['counts']['released']==1
    finally: other.close()


def test_expired_machine_offline_claim_can_queue_same_address_without_fabricated_release(env):
    current=automatic(env); checked(env)
    env.project.open=[]; env.project.now=epoch(current['lease']['expires'])+1
    outcome=env.engine.release(lane(env),'Machine returned after deadline')
    assert outcome['status']=='expired_observed'
    assert lane(env)['state']=='resume_pending'
    assert not any(tool=='giveup' for tool,_ in env.project.calls)
    assert (Path(current['lease']['directory'])/'expired-observed.json').exists()


def test_shutdown_after_check_defers_notion_and_still_releases_best(env,monkeypatch):
    automatic(env); original=env.checker.__call__; stopped=[False]
    def checker(*args,**kwargs):
        result=original(*args,**kwargs); stopped[0]=True; return result
    env.engine.checker=checker; env.engine._should_stop=lambda:stopped[0]
    post_batch(env,[candidate(env,'first','int target(){return 41;}'),candidate(env,'second','int target(){return 40;}')])
    before=env.notion.appends; env.engine.poll()
    assert len(env.checker.calls)==1 and env.notion.appends==before
    directory=Path(lane(env)['lease']['directory'])
    assert list(directory.glob('deferred-batch-*.json'))
    result=env.engine.release_all('Interrupt')
    assert result['counts']['released']==1
    assert [args for tool,args in env.project.calls if tool=='giveup'][0]['best_code']=='int target(){return 41;}'


def test_owner_identity_compares_process_start_time(monkeypatch):
    current={'pid':123,'start_time':'new'}
    monkeypatch.setattr(lifecycle,'process_identity',lambda pid:current)
    assert lifecycle.owner_alive(current)
    assert not lifecycle.owner_alive({'pid':123,'start_time':'old'})
    assert lifecycle.owner_alive(None)


def test_owner_death_guardian_releases_only_its_claim_and_never_reacquires(env,monkeypatch):
    current=automatic(env); env.store.set_secret('fnv_api_key','fake')
    monkeypatch.setattr(watch,'owner_alive',lambda owner:False)
    with patch.object(watch,'FNV',return_value=env.project):
        watch.run(env.store,current['lease']['claim_id'],owner=current['lease']['owner'],clock=lambda:env.project.now,sleep=lambda _:pytest.fail('dead owner must release promptly'))
    assert lane(env)['state']=='resume_pending' and env.project.claims==1
    assert len([1 for tool,_ in env.project.calls if tool=='giveup'])==1


def test_guardian_exits_if_owner_binding_was_replaced(env):
    current=automatic(env); prior=deepcopy(current['lease']['owner'])
    current['lease']['owner']={'pid':prior['pid'],'start_time':'different'}; env.store.put_lane(current)
    with patch.object(watch,'FNV') as project:
        watch.run(env.store,current['lease']['claim_id'],owner=prior,sleep=lambda _:pytest.fail('stale owner guardian must exit'))
    project.assert_not_called()


def test_confirmed_denial_crash_window_recovers_receipt_without_new_request(env):
    automatic(env); env.engine.release(lane(env),'Preserve same function')
    current=lane(env); current.update(state='claim_intent',sequence=2); env.store.put_lane(current)
    denial={'status':404,'retry_after':600,'code':'object_not_found','observed_at':env.project.now}
    env.store.operation('claim:'+env.route+':2',{'address':'0x401000'},lambda:{'_relay_definite_denial':denial})
    before=env.project.claims
    env.engine.poll()
    current=lane(env)
    assert current['state']=='resume_pending' and current['admission_not_before']==env.project.now+600
    assert env.project.claims==before and env.store.pending_operations()==[]
    env.engine.poll()
    assert lane(env)['admission_not_before']==env.project.now+600  # Recovery never extends the cooldown repeatedly.


def test_saved_successful_claim_is_resolved_before_new_assignment_and_prior_best_survives(env):
    automatic(env); checked(env); env.engine.release(lane(env),'Keep working later')
    current=lane(env); current.update(state='claim_intent',sequence=2,claim_owner=env.engine.owner); env.store.put_lane(current)
    args={'address':'0x401000','context_mode':'resume'}
    env.store.operation('claim:'+env.route+':2',args,lambda:env.project.call('claim',args))
    env.engine.poll()
    assert [row['claim_id'] for row in env.project.open]==['claim-3']
    assert len([1 for tool,args in env.project.calls if tool=='giveup' and args['claim_id']=='claim-2'])==1
    current=lane(env)
    assert current['lease']['claim_id']=='claim-3'
    carried=Path(current['lease']['directory'])/'sources'/(''+sha(b'int target(){return 41;}')+'.cpp')
    assert carried.read_text()=='int target(){return 41;}'
    assert len(env.checker.calls)==1


def test_pause_release_preserves_same_function_queue_and_manual_pause_through_shutdown(env):
    automatic(env); checked(env)
    paused=env.engine.pause_lane(env.route,release=True)
    assert paused['paused'] is True and paused['resume']['address']=='0x401000'
    assert 'lease' not in paused
    env.engine.release_all('Stop while manually paused')
    assert lane(env)['paused'] is True
    env.store.set('shutdown_requested',False); env.store.set('drain',False)
    env.engine.assign(lane(env)); assert env.project.claims==1
    env.engine.resume_lane(env.route); env.engine.assign(lane(env))
    assert env.project.claims==2


def test_stop_during_preparation_releases_owned_claim_without_notion_upload(env,monkeypatch):
    current=lane(env); current['policy']['auto_resume_unfinished']=True; env.store.put_lane(current)
    stopped=[False]; original=env.project.call
    def call(tool,args):
        value=original(tool,args)
        if tool=='packet': stopped[0]=True
        return value
    monkeypatch.setattr(env.project,'call',call); env.engine._should_stop=lambda:stopped[0]
    env.notion.append(current['inbox'],message(env,'HELLO','hello'))
    before=env.notion.appends; env.engine.poll()
    assert env.project.open==[] and lane(env)['state']=='resume_pending'
    assert env.notion.appends==before
    assert len([1 for tool,_ in env.project.calls if tool=='claim'])==1


def test_custom_owner_model_label_is_used_and_web_claim_cannot_override_it(env):
    current=lane(env); current['policy']['model_label']='Owner-supplied label'; env.store.put_lane(current)
    assign(env)
    env.notion.append(lane(env)['inbox'],message(env,'BATCH','forged-model',model='Unobserved Model',candidates=[candidate(env,'matched','int target(){return 42;}')]))
    env.engine.poll()
    submit=next(args for tool,args in env.project.calls if tool=='submit')
    assert submit['model']=='Owner-supplied label'
    submitted=next(row['payload'] for row in env.notion.messages(lane(env)['outbox']) if row.get('payload',{}).get('status')=='SUBMITTED_AWAITING_VERIFICATION')
    assert submitted['verified'] is False and submitted['next_task']=='pending_if_eligible'
    assert submitted['next_action']['poll_outbox'] is True
    assert lane(env)['lease']['claim_id']=='claim-2'


@pytest.mark.parametrize('label',['', 'x'*121, 'line\nfeed', 'hidden\x85control', '   '])
def test_invalid_model_labels_fail_before_creating_notions_or_claims(env,label):
    before=len(env.notion.pages)
    with pytest.raises(ValueError,match='Model label'):
        env.engine.create_lane('parent',{'model_label':label})
    assert len(env.notion.pages)==before and env.project.claims==0


def test_giveup_uses_truthful_default_channel_metadata(env):
    assign(env); env.engine.release(lane(env),'Unfinished')
    assert next(args for tool,args in env.project.calls if tool=='giveup')['model']=='ChatGPT Web via Notion'


def test_unexpected_continuation_address_is_released_and_original_address_stays_queued(env,monkeypatch):
    automatic(env); env.engine.release(lane(env),'Continue original address')
    original=env.project.call
    def call(tool,args):
        response=original(tool,args)
        if tool=='claim':
            response['address']='0x999999'; env.project.open[-1]['address']='0x999999'
        return response
    monkeypatch.setattr(env.project,'call',call)
    env.engine.assign(lane(env))
    current=lane(env)
    assert current['state']=='resume_pending' and current['resume']['address']=='0x401000'
    assert current['resume']['not_before']==env.project.now+300 and not current.get('lease')
    assert env.project.open==[]
    assert len([1 for tool,args in env.project.calls if tool=='giveup' and args['claim_id']=='claim-2'])==1


def test_long_check_still_queues_one_next_task_after_accepted_submission(env):
    automatic(env)
    checker = env.checker
    def slow_check(*args, **kwargs):
        result = checker(*args, **kwargs)
        env.project.now += 901  # Each check exceeds the web activity window.
        return result
    env.engine.checker = slow_check
    post_batch(env,[candidate(env,'long-winner','int target(){return 42;}')])
    env.engine.poll()
    assert lane(env)['lease']['claim_id'] == 'claim-2'
    assert lane(env)['explicit_resume'] is False
    assert len([tool for tool,_ in env.project.calls if tool=='submit']) == 1
    env.engine.poll()
    assert env.project.claims == 2


def test_unproven_submission_ack_never_frees_slot_or_queues_next(env):
    automatic(env)
    original = env.project.call
    def unproven(tool,args):
        if tool == 'submit':
            env.project.calls.append((tool,dict(args)))
            return {'submission_id':'sub_unproven'}
        return original(tool,args)
    env.project.call = unproven
    post_batch(env,[candidate(env,'unproven','int target(){return 42;}')])
    env.engine.poll()
    assert env.project.claims == 1
    assert lane(env)['lease']['claim_id'] == 'claim-1'
    summary = env.engine.release_all('stop with unknown submit status')
    assert summary['counts']['uncertain'] == 1
    assert not any(tool == 'giveup' for tool,_ in env.project.calls)


def test_random_order_shuffles_only_available_eligible_size_bands(env,monkeypatch):
    from fnv_relay import engine as module
    current=lane(env); current['policy']={'order':'random','min_bytes':256,'max_bytes':4096}
    rows=[{'band':'<=255','allowed':True,'open':10},
          {'band':'256-511','allowed':True,'open':3,'reserved_for_new':3},
          {'band':'512-1K','allowed':True,'open':4},
          {'band':'1K-4K','allowed':True,'open':4},
          {'band':'4K+','allowed':True,'open':10}]
    original=env.project.call
    monkeypatch.setattr(env.project,'call',lambda tool,args:{'bands':rows} if tool=='list_bands' else original(tool,args))
    shuffled=[]
    def reverse(values):
        shuffled.append(list(values)); values.reverse()
    monkeypatch.setattr(module.random,'shuffle',reverse)
    status=original('status',{})
    assert env.engine.select(current,status)=={'band':'1K-4K','prefer':'any','context_mode':'resume'}
    assert shuffled==[['512-1K','1K-4K']]
    rows[3]['allowed']=False
    assert env.engine.select(current,status)['band']=='512-1K'
    assert shuffled[-1]==['512-1K']
    assert env.project.claims==0


def test_invalid_task_order_still_rejected_without_allocating(env):
    current=lane(env); current['policy']={'order':'uniform-over-all-functions'}
    with pytest.raises(ValueError,match='Unsupported selection order'):
        env.engine.select(current,env.project.call('status',{}))
    assert env.project.claims==0


@pytest.mark.parametrize('status', [401,403])
def test_claim_access_denial_pauses_without_uncertain_intent_or_losing_continuation(env,monkeypatch,status):
    automatic(env)
    env.engine.release(lane(env),'Preserve unfinished task')
    original=env.project.call
    def denied(tool,args):
        if tool=='claim': raise ProjectError(status,None,False,'access_denied')
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',denied)
    env.engine.assign(lane(env))
    current=lane(env)
    assert current['state']=='resume_pending' and current['paused']
    assert current['blocked_reason']=='fnv_access_denied'
    assert current['resume']['address']=='0x401000'
    assert env.store.pending_operations()==[]
    assert env.store.operation_record('claim:'+env.route+':2')['status']=='response'


def test_project_paused_continuation_moves_to_next_task_and_is_explicitly_resumable(env,monkeypatch):
    automatic(env)
    env.engine.release(lane(env),'Preserve unfinished work')
    previous=deepcopy(lane(env)['resume'])
    original=env.project.call
    def call(tool,args):
        if tool=='function_info':return {'address':args['address'],'state':'paused (problem reports under review)'}
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',call)
    env.project.address='0x402000'
    env.engine.assign(lane(env))
    current=lane(env)
    assert current['state']=='assigned' and not current['paused']
    assert current['lease']['address']=='0x402000'
    assert current['parked'][0]['continuation']==previous
    assert current['parked'][0]['notified']
    assert not current.get('resume')
    claims=[args for tool,args in env.project.calls if tool=='claim']
    assert 'band' in claims[-1] and 'address' not in claims[-1]
    notices=[r['payload'] for r in env.notion.messages(current['outbox']) if r.get('payload',{}).get('status')=='CONTINUATION_PARKED']
    assert len(notices)==1 and notices[0]['address']=='0x401000'
    with pytest.raises(ValueError,match='current claim'):
        env.engine.resume_parked(env.route,'0x401000')
    env.engine.release(current,'Owner switches back')
    env.engine.resume_parked(env.route,'0x401000')
    assert lane(env)['resume']['directory']==previous['directory']
    assert lane(env)['parked'][0]['continuation']['address']=='0x402000'


def test_existing_system_paused_lane_automatically_recovers_on_poll(env,monkeypatch):
    automatic(env)
    env.engine.release(lane(env),'Preserve old task')
    current=lane(env);current.update(paused=True,blocked_reason='function_paused',explicit_resume=True)
    env.store.put_lane(current)
    env.project.address='0x402000'
    env.engine.poll()
    current=lane(env)
    assert current['state']=='assigned'
    assert current['lease']['address']=='0x402000'
    assert current['parked'][0]['continuation']['address']=='0x401000'


def test_park_notification_readback_retry_does_not_allocate_until_delivered(env,monkeypatch):
    automatic(env)
    env.engine.release(lane(env),'Preserve old task')
    current=lane(env);current.update(paused=True,blocked_reason='function_paused',explicit_resume=True)
    env.store.put_lane(current)
    env.project.address='0x402000'
    original=env.notion.messages;failed=[False]
    def messages(page):
        rows=original(page)
        if page==lane(env)['outbox'] and not failed[0] and any(x.get('payload',{}).get('status')=='CONTINUATION_PARKED' for x in rows):
            failed[0]=True;raise RuntimeError('Readback interrupted')
        return rows
    monkeypatch.setattr(env.notion,'messages',messages)
    env.engine.poll()
    assert env.project.claims==1
    env.engine.poll()
    assert lane(env)['state']=='assigned' and env.project.claims==2
    notices=[r for r in original(lane(env)['outbox']) if r.get('payload',{}).get('status')=='CONTINUATION_PARKED']
    assert len(notices)==1


def test_long_project_backoff_is_scheduled_without_retaining_claim_locks(env,monkeypatch):
    automatic(env)
    original=env.project.call
    def call(tool,args):
        if tool=='status':raise ProjectError(429,3600,False)
        return original(tool,args)
    monkeypatch.setattr(env.project,'call',call)
    post_batch(env,[candidate(env,'rate-limited','int target(){return 41;}')])
    env.engine.poll()
    assert env.store.get('project_read_not_before')==env.project.now+3600
    with env.engine.lane_lock(env.route),env.store.lock('lease-'+lane(env)['lease']['claim_id']):pass
    env.engine.poll()


def test_closed_lease_notice_is_saved_for_next_controller_and_published_once(env):
    automatic(env)
    env.engine.release(lane(env),'Owner shutdown')
    assert not lane(env)['closed_lease_notices'][0]['notified']
    env.engine.poll()
    env.engine.poll()
    notices=[x['payload'] for x in env.notion.messages(lane(env)['outbox']) if x.get('payload',{}).get('status')=='LEASE_CLOSED']
    assert len(notices)==1 and notices[0]['lease_status']=='released'


def test_shutdown_during_ownership_read_does_not_start_another_check(env,monkeypatch):
    automatic(env)
    stopped=[False];env.engine._should_stop=lambda:stopped[0]
    original=env.project.call
    def call(tool,args):
        result=original(tool,args)
        if tool=='status':stopped[0]=True
        return result
    monkeypatch.setattr(env.project,'call',call)
    post_batch(env,[candidate(env,'stop-during-read','int target(){return 41;}')])
    env.engine.poll()
    assert env.checker.calls==[]
    assert not env.store.get('checks:'+lane(env)['lease']['claim_id'])
