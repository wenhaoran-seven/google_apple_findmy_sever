"""Loopback-only Google CONNECT proxy and reconnecting SSH tunnel. No TLS termination."""
import asyncio
import contextlib
import ipaddress
import os
import re
import socket
from pathlib import Path


def target(line):
    match = re.fullmatch(r'CONNECT ([A-Za-z0-9.-]+):(443|5228) HTTP/1\.[01]', line)
    if not match:
        raise ValueError('Invalid CONNECT')
    host, port = match[1].lower(), int(match[2])
    if port == 5228:
        allowed = host == 'mtalk.google.com' or re.fullmatch(r'mtalk[0-9]+\.google\.com', host)
    else:
        allowed = any(host == d or host.endswith('.'+d) for d in ('google.com', 'googleapis.com', 'gstatic.com', 'googleusercontent.com'))
    if not allowed:
        raise ValueError('Not a Google destination')
    return host, port


async def connect(host, port):
    addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 8)
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Private address rejected')
    for family, _, _, _, addr in addresses[:4]:
        try:
            return await asyncio.wait_for(asyncio.open_connection(addr[0], port, family=family), 8)
        except (OSError, TimeoutError):
            continue
    raise ConnectionError('No route')


async def handle(reader, writer):
    remote = None
    connected = False
    try:
        raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        host, port = target(raw.split(b'\r\n', 1)[0].decode('ascii'))
        remote_reader, remote = await connect(host, port)
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        await writer.drain()
        connected = True
        async def pump(source, destination):
            while True:
                data = await asyncio.wait_for(source.read(65536), 3600)
                if not data: return
                destination.write(data)
                await destination.drain()
        tasks = [asyncio.create_task(pump(reader, remote)), asyncio.create_task(pump(remote_reader, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        if not connected:
            with contextlib.suppress(Exception):
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
    finally:
        for stream in (remote, writer):
            if stream:
                stream.close()
                with contextlib.suppress(Exception): await stream.wait_closed()


async def tunnel():
    ssh = str(Path(os.environ.get('WINDIR', 'C:/Windows'))/'System32/OpenSSH/ssh.exe')
    identity = Path.home()/'.ssh/startag_cloud_ed25519'
    while True:
        proc = await asyncio.create_subprocess_exec(ssh, '-i', str(identity), '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=12',
            '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3', '-o', 'ExitOnForwardFailure=yes',
            '-N', '-R', '127.0.0.1:18768:127.0.0.1:18768', '-L', '127.0.0.1:8767:127.0.0.1:8767',
            'root@47.106.211.14', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
        print('Google tunnel disconnected; retrying in 15 seconds', flush=True)
        await asyncio.sleep(15)


async def main():
    server = await asyncio.start_server(handle, '127.0.0.1', 18768, limit=16384)
    async with server:
        await asyncio.gather(server.serve_forever(), tunnel())


if __name__ == '__main__':
    asyncio.run(main())
