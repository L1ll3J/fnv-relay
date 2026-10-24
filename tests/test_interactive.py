from fnv_relay import cli, interactive
from fnv_relay.store import Store


PAGE='01234567-89ab-cdef-0123-456789abcdef'


def answers(*values):
    values=iter(values)
    return lambda _:next(values)


def test_add_lane_uses_numbered_order_and_prints_complete_starter(tmp_path):
    store=Store(tmp_path);shown=[];calls=[]
    def dispatch(command):
        calls.append(command)
        store.put_lane({'route_token':'new-route','label':'Lane 1','landing':PAGE,'state':'waiting_hello'})
        print('{"route_token":"new-route"}')
        return 0
    assert interactive.run(store,dispatch,ask=answers('2','2','0'),show=shown.append)==0
    assert calls==[['lane','create','--order','smallest','--label','Lane 1']]
    text='\n'.join(shown)
    assert 'Welcome courier o7' in text and 'explicit approval' in text
    assert 'BEGIN CHATGPT PROMPT' in text and '[https://' not in text
    assert '{"route_token"' not in text
    store.close()


def test_starter_selection_uses_numbers_instead_of_route_ids(tmp_path):
    store=Store(tmp_path);shown=[]
    for n in (1,2):store.put_lane({'route_token':'route-'+str(n),'label':'Lane '+str(n),'landing':PAGE,'state':'waiting_hello'})
    interactive.run(store,lambda _:0,ask=answers('3','2','0'),show=shown.append)
    assert 'route UUID `route-2`' in '\n'.join(shown)
    store.close()


def test_failed_creation_does_not_claim_success_or_remove_existing_lane(tmp_path):
    store=Store(tmp_path);shown=[]
    store.put_lane({'route_token':'existing','label':'Lane 1','landing':PAGE,'state':'idle'})
    interactive.run(store,lambda _:1,ask=answers('2','1','0'),show=shown.append)
    assert len(store.lanes())==1
    assert any('did not finish' in line for line in shown)
    assert 'BEGIN CHATGPT PROMPT' not in '\n'.join(shown)
    store.close()


def test_resume_saved_function_requires_only_numbered_selections(tmp_path):
    store=Store(tmp_path);calls=[]
    store.put_lane({'route_token':'route','label':'Lane','landing':PAGE,'state':'idle','parked':[{'continuation':{'address':'0x401000'}}]})
    interactive.run(store,lambda command:calls.append(command),ask=answers('5','1','0'),show=lambda _:None)
    assert calls==[['lane','resume','route','--address','0x401000']]
    store.close()


def test_no_command_defaults_to_menu():
    assert cli.parser().parse_args([]).command=='menu'


def test_menu_status_start_stop_are_existing_handlers(tmp_path):
    store=Store(tmp_path);calls=[]
    interactive.run(store,lambda command:calls.append(command),ask=answers('4','1','7','0'),show=lambda _:None)
    assert calls==[['status'],['start'],['stop']]
    store.close()


def test_launcher_uses_saved_paths_without_shell_expansion(tmp_path,monkeypatch):
    import json,os,runpy,sys
    from pathlib import Path
    from unittest.mock import Mock
    source=Path(__file__).resolve().parents[1]/'relay'
    launcher=tmp_path/'relay';launcher.write_bytes(source.read_bytes())
    executable=tmp_path/'tools with spaces'/'fnv-relay';executable.parent.mkdir();executable.touch()
    state=tmp_path/'private state; untouched'
    (tmp_path/'.relay-launcher.json').write_text(json.dumps({'executable':str(executable),'state_dir':str(state)}))
    execute=Mock();monkeypatch.setattr(os,'execv',execute);monkeypatch.setattr(sys,'argv',[str(launcher),'menu'])
    runpy.run_path(str(launcher),run_name='__main__')
    execute.assert_called_once_with(str(executable),[str(executable),'--state-dir',str(state),'menu'])


def test_launcher_without_configuration_explains_setup(tmp_path,capsys):
    import runpy
    from pathlib import Path
    import pytest
    launcher=tmp_path/'relay';launcher.write_bytes((Path(__file__).resolve().parents[1]/'relay').read_bytes())
    with pytest.raises(SystemExit) as caught:runpy.run_path(str(launcher),run_name='__main__')
    assert caught.value.code==1
    assert 'bootstrap' in capsys.readouterr().err
