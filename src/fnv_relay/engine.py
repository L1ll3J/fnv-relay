"""Durable, model-free lane scheduling. Remote messages are data, never commands."""
from __future__ import annotations
import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import random
import subprocess
import sys
import tarfile
import time
import uuid
from .store import Store, AlreadyRunning, UncertainOperation, write_json
from .project import ProjectError
from .lifecycle import process_identity, owner_alive
from . import compiler, context, guidance, attempts, official_tools

PROTOCOL = 'fnv-relay/2'
BANDS = ['<=255', '256-511', '512-1K', '1K-4K', '4K+']
NEXT_ACTION = {'poll_outbox':True, 'after_seconds':30, 'until':'new assignment or explicit paused/cooldown/error', 'instructions':'During the current turn, use Python to run `import time; time.sleep(30)`, then read the lane outbox again. Repeat until a new assignment/result or an explicit paused, cooldown, or error state appears. This means waiting and checking within the active turn, not polling between user messages.'}

def sha(data): return hashlib.sha256(data).hexdigest()
def canonical(data): return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
def epoch(value): return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
def band(size): return BANDS[0] if size <= 255 else BANDS[1] if size < 512 else BANDS[2] if size <= 1024 else BANDS[3] if size <= 4096 else BANDS[4]
def submission_accepted(response):
    return (isinstance(response,dict)
            and isinstance(response.get('submission_id'),str)
            and re.fullmatch(r'sub_[A-Za-z0-9]+',response['submission_id']) is not None
            and str(response.get('state','')).startswith('queued for verification'))

def check_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', value): raise ValueError('Invalid message/candidate identifier')
    return value

def extract(archive, target):
    target = Path(target); target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        total = 0; names = set()
        for member in tar.getmembers():
            p = Path(member.name)
            if p.is_absolute() or '..' in p.parts or (not member.isfile() and not member.isdir()) or member.name in names: raise ValueError('Unsafe or duplicate pack archive member')
            total += member.size; names.add(member.name)
            if total > 128 * 1024 * 1024: raise ValueError('Pack exceeds extraction ceiling')
        tar.extractall(target, filter='data')

