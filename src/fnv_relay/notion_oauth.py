"""Standalone Notion OAuth using the official MCP SDK, never another app's tokens."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit, urlencode
import webbrowser

from .notion import NotionError, _retry_after, page_id
from .store import AlreadyRunning, OperationNotStarted

SERVER = 'https://mcp.notion.com/mcp'
ORIGIN = 'https://mcp.notion.com'
SECRET_NAME = 'notion_oauth'


class OAuthError(RuntimeError):
    """Safe user-facing OAuth failure, without server text or credentials."""


def official_url(url):
    try:
        parsed = urlsplit(str(url))
        return parsed.scheme == 'https' and parsed.hostname == 'mcp.notion.com' and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
    except (ValueError, TypeError):
        return False


def _sdk():
    try:
        import anyio
        import httpx2
        from mcp.client.auth import OAuthClientProvider, AuthorizationCodeResult
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata, OAuthClientInformationFull, OAuthToken
    except ImportError:
        raise OAuthError('Notion browser sign-in requires the MCP SDK. Reinstall fnv-relay with its dependencies.') from None
    # SDK transport errors can contain remote text. This CLI exposes sanitized failures instead.
    logger = logging.getLogger('mcp')
    if not logger.handlers: logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return locals()


class TokenStorage:
    """SDK storage backed only by this relay's 0600 secret file."""
    def __init__(self, store, *, staged=False, clock=time.time):
        self.store, self.staged, self.clock = store, staged, clock
        self.data = {} if staged else deepcopy(store.secrets().get(SECRET_NAME, {}))

    def save(self):
        if not self.staged: self.store.set_secret(SECRET_NAME, self.data)

    async def get_tokens(self):
        sdk = _sdk(); value = deepcopy(self.data.get('tokens'))
        if not value: return None
        if self.data.get('expires_at') is not None:
            value['expires_in'] = max(0, int(self.data['expires_at'] - self.clock()))
        return sdk['OAuthToken'].model_validate(value)

    async def set_tokens(self, tokens):
        if tokens.scope and not set(tokens.scope.split()).issubset({'default'}):
            raise OAuthError('Notion returned permissions outside the requested default scope.')
        value = tokens.model_dump(mode='json', exclude_none=True)
        # A provider may omit a replacement refresh token; preserve the existing one.
        if 'refresh_token' not in value and self.data.get('tokens', {}).get('refresh_token'):
            value['refresh_token'] = self.data['tokens']['refresh_token']
        self.data['tokens'] = value
        self.data['expires_at'] = self.clock() + tokens.expires_in if tokens.expires_in is not None else None
        self.save()

    async def get_client_info(self):
        value = self.data.get('client')
        return _sdk()['OAuthClientInformationFull'].model_validate(value) if value else None

    async def set_client_info(self, client_info):
        self.data['client'] = client_info.model_dump(mode='json', exclude_none=True)
        self.save()


