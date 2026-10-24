"""Menu-owned worker process; the menu never polls or compiles in its own thread."""
from contextlib import contextmanager
import os
import signal
import subprocess
import sys

from .lifecycle import process_identity, owner_alive
from .store import AlreadyRunning


@contextmanager
def menu_signals():
    previous = {}
    def interrupted(signum, *_):
        raise SystemExit(128+signum)
    try:
        for name in ('SIGTERM','SIGHUP'):
            signum = getattr(signal,name,None)
            if signum is not None:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum,interrupted)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum,handler)


class MenuWorker:
    def __init__(self, store, dispatch, show=print):
        self.store, self.dispatch, self.show = store, dispatch, show
        self.process = None

    def running(self):
        if self.process is not None and self.process.poll() is None:
            return True
        starting = self.store.get('menu_starting_worker') or {}
        if starting.get('owner') and owner_alive(starting['owner']):
            return True
        try:
            with self.store.lock('serve'):
                return False
        except AlreadyRunning:
            return True

    def start(self):
        with self.store.lock('menu-worker-start'):
            if self.running():
                self.show('Relay already running. This menu can manage its lanes and stop it on exit.')
                return
            if self.store.pending_operations():
                self.show('Finish incomplete lane setup with Add lane, or reconcile unresolved operations before starting.')
                return
            owner = process_identity()
            if owner is None: raise ValueError('Cannot bind the menu process identity')
            self.store.set('shutdown_requested',False)
            self.store.set('drain',False)
            log_path = self.store.root/'worker.log'
            fd = os.open(log_path,os.O_CREAT|os.O_APPEND|os.O_WRONLY,0o600)
            with os.fdopen(fd,'ab') as log:
                self.process = subprocess.Popen([sys.executable,'-u','-m','fnv_relay.cli',
                    '--state-dir',str(self.store.root),'start','--menu-owner-pid',str(owner['pid']),
                    '--menu-owner-start-time',owner['start_time']],stdin=subprocess.DEVNULL,
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            self.store.set('menu_starting_worker',{'owner':process_identity(self.process.pid),'menu_owner':owner})
            self.show('Relay starting. Keep this menu open; Add lane and Show status remain available.')
            self.show('Worker output: '+str(log_path))

    def stop(self):
        if not self.running() and not any(lane.get('lease') for lane in self.store.lanes()) and not self.store.pending_operations():
            return True
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        code = self.dispatch(['stop','--timeout','30'])
        if self.process is not None:
            try: self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.show('Worker is still stopping. Keep this menu open; claims remain guarded.')
                return False
        if code != 0:
            self.show('Check status: shutdown or claim reconciliation still needs attention.')
            return False
        return True


    def output(self):
        path = self.store.root/'worker.log'
        try:
            with path.open('rb') as stream:
                stream.seek(0,2);stream.seek(max(0,stream.tell()-16000))
                lines = stream.read().decode('utf-8',errors='replace').splitlines()[-40:]
        except FileNotFoundError:
            self.show('No worker output yet.');return
        from .presentation import clean
        for line in lines:self.show(clean(line,400))


    def live(self):
        from .live import watch
        watch(self.store,self.show,worker_alive=self.running)
        if self.process is not None and self.process.poll() not in (None,0):
            self.show('Worker exited with an error. Recent output:')
            self.output()
