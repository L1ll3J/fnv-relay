"""Compact read-only live monitoring and saved-best comparison regressions."""
import json
from fnv_relay.live import watch
from fnv_relay.live_best import BestTracker
from fnv_relay.store import Store


def fixture_store(tmp_path):
    store=Store(tmp_path)
    store.put_lane({'route_token':'route','label':'Pro1','state':'assigned','policy':{},
                    'lease':{'claim_id':'claim-1','pack_sha256':'pack-A','address':'0x401000','size':41,'expires':'2026-10-24T23:00:00Z'}})
    return store


def saved_best(store,score=12,metric='ndiff_insn',pack='pack-A'):
    directory=store.root/'claims'/'old-claim';directory.mkdir(parents=True,exist_ok=True)
    (directory/'checked-best.json').write_text(json.dumps({'source_sha256':'saved','actually_checked':True,'pack_sha256':pack,'rank':[score,0]}))
    store.set('check:old-claim:saved',{'state':'complete','result':{'pack_sha256':pack,'checker':{metric:score}}})


def test_live_view_keeps_tables_and_errors_without_instruction_dumps(tmp_path):
    store=fixture_store(tmp_path);shown=[];polls=[0];read=[];saved_best(store,12)
    def wait(timeout):
        polls[0]+=1
        if polls[0]==1:
            store.set('checks:claim-1',1)
            store.set('check:claim-1:hash',{'state':'complete','result':{'candidate_id':'candidate-A','status':'wip','pack_sha256':'pack-A',
                'checker':{'ndiff_insn':7,'diffs':900,'extent':36,'errors':['compiled extent differs'],
                           'diff':'ORIG mov eax, ecx\nYOURS lea eax, [ecx]'}}})
            store.event('check_complete',{'claim_id':'claim-1','source_sha256':'hash','status':'wip'})
            store.event('lane_error',{'route_token':'route','type':'NotionError','message':'HTTP 405'})
            return False
        return True
    before=store.lane('route')
    watch(store,shown.append,worker_alive=lambda:True,wait=wait,read_line=lambda:read.append(True))
    text='\n'.join(shown)
    assert 'CHECKS' in text and 'BEST' in text
    assert 'NEW BEST: 12 → 7 (5 fewer instruction differences)' in text
    assert '900' not in text and 'ORIG mov' not in text and 'YOURS lea' not in text
    assert 'compiled extent differs' in text and 'ERROR NotionError: HTTP 405' in text
    assert read==[True] and store.lane('route')==before and store.pending_operations()==[]
    store.close()


def test_live_view_marks_cached_errors_as_past_records_and_enter_is_read_only(tmp_path):
    store=fixture_store(tmp_path);shown=[]
    store.set('last_error:route',{'type':'AttributeError','message':'old error'})
    before=store.lanes()
    watch(store,shown.append,worker_alive=lambda:True,wait=lambda _:True,read_line=lambda:'\n')
    assert 'Last recorded error (may be historical)' in '\n'.join(shown)
    assert 'last error: AttributeError' in '\n'.join(shown)
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


def test_opening_view_does_not_reannounce_existing_best(tmp_path):
    store=fixture_store(tmp_path);shown=[];saved_best(store,12)
    store.event('check_complete',{'claim_id':'old-claim','source_sha256':'saved','status':'wip'})
    watch(store,shown.append,worker_alive=lambda:False)
    assert 'NEW BEST' not in '\n'.join(shown)
    store.close()


def test_multiple_improvements_in_one_poll_compare_in_order(tmp_path):
    store=fixture_store(tmp_path);shown=[];saved_best(store,12);first=[True]
    original=store.events_after
    def arriving(cursor):
        if first[0]:
            first[0]=False
            for score in (9,7,7,10,6):
                digest='score-'+str(score)
                store.set('check:claim-1:'+digest,{'state':'complete','result':{'pack_sha256':'pack-A','checker':{'ndiff_insn':score}}})
                store.event('check_complete',{'claim_id':'claim-1','source_sha256':digest,'status':'wip'})
        return original(cursor)
    store.events_after=arriving
    watch(store,shown.append,worker_alive=lambda:True,wait=lambda _:True,read_line=lambda:'\n')
    notices=[s for s in shown if 'NEW BEST' in s]
    assert len(notices)==3
    assert '12 → 9' in notices[0] and '9 → 7' in notices[1] and '7 → 6' in notices[2]
    store.close()


def test_saved_best_comparisons_require_same_pack_and_metric(tmp_path):
    store=fixture_store(tmp_path);saved_best(store,12,metric='full_extent_ndiff_insn')
    tracker=BestTracker(store)
    assert tracker.observe('claim-1',{'pack_sha256':'pack-A','checker':{'ndiff_insn':7}}) is None
    assert tracker.observe('claim-1',{'pack_sha256':'pack-B','checker':{'full_extent_ndiff_insn':1}}) is None
    assert tracker.observe('claim-1',{'pack_sha256':'pack-A','checker':{'full_extent_ndiff_insn':10}})==(12,10,'full_extent_ndiff_insn')
    store.close()


def test_imported_historical_named_metrics_are_compared_separately(tmp_path):
    store=fixture_store(tmp_path)
    directory=store.root/'claims'/'legacy';directory.mkdir(parents=True)
    (directory/'checked-best.json').write_text(json.dumps({'actually_checked':True,'pack_sha256':'pack-A','source_sha256':'saved',
        'historical_metrics':{'full_extent_ndiff_insn':132,'official_ndiff_insn':4528}}))
    tracker=BestTracker(store)
    assert tracker.observe('claim-1',{'pack_sha256':'pack-A','checker':{'ndiff_insn':4257}})==(4528,4257,'ndiff_insn')
    assert tracker.observe('claim-1',{'pack_sha256':'pack-A','checker':{'full_extent_ndiff_insn':130}})==(132,130,'full_extent_ndiff_insn')
    store.close()


def test_unnamed_saved_rank_is_not_guessed_to_be_a_comparable_metric(tmp_path):
    store=fixture_store(tmp_path);directory=store.root/'claims'/'unknown';directory.mkdir(parents=True)
    (directory/'checked-best.json').write_text(json.dumps({'actually_checked':True,'pack_sha256':'pack-A','source_sha256':'saved','rank':[132,0]}))
    tracker=BestTracker(store)
    assert tracker.observe('claim-1',{'pack_sha256':'pack-A','checker':{'ndiff_insn':120}}) is None
    store.close()