class LoopbackCallback:
    """One short-lived loopback listener; authorization values are never logged."""
    def __init__(self, *, timeout=300):
        if not 1 <= timeout <= 600: raise ValueError('OAuth timeout must be between 1 and 600 seconds')
        self.timeout = timeout
        self.expected_state = None
        self.result = None
        self.failure = False
        self.event = threading.Event()
        callback = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup(); self.connection.settimeout(3)
            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                good_host = self.headers.get('Host') == f'127.0.0.1:{callback.server.server_port}'
                state = query.get('state', [])
                valid = good_host and parsed.path == '/oauth/callback' and not parsed.scheme and not parsed.fragment and len(state) == 1 and state[0].isascii() and len(state[0])<=512 and callback.expected_state is not None and secrets.compare_digest(state[0], callback.expected_state)
                code, issuer = query.get('code', []), query.get('iss', [])
                if not valid or callback.event.is_set():
                    self.send_response(400); self.end_headers(); self.wfile.write(b'Invalid authorization callback.'); return
                if 'error' in query:
                    callback.failure = True; callback.event.set()
                elif len(code) == 1 and 0 < len(code[0]) <= 4096 and not any(ord(c) < 32 for c in code[0]) and len(issuer) <= 1 and (not issuer or issuer[0] == ORIGIN):
                    callback.result = {'code':code[0], 'state':state[0], 'iss':issuer[0] if issuer else None}; callback.event.set()
                else:
                    self.send_response(400); self.end_headers(); self.wfile.write(b'Invalid authorization callback.'); return
                self.send_response(200); self.send_header('Content-Type', 'text/plain; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers()
                self.wfile.write(b'Authorization received. Return to the terminal to finish connecting.')
            def log_message(self, *args): pass
        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.server.timeout = 1
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.redirect_uri = f'http://127.0.0.1:{self.server.server_port}/oauth/callback'

    def __enter__(self):
        self.thread.start(); return self

    def __exit__(self, *args):
        self.event.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.result = None; self.expected_state = None

    async def wait(self):
        sdk = _sdk()
        ready = await sdk['anyio'].to_thread.run_sync(lambda:self.event.wait(self.timeout), abandon_on_cancel=True)
        if not ready: raise OAuthError('Notion sign-in timed out. Run auth notion connect again when ready.')
        if self.failure or self.result is None: raise OAuthError('Notion sign-in was declined. No connection settings changed.')
        return sdk['AuthorizationCodeResult'](**self.result)


def provider(storage, *, callback=None, show=print, open_browser=webbrowser.open):
    sdk = _sdk()
    registered = storage.data.get('client', {}).get('redirect_uris', [])
    redirect_uri = callback.redirect_uri if callback else registered[0] if registered else 'http://127.0.0.1:1/oauth/callback'
    async def redirect(url):
        if callback is None: raise OAuthError('Notion sign-in needs attention. Run auth notion connect; browser sign-in is never started by background work.')
        if not official_url(url): raise OAuthError('Notion returned an unsupported sign-in URL.')
        query = parse_qs(urlsplit(url).query)
        if len(query.get('state', [])) != 1 or query.get('code_challenge_method') != ['S256'] or len(query.get('code_challenge', [])) != 1:
            raise OAuthError('Notion sign-in did not provide valid PKCE parameters.')
        callback.expected_state = query['state'][0]
        # SDK2.3 selects all advertised scopes; this client requests only Notion's default.
        query['scope']=['default']; auth.context.client_metadata.scope='default'
        parsed=urlsplit(url); url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query,doseq=True),''))
        show('Open this Notion sign-in link and approve access to your workspace: ' + str(url))
        def launch():
            try: open_browser(str(url))
            except Exception: pass  # The displayed link remains usable manually, including WSL.
        # Some text-browser launchers block until the browser exits; never block OAuth on them.
        threading.Thread(target=launch,daemon=True).start()
    async def forbidden_callback():
        raise OAuthError('Notion sign-in requires auth notion connect.')
    metadata = sdk['OAuthClientMetadata'](client_name='fnv-relay', redirect_uris=[redirect_uri], grant_types=['authorization_code','refresh_token'], response_types=['code'], token_endpoint_auth_method='none', scope='default')
    auth=sdk['OAuthClientProvider'](server_url=SERVER, client_metadata=metadata, storage=storage, redirect_handler=redirect, callback_handler=callback.wait if callback else forbidden_callback)
    # SDK2.3 restores token objects but not their expiry clock in _initialize().
    # Our absolute persisted deadline must survive every per-operation session.
    auth.context.token_expiry_time=storage.data.get('expires_at')
    return auth


def safe_nested(error):
    if isinstance(error,(OAuthError,NotionError)): return error
    for child in getattr(error,'exceptions',()):
        found=safe_nested(child)
        if found is not None: return found
    return None


async def sdk_call(auth, name, arguments, *, timeout=60):
    sdk = _sdk(); failure={}; read=name in ('notion-fetch','notion-get-self','notion-get-async-task')
    async def official_request(request):
        if not official_url(request.url): raise OAuthError('Blocked a Notion OAuth request outside the official HTTPS service.')
    async def response_metadata(response):
        failure.clear()
        if response.status_code>=400:
            failure.update(status=response.status_code,retry_after=_retry_after(response.headers,{}))
    try:
        with sdk['anyio'].fail_after(timeout):
            async with sdk['httpx2'].AsyncClient(auth=auth, timeout=sdk['httpx2'].Timeout(timeout), follow_redirects=False, trust_env=False, event_hooks={'request':[official_request],'response':[response_metadata]}) as client:
                async with sdk['streamable_http_client'](SERVER, http_client=client, max_sse_event_size=8*1024*1024) as streams:
                    async with sdk['ClientSession'](streams[0], streams[1], read_timeout_seconds=timeout) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
                        return result.model_dump(mode='json', by_alias=True, exclude_none=True)
    except Exception as error:
        safe=safe_nested(error)
        if safe is not None: raise safe from None
        raise NotionError(status=failure.get('status'),retry_after=failure.get('retry_after'),safe_retry=read,code='mcp_http_error' if failure else 'mcp_transport_error') from None


