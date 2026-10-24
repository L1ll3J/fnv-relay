"""Read-only live monitor tests using local state and synthetic events."""
from fnv_relay.live import watch,event_lines
from fnv_relay.store import Store


def fixture_store(tmp_path):
    store=Store(tmp_path)
    store.put_lane({'route_token':'route','label':'Pro1','state':'assigned','policy':{},
                    'lease':{'claim_id':'claim-1','address':'0x401000','size':41,'expires':'2026-10-24T23:00:00Z'}})
    return store


def test_live_view_shows_instruction_counts_diagnostics_and_controller_errors(tmp_path):
    store=fixture_store(tmp_path);shown=[];polls=[0];read=[]
    def wait(timeout):
        polls[0]+=1
        if polls[0]==1:
            store.set('checks:claim-1',1)
            store.set('check:claim-1:hash',{'state':'complete','result':{'candidate_id':'candidate-A','status':'wip',
                'checker':{'ndiff_insn':7,'diffs':900,'extent':36,'errors':['compiled extent differs'],
                           'diff':'ORIG mov eax, ecx\nYOURS lea eax, [ecx]'}}})
            store.event('check_complete',{'claim_id':'claim-1','source_sha256':'hash','status':'wip'})
            store.event('lane_error',{'route_token':'route','type':'NotionError','message':'HTTP 405'})
            return False
        return True
    before=store.lane('route')
    watch(store,shown.append,worker_alive=lambda:True,wait=wait,read_line=lambda:read.append(True))
    text='\n'.join(shown)
    assert 'instruction differences: 7' in text and 'differences: 900' not in text
    assert 'bytes: 36' in text and 'compiled extent differs' in text
    assert 'ORIG mov eax, ecx' in text and 'YOURS lea eax, [ecx]' in text
    assert 'ERROR NotionError: HTTP 405' in text
    assert read==[True] and store.lane('route')==before
    assert store.pending_operations()==[]
    store.close()


def test_live_view_marks_cached_errors_as_past_records_and_enter_is_read_only(tmp_path):
    store=fixture_store(tmp_path);shown=[]
    store.set('last_error:route',{'type':'AttributeError','message':'old error'})
    before=store.lanes()
    watch(store,shown.append,worker_alive=lambda:True,wait=lambda _:True,read_line=lambda:'\n')
    text='\n'.join(shown)
    assert 'Last recorded error (may be historical)' in text
    assert 'last error: AttributeError' in text
    assert store.lanes()==before
    store.close()


def test_live_view_returns_to_menu_when_worker_stops(tmp_path):
    store=fixture_store(tmp_path);shown=[]
    def do_not_wait(_):raise AssertionError('Stopped monitor must not wait')
    watch(store,shown.append,worker_alive=lambda:False,wait=do_not_wait)
    assert any('Worker is stopped' in line for line in shown)
    store.close()


def test_event_cursor_does_not_drop_bursts_larger_than_one_page(tmp_path):
    store=Store(tmp_path)
    for n in range(205):store.event('synthetic',{'n':n})
    first=store.events_after(0);second=store.events_after(first[-1]['id'])
    assert len(first)==200 and len(second)==5
    assert [e['data']['n']for e in first+second]==list(range(205))
    store.close()



def test_opening_live_view_includes_latest_current_claim_result(tmp_path):
    store=fixture_store(tmp_path);shown=[]
    store.set('check:claim-1:hash',{'state':'complete','result':{'candidate_id':'saved','status':'wip','checker':{'ndiff_insn':12}}})
    store.event('check_complete',{'claim_id':'claim-1','source_sha256':'hash','status':'wip'})
    watch(store,shown.append,worker_alive=lambda:False)
    assert 'Latest recorded check:' in '\n'.join(shown)
    assert 'instruction differences: 12' in '\n'.join(shown)
    store.close()


def test_completion_during_initial_summary_is_not_displayed_twice(tmp_path,monkeypatch):
    store=fixture_store(tmp_path);shown=[];original=store.events
    def arriving(limit):
        store.set('check:claim-1:hash',{'state':'complete','result':{'candidate_id':'during-open','status':'wip','checker':{'ndiff_insn':3}}})
        store.event('check_complete',{'claim_id':'claim-1','source_sha256':'hash','status':'wip'})
        return original(limit)
    monkeypatch.setattr(store,'events',arriving)
    watch(store,shown.append,worker_alive=lambda:True,wait=lambda _:True,read_line=lambda:'\n')
    assert '\n'.join(shown).count('instruction differences: 3')==1
    store.close()
