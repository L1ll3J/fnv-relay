"""Notion protocol adapter over the official OAuth MCP service; no model calls."""
from __future__ import annotations

import json
import re
import time

from .notion import MAX_PAYLOAD, MAX_RECORD, NotionError, page_id
from .store import OperationNotStarted

MAX_RESPONSE = 8 * 1024 * 1024


def unpack(result):
    """Decode MCP structured/text envelopes without mistaking tool errors for data."""
    if not isinstance(result,dict) or result.get('isError'):
        raise NotionError(code='mcp_tool_error')
    if len(json.dumps(result,ensure_ascii=False).encode()) > MAX_RESPONSE:
        raise NotionError(code='mcp_response_too_large')
    structured = result.get('structuredContent')
    if isinstance(structured,dict): return structured
    content = result.get('content')
    if isinstance(content,list):
        texts = [part['text'] for part in content if isinstance(part,dict) and part.get('type')=='text' and isinstance(part.get('text'),str)]
        if len(texts)==1:
            try:
                data=json.loads(texts[0])
                if isinstance(data,dict): return data
            except ValueError: pass
        return {'text':'\n'.join(texts)}
    return result


def markdown(records, text=''):
    parts = [text] if text else []
    for record in records:
        if not isinstance(record,dict): raise ValueError('Relay records must be JSON objects')
        raw = json.dumps(record,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        if len(raw.encode())>MAX_RECORD: raise ValueError('Relay record exceeds 64,000 UTF-8 bytes')
        fence = '`' * max(3,1+max((len(run) for run in re.findall(r'`+',raw)),default=0))
        parts.append(fence+'json\n'+raw+'\n'+fence)
    result='\n\n'.join(parts)
    if len(result.encode())>=MAX_PAYLOAD: raise ValueError('Notion content must be smaller than 500,000 UTF-8 bytes')
    return result


def records_in(text):
    # Closing fence must exactly match the opening fence; embedded C++/Markdown stays JSON data.
    pattern = re.compile(r'^[ \t]*(?P<fence>`{3,})json[ \t]*\r?\n(?P<body>.*?)[\r\n]+[ \t]*(?P=fence)[ \t]*(?=\r?$)',re.M|re.S|re.I)
    records=[]
    for match in pattern.finditer(text):
        try: record=json.loads(match.group('body'))
        except ValueError: continue
        if isinstance(record,dict): records.append(record)
    return records


class NotionMCP:
    def __init__(self, store, *, caller=None, sleep=time.sleep):
        if caller is None:
            from .notion_oauth import SDKCaller
            caller=SDKCaller(store)
        self.store, self.caller, self.sleep = store, caller, sleep

    def close(self):
        # SDKCaller closes every HTTP/session context before returning.
        pass

    def _call(self, name, arguments):
        try:
            return unpack(self.caller(name,arguments))
        except (NotionError, OperationNotStarted): raise
        except Exception as error:
            # No remote error strings enter logs, even when a token is echoed.
            from .notion_oauth import OAuthError
            if isinstance(error,OAuthError): raise
            raise NotionError(safe_retry=name in ('notion-fetch','notion-get-self'),code='mcp_transport_error') from None

    def _await_write(self, result):
        task=result.get('async_task')
        if result.get('object')=='async_task': task=result
        if not task: return result
        identifier=task.get('id') if isinstance(task,dict) else None
        if not isinstance(identifier,str) or not identifier: raise NotionError(code='mcp_invalid_task')
        for _ in range(12):
            delay=task.get('poll_after_seconds',2)
            if not isinstance(delay,(int,float)) or not 0 <= delay <= 30: raise NotionError(code='mcp_invalid_task')
            self.sleep(max(1,delay))
            try:
                task=self._call('notion-get-async-task',{'task_id':identifier})
            except OperationNotStarted:
                # The original mutation was already accepted; an unsent poll
                # cannot make replaying that mutation safe.
                raise NotionError(code='mcp_async_poll_busy') from None
            status=task.get('status')
            if status=='succeeded':
                output=task.get('result')
                if not isinstance(output,dict): raise NotionError(code='mcp_invalid_task')
                return output
            if status=='failed': raise NotionError(code='mcp_task_failed')
            if status not in ('queued','running','retrying'): raise NotionError(code='mcp_invalid_task')
        raise NotionError(code='mcp_task_timeout')

    def whoami(self):
        return self._call('notion-get-self',{})

    def page(self, identifier):
        normalized=page_id(identifier)
        result=self._call('notion-fetch',{'id':normalized})
        result.setdefault('id',normalized)
        return result

    def _fetch_texts(self, identifier):
        result=self.page(identifier)
        # Appending omitted subtrees later would silently reorder an append-only log.
        if result.get('truncated') or result.get('unknown_block_count') or result.get('unknown_block_ids'):
            raise NotionError(code='mcp_incomplete_page',safe_retry=True)
        text=result.get('text',result.get('content',''))
        if not isinstance(text,str): raise NotionError(code='mcp_invalid_page',safe_retry=True)
        if len(text.encode())>MAX_RESPONSE: raise NotionError(code='mcp_response_too_large',safe_retry=True)
        return [text]

    def messages(self, identifier):
        return [record for text in self._fetch_texts(identifier) for record in records_in(text)]

    def _create(self, title, records, text='', parent=None):
        if not isinstance(title,str) or not title or len(title)>2000: raise ValueError('Invalid Notion page title')
        arguments={'pages':[{'properties':{'title':title},'content':markdown(records,text)}],'allow_async':False}
        if parent is not None: arguments['parent']={'page_id':page_id(parent)}
        if len(json.dumps(arguments,ensure_ascii=False).encode())>=MAX_PAYLOAD: raise ValueError('Notion request is too large')
        result=self._await_write(self._call('notion-create-pages',arguments))
        pages=result.get('pages',[])
        if not isinstance(pages,list) or len(pages)!=1 or not isinstance(pages[0],dict): raise NotionError(code='mcp_invalid_create_response')
        return page_id(pages[0].get('id') or pages[0].get('url',''))

    def create_page(self, parent_id, title, records, text=''):
        return self._create(title,records,text,parent_id)

    def create_workspace_page(self, title):
        # Official MCP semantics: omitting parent creates a private workspace page.
        return self._create(title,[])

    def append(self, identifier, record):
        arguments={'page_id':page_id(identifier),'command':'insert_content','position':{'type':'end'},'content':'\n\n'+markdown([record])+'\n','allow_async':False}
        self._await_write(self._call('notion-update-page',arguments))

    def child_pages(self, identifier):
        parent=page_id(identifier); found={}
        for text in self._fetch_texts(parent):
            # Page-shaped strings inside JSON/code are data, never navigation.
            text=re.sub(r'^(`{3,})[^\n]*\n.*?^\1[ \t]*$', '',text,flags=re.M|re.S)
            if '<content>' in text and '</content>' in text: text=text.split('<content>',1)[1].rsplit('</content>',1)[0]
            for match in re.finditer(r'<page\s+[^>]*url="([^"]+)"[^>]*>(.*?)</page>',text,flags=re.S):
                try: child=page_id(match.group(1))
                except ValueError: continue
                if child!=parent: found[child]={'id':child,'title':match.group(2).strip()}
        return list(found.values())