class SDKCaller:
    """Per-operation sessions close deterministically; SDK owns OAuth refresh/PKCE."""
    def __init__(self, store, *, timeout=60):
        self.store, self.timeout = store, timeout

    def __call__(self, name, arguments):
        # Serialize refresh-token rotation with other relay processes on this state dir.
        acquired = False
        try:
            with self.store.lock('notion-oauth-session', timeout=min(self.timeout,30)):
                acquired = True
                storage = TokenStorage(self.store)
                if not storage.data.get('tokens'): raise OAuthError('Notion is not connected. Run auth notion connect.')
                try:
                    return asyncio.run(sdk_call(provider(storage), name, arguments, timeout=self.timeout))
                except (OAuthError,NotionError): raise
                except Exception:
                    raise NotionError(safe_retry=name in ('notion-fetch','notion-get-self'), code='mcp_transport_error') from None
        except AlreadyRunning:
            if not acquired:
                raise OperationNotStarted('Notion is busy with another relay operation. No request was sent; try again shortly.') from None
            raise


def connect(store, *, show=print, open_browser=webbrowser.open, timeout=300, _call=None):
    """Explicit browser sign-in; persist/switch settings only after read-only proof."""
    lanes = store.lanes()
    if any(row.get('lease') for row in lanes): raise OAuthError('Stop the relay and release active claims before reconnecting Notion.')
    from .notion_mcp import unpack
    with store.lock('notion-oauth-session'):
        storage = TokenStorage(store, staged=True)
        try:
            with LoopbackCallback(timeout=timeout) as callback:
                auth = provider(storage, callback=callback, show=show, open_browser=open_browser)
                result = asyncio.run((_call or sdk_call)(auth, 'notion-get-self', {}, timeout=timeout+30))
                data = unpack(result)
                workspace, user = data.get('workspace', {}), data.get('user', {})
                if not isinstance(workspace.get('id'), str) or not workspace['id'] or not isinstance(user.get('id'), str):
                    raise OAuthError('Notion did not confirm the connected workspace. No connection settings changed.')
                if not storage.data.get('tokens') or not storage.data.get('client'):
                    raise OAuthError('Notion did not complete sign-in. No connection settings changed.')
                workspace['id']=page_id(workspace['id']); user['id']=page_id(user['id'])
                storage.data['workspace_id'] = workspace['id']
                if lanes:
                    if store.config().get('notion_workspace_id') != workspace['id']:
                        raise OAuthError('Choose the existing relay workspace. Connection settings were not changed.')
                    for lane in lanes:
                        for role in ('landing', 'inbox', 'outbox', 'data'):
                            page = lane.get(role)
                            if not page: raise OAuthError('Existing lane is incomplete. Connection settings were not changed.')
                            fetched = unpack(asyncio.run((_call or sdk_call)(auth, 'notion-fetch', {'id':page}, timeout=timeout+30)))
                            if fetched.get('object') != 'page' and not fetched.get('id') and not fetched.get('text'):
                                raise OAuthError('Existing relay page access was not confirmed. Connection settings were not changed.')

        except OAuthError: raise
        except Exception:
            raise OAuthError('Notion sign-in failed. No connection settings changed; run auth notion connect again.') from None
        config = store.config()
        if config.get('notion_workspace_id') != workspace['id']:
            config.pop('notion_parent_page', None)
        store.set_secret(SECRET_NAME, storage.data)
        config.update(notion_transport='mcp', notion_workspace_id=workspace['id'])
        store.set_config(config)
        show('Notion browser connection saved privately.')
        return {'workspace':{'id':workspace['id'], 'name':workspace.get('name','')}, 'user':{'id':user['id'], 'name':user.get('name',''), 'type':user.get('type','person')}}
