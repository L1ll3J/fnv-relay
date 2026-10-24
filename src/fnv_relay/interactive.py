"""Numbered human-facing actions using the existing command handlers."""
from contextlib import redirect_stdout
from io import StringIO

from . import preferences, presentation


def choose_lane(store, *, ask, show):
    lanes = store.lanes()
    if not lanes:
        show("No lanes yet. Choose Add lane first.")
        return None
    if len(lanes) == 1:
        return lanes[0]
    for number, lane in enumerate(lanes, 1):
        show(str(number) + ". " + presentation.clean(lane.get('label', 'Lane')))
    while True:
        value = ask("Choose a lane number [Enter cancels]: ").strip()
        if not value: return None
        if value.isdigit() and 1 <= int(value) <= len(lanes): return lanes[int(value)-1]
        show("Enter one of the lane numbers shown.")


def show_starter(lane, show):
    show("\nOpen a new ChatGPT chat after configuring Notion, then paste everything between these markers:")
    show("----- BEGIN CHATGPT PROMPT -----")
    show(presentation.starter_prompt(lane))
    show("----- END CHATGPT PROMPT -----")
    show("Leave the relay running while ChatGPT works. Choose Start relay when ready.")


def run(store, dispatch, *, ask=None, show=print):
    ask = ask or input
    while True:
        show("\nFNV Relay")
        show("1. Start relay")
        show("2. Add lane")
        show("3. Show a lane's ChatGPT starter")
        show("4. Show status")
        show("5. Resume saved work")
        show("6. Account setup")
        show("7. Stop relay")
        show("0. Exit menu")
        try:
            choice = ask("Choose a number: ").strip()
            if choice == '0': return 0
            if choice == '1':
                show("Keep this terminal open. Press Ctrl+C to stop safely and return to this menu.")
                dispatch(['start'])
            elif choice == '2':
                order = preferences.choose_order(store, ask=ask, show=show)
                before = {lane['route_token'] for lane in store.lanes()}
                number = len(before)+1
                while any(lane.get('label') == 'Lane '+str(number) for lane in store.lanes()):
                    number += 1
                label = 'Lane '+str(number)
                # The command's JSON is useful for scripts; the menu presents the new starter.
                with redirect_stdout(StringIO()):
                    result = dispatch(['lane','create','--order',order,'--label',label])
                if result != 0:
                    show("Lane setup did not finish. Check status before trying again; existing work is preserved.")
                    continue
                added = [lane for lane in store.lanes() if lane['route_token'] not in before]
                if len(added) != 1:
                    show("Check status before creating another lane; the result needs review.")
                    continue
                show("\n" + label + " created.")
                show_starter(added[0], show)
            elif choice == '3':
                lane = choose_lane(store, ask=ask, show=show)
                if lane: show_starter(lane, show)
            elif choice == '4': dispatch(['status'])
            elif choice == '5':
                lane = choose_lane(store, ask=ask, show=show)
                if not lane: continue
                saved = lane.get('parked', [])
                if not saved:
                    dispatch(['lane','resume',lane['route_token']])
                    continue
                for number, entry in enumerate(saved, 1):
                    show(str(number) + '. ' + entry['continuation']['address'])
                value = ask("Choose a saved function number [Enter cancels]: ").strip()
                if value.isdigit() and 1 <= int(value) <= len(saved):
                    dispatch(['lane','resume',lane['route_token'],'--address',saved[int(value)-1]['continuation']['address']])
                elif value: show("Enter one of the saved function numbers shown.")
            elif choice == '6': dispatch(['setup'])
            elif choice == '7': dispatch(['stop'])
            else: show("Enter one of the menu numbers.")
        except EOFError:
            return 0
