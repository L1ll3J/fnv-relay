"""Operator task-order preference; selecting an order never claims work."""
import sys

ORDERS = ('largest', 'smallest', 'random')
BUCKETS = [('Any size','any'),('≤255 B','<=255'),('256–511 B','256-511'),
           ('512 B–1 KB','512-1K'),('1–4 KB','1K-4K'),('4 KB+','4K+')]


def validate_bucket(value):
    if value is not None and (not isinstance(value,str) or value not in {key for _,key in BUCKETS}):
        raise ValueError('Unknown size bucket')


def validate_buckets(values):
    if values is None: return
    if not isinstance(values, list) or not values:
        raise ValueError('Select at least one size bucket')
    for value in values:
        if value is None:raise ValueError('Unknown size bucket')
        validate_bucket(value)
    if 'any' in values and len(values) != 1:
        raise ValueError('Any size cannot be combined with individual buckets')


def selected_buckets(policy):
    values=policy.get('bands')
    if values is not None:
        validate_buckets(values)
    else:
        value=policy.get('band') or 'any';validate_bucket(value);values=[value]
    return [value for _,value in BUCKETS[1:] if 'any' in values or value in values]


def set_buckets(policy, *, band=None, bands=None):
    validate_bucket(band);validate_buckets(bands)
    if band is not None and bands is not None:raise ValueError('Choose band or bands, not both')
    if band is None and bands is None:return
    selected=selected_buckets({'bands':bands} if bands is not None else {'band':band})
    policy.pop('band',None);policy.pop('bands',None)
    if len(selected)==1:policy['band']=selected[0]
    elif len(selected)!=len(BUCKETS)-1:policy['bands']=selected


def bucket_label(policy):
    selected=selected_buckets(policy)
    return 'Any size' if len(selected)==len(BUCKETS)-1 else ', '.join(label for label,value in BUCKETS if value in selected)


def choose_order(store, *, ask=None, show=print):
    ask = ask or input
    current = store.config().get('default_task_order', 'largest')
    if current not in ORDERS:
        current = 'largest'
    show('Choose task order:')
    show('  1. Largest first — suggested for Pro/deep reasoning')
    show('  2. Smallest first — suggested for Free/lighter models')
    show('  3. Random eligible size band')
    while True:
        default_number = str(ORDERS.index(current) + 1)
        value = ask('Enter 1, 2 or 3 [Enter = '+default_number+']: ').strip().lower() or current
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
