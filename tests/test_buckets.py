"""Bucket changes compose with order and safe lane-control boundaries."""
import pytest

from fnv_relay import cli
from fnv_relay.engine import BANDS
from fnv_relay.lane_policy import configure,queue,process
from test_engine import env,assign,lane


@pytest.mark.parametrize('bucket',BANDS)
def test_bucket_change_preserves_current_assignment_and_other_policy(env,bucket):
    original=assign(env)
    updated=configure(env.engine,env.route,band=bucket,order='random')
    assert updated['lease']==original['lease']
    assert updated['state']=='assigned'
    assert updated['policy']['band']==bucket and updated['policy']['order']=='random'
    assert not any(name=='giveup' for name,_ in env.project.calls)


def test_any_size_clears_only_bucket_and_live_request_applies_at_boundary(env):
    assign(env)
    configure(env.engine,env.route,band='4K+')
    queue(env.store,env.route,band='any')
    assert lane(env)['policy']['band']=='4K+'
    process(env.engine,lane(env))
    assert 'band' not in lane(env)['policy']
    assert lane(env)['state']=='assigned'


def test_switch_now_keeps_newly_chosen_bucket_instead_of_discarding_it(env):
    assign(env)
    updated=configure(env.engine,env.route,band='512-1K',order='smallest',switch_now=True)
    assert updated['policy']['band']=='512-1K'
    assert updated['policy']['order']=='smallest'
    assert updated['parked'] and not updated.get('lease')


def test_invalid_bucket_does_not_mutate_or_queue_policy(env):
    before=lane(env)
    with pytest.raises(ValueError):configure(env.engine,env.route,band='made-up')
    with pytest.raises(ValueError):queue(env.store,env.route,band='made-up')
    assert lane(env)==before and not env.store.get('lane_control:'+env.route)


def test_command_line_bucket_applies_while_order_remains_independent(env):
    cli.run(cli.parser().parse_args(['lane','configure',env.route,'--band','1K-4K','--order','largest']),env.store)
    assert lane(env)['policy']['band']=='1K-4K'
    assert lane(env)['policy']['order']=='largest'
    cli.run(cli.parser().parse_args(['lane','configure',env.route,'--band','any']),env.store)
    assert 'band' not in lane(env)['policy']


@pytest.mark.parametrize('order,expected',[('largest','0x00403000'),('smallest','0x00402000')])
def test_order_remains_effective_inside_selected_bucket_without_attempt_limit(env,order,expected):
    from unittest.mock import Mock
    current=lane(env);current['policy'].update(order=order,band='512-1K')
    env.project.call=Mock(side_effect=[{'bands':[{'band':'512-1K','allowed':True,'open':5}]},
                                      {'functions':[{'address':'0x00402000','size':600},
                                                    {'address':'0x00403000','size':900},
                                                    {'address':'0x00404000','size':5000}],'next':None}])
    status={'limits':{'claims':4,'subsPerDay':60,'pending':25},'open_claims':[],'submissions_today':0,'awaiting_verification':0}
    result=env.engine.select(current,status)
    assert result['address']==expected
    assert env.project.call.call_args.args[1]['band']=='512-1K'
