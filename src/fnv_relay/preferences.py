"""Operator task-order preference; selecting an order never claims work."""
import sys

ORDERS = ('largest', 'smallest', 'random')


def choose_order(store, *, ask=None, show=print):
    ask = ask or input
    current = store.config().get('default_task_order', 'largest')
    if current not in ORDERS:
        current = 'largest'
    show('Choose future task order:')
    show('  1. Largest first — suggested for Pro/deep reasoning')
    show('  2. Smallest first — suggested for Free/lighter models, if Notion tools are available')
    show('  3. Random eligible size band')
    show('These are starting heuristics, not measured guarantees about subscription tiers.')
    while True:
        value = ask('Task order ['+current+']: ').strip().lower() or current
        selected = {'1':'largest','2':'smallest','3':'random'}.get(value,value)
        if selected in ORDERS:
            with store.lock('config'):
                config=store.config(); config['default_task_order']=selected; store.set_config(config)
            return selected
        show('Choose 1, 2, 3, largest, smallest, or random.')


def start_order(store, explicit=None):
    if explicit is not None:
        if explicit not in ORDERS:
            raise ValueError('Unknown task order')
        with store.lock('config'):
            config=store.config(); config['default_task_order']=explicit; store.set_config(config)
        return explicit,True
    saved=store.config().get('default_task_order')
    if saved in ORDERS:
        return saved,False
    if sys.stdin.isatty():
        return choose_order(store),True
    return 'largest',False
