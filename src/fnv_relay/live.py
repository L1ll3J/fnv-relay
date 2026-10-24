"""Read-only live progress from local state; never calls a project or Notion service."""
from datetime import datetime, timezone
import select
import sys

from . import __version__, presentation


def event_lines(store, event, labels):
    data=event['data']; kind=event['kind']
    if not isinstance(data,dict):return []
    when=datetime.fromtimestamp(event['created'],timezone.utc).strftime('%H:%M:%S UTC')
    label=labels.get(data.get('claim_id')) or labels.get(data.get('route_token')) or data.get('claim_id') or 'Relay'
    prefix='['+when+'] '+presentation.clean(label)
    if kind=='check_complete':
        record=store.get('check:'+data['claim_id']+':'+data['source_sha256']) or {}
        record=record if isinstance(record,dict) else {}
        result=record.get('result') or {}
        result=result if isinstance(result,dict) else {}
        checker=result.get('checker') or {}
        checker=checker if isinstance(checker,dict) else {}
        score=checker.get('ndiff_insn'); extent=checker.get('extent')
        errors=checker.get('errors',[])
        errors=errors if isinstance(errors,list) else [errors]
        lines=[prefix+' | '+presentation.clean(result.get('candidate_id',data['source_sha256'][:12]))
               +' | '+presentation.clean(result.get('status',data.get('status')))
               +' | instruction differences: '+presentation.clean(score)
               +' | bytes: '+presentation.clean(extent)+' | errors: '+str(len(errors))]
        lines.extend('  '+presentation.clean(error,400) for error in errors[:3])
        if len(errors)>3:lines.append('  '+str(len(errors)-3)+' more diagnostics are saved with this result.')
        diff=checker.get('diff','')
        if isinstance(diff,str):
            changed=[line for line in diff.splitlines() if line.lstrip().startswith(('ORIG','YOURS'))]
            lines.extend('  '+presentation.clean(line,240) for line in changed[:6])
            if len(changed)>6:lines.append('  Additional instruction differences are saved with this result.')
        return lines
    if kind=='lane_error':
        return [prefix+' | ERROR '+presentation.clean(data.get('type'))+': '+presentation.clean(data.get('message'),400)]
    if kind in ('claim_released','claim_expired_observed','claim_denied','continuation_parked','claim_reconciled_no_active_ownership'):
        return [prefix+' | '+kind.replace('_',' ')+' | '+presentation.clean(data.get('address') or data.get('claim_id'))]
    return []


def watch(store, show=print, *, worker_alive, wait=None, read_line=None):
    wait=wait or (lambda timeout:bool(select.select([sys.stdin],[],[],timeout)[0]))
    read_line=read_line or sys.stdin.readline
    cursor=store.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
    labels={};previous=None
    show('\nLive updates — press Enter to return to the menu. The worker will keep running.')
    show('Times are UTC. Instruction counts are the official checker metric; displayed errors are checker diagnostics.')
    active_claims=set()
    for lane in store.lanes():
        labels[lane['route_token']]=lane.get('label','Lane')
        claim=(lane.get('lease') or {}).get('claim_id')
        if claim:
            active_claims.add(claim);labels[claim]=lane.get('label','Lane')
        error=store.get('last_error:'+lane['route_token']) or {}
        if error:
            show('Last recorded error (may be historical) — '+presentation.clean(lane.get('label','Lane'))+': '
                 +presentation.clean(error.get('type'))+' — '+presentation.clean(error.get('message'),400))
    latest={}
    for event in store.events(200):
        if event['id'] <= cursor and event['kind']=='check_complete' and event['data'].get('claim_id') in active_claims:
            latest[event['data']['claim_id']]=event
    for event in latest.values():
        show('Latest recorded check:')
        for line in event_lines(store,event,labels):show(line)
    while True:
        snapshot=presentation.snapshot(store,__version__)
        for lane in snapshot['lanes']:
            labels[lane['route_token']]=lane['label']
            if lane['claim_id']:labels[lane['claim_id']]=lane['label']
        current=presentation.human_status(snapshot)
        if current!=previous:
            show(current);previous=current
        for event in store.events_after(cursor):
            for line in event_lines(store,event,labels):show(line)
            cursor=event['id']
        if not worker_alive():
            show('Worker is stopped. Use Start relay or Show recent worker output from the menu.')
            return
        if wait(1):
            read_line()
            return
