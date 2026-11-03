"""Real local TCP traffic, including half-close, through the publication gate."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'))
from forward import handle


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_response_after_client_half_close(self):
        payload = b'vpn-test-' * 65536

        async def backend(reader, writer):
            data = await reader.read()
            writer.write(data[::-1])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream = await asyncio.start_server(backend, '127.0.0.1', 0)
        gateway = await asyncio.start_server(handle, '127.0.0.1', 0)
        original = asyncio.open_connection

        async def connect(host, port, **kwargs):
            if port == 1080:
                port = upstream.sockets[0].getsockname()[1]
            return await original(host, port, **kwargs)

        try:
            with patch('forward.asyncio.open_connection', side_effect=connect):
                reader, writer = await original('127.0.0.1', gateway.sockets[0].getsockname()[1])
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                response = await asyncio.wait_for(reader.read(), timeout=5)
                self.assertEqual(response, payload[::-1])
                writer.close()
                await writer.wait_closed()
        finally:
            gateway.close()
            upstream.close()
            await gateway.wait_closed()
            await upstream.wait_closed()


if __name__ == '__main__':
    unittest.main()
