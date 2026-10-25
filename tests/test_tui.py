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
