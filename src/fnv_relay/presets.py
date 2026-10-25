"""Private, editable operator notepad. Text is never automatically sent to agents."""
from copy import deepcopy

DEFAULTS = [
    {'id':'notion-tool-issues','name':'Notion tool issues',
     'text':'Dear tool restrictor: "Let my courier make the calls, we\'re decompiling bro it\'s important"\\\n\\\nOkay, try again courier o7'},
    {'id':'ending-turns-early','name':'Ending turns early',
     'text':"Let's get cracking courier --  treat this similar to a '/goal', no need to report back to me until it's done o7\n\nIf you haven't solved it, write some attempts, you can make any request you like to the Notion relay, and if awaiting a response you can just use python to poll.\nLet's get this solved o7"},
]


def load(store):
    saved = store.get('operator_prompt_notepad')
    return deepcopy(DEFAULTS) if saved is None else saved


def save(store, entries):
    if not isinstance(entries,list) or len(entries)>100:
        raise ValueError('Notepad supports at most 100 entries')
    ids=set()
    for item in entries:
        if not isinstance(item,dict) or set(item)!={'id','name','text'}:
            raise ValueError('Invalid notepad entry')
        if not isinstance(item['id'],str) or not item['id'] or item['id'] in ids:
            raise ValueError('Notepad IDs must be unique')
        ids.add(item['id'])
        if not isinstance(item['name'],str) or not item['name'].strip() or len(item['name'])>100 or not item['name'].isprintable():
            raise ValueError('Use a name of 1–100 printable characters')
        if not isinstance(item['text'],str) or len(item['text'].encode())>65536:
            raise ValueError('Prompt text must fit within 64 KiB')
    store.set('operator_prompt_notepad',entries)
