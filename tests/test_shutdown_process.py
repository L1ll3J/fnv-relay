"""Actual OS signal delivery with synthetic local services; no network/model calls."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


_WORKER = r'''
from pathlib import Path
import sys,time
from fnv_relay import cli
from fnv_relay.store import Store
root=Path(sys.argv[1]); store=Store(root/'state')
store.set_secret('fnv_api_key','synthetic-owner-key')
store.put_lane({'route_token':'synthetic-lane','state':'assigned','lease':{'claim_id':'synthetic-claim'}})
class Project:
 def call(self,*a): return {}
class Notion:
 def whoami(self): return {}
class Engine:
 def __init__(self,store,*a,should_stop=None): self.store=store; self.stopping=should_stop
 def poll(self):
  (root/'ready').write_text('ready')
  while not self.stopping(): time.sleep(.01)
 def release_all(self,reason):
  lane=self.store.lane('synthetic-lane'); lane.pop('lease'); lane['state']='idle'; self.store.put_lane(lane)
  (root/'released').write_text(reason)
  return {'counts':{'released':1,'pending':0,'uncertain':0}}
cli.Engine=Engine
cli.clients=lambda store:(Project(),Notion())
cli.host_checks=lambda paths:{'synthetic_check':True}
raise SystemExit(cli.serve(store))
'''


@pytest.mark.parametrize('name',['SIGINT','SIGTERM','SIGHUP'])
def test_controller_signal_releases_tracked_claim(tmp_path, name):
    if sys.platform != 'linux':
        pytest.skip('Supported controller platform is Linux')
    env=dict(os.environ, HOME=str(tmp_path))
    child=subprocess.Popen([sys.executable,'-c',_WORKER,str(tmp_path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline and child.poll() is None:
            time.sleep(.02)
        assert (tmp_path/'ready').exists(), 'Synthetic controller did not start'
        child.send_signal(getattr(signal,name))
        output,error=child.communicate(timeout=10)
        assert child.returncode==0,(output,error)
        assert (tmp_path/'released').exists()
        assert b'1 released, 0 pending, 0 uncertain' in output
    finally:
        if child.poll() is None:
            child.kill(); child.wait()
