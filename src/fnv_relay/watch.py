"""Independent code-only lease guardian; survives the main polling process."""
import argparse
from pathlib import Path
import time
from .engine import Engine, epoch
from .project import FNV
from .store import Store


def run(store, claim_id, *, sleep=time.sleep, clock=time.time):
    while True:
        lane = next((x for x in store.lanes() if x.get('lease',{}).get('claim_id') == claim_id), None)
        if lane is None:
            return
        lease = lane['lease']; directory = Path(lease['directory'])
        if any((directory/name).exists() for name in ('submission-response.json', 'giveup-response.json')):
            return
        remaining = epoch(lease['expires']) - clock() - 600
        if remaining > 0:
            sleep(min(30, remaining)); continue
        # The shared lease lock arbitrates final submission versus release.
        try:
            config = store.config()
            project = FNV(store.secrets()['fnv_api_key'], base=config.get('project_url', 'https://fnvdecomp.com'))
            Engine(store, project, None, clock=clock, watchdog=False).release(lane, 'Code watchdog: unfinished claim released before expiry')
            return
        except Exception as exc:
            store.event('lease_watch_error', {'claim_id':claim_id, 'type':type(exc).__name__})
            # No ambiguous write is retried: release uses a durable operation intent.
            # Read/status outages can recover while the lease is still owned.
            if clock() >= epoch(lease['expires']):
                return
            sleep(min(30, max(1, epoch(lease['expires'])-clock())))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--claim-id', required=True)
    args=parser.parse_args()
    store=Store(Path(args.state_dir))
    try:
        with store.lock('watch-'+args.claim_id):
            run(store,args.claim_id)
    finally:
        store.close()


if __name__=='__main__':
    main()
