"""Export target evidence using the operator's original official checker modules."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

_SCRIPT = r'''
import hashlib,json,pathlib,sys
sys.path.insert(0,sys.argv[1])
import fc_common as f
p=json.loads(pathlib.Path(sys.argv[2]).read_text())
a=int(p['address'],16); n=p['size']; b=f.PE.read(a,n)
assert len(b)==n
names={a:sorted(v)[0] for a,v in f.bindings().by_addr.items() if v}
print(json.dumps({'address':p['address'],'bytes':n,'sha256':hashlib.sha256(b).hexdigest()}))
print('\n; Original game bytes (complete extent, including embedded tables)')
for k in range(0,n,16): print('%08x  %s'%(a+k,b[k:k+16].hex(' ')))
print('\n; Official checker disassembly (offsets relative to function start)')
for offset,line in f.render_function(b,a,n,f.Namer(prefer=names)): print('%08x +%04x  %s'%(a+offset,offset,line))
'''

def export(config, pack, work):
    """Never imports pack sources or executes candidate text."""
    pack, work = Path(pack), Path(work)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    cache = work/'context-cache'; cache.mkdir(mode=0o700, exist_ok=True)
    env={'PATH':'/usr/bin:/bin','HOME':str(work),'PYTHONDONTWRITEBYTECODE':'1',
         'FNVCHECK_TREE':str(pack), 'FNVCHECK_CACHE':str(cache),
         'FNVCHECK_TOOLS':str(config['checker_dir']), 'FNVCHECK_INDEX':str(config['function_index']),
         'FNVCHECK_SYMBOLS':str(config['symbols']), 'FNVCHECK_IMAGE':str(config['game_exe'])}
    # These are installed official operator tools, not remote supplied code.
    output=subprocess.run([config.get('python',sys.executable),'-I','-B','-c',_SCRIPT,
                           str(config['checker_dir']),str(pack/'pack.json')],
                          env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    (work/'target-export.stderr.log').write_bytes(output.stderr)
    if output.returncode:
        raise RuntimeError('Target export failed; inspect local target-export.stderr.log (requires original checker dependencies)')
    if len(output.stdout)>16*1024*1024:
        raise RuntimeError('Target context exceeds 16 MiB limit')
    target=work/'target-disassembly.txt'; target.write_bytes(output.stdout)
    return target
