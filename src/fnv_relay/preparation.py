"""Bounded assignment preparation; only the controller acquires claims or checks code."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from .engine import Engine
from .store import Store


class PreparationPool:
    """Controller-thread scheduler with private state/client instances in each job."""
    def __init__(self, root, clients, should_stop, on_error, *, limit=4):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('Preparation concurrency must be between 1 and 16')
        self.root=Path(root)
        self.clients=clients
        self.should_stop=should_stop
        self.on_error=on_error
        self.limit=limit
        self.closing=threading.Event()
        self.executor=ThreadPoolExecutor(max_workers=limit,thread_name_prefix='fnv-prepare')
        self.jobs={}

    def stopped(self):
        return self.closing.is_set() or self.should_stop()

    def reap(self):
        for route,(claim,future) in list(self.jobs.items()):
            if not future.done():continue
            del self.jobs[route]
            if future.cancelled():continue
            error=future.exception()
            if error is not None:self.on_error(route,claim,error)

    def available(self, route):
        self.reap()
        return not self.stopped() and route not in self.jobs and len(self.jobs)<self.limit

    def submit(self, lane):
        route=lane['route_token']
        if not self.available(route):return False
        claim=lane['lease']['claim_id']
        future=self.executor.submit(self._run,route,claim)
        self.jobs[route]=(claim,future)
        return True

    def _run(self, route, claim):
        # SQLite connections and OAuth storage never cross thread boundaries.
        store=Store(self.root)
        notion=None
        try:
            if self.stopped():return
            project,notion=self.clients(store)
            engine=Engine(store,project,notion,watchdog=False,should_stop=self.stopped)
            lane=store.lane(route)
            if not lane or lane['state']!='preparing' or (lane.get('lease') or {}).get('claim_id')!=claim:return
            engine.prepare_assignment(lane)
        finally:
            try:
                if notion is not None and callable(getattr(notion,'close',None)):notion.close()
            finally:store.close()

    def close(self):
        self.closing.set()
        self.executor.shutdown(wait=True,cancel_futures=True)
        self.reap()