class Engine:
    def __init__(self, store, project, notion, *, checker=compiler.check, clock=time.time, watchdog=True, should_stop=None):
        self.store, self.project, self.notion = store, project, notion
        self.checker, self.clock, self.watchdog = checker, clock, watchdog
        self.config = store.config()
        self.owner = process_identity()
        self._should_stop = should_stop or (lambda: False)
        self._locked_routes = set()

    def stop_requested(self):
        return bool(self._should_stop() or self.store.get('shutdown_requested'))

    def admission_blocked(self):
        return self.stop_requested() or bool(self.store.get('drain'))

    @contextmanager
    def lane_lock(self, route):
        # Reentrant only within this Engine; other processes still use flock.
        if route in self._locked_routes:
            yield
            return
        with self.store.lock('lane-' + sha(route.encode())[:40]):
            self._locked_routes.add(route)
            try:
                yield
            finally:
                self._locked_routes.remove(route)

    def refresh(self, lane):
        current = self.store.lane(lane['route_token'])
        if current is None:
            raise ValueError('Lane no longer exists')
        lane.clear(); lane.update(current)
        return lane

    def ready_for_claim(self, lane):
        if not lane.get('ready') or lane.get('paused'):
            return False
        if lane.get('resume') or lane.get('policy', {}).get('auto_resume_unfinished', False):
            return lane.get('explicit_resume') is True or self.clock() - lane.get('last_heartbeat', 0) <= 900
        return True

    def pause_lane(self, route, release=False):
        with self.lane_lock(route):
            lane = self.store.lane(route)
            if lane is None: raise ValueError('Unknown lane')
            lane['paused'] = True; lane['explicit_resume'] = False
            lane.pop('blocked_reason', None)
            self.store.put_lane(lane)
            if release and lane.get('lease'):
                self.release(lane, 'Owner paused lane and requested release')
            return self.store.lane(route)

    def park_continuation(self, lane, reason='function_paused'):
        resume = lane.get('resume')
        if not resume: return
        parked = lane.setdefault('parked', [])
        if not any(x['continuation']['previous_claim_id'] == resume['previous_claim_id'] for x in parked):
            parked.append({'continuation':dict(resume), 'reason':reason, 'parked_at':self.clock(), 'notified':False})
        lane.pop('resume', None)
        lane.pop('blocked_reason', None)
        denial = lane.get('last_claim_denial') or {}
        if denial.get('status') == 403 and denial.get('retry_after') is None:
            lane.pop('admission_not_before', None)
        lane.update(state='idle', paused=False)
        self.store.put_lane(lane)
        self.store.event('continuation_parked', {'route_token':lane['route_token'], 'address':resume['address']})

    def publish_parked(self, lane):
        for entry in lane.get('parked', []):
            if entry.get('notified'): continue
            resume = entry['continuation']
            self.publish(lane, 'parked:'+lane['route_token']+':'+resume['previous_claim_id']+':'+entry['reason'],
                         {'status':'CONTINUATION_PARKED', 'address':resume['address'],
                          'previous_claim_id':resume['previous_claim_id'], 'reason':entry['reason'],
                          'work_preserved':True, 'next_task':'next_eligible_function', 'next_action':dict(NEXT_ACTION)})
            entry['notified'] = True
            self.store.put_lane(lane)

    def publish_closed_leases(self, lane):
        for notice in lane.get('closed_lease_notices', []):
            if notice.get('notified'): continue
            self.publish(lane, 'lease-closed:'+lane['route_token']+':'+notice['payload']['claim_id'], notice['payload'])
            notice['notified'] = True
            self.store.put_lane(lane)

    def resume_parked(self, route, address):
        with self.lane_lock(route):
            lane = self.store.lane(route)
            if lane is None: raise ValueError('Unknown lane')
            if lane.get('lease') or lane['state'] not in ('idle','resume_pending'):
                raise ValueError('Finish or release the current claim before selecting saved work')
            selected = next((x for x in lane.get('parked',[]) if x['continuation']['address'].lower() == address.lower()), None)
            if selected is None: raise ValueError('No parked continuation for this address')
            if lane.get('resume'): self.park_continuation(lane, 'owner_selected_other_saved_task')
            lane['parked'].remove(selected)
            lane['resume'] = selected['continuation']
            lane['resume']['not_before'] = self.clock()
            lane.update(state='resume_pending', paused=False, ready=True, explicit_resume=True)
            lane.pop('blocked_reason',None)
            self.store.put_lane(lane)
            return lane

    def resume_lane(self, route):
        with self.lane_lock(route):
            lane = self.store.lane(route)
            if lane is None: raise ValueError('Unknown lane')
            lane.update(paused=False, ready=True, explicit_resume=True)
            lane.pop('blocked_reason', None)
            self.store.put_lane(lane)
            return lane

    def publish(self, lane, key, payload):
        record = {'protocol': PROTOCOL, 'kind': 'RESULT', 'sender': 'controller', 'route_token': lane['route_token'], 'message_id': key, 'payload': payload}
        # Append only once; if the connection broke, reconcile by exact readback.
        prior = self.store.operation_record('notion-append:' + key)
        if prior and prior['status'] != 'response':
            if record in self.notion.messages(lane['outbox']):
                self.store.set('reconciled:' + key, True); return
            raise UncertainOperation('Uncertain mailbox append; inspect with reconcile')
        self.store.operation('notion-append:' + key, record, lambda: self.notion.append(lane['outbox'], record))
        if record not in self.notion.messages(lane['outbox']): raise RuntimeError('Mailbox readback did not confirm result')

    def model_label(self, lane):
        label = lane.get('policy', {}).get('model_label', 'ChatGPT Web via Notion')
        if not isinstance(label,str) or not 1 <= len(label) <= 120 or not label.strip() or not label.isprintable():
            raise ValueError('Model label must contain 1..120 characters without control characters')
        return label

    def create_lane(self, parent, policy, label='Pro lane'):
        attempts.validate(policy.get('max_attempts'))
        self.model_label({'policy':policy})
        if any(row.get('state') == 'setup' for row in self.store.lanes()):
            raise ValueError('Finish the incomplete lane setup before adding another lane')
        token = str(uuid.uuid4()); lane = {'route_token': token, 'label': label, 'state': 'setup', 'policy': policy, 'sequence': 0, 'seen': [], 'parent_page':parent}
        self.store.put_lane(lane)
        return self.finish_lane_setup(lane)

    def setup_operation(self, key, args, action, readback, retry_missing):
        digest = sha(canonical(args)); operation = self.store.operation_record(key)
        if operation and operation['args_hash'] != digest:
            raise ValueError('Lane setup operation arguments changed')
        if operation and operation['status'] not in ('response','not_sent'):
            resolution = self.store.get('lane-setup-resolution:'+key)
            if resolution:
                if resolution.get('args_hash') != digest: raise ValueError('Lane recovery binding differs')
                if 'result' in resolution: return resolution['result']
                return self.setup_operation(resolution['retry_key'],args,action,readback,retry_missing)
            found, result = readback()
            receipt = {'args_hash':digest,'readback_at':self.clock()}
            if found:
                receipt['result'] = result
                self.store.set('lane-setup-resolution:'+key,receipt)
                return result
            if not retry_missing:
                raise UncertainOperation('No matching Notion page was found. Owner confirmation is required before a new creation attempt.')
            receipt.update(retry_key=key+':retry:'+uuid.uuid4().hex, owner_authorized_missing_retry=True)
            self.store.set('lane-setup-resolution:'+key,receipt)
            return self.setup_operation(receipt['retry_key'],args,action,readback,False)
        return self.store.operation(key,args,action)

    def setup_page_readback(self, parent, title, record):
        matches = []
        for child in self.notion.child_pages(parent):
            if child.get('title') == title and record in self.notion.messages(child['id']):
                matches.append(child['id'])
        if len(matches) > 1: raise UncertainOperation('Multiple matching lane pages need review')
        return bool(matches), matches[0] if matches else None

    def ensure_work_guidance(self, lane, *, retry_missing=False):
        key,args=guidance.publication(lane)
        if self.store.get('guidance-confirmed:'+key):return
        record=args['record']
        self.setup_operation(key,args,lambda:self.notion.append(lane['landing'],record),
                             lambda:(record in self.notion.messages(lane['landing']),None),retry_missing)
        if record not in self.notion.messages(lane['landing']):raise RuntimeError('Work guidance readback failed')
        self.store.set('guidance-confirmed:'+key,True)
        self.store.event('work_guidance_published',{'route_token':lane['route_token'],'record_sha256':sha(canonical(record))})

    def finish_lane_setup(self, lane, *, retry_missing=False):
        if lane.get('state') != 'setup': raise ValueError('Lane setup is already complete')
        token, label, policy = lane['route_token'],lane['label'],lane['policy']
        parent = lane.get('parent_page') or self.config.get('notion_parent_page')
        if not parent: raise ValueError('Lane parent page is required')
        lane['parent_page'] = parent
        for role in ('inbox', 'outbox', 'data', 'landing'):
            record = {'protocol': PROTOCOL, 'route_token': token, 'role': role}
            title = label+' '+role
            lane[role] = self.setup_operation('create:'+token+':'+role, {'parent':parent,'title':title},
                lambda title=title,record=record:self.notion.create_page(parent,title,[record]),
                lambda title=title,record=record:self.setup_page_readback(parent,title,record),retry_missing)
            self.store.put_lane(lane)
        introduction = {'next_action':dict(NEXT_ACTION), 'protocol': PROTOCOL, 'kind': 'LANE', 'route_token': token, 'inbox': lane['inbox'], 'outbox': lane['outbox'], 'data': lane['data'], 'lease_check_limit': None, 'policy': policy,
                        'instructions': 'Register by appending HELLO JSON to the inbox. Read subsequent assignments/results from outbox. No lifetime check cap; owner/lease/batch/timeouts remain. Original source is private context. Supplemental findings never override current assignment.',
                        'hello': {'protocol': PROTOCOL, 'kind': 'HELLO', 'sender': 'chatgpt', 'route_token': token, 'message_id': 'hello-' + token},
                        'candidate_delivery': 'Create CANDIDATE pages under data, then post BATCH with candidates:[{candidate_id,page}]. Copy exact route/claim_id/pack_sha256 from ASSIGNED. Complete source or invertible literal_edits against a delivered source_sha256. See assignment schema.'}
        self.setup_operation('landing:'+token, introduction, lambda:self.notion.append(lane['landing'],introduction), lambda:(introduction in self.notion.messages(lane['landing']),None),retry_missing)
        if introduction not in self.notion.messages(lane['landing']): raise RuntimeError('Landing readback failed')
        self.ensure_work_guidance(lane,retry_missing=retry_missing)
        lane['state'] = 'waiting_hello'; self.store.put_lane(lane); return lane

    def guard(self, status, claim_id=None):
        if status.get('strikes_last_7_days', 0) or status.get('submissions_paused'): raise RuntimeError('Account health blocks work; inspect official status')
        if any(r.get('verdict') in ('failed', 'rejected', 'needs_review') for r in status.get('recent', [])): raise RuntimeError('Verification rejection requires review')
        if claim_id and not any(c.get('claim_id') == claim_id and c.get('this_key') for c in status['open_claims']): raise RuntimeError('Claim is no longer owned/live')

    def select(self, lane, status):
        policy = lane['policy']
        if self.store.get('policy_miss:'+lane['route_token']) == sha(canonical(policy)): return None
        limits = status['limits']; own = [c for c in status['open_claims'] if c.get('this_key')]
        if len(status['open_claims']) >= limits['claims'] or status['submissions_today'] + len(own) >= limits['subsPerDay'] or status['awaiting_verification'] + len(own) >= limits['pending']: return None
        parked_addresses = {x['continuation']['address'].lower() for x in lane.get('parked',[]) if x.get('reason') != 'function_paused'}
        if policy.get('address') and policy['address'].lower() not in parked_addresses and not attempts.capped(self.store,lane,policy['address']):
            paused_history = any(x.get('reason')=='function_paused' and x['continuation']['address'].lower()==policy['address'].lower() for x in lane.get('parked',[]))
            if not paused_history or self.project.call('function_info', {'address':policy['address']}).get('state','').split(' ',1)[0].lower() == 'open':
                return {'address': policy['address'], 'context_mode': 'resume'}
        # Random selection shuffles size bands; the service chooses within a band.
        available = {r['band']: r for r in self.project.call('list_bands', {})['bands']}
        order = policy.get('order', 'largest')
        if order not in ('largest', 'smallest', 'random'): raise ValueError('Unsupported selection order')
        choices = [policy['band']] if policy.get('band') else list(BANDS)
        bounds = [(0,255),(256,511),(512,1024),(1025,4096),(4097,2**31)]
        choices = [b for b in choices if bounds[BANDS.index(b)][1] >= policy.get('min_bytes',0) and bounds[BANDS.index(b)][0] <= policy.get('max_bytes',2**31)]
        if order == 'random':
            # Random band priority; the API chooses a function within the band.
            choices = [b for b in choices if available.get(b, {}).get('allowed') and available[b].get('open', 0) > available[b].get('reserved_for_new', 0)]
            random.shuffle(choices)
        elif order == 'largest': choices.reverse()
        for b in choices:
            row = available.get(b, {})
            empty = lane.get('fresh_empty_bands', {}).get(b, 0) if policy.get('allow_attempted',True) is False else 0
            if empty > self.clock(): continue
            if row.get('allowed') and row.get('open', 0) > row.get('reserved_for_new', 0):
                if policy.get('max_attempts') or policy.get('band'):
                    chosen=attempts.selection(self,lane,b)
                    if chosen:
                        lane.pop('attempt_pool_empty',None);self.store.put_lane(lane)
                        return chosen
                    continue
                return {'band': b, 'prefer': 'any' if policy.get('allow_attempted',True) else 'fresh', 'context_mode': 'resume'}
        if policy.get('max_attempts') or policy.get('band'):
            lane['attempt_pool_empty']=True;lane['admission_not_before']=self.clock()+60;self.store.put_lane(lane)
        return None

    def defer_claim(self, lane, denial):
        retry = max(300,float(denial.get('retry_after') or 0))
        deadline = denial.get('observed_at',self.clock()) + retry
        selected = lane.get('claim_selection', {})
        if denial['status'] == 404 and selected.get('prefer') == 'fresh' and not lane.get('resume'):
            lane.setdefault('fresh_empty_bands', {})[selected['band']] = deadline
            lane.update(state='idle', admission_not_before=0, last_claim_denial=denial)
            self.store.put_lane(lane)
            self.store.event('fresh_band_empty',{'route_token':lane['route_token'],'band':selected['band'],'retry_at':deadline})
            return

        resume = lane.get('resume')
        lane.update(state='resume_pending' if resume else 'idle', admission_not_before=deadline, last_claim_denial=denial)
        if resume:
            resume.update(not_before=deadline,retries=resume.get('retries',0)+1); lane['resume']=resume
        if denial['status'] in (429,503): self.store.set('project_not_before',max(deadline,self.store.get('project_not_before') or 0))
        if denial['status'] in (401,403):
            lane.update(paused=True, blocked_reason='fnv_access_denied')
        self.store.put_lane(lane)
        self.store.event('claim_denied',{'route_token':lane['route_token'],'status':denial['status'],'not_before':deadline})

    def recover_claim_intent(self, lane):
        with self.lane_lock(lane['route_token']):
            self.refresh(lane)
            if lane['state']!='claim_intent': return None
            record=self.store.operation_record('claim:'+lane['route_token']+':'+str(lane['sequence']))
            op='claim:'+lane['route_token']+':'+str(lane['sequence'])
            resolution=self.store.get('claim-resolution:'+op)
            if record and resolution and resolution.get('args_hash')==record['args_hash']:
                lane['state']='resume_pending' if lane.get('resume') else 'idle'
                self.store.put_lane(lane); return 'no_active_claim_confirmed'
            if not record or record['status']!='response': return None
            response=record['result']
            if response.get('_relay_cancelled'):
                lane['state']='resume_pending' if lane.get('resume') else 'idle'; self.store.put_lane(lane); return 'cancelled'
            if '_relay_definite_denial' in response:
                self.defer_claim(lane,response['_relay_definite_denial']); return 'denied'
            cid=check_id(response['claim_id']); directory=self.store.root/'claims'/cid; directory.mkdir(parents=True,exist_ok=True)
            write_json(directory/'claim.json',response)
            lane['lease']={'claim_id':cid,'address':response['address'],'size':response['size'],'expires':response.get('expires_at') or response['expires'],'directory':str(directory),'lease_check_limit':None,'owner':lane.get('claim_owner')}
            lane['state']='preparing'; self.store.put_lane(lane)
            return 'owned_claim_recovered'

    def assign(self, lane):
        with self.lane_lock(lane['route_token']):
            self.refresh(lane)
            if self.admission_blocked() or not self.ready_for_claim(lane) or lane['state'] not in ('idle', 'resume_pending'): return
            attempts.tick(self,lane)
            if lane.get('attempt_handoff'):return
            resume = lane.get('resume')
            deadline = max(lane.get('admission_not_before', 0), self.store.get('project_not_before') or 0, (resume or {}).get('not_before', 0))
            if self.clock() < deadline: return
            status = self.project.call('status', {}); self.guard(status)
            paused_until = status.get('claiming_paused_until')
            if paused_until:
                try: deadline = float(paused_until)
                except (ValueError, TypeError): deadline = epoch(paused_until)
                if deadline > self.clock():
                    lane['admission_not_before'] = deadline; self.store.put_lane(lane); return
            if resume:
                info = self.project.call('function_info', {'address':resume['address']})
                if not isinstance(info,dict) or info.get('address') != resume['address'] or not isinstance(info.get('state'),str):
                    raise ValueError('Continuation eligibility was not confirmed')
                if info['state'].split(' ',1)[0].lower() == 'paused':
                    self.park_continuation(lane)
                    self.publish_parked(lane)
                    resume = None
            args = {'address': resume['address'], 'context_mode': 'resume'} if resume else self.select(lane, status)
            if args is None or self.admission_blocked(): return
            official_tools.current(self.store, self.project, self.config['paths'], now=self.clock())
            if self.admission_blocked(): return
            lane['claim_selection'] = dict(args)
            lane['sequence'] += 1; lane['state'] = 'claim_intent'; lane['claim_owner']=self.owner; self.store.put_lane(lane)
            op = 'claim:' + lane['route_token'] + ':' + str(lane['sequence'])
            def claim_once():
                if self.admission_blocked(): return {'_relay_cancelled':True}
                # Confirmed denials are durable responses, never unknown claim intents.
                try:
                    return self.project.call('claim', args)
                except ProjectError as error:
                    if not error.uncertain and error.status in (400, 401, 403, 404, 409, 422, 429, 503):
                        return {'_relay_definite_denial': {'status': error.status, 'retry_after': error.retry_after, 'code': error.code, 'observed_at':self.clock()}}
                    raise
            claim = self.store.operation(op, args, claim_once)
            if claim.get('_relay_cancelled'):
                lane['state']='resume_pending' if resume else 'idle'; self.store.put_lane(lane); return
            if '_relay_definite_denial' in claim:
                self.defer_claim(lane,claim['_relay_definite_denial']); return
            cid = check_id(claim['claim_id']); directory = self.store.root/'claims'/cid; directory.mkdir(parents=True, exist_ok=True)
            write_json(directory/'claim.json', claim)
            lease = {'claim_id': cid, 'address': claim['address'], 'size': claim['size'], 'expires': claim.get('expires_at') or claim['expires'], 'directory': str(directory), 'lease_check_limit': None, 'owner':self.owner}
            lane.update(state='preparing', lease=lease, explicit_resume=False); self.store.put_lane(lane)
            if 'flags' in claim: lease['claim_flags']=claim['flags']; self.store.put_lane(lane)
            if self.watchdog:
                argv = [sys.executable, '-m', 'fnv_relay.watch', '--state-dir', str(self.store.root), '--claim-id', cid]
                if self.owner: argv += ['--owner-pid', str(self.owner['pid']), '--owner-start-time', self.owner['start_time']]
                with (directory/'watchdog.log').open('ab') as log:
                    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                write_json(directory/'watchdog.json', {'pid': process.pid, 'owner':self.owner, 'model_calls': 0})
        self.prepare_assignment(lane)

    def prepare_assignment(self, lane):
        self.refresh(lane)
        lease = lane.get('lease')
        if lane['state'] != 'preparing' or not lease: return
        cid = lease['claim_id']; directory = Path(lease['directory'])
        resume = lane.get('resume')
        if resume is not None and lease['address'] != resume['address']:
            self.release(lane, 'Continuation response selected a different address', resume=False)
            with self.lane_lock(lane['route_token']):
                self.refresh(lane)
                if not lane.get('lease'):
                    lane['resume']=resume; lane['resume']['not_before']=self.clock()+300; lane['state']='resume_pending'; self.store.put_lane(lane)
            self.store.set('last_error:'+lane['route_token'],{'type':'ValueError','message':'Continuation returned an unexpected address; requested address retained'})
            return
        if not self.preparing_current(lane, cid): return
        packet_file = directory/'packet.json'
        packet = json.loads(packet_file.read_text()) if packet_file.exists() else self.project.call('packet', {'claim_id': cid, 'context_mode': 'resume'})
        if not packet_file.exists(): write_json(packet_file, packet)
        if not self.preparing_current(lane, cid): return
        archive = directory/'pack.tar.gz'
        if lease.get('pack_sha256'):
            if sha(archive.read_bytes()) != lease['pack_sha256']: raise ValueError('Saved assignment pack changed')
        else:
            self.project.download_pack(packet['pack_url'], archive)
        extract(archive, directory/'pack')
        if not self.preparing_current(lane, cid): return
        pack = json.loads((directory/'pack/pack.json').read_text())
        if pack['address'] != lease['address'] or pack['size'] != lease['size']: raise ValueError('Pack function identity differs')
        if 'claim_flags' in lease and pack['flags']!=lease['claim_flags']: raise ValueError('Original pack flags differ from the current claim')
        lease['pack_sha256'] = sha((directory/'pack.tar.gz').read_bytes()); lease['flags'] = pack['flags']
        with self.lane_lock(lane['route_token']):
            if not self.preparing_current(lane, cid): return
            lane['lease'] = lease; self.store.put_lane(lane)
        low, high = lane['policy'].get('min_bytes', 0), lane['policy'].get('max_bytes', 2**31)
        if not low <= lease['size'] <= high:
            self.release(lane, 'Selected function outside configured size filter')
            self.store.set('policy_miss:'+lane['route_token'], sha(canonical(lane['policy'])))
            self.publish(lane,'filter-miss:'+cid,{'status':'FILTER_MISS_ADMISSION_PAUSED','bytes':lease['size'],'instructions':'Project API selects by band rather than exact size. Choose a band-aligned filter or an explicit address in a new lane.'}); return
        key = 'assigned:' + cid
        files = [p for p in (directory/'pack').rglob('*') if p.is_file() and p.suffix in ('.cpp', '.h', '.hpp', '.json')]
        briefing = {k:packet[k] for k in ('best_attempts','best_attempts_note','continuation','history','signature','symbol','source_file','tips','rules','context_generation') if k in packet}
        if briefing:
            write_json(directory/'briefing.json',briefing); files.append(directory/'briefing.json')
        for attempt in packet.get('best_attempts',[]):
            code = attempt.get('code')
            if isinstance(code,str) and 0 < len(code.encode()) <= self.config.get('max_candidate_bytes',1048576):
                sources = directory/'sources'; sources.mkdir(exist_ok=True); raw = code.encode(); source = sources/(sha(raw)+'.cpp')
                if not source.exists(): source.write_bytes(raw)
        paths = self.tool_paths(lane)
        if paths.get('compiler_tips'): files.append(Path(paths['compiler_tips']))
        if paths.get('official_guidance'): files.append(Path(paths['official_guidance']))
        self.continuation_context(lane, directory, files)
        if not self.preparing_current(lane, cid): return
        files.append(context.export(paths, directory/'pack', directory/'context'))
        if not self.preparing_current(lane, cid): return
        pages = self.upload(lane, cid, files, preparing=True)
        if not self.preparing_current(lane, cid): return
        scope = {'route_token': lane['route_token'], 'claim_id': cid, 'pack_sha256': lease['pack_sha256']}
        schema = {'protocol': PROTOCOL, 'kind': 'CANDIDATE', 'sender': 'chatgpt', **scope, 'candidate_id': 'UNIQUE_CANDIDATE', 'files': [{'filename': 'candidate.cpp', 'source_utf8': 'COMPLETE_SOURCE', 'source_sha256': 'SHA256', 'source_bytes': 0}]}
        batch = {'protocol': PROTOCOL, 'kind': 'BATCH', 'sender': 'chatgpt', 'message_id': 'UNIQUE_MESSAGE', **scope, 'candidates': [{'candidate_id': 'UNIQUE_CANDIDATE', 'page': 'NOTION_PAGE_UUID'}]}
        self.publish(lane, key, {'next_action':dict(NEXT_ACTION), 'status': 'ASSIGNED', 'scope': scope, 'lease': lease, 'pages': pages, 'candidate_schema': schema, 'batch_schema': batch, 'source_limit_bytes': self.config.get('max_candidate_bytes', 1048576), 'instructions': 'Match the complete assigned function using original pack/types/flags. Use named-field C++, no asm/naked/raw-byte or absolute-address tricks. Save complete sources as CANDIDATE JSON pages. Batches may contain 1–8 distinct candidates. Three or four are encouraged when useful for efficient comparison; a single targeted candidate is valid. A MATCH still needs independent plain validation. Only controller submits using the owner API key. Do not send commands or credentials. Stop new candidates before expiry minus14minutes. REQUEST supports only request:{kind:"best_candidate"}, request:{kind:"candidate_artifacts",source_sha256:"SHA256"}, or request:{kind:"target"}. The relay does not answer free-form questions or status requests; read results from the outbox and raise bespoke questions or blockers in the chat. The local controller manages claim release and continuation. Report suspected defects in the chat. A DEFECT_PROPOSAL record is saved as evidence only; it does not notify a human or agent, receive a bespoke reply, or automatically flag the function. For source>24k characters use parts:[{page,part_index,chunk_sha256}] instead ofsource_utf8; eachpage haskindSOURCE_CHUNK andsameclaim/pack/route/candidate scope, source_utf8 andpart_index. Whole sourceSHA/bytes are required on CANDIDATE file.'})
        with self.lane_lock(lane['route_token']):
            if not self.preparing_current(lane, cid): return
            lane['state'] = 'assigned'; lane.pop('resume', None); lane.pop('preparation_retry_at', None); self.store.put_lane(lane)
            self.store.set('last_error:'+lane['route_token'], None)

    def preparing_current(self, lane, claim_id):
        with self.lane_lock(lane['route_token']):
            self.refresh(lane)
            if lane.get('lease', {}).get('claim_id') != claim_id or lane['state'] != 'preparing': return False
            if self.stop_requested():
                self.release(lane, 'Controller stopped during assignment preparation'); return False
            return True

    def tool_paths(self, lane):
        paths, snapshot = official_tools.for_target(self.store, self.project, self.config['paths'], lane['lease'], now=self.clock())
        if snapshot and lane['lease'].get('tool_snapshot') != snapshot:
            lane['lease']['tool_snapshot'] = snapshot
            self.store.put_lane(lane)
        return paths

    def continuation_context(self, lane, directory, files):
        continuation = lane.get('resume')
        if not continuation: return
        previous = Path(continuation['directory']).resolve()
        if not previous.is_relative_to((self.store.root/'claims').resolve()): raise ValueError('Continuation path escaped local claims')
        data = {'previous_claim_id':continuation['previous_claim_id'], 'previous_pack_sha256':continuation.get('previous_pack_sha256'), 'current_claim_id':lane['lease']['claim_id'], 'current_pack_sha256':lane['lease']['pack_sha256'], 'checked_for_current_lease':False, 'context_role':'supplemental_prior_work', 'primary_evaluation_input':'current_original_pack'}
        best_file = previous/'checked-best.json'
        if best_file.exists():
            best = json.loads(best_file.read_text()); source = Path(best['source_path']).resolve()
            if not source.is_relative_to(previous) or best.get('actually_checked') is not True or best.get('pack_sha256') != continuation.get('previous_pack_sha256'): raise ValueError('Continuation best source provenance differs')
            raw = source.read_bytes()
            if sha(raw) != best['source_sha256']: raise ValueError('Continuation source changed')
            target = directory/'sources'/(sha(raw)+'.cpp'); target.parent.mkdir(exist_ok=True); target.write_bytes(raw); files.append(target)
            data.update(source_sha256=sha(raw), previous_rank=best.get('rank'))
        notes = previous/'continuation-notes.json'
        if notes.exists():
            target = directory/'prior-notes.json'; target.write_bytes(notes.read_bytes()); files.append(target)
        target = directory/'continuation.json'; write_json(target,data); files.append(target)

    def upload(self, lane, cid, files, *, preparing=False):
        refs = []
        for file in files:
            raw = Path(file).read_bytes(); text = raw.decode(); parts = [text[i:i+24000] for i in range(0, len(text), 24000)] or ['']
            for i, part in enumerate(parts):
                if preparing and not self.preparing_current(lane, cid): return refs
                record = {'protocol': PROTOCOL, 'kind': 'CONTEXT', 'claim_id': cid, 'filename': Path(file).name, 'source_utf8': part, 'source_sha256': sha(raw), 'chunk_sha256': sha(part.encode()), 'part_index': i+1, 'part_count': len(parts)}
                key = 'file:' + lane['route_token'] + ':' + cid + ':' + sha(canonical(record))
                page = self.store.operation(key, record, lambda record=record: self.notion.create_page(lane['data'], Path(file).name, [record]))
                if record not in self.notion.messages(page): raise RuntimeError('Context page readback differs')
                refs.append({k: v for k, v in record.items() if k != 'source_utf8'} | {'page': page})
        return refs

    def candidate(self, lane, pointer):
        cid = check_id(pointer['candidate_id']); rows = [r for r in self.notion.messages(pointer['page']) if r.get('kind') == 'CANDIDATE' and r.get('candidate_id') == cid]
        if len(rows) != 1: raise ValueError('Missing or ambiguous candidate page')
        row = rows[0]; self.scope(lane, row); files = row.get('files', [])
        if len(files) != 1: raise ValueError('Candidate must have one source file')
        f = files[0]
        if 'literal_edits' in f:
            base = f['base_source_sha256']
            if not isinstance(base,str) or not re.fullmatch('[0-9a-f]{64}',base): raise ValueError('Invalid base source digest')
            source = self.store.root/'claims'/lane['lease']['claim_id']/'sources'/(base+'.cpp')
            raw = source.read_bytes()
            if sha(raw) != base: raise ValueError('Patch base hash differs')
            edits = f['literal_edits']
            if not isinstance(edits, list) or not 1 <= len(edits) <= 16: raise ValueError('Invalid edit count')
            original = raw
            for edit in edits:
                a, b = edit['before_utf8'].encode(), edit['after_utf8'].encode()
                if not a or not b or raw.count(a) != 1: raise ValueError('Patch must replace one unique nonempty literal')
                raw = raw.replace(a, b, 1)
            inverse = raw
            for edit in reversed(edits):
                a, b = edit['before_utf8'].encode(), edit['after_utf8'].encode()
                if inverse.count(b) != 1: raise ValueError('Patch inverse is ambiguous')
                inverse = inverse.replace(b, a, 1)
            if inverse != original: raise ValueError('Patch inverse differs')
        elif 'parts' in f:
            chunks = []
            if not isinstance(f['parts'], list) or len(f['parts']) > 100: raise ValueError('Too many source parts')
            for index, part in enumerate(f['parts'], 1):
                candidates = [x for x in self.notion.messages(part['page']) if x.get('kind') == 'SOURCE_CHUNK' and x.get('candidate_id') == cid and x.get('part_index') == index]
                if len(candidates) != 1 or part['part_index'] != index: raise ValueError('Ambiguous source chunk')
                chunk = candidates[0]; self.scope(lane, chunk); encoded = chunk['source_utf8'].encode()
                if sha(encoded) != part['chunk_sha256']: raise ValueError('Source chunk hash differs')
                chunks.append(encoded)
            raw = b''.join(chunks)
        else: raw = f['source_utf8'].encode()
        if not 0 < len(raw) <= self.config.get('max_candidate_bytes', 1048576) or sha(raw) != f['source_sha256'] or len(raw) != f['source_bytes']: raise ValueError('Source hash/size differs')
        return cid, raw

    def scope(self, lane, message):
        if message.get('protocol') != PROTOCOL or message.get('sender') != 'chatgpt': raise ValueError('Invalid sender/protocol')
        for key in ('route_token',):
            if message.get(key) != lane[key]: raise ValueError('Cross-lane message rejected')
        for key in ('claim_id', 'pack_sha256'):
            if message.get(key) != lane['lease'][key]: raise ValueError('Stale claim/pack message rejected')

    def batch(self, lane, message):
        self.scope(lane, message); mid = check_id(message['message_id']); pointers = message.get('candidates', [])
        publication = 'batch:'+lane['lease']['claim_id']+':'+mid
        binding_key = 'batch-input:'+publication
        identity = sha(canonical(message))
        bound = self.store.get(binding_key)
        if bound and bound != identity: raise ValueError('Batch message identifier already bound to different content')
        self.store.set(binding_key, identity)
        saved = self.store.get('batch-result:'+publication)
        if saved is not None:
            self.publish(lane, publication, saved)
            return
        prior = self.store.operation_record('notion-append:'+publication)
        if prior:
            # Older runs did not save the result payload locally. Recover only
            # the exact original record proven by the durable operation digest.
            records = [r for r in self.notion.messages(lane['outbox'])
                       if isinstance(r, dict) and r.get('message_id') == publication
                       and sha(canonical(r)) == prior['args_hash']]
            if not records: raise UncertainOperation('Saved batch publication requires exact outbox reconciliation; no checks replayed')
            self.store.set('batch-result:'+publication, records[0]['payload'])
            self.store.set('reconciled:'+publication, True)
            return
        try:
            if not isinstance(pointers, list) or not 1 <= len(pointers) <= 8: raise ValueError('Batch must contain1–8 candidates')
            prepared = [self.candidate(lane, pointer) for pointer in pointers]
            if len({cid for cid, _ in prepared}) != len(prepared): raise ValueError('Duplicate candidate identifier within batch')
            if len({sha(raw) for _, raw in prepared}) != len(prepared): raise ValueError('Duplicate source within batch')
        except (ValueError, KeyError, TypeError) as error:
            rejection = {'status':'BATCH_REJECTED', 'batch_message_id':mid,
                         'reason_code':'source_integrity_mismatch' if str(error) == 'Source hash/size differs' else 'invalid_candidate_batch',
                         'checks_performed':0, 'requires_new_batch_message_id':True,
                         'scope':{key:lane['lease'][key] for key in ('claim_id','pack_sha256')}}
            self.store.set('batch-result:'+publication, rejection)
            self.publish(lane, publication, rejection)
            if (self.store.get('last_error:'+lane['route_token']) or {}).get('message') == str(error):
                self.store.set('last_error:'+lane['route_token'], None)
            return
        paths = self.tool_paths(lane)
        if not attempts.reserve(self,lane,publication,identity):
            self.store.set('batch-result:'+publication,{'status':'BATCH_REJECTED','reason_code':'attempt_limit','checks_performed':0})
            attempts.tick(self,lane)
            return
        lease = dict(lane['lease']); directory = Path(lease['directory']); (directory/'sources').mkdir(exist_ok=True)
        results = []
        for cid, raw in prepared:
            if self.stop_requested(): break
            with self.lane_lock(lane['route_token']):
                self.refresh(lane); self.scope(lane, message)
                if lane.get('paused') or lane['state'] != 'assigned' or self.stop_requested() or self.clock()+840 >= epoch(lease['expires']): break
                with self.store.lock('lease-' + lease['claim_id']):
                    self.guard(self.project.call('status', {}), lease['claim_id'])
                    if self.stop_requested() or self.clock()+840 >= epoch(lease['expires']): break
                    if sha((directory/'pack.tar.gz').read_bytes()) != lease['pack_sha256']: raise ValueError('Original pack changed')
                    digest = sha(raw); base_key = 'check:' + lease['claim_id'] + ':' + digest
                    snapshot_id = (lane['lease'].get('tool_snapshot') or {}).get('id')
                    key = base_key+':tools:'+snapshot_id if snapshot_id else base_key
                    previous = self.store.get(key)
                    if previous:
                        if previous['state'] != 'complete': raise RuntimeError('Incomplete check reservation requires reconciliation; no replay')
                        results.append({**previous['result'], 'reused_prior_result': True}); continue
                    source = directory/'sources'/(digest+'.cpp')
                    if source.exists() and source.read_bytes() != raw: raise ValueError('Immutable candidate differs')
                    source.write_bytes(raw)
                    # Retain the original cache entry when adopting generation-scoped results.
                    if key != base_key and self.store.get(base_key) and not self.store.get(base_key+':legacy'):
                        self.store.set(base_key+':legacy', self.store.get(base_key))
                    index = self.store.get('checks:' + lease['claim_id']) or 0; self.store.set('checks:' + lease['claim_id'], index+1)
                    work = directory/'checks'/str(index+1)
                    self.store.set(key, {'state': 'reserved', 'source_sha256': digest, 'work': str(work)})
                    result = self.checker(paths | {'max_candidate_bytes': self.config.get('max_candidate_bytes', 1048576)}, directory/'pack', source, work)
                    if snapshot_id: result['tool_snapshot_id'] = snapshot_id
                    if result.get('source_sha256') != digest: raise RuntimeError('Compiler source evidence differs')
                    compact = {k: value for k, value in result.items() if k != 'versions'} | {'candidate_id': cid, 'source_sha256': digest}
                    suffix = '-'+snapshot_id if snapshot_id else ''
                    write_json(directory/('result-'+digest+suffix+'.json'), result)
                    self.store.set(key, {'state': 'complete', 'result': compact})
                    if key != base_key: self.store.set(base_key, {'state':'complete', 'result':compact})
                    results.append(compact)
                    self.record_best(lease, source, result)
                    self.store.event('check_complete', {'route_token':lane['route_token'], 'check_index':index+1, 'claim_id': lease['claim_id'], 'source_sha256': digest, 'check_key':key, 'status': result['status']})
            if self.stop_requested(): break
            if result['status'] == 'match' and result['exit_code'] == 0:
                self.finalize(lane, source, digest); break
        payload = {'status':'BATCH_RESULT', 'claim_id':lease['claim_id'], 'results':results, 'pages':[], 'lease_check_limit':None, 'checks_recorded':self.store.get('checks:'+lease['claim_id'])}
        if self.stop_requested():
            write_json(directory/('deferred-batch-'+mid+'.json'), payload)
            return
        files = []
        for result in results:
            for key in ('log_path', 'stderr_path'):
                if result.get(key) and Path(result[key]).is_file(): files.append(Path(result[key]))
        payload['pages'] = self.upload(lane, lease['claim_id'], files) if files else []
        self.store.set('batch-result:'+publication, payload)
        self.publish(lane, publication, payload)

    def finalize(self, lane, source, digest):
        claim_id = lane['lease']['claim_id']
        with self.lane_lock(lane['route_token']):
            self.refresh(lane)
            if lane.get('lease', {}).get('claim_id') != claim_id: raise RuntimeError('Finalization lease changed')
            if lane.get('paused'): return
            self._finalize_locked(lane, source, digest)

    def _finalize_locked(self, lane, source, digest):
        lease = lane['lease']; directory = Path(lease['directory'])
        with self.store.lock('lease-' + lease['claim_id']):
            if self.stop_requested(): return
            if self.clock()+840 >= epoch(lease['expires']): raise RuntimeError('Too close to expiry for independent validation')
            self.guard(self.project.call('status', {}), lease['claim_id'])
            if self.stop_requested() or self.clock()+840 >= epoch(lease['expires']): return
            if sha((directory/'pack.tar.gz').read_bytes()) != lease['pack_sha256'] or sha(source.read_bytes()) != digest: raise RuntimeError('Finalization inputs changed')
            final_key = 'finalize:' + lease['claim_id']
            if self.store.get(final_key): raise RuntimeError('Existing finalization intent; reconcile rather than replay')
            paths = self.tool_paths(lane)
            self.store.set(final_key, {'state': 'independent_check', 'source_sha256': digest})
            snapshot = directory/'submission.cpp'; snapshot.write_bytes(source.read_bytes()); snapshot.chmod(0o400)
            proof = self.checker(paths, directory/'pack', snapshot, directory/'independent-plain', plain=True)
            write_json(directory/'independent-proof.json', proof)
            if proof['exit_code'] != 0 or proof.get('standalone_match') is not True or proof['source_sha256'] != digest or sha(snapshot.read_bytes()) != digest: raise RuntimeError('Independent plain MATCH not established; no submission')
            if self.stop_requested(): return
            self.guard(self.project.call('status', {}), lease['claim_id'])
            if self.stop_requested() or self.clock()+840 >= epoch(lease['expires']): return
            if sha((directory/'pack.tar.gz').read_bytes()) != lease['pack_sha256']: raise RuntimeError('Pack changed after plain validation')
            args={'claim_id':lease['claim_id'],'source':snapshot.read_bytes().decode(),'model':self.model_label(lane)}
            self.store.set(final_key, {'state': 'submit_intent', 'source_sha256': digest})
            response = self.store.operation('submit:'+lease['claim_id'],args,lambda:self.project.call('submit',args))
            if not submission_accepted(response): raise RuntimeError('Submission acknowledgement unproven')
            write_json(directory/'submission-response.json', response)
            lane['state'] = 'idle'; lane.pop('lease')
            # A completed owner-authorized assignment grants one next admission.
            # This is not a fabricated web heartbeat and is consumed by assignment.
            lane['explicit_resume'] = True
            lane['next_admission_reason'] = 'accepted_submission'
            self.store.put_lane(lane)
            notification={'status':'SUBMITTED_AWAITING_VERIFICATION','claim_id':lease['claim_id'],'response':response,'verified':False,'next_task':'pending_if_eligible','next_action':dict(NEXT_ACTION)}
            if self.stop_requested(): write_json(directory/'deferred-submitted.json',notification)
            else: self.publish(lane,'submitted:'+lease['claim_id'],notification)

    def record_best(self, lease, source, result):
        checker = result.get('checker', {}); metrics = checker.get('metrics', checker)
        score = metrics.get('full_extent_ndiff_insn', metrics.get('ndiff_insn'))
        if type(score) is not int or score < 0: return
        extent = metrics.get('candidate_extent', lease['size'])
        if type(extent) is not int: return
        path = Path(lease['directory'])/'checked-best.json'; old = json.loads(path.read_text()) if path.exists() else {}
        rank = [score, abs(extent-lease['size'])]
        if old and old['rank'] <= rank: return
        raw = source.read_bytes()
        if sha(raw) != result['source_sha256']: raise RuntimeError('Best source binding differs')
        write_json(path, {'rank':rank, 'source_path':str(source), 'source_sha256':sha(raw), 'pack_sha256':lease['pack_sha256'], 'actually_checked':True})

    def finish_release(self, lane, lease, *, resume=None, status='released'):
        auto = lane.get('policy', {}).get('auto_resume_unfinished', False) is True if resume is None else resume
        lane.pop('lease', None)
        lane['last_release'] = {'claim_id':lease['claim_id'], 'status':status, 'at':self.clock()}
        if auto and lease.get('address'):
            previous=lane.get('resume')
            lane['resume'] = {'address':lease['address'], 'previous_claim_id':lease['claim_id'], 'directory':lease['directory'], 'previous_pack_sha256':lease.get('pack_sha256'), 'not_before':self.clock(), 'retries':0}
            if previous and not (Path(lease['directory'])/'checked-best.json').exists() and (Path(previous['directory'])/'checked-best.json').exists():
                lane['resume'].update(previous_claim_id=previous['previous_claim_id'],directory=previous['directory'],previous_pack_sha256=previous.get('previous_pack_sha256'),released_claim_id=lease['claim_id'])
            lane['state'] = 'resume_pending'
        else:
            lane.pop('resume', None); lane['state'] = 'idle'
        notices = lane.setdefault('closed_lease_notices', [])
        if not any(x['payload']['claim_id'] == lease['claim_id'] for x in notices):
            notices.append({'notified':False, 'payload':{'status':'LEASE_CLOSED', 'claim_id':lease['claim_id'],
                            'address':lease.get('address'), 'lease_status':status,
                            'checks_recorded':self.store.get('checks:'+lease['claim_id']) or 0,
                            'work_preserved':True, 'continuation_pending':bool(lane.get('resume')),
                            'next_action':dict(NEXT_ACTION)}})
        self.store.put_lane(lane)
        self.store.event('claim_'+status, {'route_token':lane['route_token'], 'claim_id':lease['claim_id']})
        return {'status':status, 'claim_id':lease['claim_id'], 'route_token':lane['route_token']}

    def release(self, lane, reason, *, resume=None):
        requested = lane.get('lease', {}).get('claim_id')
        with self.lane_lock(lane['route_token']):
            self.refresh(lane)
            lease = lane.get('lease')
            if not lease: return {'status':'no_lease', 'route_token':lane['route_token']}
            if requested and lease['claim_id'] != requested:
                return {'status':'stale_lease_ignored', 'route_token':lane['route_token'], 'claim_id':requested}
            with self.store.lock('lease-' + lease['claim_id']):
                directory = Path(lease['directory'])
                submission = self.store.operation_record('submit:'+lease['claim_id'])
                if (directory/'submission-response.json').exists() or (submission and submission['status']=='response' and submission_accepted(submission.get('result'))):
                    if not (directory/'submission-response.json').exists(): write_json(directory/'submission-response.json',submission['result'])
                    return self.finish_release(lane,lease,resume=False,status='already_submitted')
                final = self.store.get('finalize:' + lease['claim_id'])
                if (submission and submission['status']!='response') or (final and final.get('state')=='submit_intent'):
                    raise UncertainOperation('Uncertain submission blocks automatic giveup')
                giveup = self.store.operation_record('giveup:'+lease['claim_id'])
                if giveup and giveup['status']!='response': raise UncertainOperation('Uncertain giveup requires reconciliation')
                if (directory/'giveup-response.json').exists() or (giveup and (giveup.get('result') or {}).get('released') is True):
                    if not (directory/'giveup-response.json').exists(): write_json(directory/'giveup-response.json',giveup['result'])
                    return self.finish_release(lane,lease,resume=resume)
                status = self.project.call('status', {})
                if not any(row.get('claim_id')==lease['claim_id'] and row.get('this_key') for row in status['open_claims']):
                    if self.clock() >= epoch(lease['expires']):
                        write_json(directory/'expired-observed.json',{'claim_id':lease['claim_id'], 'observed_at':self.clock(), 'released_by_controller':False})
                        return self.finish_release(lane,lease,resume=resume,status='expired_observed')
                    lane['state'] = 'closed_requires_reconciliation'; self.store.put_lane(lane)
                    return {'status':'pending', 'route_token':lane['route_token'], 'claim_id':lease['claim_id'], 'reason':'Ownership no longer proven'}
                args = {'claim_id':lease['claim_id'], 'reason':reason[:900], 'model':self.model_label(lane)}
                best_file = directory/'checked-best.json'
                if best_file.exists():
                    best = json.loads(best_file.read_text()); source = Path(best['source_path']).resolve()
                    if not source.is_relative_to(directory.resolve()) or best.get('pack_sha256')!=lease.get('pack_sha256') or best.get('actually_checked') is not True: raise RuntimeError('Best candidate scope differs')
                    raw = source.read_bytes()
                    if sha(raw)!=best['source_sha256']: raise RuntimeError('Best candidate changed')
                    if len(raw)<=65536: args['best_code'] = raw.decode()
                    else: self.store.event('best_code_retained_locally',{'claim_id':lease['claim_id'], 'source_sha256':sha(raw), 'source_bytes':len(raw)})
                response = self.store.operation('giveup:'+lease['claim_id'],args,lambda:self.project.call('giveup',args))
                if response.get('released') is not True: raise RuntimeError('Giveup acknowledgement unproven')
                write_json(directory/'giveup-response.json',response)
                return self.finish_release(lane,lease,resume=resume)

    def release_all(self, reason):
        # Atomic flags become visible before waiting on any lane or service request.
        self.store.set('shutdown_requested',True); self.store.set('drain',True)
        result = {'released':[], 'expired':[], 'pending':[], 'uncertain':[]}
        for snapshot in self.store.lanes():
            entry = {'route_token':snapshot['route_token'], 'claim_id':snapshot.get('lease', {}).get('claim_id')}
            try:
                with self.lane_lock(snapshot['route_token']):
                    lane = self.store.lane(snapshot['route_token']); lane['explicit_resume']=False; self.store.put_lane(lane)
                    if lane['state']=='claim_intent': self.recover_claim_intent(lane)
                    entry['claim_id']=lane.get('lease',{}).get('claim_id')
                    if lane.get('lease'):
                        outcome = self.release(lane,reason)
                        category = 'expired' if outcome['status']=='expired_observed' else 'released' if outcome['status'] in ('released','already_submitted') else 'pending'
                        result[category].append(entry | {'status':outcome['status']})
                    elif lane['state']=='claim_intent':
                        result['uncertain'].append(entry | {'reason':'Claim intent requires reconciliation'})
            except (UncertainOperation,) as error:
                result['uncertain'].append(entry | {'reason':str(error)})
            except Exception as error:
                operation = self.store.operation_record('giveup:'+str(entry['claim_id'])) if entry['claim_id'] else None
                uncertain = isinstance(error,ProjectError) and error.uncertain or operation and operation['status']!='response'
                result['uncertain' if uncertain else 'pending'].append(entry | {'reason':type(error).__name__})
        result['counts'] = {key:len(value) for key,value in result.items()}
        result['counts']['remaining'] = result['counts']['pending']+result['counts']['uncertain']
        return result

    def request(self, lane, message):
        request = message.get('request', {}); lease = lane['lease']; directory = Path(lease['directory']); files = []
        diagnostics = None
        reason = None
        if not isinstance(request, dict):
            reason = 'request_must_be_object'
        elif request.get('kind') == 'candidate_artifacts':
            digest = request.get('source_sha256')
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                reason = 'invalid_artifact_digest'
        if reason:
            self.publish(lane, 'request:'+lease['claim_id']+':'+message['message_id'],
                         {'status':'REQUEST_REJECTED', 'reason_code':reason,
                          'supported':['best_candidate','candidate_artifacts','target'],
                          'scope':{key:lease[key] for key in ('claim_id','pack_sha256')}})
            previous_error = self.store.get('last_error:'+lane['route_token']) or {}
            if previous_error.get('message') in ("'"+type(request).__name__+"' object has no attribute 'get'", 'Invalid artifact digest'):
                self.store.set('last_error:'+lane['route_token'], None)
            return
        if request.get('kind') == 'best_candidate':
            pointer = directory/'checked-best.json'
            if pointer.exists():
                row = json.loads(pointer.read_text()); source = Path(row['source_path']).resolve()
                if not source.is_relative_to(directory.resolve()) or sha(source.read_bytes()) != row['source_sha256']: raise ValueError('Best source binding differs')
                files = [source]
        elif request.get('kind') == 'candidate_artifacts':
            digest = request.get('source_sha256')
            record = self.store.get('check:'+lease['claim_id']+':'+digest)
            diagnostics = {'status':'unavailable', 'reason_code':'no_completed_check', 'additional_compiler_invocations':0}
            if record and record.get('state')=='complete':
                diagnostics = record['result'].get('diagnostics') or {'status':'unavailable', 'reason_code':'not_captured', 'additional_compiler_invocations':0}
                source = directory/'sources'/(digest+'.cpp')
                if sha(source.read_bytes()) != digest: raise ValueError('Artifact source binding differs')
                files.append(source)
                for artifact in record['result'].get('diagnostic_artifacts', []):
                    path = Path(artifact['path']).resolve()
                    if not path.is_relative_to(directory.resolve()) or not path.is_file() or sha(path.read_bytes()) != artifact['sha256']:
                        diagnostics = {'status':'unavailable', 'reason_code':'invalid_saved_evidence', 'additional_compiler_invocations':0}
                        break
                else:
                    files.extend(Path(artifact['path']) for artifact in record['result'].get('diagnostic_artifacts', []))
                for field in ('log_path','stderr_path'):
                    value = record['result'].get(field)
                    if value:
                        path = Path(value).resolve()
                        if not path.is_relative_to(directory.resolve()): raise ValueError('Artifact path escaped claim')
                        if path.is_file(): files.append(path)
        elif request.get('kind') == 'target':
            files = [directory/'context'/'target-disassembly.txt']
        else:
            self.publish(lane,'request:'+lease['claim_id']+':'+message['message_id'], {'status':'REQUEST_UNSUPPORTED','supported':['best_candidate','candidate_artifacts','target']}); return
        pages = self.upload(lane,lease['claim_id'],files)
        response = {'status':'REQUEST_CONTEXT','pages':pages,'scope':{'claim_id':lease['claim_id'],'pack_sha256':lease['pack_sha256']}}
        if diagnostics is not None: response['diagnostics'] = diagnostics
        self.publish(lane,'request:'+lease['claim_id']+':'+message['message_id'],response)

    def poll(self):
        for lane in self.store.lanes():
            if self.stop_requested(): break
            if self.clock() < (self.store.get('project_read_not_before') or 0): break
            try:
                self.config = self.store.config()
                from .lane_policy import process as process_lane_control
                process_lane_control(self,lane)
                if lane['state']=='claim_intent':
                    recovered=self.recover_claim_intent(lane)
                    if recovered=='owned_claim_recovered': self.release(lane,'Resolved saved claim response after interrupted assignment')
                if lane.get('blocked_reason') == 'function_paused' and lane.get('resume') and not lane.get('lease'):
                    self.park_continuation(lane)
                self.publish_parked(lane)
                lease = lane.get('lease')
                if lease and (self.clock()+600 >= epoch(lease['expires']) or not owner_alive(lease.get('owner'))):
                    self.release(lane,'Controller unavailable or lease near expiry; preserved unfinished work')
                self.refresh(lane)
                self.publish_closed_leases(lane)
                if lane['state'] == 'preparing':
                    if self.clock() >= lane.get('preparation_retry_at', 0): self.prepare_assignment(lane)
                    continue
                if lane['state'] in ('setup','claim_intent','closed_requires_reconciliation') or lane.get('paused'): continue
                self.ensure_work_guidance(lane)
                attempts.tick(self,lane)
                for message in self.notion.messages(lane['inbox']):
                    if self.stop_requested(): break
                    if self.store.get('lane_control:'+lane['route_token']): break
                    if not isinstance(message,dict) or message.get('route_token')!=lane['route_token'] or message.get('protocol')!=PROTOCOL or message.get('sender')!='chatgpt': continue
                    identity = sha(canonical(message))
                    try: mid = check_id(message.get('message_id'))
                    except ValueError:
                        rejected = 'invalid-message:'+lane['route_token']+':'+identity
                        if not self.store.get(rejected): self.store.event('invalid_message',{'route_token':lane['route_token'],'record_sha256':identity}); self.store.set(rejected,True)
                        continue
                    key = 'message:'+lane['route_token']+':'+mid
                    with self.lane_lock(lane['route_token']):
                        self.refresh(lane)
                        old = self.store.get(key)
                        if old:
                            if old!=identity:
                                conflict = 'conflict:'+lane['route_token']+':'+identity
                                if not self.store.get(conflict): self.store.event('conflicting_message',{'route_token':lane['route_token'],'message_id':mid,'record_sha256':identity}); self.store.set(conflict,True)
                            continue
                        kind = message.get('kind')
                        if kind not in ('HELLO','READY') and (not lane.get('lease') or any(message.get(field)!=lane['lease'].get(field) for field in ('claim_id','pack_sha256'))):
                            self.store.event('stale_message',{'route_token':lane['route_token'],'message_id':mid}); self.store.set(key,identity); continue
                        if kind in ('HELLO','READY','BATCH','REQUEST'):
                            lane['last_heartbeat'] = self.clock(); self.store.put_lane(lane)
                        if kind in ('HELLO','READY'):
                            if lane['state']=='waiting_hello': lane['state']='idle'
                            lane['ready']=True; self.store.put_lane(lane)
                    if kind in ('HELLO','READY'): pass
                    elif kind=='BATCH' and lane['state']=='assigned': self.batch(lane,message)
                    elif kind=='REQUEST': self.scope(lane,message); self.request(lane,message)
                    elif kind=='HANDOFF': attempts.receive(self,lane,message)
                    elif kind in ('DEFECT_PROPOSAL','FINDING'):
                        self.scope(lane,message); self.store.event(kind.lower(),message)
                        with self.lane_lock(lane['route_token']):
                            self.refresh(lane); self.scope(lane,message); notes=Path(lane['lease']['directory'])/'continuation-notes.json'
                            previous=json.loads(notes.read_text()) if notes.exists() else []
                            if len(previous)<100: previous.append(message); write_json(notes,previous)
                        self.publish(lane,'note:'+lane['route_token']+':'+mid,{'status':'RECORDED_REVIEW_ONLY','message_id':mid,'instructions':'No automatic flag was sent. Source/log context is retained. Continue permitted work while the current assignment remains active.'})
                    else: continue
                    self.store.set(key,identity)
                    control_changed=bool(self.store.get('lane_control:'+lane['route_token']))
                    if control_changed:process_lane_control(self,lane)
                    attempts.tick(self,lane)
                    if control_changed:break
                self.refresh(lane)
                process_lane_control(self,lane)
                if lane['state'] in ('idle','resume_pending') and not self.stop_requested(): self.assign(lane)
            except Exception as error:
                if isinstance(error,ProjectError) and not error.uncertain and error.retry_after:
                    self.store.set('project_read_not_before', self.clock()+max(0,error.retry_after))
                self.refresh(lane)
                if lane['state'] == 'preparing':
                    lane['preparation_retry_at'] = self.clock()+30; self.store.put_lane(lane)
                self.store.event('lane_error',{'route_token':lane['route_token'],'type':type(error).__name__,'message':str(error)[:400]})
                self.store.set('last_error:'+lane['route_token'],{'type':type(error).__name__,'http_status':getattr(error,'status',None),'message':str(error)[:400]})
