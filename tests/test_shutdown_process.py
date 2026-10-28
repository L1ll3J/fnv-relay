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
 def set_stop_event(self,event): pass
class Notion:
 def whoami(self): return {}
class Engine:
 def __init__(self,store,*a,should_stop=None,watchdog=True): self.store=store; self.stopping=should_stop
 def preparation_failed(self,*a): raise AssertionError('No preparation job expected')
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



def test_disappeared_menu_owner_releases_before_polling(tmp_path):
    script=_WORKER.replace('cli.serve(store)', "cli.serve(store, menu_owner={'pid':-1,'start_time':'missing'})")
    env=dict(os.environ,HOME=str(tmp_path))
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],env=env,capture_output=True,timeout=10)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert (tmp_path/'released').is_file()
    assert not (tmp_path/'ready').exists()


@pytest.mark.parametrize('name',['SIGINT','SIGTERM','SIGHUP'])
def test_shutdown_joins_active_preparation_before_releasing_claims(tmp_path,name):
    if sys.platform != 'linux':pytest.skip('Supported controller platform is Linux')
    script=_WORKER.replace("'state':'assigned'", "'state':'preparing'")
    script=script.replace("  (root/'ready').write_text('ready')", """  self.preparation.submit(self.store.lane('synthetic-lane'))
  while not (root/'preparation-started').exists() and not self.stopping(): time.sleep(.01)
  (root/'ready').write_text('ready')""")
    script=script.replace("  lane=self.store.lane('synthetic-lane');", "  assert (root/'preparation-stopped').exists(), 'Release raced preparation'\n  lane=self.store.lane('synthetic-lane');")
    script=script.replace('cli.Engine=Engine', """from fnv_relay import preparation
class PreparingEngine:
 def __init__(self,store,*args,should_stop=None,**kwargs):self.store=store;self.stopping=should_stop
 def prepare_assignment(self,lane):
  (root/'preparation-started').write_text('started')
  while not self.stopping():time.sleep(.01)
  self.store.set('preparation-joined-before-release',True)
  (root/'preparation-stopped').write_text('stopped')
preparation.Engine=PreparingEngine
cli.Engine=Engine""")
    env=dict(os.environ,HOME=str(tmp_path))
    child=subprocess.Popen([sys.executable,'-c',script,str(tmp_path)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline and child.poll() is None:time.sleep(.01)
        assert (tmp_path/'ready').exists(),'Synthetic preparation did not start'
        child.send_signal(getattr(signal,name));output,error=child.communicate(timeout=10)
        assert child.returncode==0,(output,error)
        assert (tmp_path/'preparation-stopped').exists() and (tmp_path/'released').exists()
        assert b'1 released, 0 pending, 0 uncertain' in output
    finally:
        if child.poll() is None:child.kill();child.wait()
