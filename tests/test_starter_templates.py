"""Local owner edits never mutate default wording or cross profile/lane boundaries."""
import hashlib

import pytest

from fnv_relay import starter_templates as templates
from fnv_relay.presentation import starter_prompt
from fnv_relay.store import Store

LANE={'landing':'01234567-89ab-cdef-0123-456789abcdef','route_token':'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee','label':'First lane'}


def test_default_render_is_byte_identical_to_pre_editor_prompt():
    assert hashlib.sha256(starter_prompt(LANE).encode()).hexdigest()=='1c48fb3f997aea7f53b8e979989e4db986af45224d6b07685124fe114488b3b0'


def test_template_persists_per_profile_renders_per_lane_and_reset_restores_default(tmp_path):
    a=Store(tmp_path/'a');b=Store(tmp_path/'b')
    custom='{{lane_label}}\n{{landing_url}}\nRegister {{route_uuid}}.\nLiteral C++ { int x; }'
    templates.save(a,custom)
    assert templates.load(a)==custom
    first=starter_prompt(LANE,a)
    assert 'First lane' in first and LANE['route_token'] in first
    assert 'https://app.notion.com/p/0123456789abcdef0123456789abcdef' in first
    second=starter_prompt({**LANE,'label':'Other lane','route_token':'route-two'},a)
    assert 'Other lane' in second and 'route-two' in second
    assert LANE['route_token'] not in second
    assert starter_prompt(LANE,b)==starter_prompt(LANE)
    a.close();a=Store(tmp_path/'a');assert templates.load(a)==custom
    a.set('other-setting',{'keep':True});templates.reset(a)
    assert starter_prompt(LANE,a)==starter_prompt(LANE)
    assert a.get('other-setting')=={'keep':True}
    a.close();b.close()


@pytest.mark.parametrize('invalid',['',None,'{{landing_url}}','{{route_uuid}}','{{landing_url}} {{route_uuid}} {{unknown}}',
                                    '{{landing_url}} {{route_uuid}}\x1b[31m','{{landing_url}} {{route_uuid}}'+'x'*65536])
def test_invalid_templates_do_not_replace_the_saved_version(tmp_path,invalid):
    store=Store(tmp_path);original='{{landing_url}}:{{route_uuid}}';templates.save(store,original)
    with pytest.raises(ValueError):templates.save(store,invalid)
    assert templates.load(store)==original
    store.close()


def test_placeholder_values_are_substituted_once_without_evaluation(tmp_path):
    store=Store(tmp_path)
    templates.save(store,'{{landing_url}} {{route_uuid}} label={{lane_label}}')
    rendered=starter_prompt({**LANE,'label':'{{route_uuid}} $(never-run)'},store)
    assert 'label={{route_uuid}} $(never-run)' in rendered
    store.close()


def test_lane_prompt_command_uses_saved_template_without_network(tmp_path,capsys):
    from fnv_relay import cli
    store=Store(tmp_path)
    store.put_lane({**LANE,'route_token':LANE['route_token'],'state':'assigned'})
    templates.save(store,'Custom {{landing_url}} / {{route_uuid}}')
    cli.run(cli.parser().parse_args(['lane','prompt',LANE['route_token']]),store)
    assert 'Custom https://app.notion.com/p/' in capsys.readouterr().out
    store.close()
