"""Validated official tool generations; running assignments keep their own generation."""
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time

from .store import write_json

CURRENT = 'official-tools-current'
MAX_AGE = 300


class TargetIndexUnavailable(RuntimeError):
    pass


def configured(paths):
    return all(paths.get(key) for key in ('checker_dir', 'function_index', 'symbols'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def addresses(path):
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError('Official function index is empty or malformed')
    result = set()
    for row in rows:
        value = row.get('address') if isinstance(row, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{1,8}', value):
            raise ValueError('Official function index contains an invalid address')
        address = int(value, 16)
        if address in result: raise ValueError('Official function index contains duplicate addresses')
        result.add(address)
    return result


def validate(store, snapshot):
    from .compiler import _tree
    from .tools import FILES
    manifest = snapshot['manifest']
    token = identity(manifest)
    if snapshot['id'] != token: raise ValueError('Official tool snapshot identity differs')
    root = store.root/'official-tools'/token
    if root.is_symlink() or root.resolve().parent != (store.root/'official-tools').resolve():
        raise ValueError('Official tool snapshot escaped its directory')
    if set(manifest['files']) != set(FILES): raise ValueError('Incomplete official tool snapshot')
    for name in FILES:
        file = root/name
        if file.is_symlink() or digest(file) != manifest['files'][name]:
            raise ValueError('Official tool snapshot file changed')
    if _tree(root/'fnvcheck') != manifest['checker_tree']:
        raise ValueError('Official checker snapshot changed')
    return {'checker_dir':str(root/'fnvcheck'), 'function_index':str(root/'function_index.json'),
            'symbols':str(root/'fnvdecomp_symbols.json'), 'compiler_tips':str(root/'compiler_tips.md')}


def refresh(store, project, *, now=None):
    from .compiler import _tree
    from .engine import extract
    from .tools import BASE, FILES
    now = time.time() if now is None else now
    parent = store.root/'official-tools'
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with store.lock('official-tools-refresh'), tempfile.TemporaryDirectory(prefix='.download-', dir=parent) as temporary:
        stage = Path(temporary)
        hashes = {}
        for name in FILES:
            project.download_pack(BASE+name, stage/name)
            hashes[name] = digest(stage/name)
        addresses(stage/'function_index.json')
        symbols = json.loads((stage/'fnvdecomp_symbols.json').read_text())
        if not isinstance(symbols, (dict, list)) or not symbols:
            raise ValueError('Official symbols are empty or malformed')
        if not (stage/'compiler_tips.md').read_text().strip():
            raise ValueError('Official compiler tips are empty')
        extracted = stage/'.extract'
        extract(stage/'fnvcheck.tar.gz', extracted)
        roots = [path.parent for path in extracted.rglob('fnvcheck.py') if path.is_file()]
        if len(roots) != 1: raise ValueError('Official checker archive has an ambiguous layout')
        shutil.move(str(roots[0]), str(stage/'fnvcheck'))
        if extracted.exists(): shutil.rmtree(extracted)
        for file in (stage/'fnvcheck').rglob('*.py'):
            ast.parse(file.read_text(), filename=file.name)
        manifest = {'files':hashes, 'checker_tree':_tree(stage/'fnvcheck')}
        snapshot = {'id':identity(manifest), 'manifest':manifest, 'verified_at':now}
        write_json(stage/'snapshot.json', snapshot)
        target = parent/snapshot['id']
        if not target.exists(): stage.rename(target)
        validate(store, snapshot)
        # Publish only after every file and the extracted checker have been validated.
        store.set(CURRENT, snapshot)
        store.event('official_tools_refreshed', {'snapshot_id':snapshot['id']})
        return snapshot


def current(store, project, paths, *, now=None, force=False):
    if not configured(paths): return None
    now = time.time() if now is None else now
    snapshot = store.get(CURRENT)
    if force or not snapshot or now >= snapshot['verified_at']+MAX_AGE:
        snapshot = refresh(store, project, now=now)
    validate(store, snapshot)
    return snapshot


def for_target(store, project, paths, lease, *, now=None):
    if not configured(paths): return paths, None
    now = time.time() if now is None else now
    snapshot = lease.get('tool_snapshot') or current(store, project, paths, now=now)
    selected = validate(store, snapshot)
    target = int(lease['address'], 16)
    if target not in addresses(selected['function_index']):
        key = 'official-tools-missing:'+str(target)
        previous = store.get(key) or {}
        if previous.get('snapshot_id') != snapshot['id'] or now >= previous.get('retry_at', 0):
            snapshot = refresh(store, project, now=now)
            selected = validate(store, snapshot)
            store.set(key, {'snapshot_id':snapshot['id'], 'retry_at':now+MAX_AGE})
        if target not in addresses(selected['function_index']):
            raise TargetIndexUnavailable('Current official function index lacks '+lease['address']+'; no candidate checks were run. Official data will be checked again after the refresh cooldown.')
    return paths | selected, snapshot
