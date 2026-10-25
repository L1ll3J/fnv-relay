"""Owner-approved landing guidance, versioned separately from lane registration."""
import hashlib
import json

WORK_STYLE = """<work_style>
Treat the active assignment as a /goal: persist toward a checker-verified solution without ending the turn solely to report progress.
If the assignment remains unresolved, develop and submit concrete candidate attempts. The Notion relay is an automated CLI, not an agent or human inbox. Use structured REQUEST records only for best_candidate, candidate_artifacts (with source_sha256), or target. Read compiler results from the outbox. Raise bespoke questions, unsupported requests, and blockers needing operator action directly in the chat; do not post them to Notion expecting a reply.
While awaiting a relay response within the current turn, execute Python `import time; time.sleep(30)`, then read the outbox again. Repeat as needed within that turn; do not interpret this as background polling between messages.
Respect the current assignment's scope, lease and relay status throughout.
</work_style>"""


def publication(lane):
    record={'protocol':'fnv-relay/2','kind':'WORK_GUIDANCE','route_token':lane['route_token'],'instructions':WORK_STYLE}
    args={'page':lane['landing'],'record':record}
    digest=hashlib.sha256(json.dumps(args,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return 'landing:'+lane['route_token']+':work-guidance:'+digest,args


def recoverable(store):
    """Only exact current guidance intents may reach startup's read-only recovery."""
    expected={key:args for lane in store.lanes() if lane.get('landing') for key,args in [publication(lane)]}
    return [op for op in store.pending_operations() if op['key'] in expected
            and op['args_hash']==op['key'].rsplit(':',1)[1]]
