"""Full-screen owner console. Workers and service mutations stay in separate processes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import signal
import subprocess
import sys
import uuid

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane, TextArea

from . import presets, presentation
from .dashboard import Dashboard, clock
from .managed import MenuWorker
from .store import AlreadyRunning


class PromptScreen(ModalScreen):
    def __init__(self, text):
        super().__init__(); self.text=text

    def compose(self):
        with Vertical(classes='dialog'):
            yield Label('ChatGPT starter')
            yield Static('Keep the relay running. Open a NEW ChatGPT chat after configuring Notion, then paste this text.')
            yield TextArea(self.text,read_only=True,id='starter-text')
            with Horizontal(classes='buttons'):
                yield Button('Copy starter',id='copy-starter',variant='primary')
                yield Button('Close',id='close-starter')

    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='copy-starter':self.app.copy_text(self.text)
        else:self.dismiss()


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self,text):super().__init__();self.text=text
    def compose(self):
        with Vertical(classes='confirmation'):
            yield Static(Text(self.text))
            with Horizontal(classes='buttons'):
                yield Button('Continue',id='yes',variant='primary')
                yield Button('Cancel',id='no')
    def on_button_pressed(self,event):event.stop();self.dismiss(event.button.id=='yes')


class AddLaneScreen(ModalScreen[dict | None]):
    def __init__(self,number):super().__init__();self.number=number
    def compose(self):
        with Vertical(classes='confirmation'):
            yield Label('Add a lane')
            yield Input('Lane '+str(self.number),placeholder='Lane name',id='new-label',max_length=100)
            yield Select([('Largest first','largest'),('Smallest first','smallest'),('Random size band','random')],value='largest',allow_blank=False,id='new-order')
            yield Checkbox('Allow previously attempted functions',value=False,id='new-attempted')
            yield Input('0',placeholder='Batch attempt limit per function (0 = off)',id='new-max-attempts',type='integer')
            yield Static('Attempt limit: 0 = off. One accepted batch is one round, whether it has one candidate or eight.')
            yield Static('Unchecked = fresh functions only. Random shuffles eligible size bands; FNV selects a function within a band.')
            with Horizontal(classes='buttons'):
                yield Button('Create lane',id='create',variant='primary')
                yield Button('Cancel',id='cancel')
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='cancel':self.dismiss(None);return
        label=self.query_one('#new-label',Input).value.strip()
        if not label:self.notify('Enter a lane name.',severity='warning');return
        try:
            maximum=int(self.query_one('#new-max-attempts',Input).value)
            from .attempts import validate
            validate(maximum)
        except ValueError:self.notify('Enter a non-negative attempt limit (0 = off).',severity='warning');return
        self.dismiss({'label':label,'max_attempts':maximum,'order':self.query_one('#new-order',Select).value,
                      'allow_attempted':self.query_one('#new-attempted',Checkbox).value})


class RelayApp(App):
    TITLE='FNV Relay · Courier console'
    BINDINGS=[('ctrl+q','quit','Stop & exit'),('f1',"show_tab('overview')",'Overview'),
              ('f2',"show_tab('lanes')",'Lanes'),('f3',"show_tab('history')",'History'),
              ('f4',"show_tab('activity')",'Activity'),('f5',"show_tab('notepad')",'Notepad'),
              ('f6',"show_tab('settings')",'Settings')]
    BINDINGS=[Binding(key,action,label,priority=True) for key,action,label in BINDINGS]
    CSS='''
    Screen { background: #10151a; color: #dedbcf; }
    Header { background: #222c35; color: #efd59a; }
    Footer { background: #222c35; }
    #status { height: 3; padding: 1 2; background: #192229; color: #efd59a; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 1 2; }
    DataTable { height: 1fr; min-height: 6; background: #10151a; }
    DataTable > .datatable--header { background: #293640; color: #efd59a; text-style: bold; }
    DataTable > .datatable--cursor { background: #405f64; color: white; }
    .help { height: auto; margin: 0 0 1 0; color: #a5b2b6; }
    .buttons { height: auto; min-height: 3; margin: 1 0; }
    Button { margin-right: 1; }
    #lane-detail { height: auto; min-height: 3; max-height: 8; margin-top: 1; }
    #notice { height: auto; min-height: 1; max-height: 3; padding: 0 2; color: #e9c775; }
    Select, Input { margin: 0 0 1 0; }
    #prompt-editor { height: 1fr; min-height: 8; }
    .dialog { width: 90%; height: 85%; padding: 1 2; border: thick #d5b775; background: #192229; }
    .confirmation { width: 72; height: auto; max-height: 90%; padding: 1 2; border: thick #d5b775; background: #192229; }
    ModalScreen { align: center middle; background: #00000090; }
    #starter-text { height: 1fr; margin-top: 1; }
    Checkbox { margin: 0 0 1 0; }
    #policy-info, #saved-info, #account-info { height: auto; margin-bottom: 1; }
    '''

    def __init__(self,store,dispatch,worker=None):
        super().__init__()
        self.store,self.dispatch=store,dispatch
        self.worker=worker or MenuWorker(store,dispatch,self.notice)
        self.model=Dashboard(store)
        self.selected=None;self.busy=False;self.data=None;self.table_shape=None;self.route_options=None
        self.entries=presets.load(store);self.note_id=self.entries[0]['id'] if self.entries else None
        self.note_loading=False
        self.settings=store.get('terminal_display') or {'size':True,'lease':True,'checks':True,'attempts':True,'best':True,'stagnation':True,'policy':True,'metric':'ndiff_insn'}
        self.sub_title=store.root.name

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static('Loading saved state…',id='status')
        with TabbedContent(initial='overview'):
            with TabPane('Overview',id='overview'):
                yield Static('Select a lane to inspect it. Progress updates in place. Times are UTC.',classes='help')
                yield DataTable(id='lanes-table',cursor_type='row',zebra_stripes=True)
                yield Static('',id='lane-detail',markup=False)
                with Horizontal(classes='buttons'):
                    yield Button('Start relay',id='start',variant='success')
                    yield Button('Stop safely',id='stop')
                    yield Button('Add lane',id='add',variant='primary')
                    yield Button('Copy / view starter',id='starter')
            with TabPane('Lane controls',id='lanes'):
                with VerticalScroll():
                    yield Static('Select a lane in Overview. Settings apply to future selections; saved unfinished work can still be resumed.',classes='help')
                    yield Static('',id='policy-info',markup=False)
                    yield Select([('Largest first','largest'),('Smallest first','smallest'),('Random size band','random')],allow_blank=False,value='largest',id='order')
                    yield Checkbox('Allow previously attempted functions',value=True,id='allow-attempted')
                    yield Static('Batch attempt limit per function: 0 = off. Existing rounds in this lane count across reconnects. An active handoff finishes even if the limit is changed.',classes='help')
                    yield Input('0',id='max-attempts',type='integer')
                    yield Checkbox('Park current work and choose a new function now',value=False,id='switch-now')
                    yield Static('Fresh only never falls back to attempted work. Random shuffles size bands. Switching preserves sources, best results and history.',classes='help')
                    with Horizontal(classes='buttons'):
                        yield Button('Apply selection settings',id='apply-policy',variant='primary')
                        yield Button('Pause future work',id='pause')
                        yield Button('Resume lane',id='resume')
                    yield Static('',id='saved-info',markup=False)
                    yield Select([],prompt='Choose preserved work',id='saved-work')
                    yield Button('Resume selected saved work',id='resume-saved')
            with TabPane('History',id='history'):
                yield Select([],prompt='Choose a lane',id='history-lane')
                yield Static('Best scores are compared only within the same pack and metric. The first observation establishes a baseline.',classes='help')
                yield DataTable(id='history-table',cursor_type='row',zebra_stripes=True)
            with TabPane('Activity & data flow',id='activity'):
                yield Static('Agent proposal → Notion inbox → local checker → Notion results → verified submission. This view shows recorded events, not inferred agent activity.',classes='help')
                yield DataTable(id='activity-table',cursor_type='row',zebra_stripes=True)
            with TabPane('Prompt notepad',id='notepad'):
                yield Static('Copy a prompt into ChatGPT when useful. Nothing here is sent automatically or appended to the starter. Edit and save your own versions.',classes='help')
                yield Select([(x['name'],x['id']) for x in self.entries],value=self.note_id if self.note_id else Select.NULL,id='preset')
                yield Input(self.entries[0]['name'] if self.entries else '',placeholder='Prompt name',max_length=100,id='prompt-name')
                yield TextArea(self.entries[0]['text'] if self.entries else '',id='prompt-editor',show_line_numbers=False)
                with Horizontal(classes='buttons'):
                    yield Button('Copy prompt',id='copy-prompt',variant='primary')
                    yield Button('Save',id='save-prompt')
                    yield Button('New',id='new-prompt')
                    yield Button('Delete',id='delete-prompt')
                    yield Button('Restore presets',id='restore-prompts')
            with TabPane('Settings & help',id='settings'):
                with VerticalScroll():
                    yield Static('Choose which columns appear in Overview. Preferences are saved for this profile.',classes='help')
                    yield Select([('Official instruction differences','ndiff_insn'),('Full-extent diagnostic differences','full_extent_ndiff_insn')],value=self.settings.get('metric','ndiff_insn'),allow_blank=False,id='metric')
                    for key,label in [('size','Function size'),('lease','Lease expiry'),('checks','Check count'),('attempts','Batch attempts / limit'),('best','Best differences'),('stagnation','Checks / time since best'),('policy','Selection policy')]:
                        yield Checkbox(label,value=self.settings.get(key,True),id='metric-'+key)
                    yield Static('Official and full-extent counts measure different scopes. This display choice does not change submission validation.',classes='help')
                    yield Static('',id='account-info',markup=False)
                    with Horizontal(classes='buttons'):
                        yield Button('Readiness check',id='doctor')
                        yield Button('Account setup',id='setup')
                        yield Button('Reconnect Notion',id='reconnect-notion',variant='primary')
        yield Static('',id='notice',markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one('#history-table',DataTable).add_columns('UTC','Claim','Check','Differences','Change','Result','Source')
        self.query_one('#activity-table',DataTable).add_columns('UTC','Lane','Event','Details')
        self.refresh_data()
        self.set_interval(2,self.refresh_data)

    def notice(self,text):
        if self.is_mounted:self.query_one('#notice',Static).update(presentation.clean(text,600))

    def copy_text(self,text):
        self.copy_to_clipboard(text)
        self.notify('Clipboard request sent. If your terminal blocks it, select and copy the text manually.')

    def action_show_tab(self,tab):
        self.set_focus(None)
        self.query_one(TabbedContent).active=tab
        self.call_after_refresh(self.screen.focus_next, '#'+tab+' *')

    def refresh_data(self):
        self.data=self.model.update()
        rows=self.data['lanes']
        if self.selected not in {r['route_token'] for r in rows}:self.selected=rows[0]['route_token'] if rows else None
        running=self.worker.running()
        state='Running' if self.data['worker'].get('running') else 'Starting / stopping' if running else 'Stopped'
        self.query_one('#status',Static).update(Text(f'{state}  ·  {len(rows)} lanes  ·  {sum(r["has_active_lease"] for r in rows)} active claims  ·  {self.data["uncertain_operations"]} uncertain writes'+('  ·  applying action…' if self.busy else '')))
        columns=[('label','Lane'),('state','State'),('address','Function')]
        if self.settings.get('size',True):columns.append(('size','Bytes'))
        if self.settings.get('best',True):columns.append(('best','Best'))
        if self.settings.get('checks',True):columns.append(('checks','Checks'))
        if self.settings.get('attempts',True):columns.append(('attempts','Attempts'))
        if self.settings.get('stagnation',True):columns += [('since_checks','Checks since best'),('since_time','Time since best')]
        if self.settings.get('lease',True):columns.append(('expires','Lease UTC'))
        if self.settings.get('policy',True):columns.append(('policy','Selection'))
        columns.append(('error','Last error / action'))
        table=self.query_one('#lanes-table',DataTable)
        shape=(tuple(k for k,_ in columns),tuple(r['route_token'] for r in rows))
        changed=shape!=self.table_shape
        if changed:
            table.clear(columns=True)
            for key,label in columns:table.add_column(label,key=key)
        for row in rows:
            progress=self.model.progress(row['route_token'],row['claim_id'],self.settings.get('metric','ndiff_insn'))
            values={'label':row['label'],'state':{'assigned':'awaiting handoff' if row['awaiting_handoff'] else 'working','waiting_hello':'awaiting agent','resume_pending':'saved work ready'}.get(row['state'],row['state']),
                    'address':row['address'],'size':str(row['size_bytes'] or '—'),'best':str(progress['best']) if progress['best'] is not None else '—',
                    'checks':str(row['checks']),'attempts':str(row['attempts'])+(' / '+str(row['max_attempts']) if row['max_attempts'] else ' / off'),'since_checks':str(progress['checks_since_best']) if progress['checks_since_best'] is not None else '—',
                    'since_time':progress['since_best'],'expires':clock(row['expires_at']),
                    'policy':row['order']+(' · fresh' if not row['allow_attempted'] else ' · any'),
                    'error':'change queued' if row['control_pending'] else 'handoff requested' if row['awaiting_handoff'] else 'waiting for eligible work' if row['attempt_pool_empty'] else 'waiting for fresh work' if row['waiting_fresh'] else row['error_type'] or '—'}
            cells=[Text(values[key]) for key,_ in columns]
            if changed:table.add_row(*cells,key=row['route_token'])
            else:
                for (key,_),value in zip(columns,cells):table.update_cell(row['route_token'],key,value)
        self.table_shape=shape
        if changed and self.selected:
            table.move_cursor(row=next(i for i,x in enumerate(rows) if x['route_token']==self.selected))
            self.load_lane_controls()
        options=tuple((r['label'],r['route_token']) for r in rows)
        if options!=self.route_options:
            self.query_one('#history-lane',Select).set_options(options)
            if self.selected:self.query_one('#history-lane',Select).value=self.selected
            self.route_options=options
        self.refresh_details()
        self.refresh_history()
        activity=self.query_one('#activity-table',DataTable)
        labels={x['route_token']:x['label'] for x in rows}
        recent=list(reversed(self.model.activity[-100:]))
        self.replace_rows(activity,[(str(e['id']),[
            datetime.fromtimestamp(e['at'],timezone.utc).strftime('%H:%M:%S'),
            Text(labels.get(e['route'],'—')),e['kind'],Text(e['message'] or e['type'] or e['claim_id'] or '')]) for e in recent])
        configured=self.data['credentials_configured']
        self.query_one('#account-info',Static).update(Text('Accounts configured: FNV '+('yes' if configured['fnv'] else 'no')+' · Notion '+('yes' if configured['notion'] else 'no')+'\nAccount setup requires the relay stopped. Start never happens automatically. Ctrl+Q stops safely before exiting.'))

    def refresh_details(self):
        if not self.selected:
            self.query_one('#lane-detail',Static).update('Add a lane, then start the relay and copy its starter into a new ChatGPT chat.');return
        lane=self.store.lane(self.selected)
        error=self.store.get('last_error:'+self.selected) or {}
        pending=self.store.get('lane_control:'+self.selected)
        text=lane.get('label','Lane')+' · '+self.selected+'\n'
        text+=('Selection change queued; current batch may finish. ' if pending else '')
        text+='Saved functions: '+str(len(lane.get('parked',[])))
        row=next((x for x in self.data['lanes'] if x['route_token']==self.selected),None) if self.data else None
        if row:text+=' · Batch attempts: '+str(row['attempts'])+' / '+(str(row['max_attempts']) if row['max_attempts'] else 'off')
        if lane.get('fresh_empty_bands') and not lane.get('lease'):
            text+='\nSome fresh size bands are empty. Selection continues without falling back to attempted functions.'
        if error:text+='\nLast recorded error (may be historical): '+presentation.clean(error.get('type'))+' · '+presentation.clean(error.get('message'),350)
        self.query_one('#lane-detail',Static).update(Text(text))

    def load_lane_controls(self):
        if not self.selected:return
        lane=self.store.lane(self.selected);policy=lane.get('policy',{})
        self.query_one('#policy-info',Static).update(Text(lane.get('label','Lane')+' · '+self.selected))
        self.query_one('#order',Select).value=policy.get('order','largest')
        self.query_one('#allow-attempted',Checkbox).value=policy.get('allow_attempted',True)
        self.query_one('#max-attempts',Input).value=str(policy.get('max_attempts') or 0)
        saved=[('Continue current saved work','current')]
        saved += [(x['continuation']['address']+' · '+x['reason'],x['continuation']['address']) for x in lane.get('parked',[])]
        self.query_one('#saved-work',Select).set_options(saved)
        self.query_one('#saved-info',Static).update('Sources, checked bests and history stay preserved when switching tasks.')

    @staticmethod
    def replace_rows(table,rows,scope=None):
        keys=tuple(key for key,_ in rows)
        fingerprint=(scope,keys)
        if getattr(table,'relay_fingerprint',None)==fingerprint:return
        previous=getattr(table,'relay_keys',())
        anchor=previous[table.cursor_row] if previous and table.cursor_row<len(previous) and (table.cursor_row>0 or table.scroll_y>0) else None
        table.clear()
        for key,values in rows:table.add_row(*values,key=key)
        table.relay_keys=keys;table.relay_fingerprint=fingerprint
        if anchor in keys:table.move_cursor(row=keys.index(anchor))

    def refresh_history(self):
        table=self.query_one('#history-table',DataTable)
        metric=self.settings.get('metric','ndiff_insn')
        rows=[]
        if self.selected:
            for row in reversed(self.model.history(self.selected,metric)[-200:]):
                change='−'+str(row['delta'])+' NEW BEST' if row['improved'] else 'baseline / no decrease'
                rows.append((row['claim_id']+':'+row['source'],[
                    datetime.fromtimestamp(row['at'],timezone.utc).strftime('%m-%d %H:%M:%S'),
                    row['claim_id'][:14],str(row['index']),str(row['score']),change,row['status'] or '—',row['source'][:12]]))
        self.replace_rows(table,rows,scope=(self.selected,metric))

    def on_data_table_row_highlighted(self,event):
        if event.data_table.id=='lanes-table' and event.row_key.value!=self.selected:
            self.selected=event.row_key.value;self.load_lane_controls();self.refresh_details();self.refresh_history()
            self.query_one('#history-lane',Select).value=self.selected

    def on_select_changed(self,event):
        if event.select.id=='metric' and event.value is not Select.NULL:
            self.settings['metric']=event.value;self.store.set('terminal_display',self.settings);self.refresh_data()
        elif event.select.id=='history-lane' and event.value is not Select.NULL:
            self.selected=event.value;self.load_lane_controls();self.refresh_history();self.refresh_details()
            if self.data:
                routes=[row['route_token'] for row in self.data['lanes']]
                if self.selected in routes:self.query_one('#lanes-table',DataTable).move_cursor(row=routes.index(self.selected))
        elif event.select.id=='preset' and event.value is not Select.NULL and not self.note_loading:
            entry=next((x for x in self.entries if x['id']==event.value),None)
            if entry:
                self.note_id=entry['id'];self.query_one('#prompt-name',Input).value=entry['name'];self.query_one('#prompt-editor',TextArea).load_text(entry['text'])

    def on_checkbox_changed(self,event):
        if event.checkbox.id and event.checkbox.id.startswith('metric-'):
            self.settings[event.checkbox.id[7:]]=event.value;self.store.set('terminal_display',self.settings)
            if self.data:self.refresh_data()

    async def command(self,args,*,exit_after=False):
        if self.busy:self.notice('Wait for the current action to finish.');return
        self.busy=True;self.notice('Applying action… the dashboard will keep updating.')
        try:
            process=await asyncio.create_subprocess_exec(sys.executable,'-u','-m','fnv_relay.cli','--state-dir',str(self.store.root),*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            output,_=await process.communicate()
            text=presentation.clean(output.decode('utf-8','replace'),1000)
            self.notice(text or ('Action completed.' if process.returncode==0 else 'Action failed; check Activity.'))
            if process.returncode:self.notify(text or 'Action failed.',severity='error')
            elif exit_after:self.exit(0)
            return process.returncode == 0
        except (OSError,ValueError) as error:
            self.notice(type(error).__name__+': '+str(error));return False
        finally:self.busy=False;self.refresh_data()

    def launch_command(self,args):
        if self.busy:self.notice('Wait for the current action to finish.');return
        self.run_worker(self.command(args),exit_on_error=False)

    async def stop_worker(self,exit_after=False):
        if self.busy:self.notice('Wait for the current action to finish before stopping or exiting.');return
        if not self.worker.running() and not any(x.get('lease') for x in self.store.lanes()) and not self.store.pending_operations():
            if exit_after:self.exit(0)
            else:self.notice('Relay is stopped. Saved work is preserved.')
            return True
        process=getattr(self.worker,'process',None)
        if process is not None and process.poll() is None:process.send_signal(signal.SIGTERM)
        return await self.command(['stop','--timeout','30'],exit_after=exit_after)

    def interactive_command(self,args):
        # Browser OAuth owns an asyncio loop in its child, outside Textual's loop.
        with self.suspend():
            try:
                result=subprocess.run([sys.executable,'-u','-m','fnv_relay.cli','--state-dir',str(self.store.root),*args],check=False)
                code=result.returncode
            except KeyboardInterrupt:code=130
            except OSError:
                print('Could not open account sign-in. Saved work and connection settings are preserved.')
                code=1
            try:input('Press Enter to return to FNV Relay: ')
            except (KeyboardInterrupt,EOFError):pass
        return code

    async def reconnect_notion(self):
        if self.busy:self.notice('Wait for the current action to finish.');return
        restart=self.worker.running()
        if not await self.stop_worker():
            self.notice('Notion sign-in was not opened: shutdown or claim reconciliation still needs attention.');return
        if self.worker.running() or any(row.get('lease') for row in self.store.lanes()):
            self.notice('The relay is still stopping. Reconnect Notion once shutdown completes.');return
        self.busy=True
        try:
            code=self.interactive_command(['auth','notion','connect'])
            if code:
                self.notice('Notion sign-in did not complete. Saved work is preserved; relay remains stopped.');return
            if restart:
                self.worker.start();self.notice('Notion reconnected. Relay restarting with saved work.')
            else:self.notice('Notion reconnected. Choose Start relay when ready.')
        finally:self.busy=False;self.refresh_data()

    async def action_quit(self):await self.stop_worker(exit_after=True)

    def choose_add(self,choice):
        if choice:self.launch_command(['lane','create','--label',choice['label'],'--order',choice['order'],'--allow-attempted' if choice['allow_attempted'] else '--no-allow-attempted','--max-attempts',str(choice.get('max_attempts',0))])

    def update_notepad_options(self):
        self.note_loading=True
        widget=self.query_one('#preset',Select);widget.set_options([(x['name'],x['id']) for x in self.entries])
        widget.value=self.note_id if self.note_id and any(x['id']==self.note_id for x in self.entries) else Select.NULL
        self.note_loading=False

    def save_note(self):
        entry={'id':self.note_id or str(uuid.uuid4()),'name':self.query_one('#prompt-name',Input).value,'text':self.query_one('#prompt-editor',TextArea).text}
        values=[x for x in self.entries if x['id']!=entry['id']]+[entry]
        try:presets.save(self.store,values)
        except ValueError as error:self.notice(str(error));return
        self.entries=values;self.note_id=entry['id'];self.update_notepad_options();self.notice('Prompt saved privately. Nothing was sent.')

    def on_button_pressed(self,event):
        try:self.handle_button(event)
        except (AlreadyRunning,ValueError,OSError) as error:
            self.notice(type(error).__name__+': '+str(error))

    def handle_button(self,event):
        event.stop();action=event.button.id
        if action=='start':
            if self.busy:self.notice('Wait for the current action to finish.');return
            self.worker.start();self.refresh_data()
        elif action=='stop':self.run_worker(self.stop_worker(),exit_on_error=False)
        elif action=='add':
            incomplete=[x for x in self.store.lanes() if x.get('state')=='setup']
            if incomplete:
                lane=incomplete[0]
                self.push_screen(ConfirmScreen('Recover '+lane.get('label','lane')+'? Existing pages are checked first. If none match, this authorizes creating the missing pages without making a duplicate lane.'),lambda yes:self.launch_command(['lane','recover',lane['route_token'],'--retry-missing']) if yes else None)
            else:self.push_screen(AddLaneScreen(len(self.store.lanes())+1),self.choose_add)
        elif action=='starter':
            lane=self.store.lane(self.selected) if self.selected else None
            if not lane or not lane.get('landing'):self.notice('Add or recover a lane first.');return
            self.push_screen(PromptScreen(presentation.starter_prompt(lane)))
        elif action=='apply-policy' and self.selected:
            args=['lane','configure',self.selected,'--order',self.query_one('#order',Select).value,
                  '--allow-attempted' if self.query_one('#allow-attempted',Checkbox).value else '--no-allow-attempted',
                  '--max-attempts',self.query_one('#max-attempts',Input).value]
            if self.query_one('#switch-now',Checkbox).value:
                args.append('--switch-now');self.push_screen(ConfirmScreen('Preserve and release the current assignment, park its work, and select a new function with these settings?'),lambda yes:self.launch_command(args) if yes else None)
            else:self.launch_command(args)
        elif action in ('pause','resume') and self.selected:self.launch_command(['lane','configure',self.selected,'--paused' if action=='pause' else '--no-paused'])
        elif action=='resume-saved' and self.selected:
            value=self.query_one('#saved-work',Select).value
            if value is Select.NULL:self.notice('Choose saved work first.');return
            self.launch_command(['lane','resume',self.selected]+(['--address',value] if value!='current' else []))
        elif action=='doctor':self.launch_command(['doctor'])
        elif action=='reconnect-notion':
            if self.busy:self.notice('Wait for the current action to finish.');return
            self.push_screen(ConfirmScreen('Reconnect Notion for this profile? The relay will stop safely and preserve unfinished work before browser sign-in. Choose the existing workspace. After successful sign-in, a previously running relay restarts; otherwise it stays stopped.'),lambda yes:self.run_worker(self.reconnect_notion(),exit_on_error=False) if yes else None)
        elif action=='setup':
            if self.busy or self.worker.running():self.notice('Stop the relay before account setup.');return
            with self.suspend():self.dispatch(['setup'])
            self.refresh_data()
        elif action=='copy-prompt':self.copy_text(self.query_one('#prompt-editor',TextArea).text)
        elif action=='save-prompt':self.save_note()
        elif action=='new-prompt':
            self.note_id=None;self.query_one('#preset',Select).value=Select.NULL
            self.query_one('#prompt-name',Input).value='New prompt';self.query_one('#prompt-editor',TextArea).load_text('')
        elif action=='delete-prompt':
            self.entries=[x for x in self.entries if x['id']!=self.note_id];presets.save(self.store,self.entries);self.note_id=None
            self.update_notepad_options();self.query_one('#prompt-name',Input).value='';self.query_one('#prompt-editor',TextArea).load_text('')
        elif action=='restore-prompts':
            def restore(yes):
                if not yes:return
                defaults=[dict(x) for x in presets.DEFAULTS]
                self.entries=[x for x in self.entries if x['id'] not in {p['id'] for p in defaults}]+defaults
                presets.save(self.store,self.entries);self.note_id=defaults[0]['id'];self.update_notepad_options()
            self.push_screen(ConfirmScreen('Restore the two supplied presets to their original text? Custom prompts will be kept.'),restore)


def run(store,dispatch):
    app=RelayApp(store,dispatch)
    try:return app.run() or 0
    finally:
        if app.worker.running():app.worker.stop()
