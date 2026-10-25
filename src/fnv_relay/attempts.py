"""Durable operator limits on per-function batch rounds; no agent opt-out before a cap."""
import json
from datetime import datetime, timezone
from pathlib import Path
import random
import re

from .store import read_json, write_json

HANDOFF_INSTRUCTIONS = 'The operator-configured attempt limit has been reached. You are directed to giveup on this function and prepare a handoff. Do not submit further candidate batches. Post a HANDOFF record to the inbox using the supplied schema. Summarize the best candidate and its checked result, remaining mismatches, approaches tried, and recommended next steps. Distinguish checked evidence from untested hypotheses. The controller will preserve your handoff, release the claim and rotate to another function. If no handoff arrives before the stated deadline, the controller will rotate using the evidence already preserved.'


def validate(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 1000000):
        raise ValueError('Attempt limit must be 0 (off) or a positive integer up to 1000000')


def address(value):
    if not isinstance(value,str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,8}',value):return None
    return f'0x{int(value,16):08x}'


def counts(store,lane,*,exclude=None):
    """Replay unique durable batch bindings, not checker calls or mailbox deliveries."""
    route=lane['route_token'];claims={};by_address={}
    scopes=[lane.get('lease') or {},lane.get('resume') or {}]
    scopes += [x['continuation'] for x in lane.get('parked',[])]
    scopes += [x['payload'] for x in lane.get('closed_lease_notices',[])]
    for scope in scopes:
        cid=scope.get('claim_id') or scope.get('previous_claim_id')
        if cid and address(scope.get('address')):claims[cid]=address(scope['address'])
    prefix='claim:'+route+':'
    for key,result in store.db.execute('SELECT key,result FROM operations WHERE status=? AND result IS NOT NULL',('response',)):
        if not key.startswith(prefix):continue
        record=json.loads(result)
        if isinstance(record,dict) and record.get('claim_id') and address(record.get('address')):
            claims[record['claim_id']]=address(record['address'])
    # Imported scopes may retain the claim identity only in closure/continuation records.
    for cid,old_address in list(claims.items()):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',cid):continue
        claim=read_json(store.root/'claims'/cid/'claim.json',{})
        claims[cid]=address(claim.get('address')) or old_address
    for key,data in store.db.execute("SELECT key,data FROM kv WHERE key LIKE 'batch-input:batch:%'"):
        _,_,cid,mid=key.split(':',3)
        if cid not in claims:continue
        publication='batch:'+cid+':'+mid
        result=store.get('batch-result:'+publication)
        if isinstance(result,dict) and result.get('status')=='BATCH_REJECTED':continue
        # An interrupted round is retained as an attempt; re-delivery never increments it.
        by_address.setdefault(claims[cid],set()).add(publication)
    for key,data in store.db.execute("SELECT key,data FROM kv WHERE key LIKE 'attempt-round:%'"):
        row=json.loads(data)
        if row.get('route_token')==route and address(row.get('address')):
            by_address.setdefault(address(row['address']),set()).add(row['publication'])
    return {key:len(value-({exclude} if exclude else set())) for key,value in by_address.items()}


def current(store,lane):
    target=(lane.get('lease') or lane.get('resume') or {}).get('address')
    return counts(store,lane).get(address(target),0)


def capped(store,lane,target):
    maximum=lane.get('policy',{}).get('max_attempts') or 0
    return bool(maximum and counts(store,lane).get(address(target),0)>=maximum)


def reserve(engine,lane,publication,identity):
    """Called only after candidate validation, once for a new accepted batch."""
    key='attempt-round:'+publication
    old=engine.store.get(key)
    if lane.get('attempt_handoff'):return False
    if old:
        if old['identity']!=identity:raise ValueError('Attempt identifier already bound to other content')
        return True
    maximum=lane.get('policy',{}).get('max_attempts') or 0
    used=counts(engine.store,lane,exclude=publication).get(address(lane['lease']['address']),0)
    if maximum and used>=maximum:return False
    engine.store.set(key,{'route_token':lane['route_token'],'address':address(lane['lease']['address']),
                         'publication':publication,'identity':identity,'at':engine.clock()})
    engine.store.event('batch_attempt_started',{'route_token':lane['route_token'],'claim_id':lane['lease']['claim_id'],
                       'batch_message_id':publication.rsplit(':',1)[1]})
    return True


def selection(engine,lane,band):
    """Page the official eligible pool and select by address, excluding capped work."""
    from .engine import band as size_band
    policy=lane['policy'];tallies=counts(engine.store,lane);maximum=policy.get('max_attempts') or 0
    # Project-paused work returns through the official eligible pool when reopened.
    parked={address(x['continuation']['address']) for x in lane.get('parked',[]) if x.get('reason') != 'function_paused'}
    cursor=None;seen=set();best=None;eligible=0
    while not engine.stop_requested():
        args={'band':band,'order':'size','limit':500}
        if cursor is not None:args['cursor']=cursor
        response=engine.project.call('list_functions',args)
        rows=response.get('functions')
        if not isinstance(rows,list):raise ValueError('Eligible function list was not confirmed')
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Invalid eligible function record')
            target=address(row.get('address'));size=row.get('size')
            if not target or type(size) is not int:raise ValueError('Invalid eligible function record')
            if size_band(size)!=band:continue
            if target in parked or maximum and tallies.get(target,0)>=maximum:continue
            if not policy.get('allow_attempted',True) and row.get('earlier_claims')!=0:continue
            if not policy.get('min_bytes',0)<=size<=policy.get('max_bytes',2**31):continue
            eligible+=1
            if policy.get('order','largest')=='random':
                if random.randrange(eligible)==0:best=(size,target)
            elif best is None:best=(size,target)
            elif policy.get('order','largest')=='largest' and (size,target)>best:best=(size,target)
            elif policy.get('order')=='smallest' and (size,target)<best:best=(size,target)
        cursor=response.get('next')
        if cursor is None:break
        if not isinstance(cursor,str) or not cursor or cursor in seen:raise ValueError('Invalid or repeated function-list cursor')
        seen.add(cursor)
    if engine.stop_requested() or best is None:return None
    return {'address':best[1],'context_mode':'resume'}


