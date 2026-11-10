"""Loopback-only stateless MCP. No imports from the sync or Plaid modules."""
import telemetry
import logging
import os
import re
import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from access import AccessJWTMiddleware
from queries import Queries

TOOLS = ('overview','list_accounts','list_sources','list_categories','cash_flow','balance_summary','holdings','merchant_totals')

def create_server(path=None):
    public_host=os.environ.get('PLAIDMCP_PUBLIC_HOST','plaidmcp.example.com')
    if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',public_host): raise ValueError('Invalid public hostname')
    mcp = FastMCP('Plaid MCP',host='127.0.0.1',port=8767,
        stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
          allowed_hosts=['127.0.0.1:*','localhost:*',public_host],
          allowed_origins=['http://127.0.0.1:*','http://localhost:*','https://'+public_host]))
    q = Queries(path)
    # Async adapters offload SQLite; FastMCP sync tool dispatch can block its event loop.
    import functools
    import inspect
    for name in TOOLS:
        method = getattr(q,name)
        def adapt(fn):
            @functools.wraps(fn)
            async def call(*args,**kwargs):
                try:
                    with telemetry.phase('query'):
                        return await anyio.to_thread.run_sync(functools.partial(fn,*args,**kwargs))
                except BaseException as exc:
                    telemetry.failure(exc)
                    raise
            call.__signature__ = inspect.signature(fn)
            return call
        mcp.tool(description=(method.__doc__ or '')+' All monetary values are decimal units of the returned currency (USD means dollars); no currency conversion. Spending is positive, refunds negative, liabilities positive owed. Read per-source as_of and unreliable_sources.', annotations={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False})(adapt(method))
    telemetry.install(mcp, "plaidmcp", "0.1.0")
    return mcp

def create_app(path=None):
    team = os.environ.get('ACCESS_TEAM_DOMAIN','').strip()
    aud = os.environ.get('ACCESS_AUD','').strip()
    if os.environ.get('PLAIDMCP_REQUIRE_ACCESS','false').lower()=='true' and not (team and aud): raise ValueError('Access configuration is required')
    if bool(team) != bool(aud): raise ValueError('Configure both Access settings')
    app = create_server(path).streamable_http_app()
    if team:
        if not re.fullmatch(r'[a-z0-9-]+\.cloudflareaccess\.com',team): raise ValueError('Invalid Access team domain')
        app.add_middleware(AccessJWTMiddleware,team_domain=team,audience=aud)
    else: logging.getLogger(__name__).warning('Access validation disabled; trusted localhost only. Configure before exposing.')
    return app

if __name__ == '__main__':
    uvicorn.run(create_app(),host='127.0.0.1',port=8767,access_log=False,log_level='warning')
