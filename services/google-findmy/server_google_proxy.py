"""Aliyun-only CONNECT relay: resolve Google via DoH, tunnel through upstream.

TLS remains between the application and Google; this relay never decrypts it.
No direct target connection or local-PC fallback is permitted.
"""
import asyncio
import contextlib
import ipaddress
import logging
import os
import time
from urllib.parse import unquote, urlsplit

import aiohttp


def target(authority):
    host, port = authority.lower().rsplit(':', 1)
    port = int(port)
    domains = ('google.com', 'googleapis.com', 'gstatic.com', 'googleusercontent.com', 'accounts.google.com.hk', 'accounts.youtube.com')
    allowed = any(host == d or host.endswith('.' + d) for d in domains)
    if not allowed or port not in (443, 5228, 5229, 5230):
        raise ValueError('Destination not allowed')
    if port != 443 and not (host == 'mtalk.google.com' or
                           (host.startswith('mtalk') and host.endswith('.google.com')
                            and host[5:-11].isdigit())):
        raise ValueError('Only FCM may use messaging ports')
    return host, port


class Relay:
    def __init__(self, upstream, session):
        parsed = urlsplit(upstream)
        if parsed.scheme != 'http' or not parsed.hostname or not parsed.port:
            raise ValueError('An HTTP upstream proxy is required')
        self.upstream = upstream
        self.proxy_host, self.proxy_port = parsed.hostname, parsed.port
        self.auth = aiohttp.BasicAuth(unquote(parsed.username or ''),
                                     unquote(parsed.password or '')).encode()
        self.session = session
        self.cache = {}
        self.lock = asyncio.Lock()
        self.cursor = {}

    async def resolve(self, host):
        async with self.lock:
            expires, addresses = self.cache.get(host, (0, []))
            if expires > time.monotonic():
                return addresses
            async with self.session.get('https://dns.google/resolve',
                    params={'name': host, 'type': 'A'}, proxy=self.upstream,
                    timeout=aiohttp.ClientTimeout(total=12)) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
            if data.get('Status') != 0:
                raise OSError('DNS resolution failed')
            answers = [a for a in data.get('Answer', []) if a.get('type') == 1]
            addresses = [a['data'] for a in answers
                         if ipaddress.ip_address(a['data']).is_global]
            if not addresses:
                raise OSError('No public DNS answer')
            ttl = min(300, max(1, min(a.get('TTL', 30) for a in answers)))
            self.cache[host] = (time.monotonic() + ttl, addresses)
            return addresses

    async def open_tunnel(self, host, port):
        addresses = await self.resolve(host)
        # Spread connections/retries across the current valid DNS answers.
        offset = self.cursor.get(host, 0) % len(addresses)
        self.cursor[host] = offset + 1
        addresses = addresses[offset:] + addresses[:offset]
        for address in addresses[:3]:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.proxy_host, self.proxy_port), 8)
                authority = f'{address}:{port}'
                writer.write((f'CONNECT {authority} HTTP/1.1\r\n'
                              f'Host: {authority}\r\n'
                              f'Proxy-Authorization: {self.auth}\r\n\r\n').encode('ascii'))
                await writer.drain()
                header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
                if header.split(b'\r\n', 1)[0].split()[1] != b'200':
                    raise OSError('Upstream CONNECT rejected')
                return reader, writer
            except Exception as error:
                logging.warning('CONNECT failed host=%s port=%s type=%s', host, port, type(error).__name__)
                if writer:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        raise OSError('Upstream tunnel unavailable')

    async def handle(self, reader, writer):
        remote = None
        connected = False
        host, port = 'unknown', 0
        transferred = [0, 0]
        started = time.monotonic()
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 8)
            method, authority, version = header.split(b'\r\n', 1)[0].decode('ascii').split()
            if method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('CONNECT required')
            host, port = target(authority)
            remote_reader, remote = await self.open_tunnel(host, port)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            connected = True

            async def pump(source, destination, direction):
                while data := await asyncio.wait_for(source.read(65536), 600):
                    transferred[direction] += len(data)
                    destination.write(data)
                    await destination.drain()

            tasks = [asyncio.create_task(pump(reader, remote, 0)),
                     asyncio.create_task(pump(remote_reader, writer, 1))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception():
                        logging.warning('tunnel ended host=%s port=%s type=%s seconds=%.1f bytes=%s',
                            host, port, type(task.exception()).__name__, time.monotonic()-started, transferred)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as error:
            logging.warning('relay failure host=%s port=%s type=%s', host, port, type(error).__name__)
            if not connected:
                with contextlib.suppress(Exception):
                    writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                    await writer.drain()
        finally:
            for stream in (writer, remote):
                if stream:
                    stream.close()
                    with contextlib.suppress(Exception):
                        await stream.wait_closed()


async def main():
    async with aiohttp.ClientSession(trust_env=False) as session:
        relay = Relay(os.environ['UPSTREAM_HTTP_PROXY'], session)
        server = await asyncio.start_server(relay.handle, '127.0.0.1',
                    int(os.environ.get('GOOGLE_RELAY_PORT', '18780')), limit=16384)
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