def tick(engine,lane):
    """Enforce only between batches; an admitted final round may finish and match."""
    if engine.stop_requested():return
    engine.refresh(lane)
    waiting=lane.get('attempt_handoff')
    lease=lane.get('lease')
    if waiting:
        if not lease or lease['claim_id']!=waiting['claim_id']:
            rotate(engine,lane,waiting)
        elif waiting.get('received') or engine.clock()>=waiting['deadline']:
            rotate(engine,lane,waiting)
        else:publish_request(engine,lane,waiting)
        return
    if lease and lane['state']=='assigned' and capped(engine.store,lane,lease['address']):
        from .engine import epoch
        waiting={'claim_id':lease['claim_id'],'pack_sha256':lease['pack_sha256'],'address':lease['address'],
                 'request_id':'attempt-limit-'+lease['claim_id'],'attempts':current(engine.store,lane),
                 'limit':lane['policy']['max_attempts'],'deadline':max(engine.clock(),min(engine.clock()+300,epoch(lease['expires'])-600))}
        lane['attempt_handoff']=waiting;engine.store.put_lane(lane)
        publish_request(engine,lane,waiting)
    elif not lease and lane.get('resume') and capped(engine.store,lane,lane['resume']['address']):
        waiting={'claim_id':lane['resume']['previous_claim_id'],'address':lane['resume']['address']}
        rotate(engine,lane,waiting)


def publish_request(engine,lane,waiting):
    if waiting.get('notified'):return
    scope={key:lane['lease'][key] for key in ('claim_id','pack_sha256')}
    schema={'protocol':'fnv-relay/2','kind':'HANDOFF','sender':'chatgpt','route_token':lane['route_token'],
            **scope,'message_id':'UNIQUE_MESSAGE','request_id':waiting['request_id'],
            'summary':'BEST CANDIDATE AND CHECKED RESULT','remaining_mismatches':'KNOWN DIFFERENCES',
            'approaches_tried':'CONCISE EXPERIMENT SUMMARY','next_steps':'RECOMMENDED NEXT STEPS'}
    engine.publish(lane,waiting['request_id'],{'status':'ATTEMPT_LIMIT_REACHED','attempts':waiting['attempts'],
                   'limit':waiting['limit'],'handoff_deadline_utc':datetime.fromtimestamp(waiting['deadline'],timezone.utc).isoformat(),
                   'handoff_schema':schema,'instructions':HANDOFF_INSTRUCTIONS})
    waiting['notified']=True;lane['attempt_handoff']=waiting;engine.store.put_lane(lane)


def receive(engine,lane,message):
    waiting=lane.get('attempt_handoff')
    if not waiting or message.get('request_id')!=waiting.get('request_id'):raise ValueError('No matching handoff request')
    engine.scope(lane,message)
    fields=('summary','remaining_mismatches','approaches_tried','next_steps')
    if any(not isinstance(message.get(k),str) or not 1<=len(message[k])<=12000 for k in fields):raise ValueError('Handoff requires bounded summary, mismatches, approaches and next steps')
    directory=Path(lane['lease']['directory'])
    write_json(directory/'attempt-handoff.json',message)
    waiting['received']=True;waiting['handoff_path']=str(directory/'attempt-handoff.json')
    lane['attempt_handoff']=waiting;engine.store.put_lane(lane)
    engine.store.event('attempt_handoff_received',{'route_token':lane['route_token'],'claim_id':waiting['claim_id']})


def rotate(engine,lane,waiting):
    old=waiting['claim_id']
    reason='Operator attempt limit reached; best checked work preserved.'
    if waiting.get('handoff_path'):
        handoff=read_json(waiting['handoff_path'],{})
        reason+=' '+ ' '.join(handoff.get(k,'').replace('\n',' ') for k in ('summary','remaining_mismatches','next_steps'))
    if lane.get('lease'):
        if lane['lease']['claim_id']!=old:raise ValueError('Handoff claim changed before rotation')
        engine.release(lane,reason,resume=True);engine.refresh(lane)
        if lane.get('lease'):return
    if lane.get('resume') and address(lane['resume']['address'])==address(waiting['address']):
        engine.park_continuation(lane,reason='attempt_limit')
    lane.pop('attempt_handoff',None)
    if address(lane.get('policy',{}).get('address'))==address(waiting['address']):lane['policy'].pop('address',None)
    lane.update(ready=True,explicit_resume=True)
    engine.store.put_lane(lane)
    engine.store.event('attempt_limit_rotated',{'route_token':lane['route_token'],'claim_id':old,'handoff_received':bool(waiting.get('received'))})
