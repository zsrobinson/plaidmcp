"""Real loopback Streamable HTTP MCP handshake + every fixture tool, no Plaid."""
import asyncio
import json
import tempfile
from pathlib import Path
import anyio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from server import create_app,TOOLS
from tests.fixtures import seed
from tests.test_queries import ARGS

async def main():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp); seed(root/'private.sqlite',root/'aggregate.sqlite').close()
        server=uvicorn.Server(uvicorn.Config(create_app(root/'aggregate.sqlite'),host='127.0.0.1',port=8767,access_log=False,log_level='error'))
        task=asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started: break
                if task.done(): await task
                await asyncio.sleep(.05)
            async with streamable_http_client('http://127.0.0.1:8767/mcp') as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools(); assert {t.name for t in tools.tools}==set(TOOLS)
                    async def call(name):
                        result=await session.call_tool(name,ARGS.get(name,{}))
                        assert not result.isError, result
                        data=result.structuredContent or json.loads(result.content[0].text)
                        assert data['as_of'] and 'unreliable_sources' in data
                        return name,data
                    samples=dict(await asyncio.wait_for(asyncio.gather(*(call(n) for n in TOOLS)),15))
                    print(json.dumps(samples,indent=2))
        finally:
            server.should_exit=True
            await task

if __name__=='__main__': asyncio.run(main())
