"""Numbered human-facing actions while a managed worker runs separately."""
from contextlib import redirect_stdout
from io import StringIO

from . import preferences, presentation
from .managed import MenuWorker


def choose_lane(store, *, ask, show, lanes=None):
    lanes = store.lanes() if lanes is None else lanes
    if not lanes:
        show('No lanes yet. Choose Add lane first.')
        return None
    if len(lanes) == 1:
        return lanes[0]
    for number, lane in enumerate(lanes, 1):
        suffix = ' (setup incomplete)' if lane.get('state') == 'setup' else ''
        show(str(number)+'. '+presentation.clean(lane.get('label','Lane'))+suffix)
    while True:
        value = ask('Choose a lane number [Enter cancels]: ').strip()
        if not value: return None
        if value.isdigit() and 1 <= int(value) <= len(lanes): return lanes[int(value)-1]
        show('Enter one of the lane numbers shown.')


def show_starter(lane, show):
    if lane.get('state') == 'setup' or not lane.get('landing'):
        show('This lane is not ready yet. Choose Add lane to finish its incomplete setup.')
        return
    show('\nOpen a new ChatGPT chat after configuring Notion, then paste everything between these markers:')
    show('----- BEGIN CHATGPT PROMPT -----')
    show(presentation.starter_prompt(lane))
    show('----- END CHATGPT PROMPT -----')
    show('Keep the relay running while ChatGPT works. You can manage more lanes from this menu.')


def add_lane(store, dispatch, *, ask, show):
    incomplete = [lane for lane in store.lanes() if lane.get('state') == 'setup']
    selected = None
    if incomplete:
        show('Finish an existing incomplete lane before creating another:')
        selected = choose_lane(store,ask=ask,show=show,lanes=incomplete)
        if selected is None: return
        command = ['lane','recover',selected['route_token']]
        prefixes = ('create:'+selected['route_token']+':','landing:'+selected['route_token'])
        if any(op['key'].startswith(prefixes) for op in store.pending_operations()):
            show('The previous creation has no confirmed response. Recovery checks Notion before any write.')
            value = ask('If no matching page is found, allow a new creation attempt? [y/N]: ').strip().lower()
            if value in ('y','yes'): command.append('--retry-missing')
        show('Checking and finishing '+presentation.clean(selected.get('label','lane'))+'...')
    else:
        order = preferences.choose_order(store,ask=ask,show=show)
        labels = {lane.get('label') for lane in store.lanes()}
        number = len(labels)+1
        while 'Lane '+str(number) in labels: number += 1
        command = ['lane','create','--order',order,'--label','Lane '+str(number)]
        show('Creating private lane pages...')
    before = {lane['route_token'] for lane in store.lanes()}
    with redirect_stdout(StringIO()):
        result = dispatch(command)
    if result != 0:
        show('Lane setup is saved but unfinished. Choose Add lane again to recover this same lane; it will not make another lane record.')
        return
    if selected:
        ready = store.lane(selected['route_token'])
    else:
        added = [lane for lane in store.lanes() if lane['route_token'] not in before]
        ready = added[0] if len(added) == 1 else None
    if ready is None:
        show('Check status before creating another lane; the result needs review.')
        return
    show('\n'+presentation.clean(ready.get('label','Lane'))+' ready.')
    show_starter(ready,show)


def run(store, dispatch, *, ask=None, show=print, worker=None):
    ask = ask or input
    worker = worker or MenuWorker(store,dispatch,show)
    try:
        while True:
            show('\nFNV Relay — '+('worker running' if worker.running() else 'worker stopped'))
            show('1. Start relay')
            show('2. Add lane / finish incomplete setup')
            show("3. Show a lane's ChatGPT starter")
            show('4. Show status')
            show('5. Resume saved work')
            show('6. Account setup')
            show('7. Stop relay')
            show('8. Show recent worker output')
            show('9. Live updates (Enter returns to menu)')
            show('0. Stop safely and exit')
            try:
                choice = ask('Choose a number: ').strip()
            except (EOFError,KeyboardInterrupt):
                return 0 if worker.stop() else 1
            if choice == '0':
                if worker.stop(): return 0
            elif choice == '1':
                worker.start()
                for lane in store.lanes():
                    if lane.get('state') != 'setup' and lane.get('landing'): show_starter(lane,show)
                if worker.running(): worker.live()
            elif choice == '2': add_lane(store,dispatch,ask=ask,show=show)
            elif choice == '3':
                lane = choose_lane(store,ask=ask,show=show)
                if lane: show_starter(lane,show)
            elif choice == '4': dispatch(['status'])
            elif choice == '5':
                lane = choose_lane(store,ask=ask,show=show)
                if not lane: continue
                saved = lane.get('parked',[])
                if not saved:
                    dispatch(['lane','resume',lane['route_token']])
                    continue
                for number,entry in enumerate(saved,1): show(str(number)+'. '+entry['continuation']['address'])
                value = ask('Choose a saved function number [Enter cancels]: ').strip()
                if value.isdigit() and 1 <= int(value) <= len(saved):
                    dispatch(['lane','resume',lane['route_token'],'--address',saved[int(value)-1]['continuation']['address']])
                elif value: show('Enter one of the saved function numbers shown.')
            elif choice == '6': dispatch(['setup'])
            elif choice == '7': worker.stop()
            elif choice == '8': worker.output()
            elif choice == '9': worker.live()
            else: show('Enter one of the menu numbers.')
    finally:
        if worker.running(): worker.stop()
