"""Read-only presentation of durable local events, metrics and per-lane history."""
from datetime import datetime, timezone
import time

from . import __version__
from .live_best import metrics
from .presentation import clean, snapshot


def age(value, now=None):
    if value is None: return '—'
    seconds=max(0,int((time.time() if now is None else now)-value))
    if seconds<60:return str(seconds)+'s'
    if seconds<3600:return str(seconds//60)+'m'
    return str(seconds//3600)+'h '+str(seconds%3600//60)+'m'


def clock(value):
    if not value:return '—'
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).strftime('%H:%M UTC')
    except (ValueError,TypeError):return 'unknown'


class Dashboard:
    def __init__(self,store):
        self.store=store
        self.cursor=0
        self.checks=[]
        self.activity=[]
        self.claim_routes={}
        self.counts={}

    def update(self):
        lanes=self.store.lanes()
        for lane in lanes:
            claim_ids=[(lane.get('lease') or {}).get('claim_id'),(lane.get('resume') or {}).get('previous_claim_id')]
            claim_ids += [p['continuation']['previous_claim_id'] for p in lane.get('parked',[])]
            claim_ids += [p['payload']['claim_id'] for p in lane.get('closed_lease_notices',[])]
            for claim in claim_ids:
                if claim:self.claim_routes[claim]=lane['route_token']
        while True:
            events=self.store.events_after(self.cursor)
            if not events:break
            for event in events:
                self.cursor=event['id']
                data=event['data'];kind=event['kind'];claim=data.get('claim_id')
                route=data.get('route_token') or self.claim_routes.get(claim)
                if kind=='check_complete':
                    record=self.store.get('check:'+claim+':'+data['source_sha256']) or {}
                    if record.get('state')!='complete':continue
                    result=record['result'];values=metrics(result)
                    full=(result.get('diagnostics') or {}).get('full_extent_ndiff_insn')
                    if type(full) is int and full>=0:values['full_extent_ndiff_insn']=full
                    self.counts[claim]=self.counts.get(claim,0)+1
                    self.checks.append({'claim_id':claim,'route':route,'index':data.get('check_index',self.counts[claim]),
                                        'at':event['created'],'source':data['source_sha256'],'status':result.get('status'),
                                        'pack':result.get('pack_sha256'),'metrics':values})
                self.activity.append({'id':event['id'],'at':event['created'],'kind':kind,'route':route,
                                      'claim_id':claim,'type':clean(data.get('type',''),60),
                                      'message':clean(data.get('message',''),400)})
            self.activity=self.activity[-1000:]
        data=snapshot(self.store,__version__)
        for row in data['lanes']:
            lane=next(x for x in lanes if x['route_token']==row['route_token'])
            row['band']=lane.get('policy',{}).get('band','any')
            row['order']=lane.get('policy',{}).get('order','largest')
            row['allow_attempted']=lane.get('policy',{}).get('allow_attempted',True)
            row['attempt_pool_empty']=bool(lane.get('attempt_pool_empty'))
            row['control_pending']=bool(self.store.get('lane_control:'+row['route_token']))
            row['waiting_fresh']=bool(lane.get('fresh_empty_bands')) and not row['has_active_lease'] and not row['allow_attempted']
        return data

    def history(self,route,metric='ndiff_insn'):
        best={};rows=[]
        for check in self.checks:
            if (check['route'] or self.claim_routes.get(check['claim_id']))!=route:continue
            score=check['metrics'].get(metric)
            if type(score) is not int:continue
            scope=(check['pack'] or check['claim_id'],metric)
            previous=best.get(scope)
            improved=previous is not None and score<previous
            if previous is None or score<previous:best[scope]=score
            rows.append({**check,'score':score,'improved':improved,'delta':previous-score if improved else None})
        return rows

    def progress(self,route,claim,metric='ndiff_insn',now=None):
        history=self.history(route,metric)
        rows=[x for x in history if x['claim_id']==claim]
        if not rows:return {'best':None,'checks_since_best':None,'since_best':'—','last_improvement':None}
        current_pack=rows[-1]['pack'];rows=[r for r in history if r['pack']==current_pack and (current_pack is not None or r['claim_id']==claim)]
        # The first baseline is a best observation, not a claimed improvement.
        best=min(r['score'] for r in rows)
        first=next(r for r in rows if r['score']==best)
        return {'best':best,'checks_since_best':len(rows)-rows.index(first)-1,
                'since_best':age(first['at'],now),'last_improvement':next((r for r in reversed(rows) if r['improved']),None)}
