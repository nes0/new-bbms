"""
A tiny HTTPS (CONNECT) proxy that only lets through an allowlist of hosts on port 443.

Used to let the firewalled BBMS clone droplet pull Docker images and Python packages through an SSH reverse tunnel,
without giving it general internet access. Listens on 127.0.0.1 only. Logs every request, allowed or refused.
"""

import asyncio
import sys
import time

ALLOWED = {
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
    "production.cloudfront.docker.com",
    "pypi.org",
    "files.pythonhosted.org",
}
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 31280


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(reader, writer):
    try:
        request = (await reader.readline()).decode("latin-1").split()
        while await reader.readline() not in (b"\r\n", b"\n", b""):  # skip the headers
            pass
        if len(request) != 3 or request[0] != "CONNECT":
            log(f"refused (not CONNECT): {' '.join(request[:2])}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return writer.close()
        host, _, port = request[1].rpartition(":")
        if host not in ALLOWED or port != "443":
            log(f"refused: {request[1]}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return writer.close()
        upstream_reader, upstream_writer = await asyncio.open_connection(host, 443)
        log(f"allowed: {host}")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
    except Exception as error:  # keep serving other connections
        log(f"error: {error!r}")
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    log(f"listening on 127.0.0.1:{PORT}; allowing {', '.join(sorted(ALLOWED))}")
    async with server:
        await server.serve_forever()


asyncio.run(main())
