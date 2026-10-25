import asyncio
from pathlib import Path

from textual.widgets import Checkbox, DataTable, Input, Select, TextArea, TabbedContent

from fnv_relay import presets
from fnv_relay.store import Store
from fnv_relay.tui import RelayApp


class FakeWorker:
    def __init__(self):self.active=False;self.calls=[];self.process=None
    def running(self):return self.active
    def start(self):self.active=True;self.calls.append('start')
    def stop(self):self.active=False;self.calls.append('stop');return True


def make_store(tmp_path):
    store=Store(tmp_path)
    store.put_lane({'route_token':'route-one','label':'Lane 1','state':'idle','policy':{'order':'random','allow_attempted':False},'landing':'01234567-89ab-cdef-0123-456789abcdef'})
    return store


def test_full_screen_mount_navigation_metrics_and_exact_presets(tmp_path):
    async def check():
        store=make_store(tmp_path);worker=FakeWorker();calls=[]
        app=RelayApp(store,lambda command:calls.append(command),worker=worker)
        async with app.run_test(size=(145,48)) as pilot:
            await pilot.pause()
            assert app.query_one('#lanes-table',DataTable).row_count==1
            assert app.query_one('#order',Select).value=='random'
            assert not app.query_one('#allow-attempted',Checkbox).value
            await pilot.click('#start');assert worker.calls==['start']
            await pilot.press('f5');await pilot.pause()
            assert app.query_one(TabbedContent).active=='notepad'
            assert app.query_one('#prompt-editor',TextArea).text==presets.DEFAULTS[0]['text']
            copied=[];app.copy_to_clipboard=copied.append
            await pilot.click('#copy-prompt');assert copied==[presets.DEFAULTS[0]['text']]
            assert calls==[]
            await pilot.press('f6');await pilot.pause()
            app.query_one('#metric-size',Checkbox).value=False
            await pilot.pause()
            assert store.get('terminal_display')['size'] is False
            assert 'size' not in dict(app.table_shape and [(x,x) for x in app.table_shape[0]])
            await pilot.press('f1');await pilot.pause()
            assert app.query_one(TabbedContent).active=='overview'
        store.close()
    asyncio.run(check())


def test_notepad_edit_create_save_and_reload(tmp_path):
    async def check():
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(145,48)) as pilot:
            await pilot.press('f5');await pilot.pause()
            await pilot.click('#new-prompt')
            app.query_one('#prompt-name',Input).value='My fix'
            app.query_one('#prompt-editor',TextArea).load_text('exact\ncustom prompt')
            await pilot.click('#save-prompt');await pilot.pause()
            assert any(x['name']=='My fix' and x['text']=='exact\ncustom prompt' for x in presets.load(store))
            assert presets.load(store)[0]['text']==presets.DEFAULTS[0]['text']
        store.close()
    asyncio.run(check())


def test_starter_modal_keeps_original_text(tmp_path):
    async def check():
        from fnv_relay.presentation import starter_prompt
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(145,48)) as pilot:
            await pilot.click('#starter');await pilot.pause()
            assert app.screen.query_one('#starter-text',TextArea).text==starter_prompt(store.lane('route-one'))
            await pilot.click('#close-starter')
        store.close()
    asyncio.run(check())


def test_small_terminal_can_mount_and_switch_tabs(tmp_path):
    async def check():
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(90,30)) as pilot:
            await pilot.press('f5');await pilot.pause()
            assert app.query_one('#prompt-editor',TextArea).text
            await pilot.press('f2');await pilot.pause()
            assert app.query_one(TabbedContent).active=='lanes'
        store.close()
    asyncio.run(check())


def test_starter_can_stay_open_while_dashboard_refreshes(tmp_path):
    async def check():
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(120,40)) as pilot:
            await pilot.click('#starter');await pilot.pause()
            app.refresh_data()
            assert app.screen.query_one('#starter-text',TextArea).text
            await pilot.click('#close-starter')
        store.close()
    asyncio.run(check())


def test_local_policy_command_runs_without_blocking_ui_or_network(tmp_path):
    async def check():
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(120,40)) as pilot:
            await app.command(['lane','configure','route-one','--order','smallest','--no-allow-attempted'])
            assert not app.busy
            assert store.lane('route-one')['policy']['order']=='smallest'
            assert store.lane('route-one')['policy']['allow_attempted'] is False
            await pilot.press('f3');await pilot.pause()
            assert app.query_one(TabbedContent).active=='history'
        store.close()
    asyncio.run(check())



