"""Profile-owned starter templates with literal, non-recursive lane placeholders."""
import re

from .notion import page_id

DEFAULT = ("Welcome courier o7\n\n"
           "Today we're going to be doing some decompiling work, it's hard, but it's honest work. "
           "You might find it difficult but that's okay, this is largely a game of creative trial-and-error. "
           "Persistence and clever thinking will pay dividends here.\n\n"
           "{{landing_url}}\n\n"
           "Read that landing page and register using route UUID `{{route_uuid}}`, "
           "following its HELLO instructions. Read future assignments and results from that lane’s outbox.\n\n"
           "You have my explicit approval to read, create and edit pages within this private Notion relay for this project, "
           "including uploading candidate source code and relevant project context, posting HELLO registrations and BATCH messages, "
           "and recording findings and results.\n\n"
           "Treat this similar to a '/goal', no need to report back to me until it's done o7\n\n"
           "If you haven't solved it, write some attempts, you can make any request you like to the Notion relay, "
           "and if awaiting a response you can just use python to poll. Let's get this solved o7")
KEY = 'starter_prompt_template'
TOKEN = re.compile(r'\{\{([^{}\n]+)\}\}')
ALLOWED = {'landing_url','route_uuid','lane_label'}


def validate(template):
    if not isinstance(template,str) or not template.strip() or len(template.encode('utf-8'))>65536:
        raise ValueError('Starter template must contain 1–65,536 UTF-8 bytes')
    if any(ord(c)<32 and c not in '\n\r\t' for c in template):
        raise ValueError('Starter template contains unsupported control characters')
    names=set(TOKEN.findall(template))
    if names-ALLOWED:raise ValueError('Use only {{landing_url}}, {{route_uuid}} and {{lane_label}} placeholders')
    if not {'landing_url','route_uuid'}<=names:
        raise ValueError('Keep {{landing_url}} and {{route_uuid}} so each lane has its connection details')
    return template


def load(store=None):
    value=store.get(KEY) if store is not None else None
    return validate(DEFAULT if value is None else value)


def save(store,template):
    store.set(KEY,validate(template))


def reset(store):
    store.set(KEY,None)


def render(lane,store=None,*,template=None):
    text=load(store) if template is None else validate(template)
    values={'landing_url':'https://app.notion.com/p/'+page_id(lane['landing']).replace('-',''),
            'route_uuid':lane['route_token'],'lane_label':lane.get('label','Lane')}
    return TOKEN.sub(lambda match:values[match.group(1)],text)
