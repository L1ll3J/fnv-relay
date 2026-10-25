from fnv_relay.dashboard import Dashboard
from fnv_relay.store import Store
from fnv_relay import presets


def test_history_survives_restart_and_keeps_metrics_and_packs_separate(tmp_path):
    store=Store(tmp_path)
    store.put_lane({'route_token':'route','label':'Lane','state':'assigned','policy':{},'lease':{'claim_id':'c2'},'closed_lease_notices':[{'payload':{'claim_id':'c1'}}]})
    for cid,number,score,full,pack in [('c1',1,10,20,'pack-a'),('c1',2,8,18,'pack-a'),('c2',1,9,19,'pack-a'),('c2',2,7,17,'pack-a'),('c2',3,99,101,'pack-b')]:
        sha=cid+str(number)
        store.set('check:'+cid+':'+sha,{'state':'complete','result':{'status':'wip','pack_sha256':pack,'checker':{'ndiff_insn':score},'diagnostics':{'full_extent_ndiff_insn':full}}})
        store.event('check_complete',{'claim_id':cid,'source_sha256':sha,'check_index':number})
    model=Dashboard(store);model.update()
    rows=model.history('route')
    assert [r['improved'] for r in rows]==[False,True,False,True,False]
    assert model.progress('route','c2')['best']==99
    restarted=Dashboard(store);restarted.update()
    assert restarted.history('route')==rows
    assert restarted.history('route','full_extent_ndiff_insn')[-1]['score']==101
    assert len(rows)==5
    model.update();assert len(model.history('route'))==5
    store.close()


def test_notepad_is_private_editable_and_defaults_are_not_overwritten(tmp_path):
    store=Store(tmp_path);values=presets.load(store)
    assert len(values)==2
    assert values[1]['text']=="Let's get cracking courier --  treat this similar to a '/goal', no need to report back to me until it's done o7\n\nIf you haven't solved it, write some attempts, you can make any request you like to the Notion relay, and if awaiting a response you can just use python to poll.\nLet's get this solved o7"
    values[0]['text']='my private edit';presets.save(store,values)
    assert presets.load(store)[0]['text']=='my private edit'
    assert presets.DEFAULTS[0]['text']!='my private edit'
    store.close()