def test_history_keeps_reading_position_during_live_refresh(tmp_path):
    async def check():
        store=make_store(tmp_path)
        def add(number):
            source='source-'+str(number)
            store.set('check:claim:'+source,{'state':'complete','result':{'status':'wip','pack_sha256':'same-pack','checker':{'ndiff_insn':10-number}}})
            store.event('check_complete',{'route_token':'route-one','claim_id':'claim','source_sha256':source})
        for number in (1,2,3):add(number)
        app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(120,40)) as pilot:
            await pilot.press('f3');await pilot.pause()
            table=app.query_one('#history-table',DataTable)
            table.move_cursor(row=1);await pilot.pause()
            app.refresh_data();assert table.cursor_row==1
            add(4);app.refresh_data();await pilot.pause()
            assert table.cursor_row==2
        store.close()
    asyncio.run(check())



def test_history_lane_choice_and_overview_selection_stay_in_sync(tmp_path):
    async def check():
        store=make_store(tmp_path)
        store.put_lane({'route_token':'route-two','label':'Lane 2','state':'idle','policy':{'order':'smallest'}})
        app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(120,40)) as pilot:
            await pilot.press('f3');await pilot.pause()
            app.query_one('#history-lane',Select).value='route-two'
            await pilot.pause()
            assert app.selected=='route-two'
            assert app.query_one('#lanes-table',DataTable).cursor_row==1
            assert app.query_one('#order',Select).value=='smallest'
        store.close()
    asyncio.run(check())


def test_reconnect_notion_stops_before_signin_and_restarts_only_on_success(tmp_path):
    async def check():
        for result in (0,1,130):
            store=make_store(tmp_path/str(result));worker=FakeWorker();worker.active=True
            app=RelayApp(store,lambda _:None,worker=worker);actions=[]
            async def stop():
                actions.append('stop');worker.active=False;return True
            def signin(args):
                assert not worker.active
                assert app.busy
                actions.append(args)
                return result
            app.stop_worker=stop;app.interactive_command=signin
            async with app.run_test(size=(145,48)) as pilot:
                await app.reconnect_notion();await pilot.pause()
                assert actions==['stop',['auth','notion','connect']]
                assert worker.calls==(['start'] if result==0 else [])
                assert not app.busy
            store.close()
    asyncio.run(check())


def test_reconnect_notion_does_not_start_a_previously_stopped_relay(tmp_path):
    async def check():
        store=make_store(tmp_path);worker=FakeWorker();app=RelayApp(store,lambda _:None,worker=worker)
        calls=[];app.interactive_command=lambda args:calls.append(args) or 0
        async with app.run_test(size=(145,48)) as pilot:
            await app.reconnect_notion()
            assert calls==[['auth','notion','connect']]
            assert worker.calls==[]
        store.close()
    asyncio.run(check())


def test_reconnect_notion_aborts_on_failed_shutdown_or_remaining_claim(tmp_path):
    async def check():
        for blocked in ('failed','lease','running','busy'):
            store=make_store(tmp_path/blocked);worker=FakeWorker();app=RelayApp(store,lambda _:None,worker=worker)
            async def stop():
                if blocked=='lease':
                    lane=store.lane('route-one');lane['lease']={'claim_id':'clm_live'};store.put_lane(lane)
                if blocked=='running':worker.active=True
                return blocked!='failed'
            app.stop_worker=stop;calls=[];app.interactive_command=lambda args:calls.append(args) or 0
            async with app.run_test(size=(145,48)) as pilot:
                app.busy=blocked=='busy'
                await app.reconnect_notion()
                assert calls==[] and worker.calls==[]
                app.busy=False
            store.close()
    asyncio.run(check())


