"""Gateway routing uses only selected, healthy workers and pins TCP streams."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'))
from common import write_json
from forward import handle
from gateway import targets


class GatewayRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.selection = {'active': 'a', 'retiring': 'b', 'retire_at': 1300}
        write_json(self.root / 'selection.json', self.selection)
        for name in ('a', 'b'):
            write_json(self.root / f'{name}.command.json', {'revision': name, 'serve': True})
            write_json(self.root / f'{name}.status.json', {
                'revision': name, 'ready': True, 'published': True, 'updated': 1000,
            })

    def test_active_preferred_and_retiring_is_fallback_during_overlap(self):
        self.assertEqual(targets(self.root, 1001), [('vpn-socks', 1082), ('vpn-socks-b', 1082)])

    def test_retiring_is_not_used_after_deadline(self):
        self.selection['retire_at'] = 1001
        write_json(self.root / 'selection.json', self.selection)
        self.assertEqual(targets(self.root, 1001), [('vpn-socks', 1082)])

    def test_stale_active_falls_back_to_healthy_retiring(self):
        write_json(self.root / 'a.status.json', {
            'revision': 'a', 'ready': True, 'published': True, 'updated': 980,
        })
        self.assertEqual(targets(self.root, 1001), [('vpn-socks-b', 1082)])

    def test_probe_is_never_used_as_fallback(self):
        self.selection.update(active=None, retiring=None)
        write_json(self.root / 'selection.json', self.selection)
        self.assertEqual(targets(self.root, 1001), [])

    def test_old_revision_and_unpublished_workers_are_rejected(self):
        write_json(self.root / 'a.command.json', {'revision': 'next', 'serve': True})
        write_json(self.root / 'b.command.json', {'revision': 'b', 'serve': False})
        self.assertEqual(targets(self.root, 1001), [])


class GatewayStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_healthy_target_closes_without_using_host_internet(self):
        original = asyncio.open_connection
        async def route(reader, writer):
            await handle(reader, writer, [])
        server = await asyncio.start_server(route, '127.0.0.1', 0)
        try:
            with patch('forward.asyncio.open_connection') as connect:
                reader, writer = await original('127.0.0.1', server.sockets[0].getsockname()[1])
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
                connect.assert_not_called()
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_failed_upstream_can_fall_back_before_forwarding_bytes(self):
        original = asyncio.open_connection
        async def backend(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
        upstream = await asyncio.start_server(backend, '127.0.0.1', 0)
        async def connect(host, port):
            if host == 'unreachable':
                raise ConnectionRefusedError()
            return await original(host, port)
        async def route(reader, writer):
            await handle(reader, writer, [('unreachable', 1),
                                         ('127.0.0.1', upstream.sockets[0].getsockname()[1])])
        server = await asyncio.start_server(route, '127.0.0.1', 0)
        try:
            with patch('forward.asyncio.open_connection', side_effect=connect):
                reader, writer = await original('127.0.0.1', server.sockets[0].getsockname()[1])
                writer.write(b'only-once\n')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b'only-once\n')
                writer.close()
                await writer.wait_closed()
                await asyncio.sleep(0.01)
        finally:
            for item in (server, upstream):
                item.close()
                await item.wait_closed()

    async def test_new_stream_moves_to_winner_but_old_stream_stays_on_old_vpn(self):
        async def backend(label, reader, writer):
            try:
                while line := await reader.readline():
                    writer.write(label + line)
                    await writer.drain()
            finally:
                writer.close()

        a = await asyncio.start_server(lambda r, w: backend(b'A:', r, w), '127.0.0.1', 0)
        b = await asyncio.start_server(lambda r, w: backend(b'B:', r, w), '127.0.0.1', 0)
        current = [('127.0.0.1', a.sockets[0].getsockname()[1])]

        async def route(reader, writer):
            await handle(reader, writer, list(current))

        gateway = await asyncio.start_server(route, '127.0.0.1', 0)
        clients = []
        try:
            async def client():
                reader, writer = await asyncio.open_connection('127.0.0.1', gateway.sockets[0].getsockname()[1])
                clients.append(writer)
                return reader, writer

            first, old = await client()
            old.write(b'first\n')
            await old.drain()
            self.assertEqual(await asyncio.wait_for(first.readline(), 2), b'A:first\n')
            current[:] = [('127.0.0.1', b.sockets[0].getsockname()[1])]
            second, new = await client()
            new.write(b'new\n')
            await new.drain()
            self.assertEqual(await asyncio.wait_for(second.readline(), 2), b'B:new\n')
            old.write(b'existing\n')
            await old.drain()
            self.assertEqual(await asyncio.wait_for(first.readline(), 2), b'A:existing\n')
        finally:
            for writer in clients:
                writer.close()
                await writer.wait_closed()
            for server in (gateway, a, b):
                server.close()
                await server.wait_closed()
            await asyncio.sleep(0.01)


if __name__ == '__main__':
    unittest.main()
