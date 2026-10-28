"""Multiple selected size bands form a pool without filling unselected gaps."""
import pytest

from fnv_relay import cli,preferences
from fnv_relay.lane_policy import configure,queue,process
from test_engine import env,assign,lane


def test_live_multi_bucket_change_and_queue_preserve_current_assignment(env):
    before=assign(env)
    queue(env.store,env.route,bands=['4K+','<=255'],order='random')
    process(env.engine,lane(env))
    current=lane(env)
    assert current['policy']['bands']==['<=255','4K+']
    assert current['lease']==before['lease'] and current['state']=='assigned'
    assert 'band' not in current['policy']


def test_switch_now_retains_all_requested_buckets(env):
    assign(env)
    current=configure(env.engine,env.route,bands=['<=255','512-1K'],switch_now=True)
    assert current['policy']['bands']==['<=255','512-1K']
    assert current['parked'] and not current.get('lease')


def test_existing_single_bucket_and_any_size_are_backward_compatible(env):
    current=configure(env.engine,env.route,band='256-511')
    assert preferences.selected_buckets(current['policy'])==['256-511']
    current=configure(env.engine,env.route,bands=['<=255','4K+'])
    assert preferences.bucket_label(current['policy'])=='≤255 B, 4 KB+'
    current=configure(env.engine,env.route,band='any')
    assert 'bands' not in current['policy'] and 'band' not in current['policy']
    current=configure(env.engine,env.route,bands=[key for _,key in preferences.BUCKETS[1:]])
    assert 'bands' not in current['policy'] and 'band' not in current['policy']


@pytest.mark.parametrize('values',[[],['any','<=255'],['unknown'],[None],'<=255'])
def test_invalid_multi_bucket_changes_are_rejected_before_mutation(env,values):
    before=lane(env)
    with pytest.raises(ValueError):configure(env.engine,env.route,bands=values)
    with pytest.raises(ValueError):queue(env.store,env.route,bands=values)
    assert lane(env)==before
    assert not env.store.get('lane_control:'+env.route)


def test_cli_create_and_configure_accept_multiple_and_keep_single_option(env):
    args=cli.parser().parse_args(['lane','create','--bands','<=255','4K+'])
    args.order='random'
    assert cli.policy(args)['bands']==['<=255','4K+']
    cli.run(cli.parser().parse_args(['lane','configure',env.route,'--bands','<=255','512-1K']),env.store)
    assert lane(env)['policy']['bands']==['<=255','512-1K']
    with pytest.raises(SystemExit):cli.parser().parse_args(['lane','configure',env.route,'--band','4K+','--bands','<=255'])


@pytest.mark.parametrize('order,expected_band,expected_address',[
    ('largest','512-1K','0x00403000'),('smallest','<=255','0x00401000'),
])
def test_largest_and_smallest_consider_combined_selected_pool(env,monkeypatch,order,expected_band,expected_address):
    current=lane(env);current['policy'].update(order=order,bands=['512-1K','<=255'])
    calls=[]
    def read(tool,args):
        if tool=='list_bands':return {'bands':[{'band':b,'allowed':True,'open':5} for _,b in preferences.BUCKETS[1:]]}
        assert tool=='list_functions';calls.append(args['band'])
        return {'functions':[{'address':'0x00401000','size':32},
                             {'address':'0x00402000','size':400},
                             {'address':'0x00403000','size':900},
                             {'address':'0x00404000','size':5000}], 'next':None}
    monkeypatch.setattr(env.project,'call',read)
    status={'limits':{'claims':4,'subsPerDay':60,'pending':25},'open_claims':[],
            'submissions_today':0,'awaiting_verification':0}
    assert env.engine.select(current,status)['address']==expected_address
    assert calls==[expected_band]


def test_empty_selected_band_falls_through_only_to_another_selected_band(env,monkeypatch):
    current=lane(env);current['policy'].update(order='largest',bands=['<=255','4K+'])
    calls=[]
    def read(tool,args):
        if tool=='list_bands':return {'bands':[{'band':b,'allowed':True,'open':5} for _,b in preferences.BUCKETS[1:]]}
        calls.append(args['band'])
        return {'functions':[] if args['band']=='4K+' else [{'address':'0x00401000','size':32}], 'next':None}
    monkeypatch.setattr(env.project,'call',read)
    status={'limits':{'claims':4,'subsPerDay':60,'pending':25},'open_claims':[],
            'submissions_today':0,'awaiting_verification':0}
    assert env.engine.select(current,status)['address']=='0x00401000'
    assert calls==['4K+','<=255']


def test_random_never_selects_an_unchecked_band(env,monkeypatch):
    current=lane(env);current['policy'].update(order='random',bands=['256-511','1K-4K'])
    calls=[]
    def read(tool,args):
        if tool=='list_bands':return {'bands':[{'band':b,'allowed':True,'open':5} for _,b in preferences.BUCKETS[1:]]}
        calls.append(args['band'])
        return {'functions':[{'address':'0x00402000','size':400}, {'address':'0x00403000','size':2000}], 'next':None}
    monkeypatch.setattr(env.project,'call',read)
    status={'limits':{'claims':4,'subsPerDay':60,'pending':25},'open_claims':[],
            'submissions_today':0,'awaiting_verification':0}
    for _ in range(10):assert env.engine.select(current,status)['address'] in ('0x00402000','0x00403000')
    assert set(calls)<=set(current['policy']['bands'])