def test_reconnect_button_requires_confirmation_and_can_cancel(tmp_path):
    async def check():
        store=make_store(tmp_path);worker=FakeWorker();app=RelayApp(store,lambda _:None,worker=worker)
        calls=[]
        async def reconnect():calls.append('reconnect')
        app.reconnect_notion=reconnect
        async with app.run_test(size=(145,55)) as pilot:
            await pilot.press('f6');await pilot.pause()
            app.query_one('#reconnect-notion').scroll_visible()
            await pilot.pause();await pilot.click('#reconnect-notion');await pilot.pause()
            assert calls==[]
            await pilot.click('#no');await pilot.pause();assert calls==[]
            await pilot.click('#reconnect-notion');await pilot.pause()
            await pilot.click('#yes');await pilot.pause();assert calls==['reconnect']
        store.close()
    asyncio.run(check())


def test_interactive_signin_uses_child_with_exact_profile_and_no_nested_dispatch(tmp_path,monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import sys
    from fnv_relay import tui
    store=make_store(tmp_path);calls=[]
    app=RelayApp(store,lambda _:(_ for _ in ()).throw(AssertionError('No nested dispatch')),worker=FakeWorker())
    monkeypatch.setattr(app,'suspend',lambda:nullcontext())
    monkeypatch.setattr(tui.subprocess,'run',lambda args,check:calls.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr('builtins.input',lambda _: '')
    assert app.interactive_command(['auth','notion','connect'])==0
    assert calls==[[sys.executable,'-u','-m','fnv_relay.cli','--state-dir',str(store.root),'auth','notion','connect']]
    store.close()


def test_attempt_limit_controls_and_overview_default_off(tmp_path):
    async def check():
        store=make_store(tmp_path);app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(170,55)) as pilot:
            assert 'attempts' in app.table_shape[0]
            await pilot.press('f2');await pilot.pause()
            assert app.query_one('#max-attempts',Input).value=='0'
            calls=[];app.launch_command=calls.append
            app.query_one('#max-attempts',Input).value='12'
            button=app.query_one('#apply-policy');button.scroll_visible();await pilot.pause()
            await pilot.click('#apply-policy');await pilot.pause()
            assert calls and calls[0][calls[0].index('--max-attempts')+1]=='12'
            assert store.lane('route-one')['policy'].get('max_attempts',0)==0
        store.close()
    asyncio.run(check())


def test_starter_template_edit_preview_copy_save_and_cancel_while_worker_runs(tmp_path):
    from fnv_relay import starter_templates
    async def check():
        store=make_store(tmp_path);worker=FakeWorker();worker.active=True;dispatch=[]
        app=RelayApp(store,dispatch.append,worker=worker)
        custom='Profile starter {{lane_label}}\n{{landing_url}}\nRoute {{route_uuid}}'
        async with app.run_test(size=(160,55)) as pilot:
            assert str(app.query_one('#starter').label)=='Starter Prompt'
            await pilot.click('#starter');await pilot.pause()
            await pilot.click('#edit-starter');await pilot.pause()
            app.screen.query_one('#starter-text',TextArea).load_text(custom)
            await pilot.click('#preview-starter');await pilot.pause()
            preview=app.screen.query_one('#starter-text',TextArea).text
            assert 'Profile starter Lane 1' in preview and 'Route route-one' in preview
            assert '{{landing_url}}' not in preview
            assert store.get(starter_templates.KEY) is None
            copied=[];app.copy_to_clipboard=copied.append
            await pilot.click('#copy-starter');assert copied==[preview]
            await pilot.click('#save-starter');await pilot.pause()
            assert starter_templates.load(store)==custom
            await pilot.click('#edit-starter');await pilot.pause()
            app.screen.query_one('#starter-text',TextArea).load_text('Unsaved {{landing_url}} {{route_uuid}}')
            await pilot.click('#cancel-starter');await pilot.pause()
            assert app.screen.query_one('#starter-text',TextArea).text==preview
            assert starter_templates.load(store)==custom
            await pilot.click('#close-starter');await pilot.pause()
            assert worker.active and worker.calls==[] and dispatch==[]
        store.close()
    asyncio.run(check())


def test_starter_template_invalid_preview_preserves_saved_text_and_reset_is_confirmed(tmp_path):
    from fnv_relay import starter_templates
    async def check():
        store=make_store(tmp_path);custom='Custom {{landing_url}} {{route_uuid}}';starter_templates.save(store,custom)
        app=RelayApp(store,lambda _:None,worker=FakeWorker())
        async with app.run_test(size=(160,55)) as pilot:
            await pilot.click('#starter');await pilot.pause()
            await pilot.click('#edit-starter');await pilot.pause()
            app.screen.query_one('#starter-text',TextArea).load_text('Missing lane details')
            await pilot.click('#preview-starter');await pilot.pause()
            assert app.screen.mode=='edit'
            assert starter_templates.load(store)==custom
            await pilot.click('#cancel-starter');await pilot.pause()
            await pilot.click('#reset-starter');await pilot.pause()
            await pilot.click('#no');await pilot.pause()
            assert starter_templates.load(store)==custom
            await pilot.click('#reset-starter');await pilot.pause()
            await pilot.click('#yes');await pilot.pause()
            assert starter_templates.load(store)==starter_templates.DEFAULT
            assert app.screen.query_one('#starter-text',TextArea).text.startswith('Welcome courier o7')
        store.close()
    asyncio.run(check())


def test_bucket_order_controls_are_independent_and_new_lane_can_choose_bucket(tmp_path):
    async def check():
        store=make_store(tmp_path);worker=FakeWorker();worker.active=True;app=RelayApp(store,lambda _:None,worker=worker)
        async with app.run_test(size=(170,60)) as pilot:
            await pilot.press('f2');await pilot.pause()
            assert app.query_one('#band',Select).value=='any'
            app.query_one('#band',Select).value='512-1K'
            app.query_one('#order',Select).value='largest'
            calls=[];app.launch_command=calls.append
            app.query_one('#apply-policy').scroll_visible();await pilot.pause()
            await pilot.click('#apply-policy');await pilot.pause()
            assert calls[0][calls[0].index('--band')+1]=='512-1K'
            assert calls[0][calls[0].index('--order')+1]=='largest'
            await pilot.press('f1');await pilot.pause()
            await pilot.click('#add');await pilot.pause()
            app.screen.query_one('#new-band',Select).value='4K+'
            app.screen.query_one('#create').scroll_visible();await pilot.pause()
            await pilot.click('#create');await pilot.pause()
            assert calls[-1][calls[-1].index('--band')+1]=='4K+'
            assert worker.active and worker.calls==[]
        store.close()
    asyncio.run(check())


def test_running_lane_settings_keep_typed_draft_across_table_rebuild_and_apply(tmp_path):
    async def check():
        store=make_store(tmp_path)
        other=dict(store.lane('route-one'),route_token='route-two',label='Lane 2',policy={'order':'largest','allow_attempted':True,'max_attempts':7})
        store.put_lane(other)
        worker=FakeWorker();worker.active=True
        app=RelayApp(store,lambda _:None,worker=worker)
        async with app.run_test(size=(170,55)) as pilot:
            await pilot.pause()
            app.query_one('#lanes-table',DataTable).move_cursor(row=1)
            await pilot.pause()
            await pilot.press('f2');await pilot.pause()
            field=app.query_one('#max-attempts',Input)
            field.scroll_visible();await pilot.pause()
            await pilot.click('#max-attempts')
            await pilot.press('end','backspace','1','2')
            app.query_one('#order',Select).value='smallest'
            app.query_one('#band',Select).value='256-511'
            app.query_one('#allow-attempted',Checkbox).value=False
            # Rebuilding live columns previously reloaded saved values for the same lane.
            app.settings['size']=False
            app.refresh_data();await pilot.pause()
            assert app.selected=='route-two'
            assert field.value=='12'
            assert app.query_one('#order',Select).value=='smallest'
            assert app.query_one('#band',Select).value=='256-511'
            assert not app.query_one('#allow-attempted',Checkbox).value
            app.refresh_data();await pilot.pause()
            assert field.value=='12'
            calls=[];app.launch_command=calls.append
            app.query_one('#apply-policy').scroll_visible();await pilot.pause()
            await pilot.click('#apply-policy');await pilot.pause()
            assert calls[0][calls[0].index('--max-attempts')+1]=='12'
            assert store.lane('route-two')['policy']['max_attempts']==7
            # Selecting a different lane discards the prior draft and loads saved settings.
            app.query_one('#history-lane',Select).value='route-one';await pilot.pause()
            assert field.value=='0'
            assert app.query_one('#order',Select).value=='random'
            app.query_one('#history-lane',Select).value='route-two';await pilot.pause()
            assert field.value=='7'
        store.close()
    asyncio.run(check())
