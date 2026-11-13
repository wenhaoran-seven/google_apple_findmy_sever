"""Anonymous HTTPS transport comparison; contains no account information."""
import asyncio
import hashlib
import json
import socket
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import aiohttp
from findmy.util.tls import apple_trust_context


async def main():
    urls = ["https://gsa.apple.com/grandslam/GsService2", "https://gsa.apple.com/grandslam/GsService2/lookup", "https://gsas.apple.com/grandslam/GsService2"]
    for host in ("gsa.apple.com", "gsas.apple.com"):
        try:
            result = await asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM)
            print(json.dumps({"host":host,"addresses":sorted({r[4][0] for r in result})}), flush=True)
        except Exception as e:
            print(json.dumps({"host":host,"dns_error":type(e).__name__}), flush=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),trust_env=False) as session:
        async def probe(url):
            try:
                async with session.get(url,ssl=apple_trust_context(),allow_redirects=False) as response:
                    body = await response.read()
                    print(json.dumps({"url":url,"status":response.status,"server":response.headers.get("server"),"type":response.headers.get("content-type"),"bytes":len(body),"sha256":hashlib.sha256(body).hexdigest()}), flush=True)
            except Exception as e:
                print(json.dumps({"url":url,"error":type(e).__name__}), flush=True)
        await asyncio.gather(*(probe(url) for url in urls))
asyncio.run(main())
