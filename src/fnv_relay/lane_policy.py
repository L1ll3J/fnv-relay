"""Owner-controlled selection policy; current work is never silently discarded."""
from .preferences import ORDERS
from .attempts import validate


def configure(engine, route, *, order=None, allow_attempted=None, switch_now=False, paused=None, max_attempts=None):
    validate(max_attempts)
    if order is not None and order not in ORDERS:
        raise ValueError('Unknown task order')
    if allow_attempted is not None and type(allow_attempted) is not bool:
        raise ValueError('Attempted-work option must be boolean')
    if paused is not None and type(paused) is not bool:
        raise ValueError('Paused option must be boolean')
    with engine.lane_lock(route):
        lane = engine.store.lane(route)
        if lane is None:
            raise ValueError('Unknown lane')
        if lane['state'] in ('setup','claim_intent','preparing','closed_requires_reconciliation'):
            raise ValueError('Finish setup, assignment delivery or reconciliation before changing this lane')
        if switch_now:
            # Release uses the ordinary journal, ownership check and checked-best path.
            # A failed/uncertain release leaves the lane paused with its old scope intact.
            lane.update(paused=True, explicit_resume=False)
            engine.store.put_lane(lane)
            if lane.get('lease'):
                engine.release(lane, 'Owner selected a new task; previous work preserved', resume=True)
                engine.refresh(lane)
                if lane.get('lease'):
                    raise ValueError('Previous claim is not closed; lane remains paused')
            if lane.get('resume'):
                engine.park_continuation(lane, reason='owner_selected_new_task')
        policy = lane.setdefault('policy', {})
        if order is not None:
            policy['order'] = order
        if allow_attempted is not None:
            policy['allow_attempted'] = allow_attempted
        if max_attempts is not None:policy['max_attempts']=max_attempts
        if switch_now:
            policy.pop('address', None)
            policy.pop('band', None)
            lane.update(paused=False, ready=True, explicit_resume=True, state='idle')
            lane.pop('blocked_reason', None)
            lane.pop('admission_not_before', None)
        if paused is not None:
            lane['paused']=paused
            lane['explicit_resume']=not paused
            if not paused: lane['ready']=True; lane.pop('blocked_reason',None)
        lane.pop('fresh_empty_bands', None)
        if lane.pop('attempt_pool_empty',False):lane.pop('admission_not_before',None)
        engine.store.put_lane(lane)
        engine.store.set('policy_miss:'+route, None)
        engine.store.event('lane_policy_changed', {'route_token':route, 'order':policy.get('order','largest'),
                           'allow_attempted':policy.get('allow_attempted',True), 'switched_task':switch_now})
        return lane


def queue(store, route, **options):
    from uuid import uuid4
    validate(options.get('max_attempts'))
    if options.get('order') is not None and options['order'] not in ORDERS:
        raise ValueError('Unknown task order')
    if options.get('allow_attempted') is not None and type(options['allow_attempted']) is not bool:
        raise ValueError('Attempted-work option must be boolean')
    lane=store.lane(route)
    if lane is None: raise ValueError('Unknown lane')
    if lane['state'] in ('setup','claim_intent','preparing','closed_requires_reconciliation'):
        raise ValueError('Wait for assignment delivery or finish reconciliation before changing this lane')
    with store.lock('lane-controls'):
        key='lane_control:'+route
        if store.get(key): raise ValueError('This lane already has a pending change; wait for it to finish')
        request={'id':str(uuid4()),'options':options}
        store.set(key,request)
        store.event('lane_control_queued',{'route_token':route})
        return request


def process(engine, lane):
    key='lane_control:'+lane['route_token']
    request=engine.store.get(key)
    if not request: return
    configure(engine,lane['route_token'],**request['options'])
    with engine.store.lock('lane-controls'):
        if engine.store.get(key)==request: engine.store.set(key,None)
    engine.refresh(lane)
