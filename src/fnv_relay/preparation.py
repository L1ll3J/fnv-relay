"""One preparation job per lane; only the controller acquires claims or checks code."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from .engine import Engine
from .store import Store


class PreparationPool:
    """Controller-thread scheduler with private state/client instances in each job."""
    def __init__(self, root, clients, should_stop, on_error, *, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 16):
            raise ValueError('Preparation concurrency must be between 1 and 16')
        self.root=Path(root)
        self.clients=clients
        self.should_stop=should_stop
        self.on_error=on_error
        self.limit=limit
        self.closing=threading.Event()
        self.executor=ThreadPoolExecutor(max_workers=limit,thread_name_prefix='fnv-prepare') if limit is not None else None
        self.lane_executors={}
        self.jobs={}

    def stopped(self):
        return self.closing.is_set() or self.should_stop()

    def reap(self):
        for route,(claim,future) in list(self.jobs.items()):
            if not future.done():continue
            del self.jobs[route]
            executor=self.lane_executors.pop(route,None)
            if executor is not None:executor.shutdown(wait=True)
            if future.cancelled():continue
            error=future.exception()
            if error is not None:self.on_error(route,claim,error)

    def available(self, route):
        self.reap()
        return not self.stopped() and route not in self.jobs and (self.limit is None or len(self.jobs)<self.limit)

    def submit(self, lane):
        route=lane['route_token']
        if not self.available(route):return False
        claim=lane['lease']['claim_id']
        executor=self.executor
        if executor is None:
            executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='fnv-prepare')
            self.lane_executors[route]=executor
        future=executor.submit(self._run,route,claim)
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
        if self.executor is not None:self.executor.shutdown(wait=True,cancel_futures=True)
        for executor in self.lane_executors.values():executor.shutdown(wait=True,cancel_futures=True)
        self.reap()
