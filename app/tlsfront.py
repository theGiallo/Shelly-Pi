"""HTTPS in front of the web app, inside the same process.

Accepts TLS on its own port and passes the decrypted bytes to the app's plain-HTTP listener on
127.0.0.1. The app itself needs no changes, streaming (live updates, backups, uploads) works as
it does over HTTP, and a renewed certificate is picked up for new connections without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

log = logging.getLogger(__name__)


class TlsFront:
    def __init__(self, port: int, backend_port: int, bind: str = "0.0.0.0"):
        self.port, self.backend_port, self.bind = port, backend_port, bind
        self._ctx: ssl.SSLContext | None = None
        self._server: asyncio.AbstractServer | None = None

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def bound_port(self) -> int | None:
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return None

    def _load(self, crt: Path, key: Path) -> ssl.SSLContext:
        ctx = self._ctx or ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        ctx.load_cert_chain(str(crt), str(key))
        return ctx

    async def start(self, crt: Path, key: Path) -> None:
        if self.running:
            self.reload(crt, key)
            return
        self._ctx = self._load(crt, key)
        self._server = await asyncio.start_server(
            self._handle, self.bind, self.port, ssl=self._ctx, ssl_handshake_timeout=10, reuse_address=True,
        )
        log.info("HTTPS listening on %s:%s", self.bind, self.bound_port)

    def reload(self, crt: Path, key: Path) -> None:
        """New connections use the new certificate; open ones carry on with the old."""
        if self._ctx is not None:
            self._ctx.load_cert_chain(str(crt), str(key))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            log.info("HTTPS stopped")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            br, bw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.backend_port), 5)
        except (OSError, asyncio.TimeoutError):
            writer.close()
            return

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (OSError, ssl.SSLError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.gather(pump(reader, bw), pump(br, writer))
        finally:
            for w in (writer, bw):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
