import json

import pytest

from fnv_relay.notion import NotionError
from fnv_relay.notion_mcp import NotionMCP, markdown, records_in

PAGE='01234567-89ab-cdef-0123-456789abcdef'
CHILD='11234567-89ab-cdef-0123-456789abcdef'


def envelope(value):
    return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}


class Caller:
    def __init__(self,*values): self.values=list(values); self.calls=[]
    def __call__(self,name,args):
        self.calls.append((name,args)); value=self.values.pop(0)
        if isinstance(value,Exception): raise value
        return envelope(value)


def test_fenced_records_preserve_unicode_crlf_and_embedded_markdown():
    original={'source_utf8':'é😀\r\n```json\r\n{"nested":1}\n```\n``````\n\t', 'array':[1,2]}
    content=markdown([original])
    assert content.startswith('```````json\n')
    assert records_in(content)==[original]
    assert records_in('prose\n\n'+content+'\n\n'+markdown([{'second':True}]))==[original,{'second':True}]


def test_create_and_append_use_supported_synchronous_tool_shapes():
    caller=Caller({'pages':[{'id':PAGE}]},{'page_id':PAGE})
    client=NotionMCP(None,caller=caller)
    record={'source':'exact\r\n😀'}
    assert client.create_page(CHILD,'Candidate',[record])==PAGE
    create=caller.calls[0][1]
    assert create['parent']=={'page_id':CHILD} and create['allow_async'] is False
    assert records_in(create['pages'][0]['content'])==[record]
    client.append(PAGE,record)
    append=caller.calls[1][1]
    assert append['command']=='insert_content' and append['position']=={'type':'end'}
    assert append['allow_async'] is False and records_in(append['content'])==[record]


def test_workspace_root_creation_omits_parent_and_never_sets_public_sharing():
    caller=Caller({'pages':[{'id':PAGE}]})
    assert NotionMCP(None,caller=caller).create_workspace_page('Relay')==PAGE
    args=caller.calls[0][1]
    assert 'parent' not in args and 'creation_mode' not in args
    assert set(args)=={'pages','allow_async'}


def test_readback_works_with_structured_and_text_envelopes():
    record={'kind':'NOTE','text':'exact'}
    caller=Caller({'id':PAGE,'text':markdown([record])})
    assert NotionMCP(None,caller=caller).messages(PAGE)==[record]
    client=NotionMCP(None,caller=lambda name,args:{'structuredContent':{'workspace':{'id':PAGE}}})
    assert client.whoami()=={'workspace':{'id':PAGE}}


@pytest.mark.parametrize('partial',[{'truncated':True},{'unknown_block_count':1,'unknown_block_ids':[CHILD]},{'unknown_block_count':2,'unknown_block_ids':[CHILD]}])
def test_partial_fetch_fails_closed_without_reordering_or_dropping_messages(partial):
    caller=Caller({'text':markdown([{'first':1}]),**partial})
    with pytest.raises(NotionError,match='mcp_incomplete_page'):
        NotionMCP(None,caller=caller).messages(PAGE)
    assert len(caller.calls)==1


def test_async_fallback_polls_read_status_without_repeating_write():
    caller=Caller({'async_task':{'id':'task1','poll_after_seconds':2}}, {'status':'running','poll_after_seconds':3}, {'status':'succeeded','result':{'pages':[{'id':PAGE}]}})
    sleeps=[]; client=NotionMCP(None,caller=caller,sleep=sleeps.append)
    assert client.create_workspace_page('Relay')==PAGE
    assert [name for name,_ in caller.calls]==['notion-create-pages','notion-get-async-task','notion-get-async-task']
    assert sleeps==[2,3]


def test_uncertain_write_is_never_retried_and_error_text_is_redacted():
    caller=Caller(TimeoutError('secret_access_token'))
    with pytest.raises(NotionError) as caught:
        NotionMCP(None,caller=caller).append(PAGE,{'kind':'NOTE'})
    assert not caught.value.safe_retry and 'secret_access_token' not in str(caught.value)
    assert len(caller.calls)==1
    client=NotionMCP(None,caller=lambda n,a:{'isError':True,'content':[{'type':'text','text':'secret_access_token'}]})
    with pytest.raises(NotionError) as caught: client.whoami()
    assert 'secret_access_token' not in str(caught.value)


def test_oversize_records_fail_before_any_mutation():
    caller=Caller(); client=NotionMCP(None,caller=caller)
    with pytest.raises(ValueError): client.append(PAGE,{'source':'é'*40000})
    assert caller.calls==[]


def test_child_pages_ignore_json_data_and_parent_wrapper():
    content=f'<page url="https://app.notion.com/p/{PAGE}">Parent<content>\n<page url="https://app.notion.com/p/{CHILD}">Child</page>\n'+markdown([{'fake':f'<page url="https://app.notion.com/p/{PAGE}">Fake</page>'}])+'\n</content></page>'
    caller=Caller({'text':content})
    assert NotionMCP(None,caller=caller).child_pages(PAGE)==[{'id':CHILD,'title':'Child'}]



def test_pre_send_busy_is_not_misreported_as_remote_transport_error():
    from fnv_relay.store import OperationNotStarted
    from fnv_relay.notion_mcp import NotionMCP
    def busy(*args):raise OperationNotStarted('busy before send')
    with pytest.raises(OperationNotStarted):NotionMCP(None,caller=busy)._call('notion-create-pages',{})



def test_busy_async_poll_does_not_reclassify_accepted_write_as_unsent(tmp_path):
    from fnv_relay.store import Store, OperationNotStarted
    from fnv_relay.notion import NotionError
    from fnv_relay.notion_mcp import NotionMCP
    store=Store(tmp_path);calls=[]
    def caller(name,args):
        calls.append(name)
        if name=='notion-create-pages':
            return {'structuredContent':{'object':'async_task','id':'task-1','status':'queued','poll_after_seconds':0}}
        raise OperationNotStarted('async poll not sent')
    notion=NotionMCP(store,caller=caller,sleep=lambda _:None)
    with pytest.raises(NotionError,match='mcp_async_poll_busy'):
        store.operation('create:test:inbox',{},lambda:notion.create_page('01234567-89ab-cdef-0123-456789abcdef','Test',[]))
    assert store.operation_record('create:test:inbox')['status']=='uncertain'
    assert calls==['notion-create-pages','notion-get-async-task']
    store.close()
