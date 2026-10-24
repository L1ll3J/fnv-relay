import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from fnv_relay import notion_oauth as oauth
from fnv_relay.notion import NotionError
from fnv_relay.store import Store

# These tests exercise the installed maintained SDK; no external endpoint is contacted.
pytest.importorskip('mcp')
import httpx2
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

WORKSPACE='01234567-89ab-cdef-0123-456789abcdef'
USER='11234567-89ab-cdef-0123-456789abcdef'


class FakeNotion:
    def __init__(self): self.calls=[]; self.challenge=None; self.refreshes=0; self.block_token=False
    def __call__(self,request):
        self.calls.append(request)
        path=request.url.path
        if '.well-known/oauth-protected-resource' in path:
            return httpx2.Response(200,json={'resource':oauth.SERVER,'authorization_servers':[oauth.ORIGIN]})
        if path=='/.well-known/oauth-authorization-server':
            return httpx2.Response(200,json={'issuer':oauth.ORIGIN,'authorization_endpoint':oauth.ORIGIN+'/authorize','token_endpoint':('https://evil.example/token' if self.block_token else oauth.ORIGIN+'/token'),'registration_endpoint':oauth.ORIGIN+'/register','response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],'token_endpoint_auth_methods_supported':['none'],'code_challenge_methods_supported':['S256'],'authorization_response_iss_parameter_supported':True,'scopes_supported':['default','ema_issuer:manage']})
        if path=='/register':
            data=json.loads(request.content); return httpx2.Response(201,json={**data,'client_id':'local-test-client'})
        if path=='/token':
            data=parse_qs(request.content.decode())
            if data['grant_type']==['authorization_code']:
                challenge=base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'][0].encode()).digest()).decode().rstrip('=')
                assert challenge==self.challenge and data['code']==['private-auth-code']
            else:
                assert data['grant_type']==['refresh_token'] and data['refresh_token']==['old-refresh']
                self.refreshes+=1
            return httpx2.Response(200,json={'access_token':'new-access','refresh_token':'rotated-refresh','token_type':'Bearer','expires_in':3600,'scope':'default'})
        if path=='/mcp':
            if request.method in ('GET','DELETE'): return httpx2.Response(405)
            if request.headers.get('Authorization')!='Bearer new-access':
                return httpx2.Response(401,headers={'WWW-Authenticate':f'Bearer resource_metadata="{oauth.ORIGIN}/mcp/.well-known/oauth-protected-resource"'})
            message=json.loads(request.content)
            if 'id' not in message: return httpx2.Response(202)
            if message['method']=='initialize':
                result={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'fake-notion','version':'1'}}
            elif message['method']=='tools/list':
                result={'tools':[{'name':'notion-get-self','inputSchema':{'type':'object'}}]}
            elif message['method']=='tools/call':
                assert message['params']['name']=='notion-get-self'
                result={'content':[{'type':'text','text':json.dumps({'workspace':{'id':WORKSPACE,'name':'Test'},'user':{'id':USER,'name':'Owner','type':'person'}})}]}
            else: raise AssertionError(message['method'])
            return httpx2.Response(200,json={'jsonrpc':'2.0','id':message['id'],'result':result})
        raise AssertionError(str(request.url))


def use_mock_http(monkeypatch,service):
    original=httpx2.AsyncClient
    def client(*args,**kwargs):
        return original(*args,**kwargs,transport=httpx2.MockTransport(service))
    monkeypatch.setattr(httpx2,'AsyncClient',client)


def test_real_sdk_authorization_pkce_callback_and_secure_staged_commit(tmp_path,monkeypatch):
    store=Store(tmp_path); store.set_config({'schema':1,'paths':{},'notion_transport':'token','notion_parent_page':'old-page'})
    store.set_secret('notion_token','advanced-fallback')
    service=FakeNotion(); use_mock_http(monkeypatch,service); shown=[]
    def browser(url):
        query=parse_qs(urlsplit(url).query); service.challenge=query['code_challenge'][0]
        assert query['code_challenge_method']==['S256'] and query['scope']==['default']
        callback=query['redirect_uri'][0]+'?'+urlencode({'code':'private-auth-code','state':query['state'][0],'iss':oauth.ORIGIN})
        with urlopen(callback,timeout=2) as response: assert response.status==200
    result=oauth.connect(store,show=shown.append,open_browser=browser,timeout=5)
    assert result['workspace']['id']==WORKSPACE
    assert store.config()['notion_transport']=='mcp' and 'notion_parent_page' not in store.config()
    secret=store.secrets()['notion_oauth']
    assert secret['tokens']['access_token']=='new-access' and secret['tokens']['refresh_token']=='rotated-refresh'
    assert secret['client']['token_endpoint_auth_method']=='none'
    assert (tmp_path/'secrets.json').stat().st_mode & 0o777==0o600
    assert all('new-access' not in line and 'rotated-refresh' not in line and 'private-auth-code' not in line for line in shown)
    assert not any('/.codex' in str(request.url) for request in service.calls)
    store.close()


def test_refresh_rotates_privately_and_never_opens_browser(tmp_path,monkeypatch):
    store=Store(tmp_path); storage=oauth.TokenStorage(store)
    async def saved():
        await storage.set_client_info(OAuthClientInformationFull(client_id='local-test-client',issuer=oauth.ORIGIN,redirect_uris=['http://127.0.0.1:9999/oauth/callback'],token_endpoint_auth_method='none',grant_types=['authorization_code','refresh_token']))
        await storage.set_tokens(OAuthToken(access_token='old-access',refresh_token='old-refresh',token_type='Bearer',expires_in=1))
    asyncio.run(saved()); data=store.secrets()['notion_oauth']; data['expires_at']=oauth.time.time()-10; store.set_secret('notion_oauth',data)
    service=FakeNotion(); use_mock_http(monkeypatch,service)
    result=oauth.SDKCaller(store,timeout=5)('notion-get-self',{})
    assert result['content'] and service.refreshes==1
    assert store.secrets()['notion_oauth']['tokens']['refresh_token']=='rotated-refresh'
    store.close()


def test_callback_rejects_bad_state_or_issuer_without_ending_valid_flow():
    with oauth.LoopbackCallback(timeout=1) as callback:
        callback.expected_state='expected'
        for query in ({'code':'private','state':'wrong','iss':oauth.ORIGIN},{'code':'private','state':'expected','iss':'https://evil.example'}):
            with pytest.raises(Exception): urlopen(callback.redirect_uri+'?'+urlencode(query),timeout=2)
            assert not callback.event.is_set()
        with urlopen(callback.redirect_uri+'?'+urlencode({'code':'valid','state':'expected','iss':oauth.ORIGIN}),timeout=2) as response:
            assert response.status==200
        result=asyncio.run(callback.wait())
        assert result.code=='valid' and result.state=='expected'


def test_failed_signin_keeps_prior_credentials_config_and_parent(tmp_path,monkeypatch):
    store=Store(tmp_path); store.set_config({'schema':1,'paths':{},'notion_transport':'token','notion_parent_page':'original'})
    store.set_secret('notion_token','keep-private')
    before_config=store.config(); before_secret=store.secrets()
    async def failure(*args,**kwargs): raise RuntimeError('private-token-must-not-leak')
    with pytest.raises(oauth.OAuthError) as caught: oauth.connect(store,_call=failure,timeout=1,show=lambda _:None)
    assert 'private-token' not in str(caught.value)
    assert store.config()==before_config and store.secrets()==before_secret
    store.close()


def test_official_origin_hook_blocks_token_exfiltration(tmp_path,monkeypatch):
    store=Store(tmp_path); service=FakeNotion(); service.block_token=True; use_mock_http(monkeypatch,service)
    def browser(url):
        query=parse_qs(urlsplit(url).query); service.challenge=query['code_challenge'][0]
        with urlopen(query['redirect_uri'][0]+'?'+urlencode({'code':'private-auth-code','state':query['state'][0],'iss':oauth.ORIGIN}),timeout=2): pass
    with pytest.raises(oauth.OAuthError): oauth.connect(store,open_browser=browser,show=lambda _:None,timeout=2)
    assert all(request.url.host=='mcp.notion.com' for request in service.calls)
    assert store.secrets()=={}
    store.close()


def test_provider_runtime_never_opens_interactive_browser(tmp_path,monkeypatch):
    store=Store(tmp_path); storage=oauth.TokenStorage(store,staged=True)
    auth=oauth.provider(storage,open_browser=lambda _:pytest.fail('runtime browser opened'))
    # SDK callback is deliberately guarded even if discovery asks for fresh authorization.
    with pytest.raises(oauth.OAuthError,match='auth notion connect'):
        asyncio.run(auth.context.redirect_handler(oauth.ORIGIN+'/authorize'))
    store.close()


def test_sdk_call_has_an_overall_deadline_and_sanitized_failure(tmp_path,monkeypatch):
    service=FakeNotion()
    async def stall(request): await asyncio.sleep(1); return service(request)
    use_mock_http(monkeypatch,stall)
    store=Store(tmp_path); auth=oauth.provider(oauth.TokenStorage(store,staged=True))
    with pytest.raises(NotionError): asyncio.run(oauth.sdk_call(auth,'notion-get-self',{},timeout=.03))
    store.close()
