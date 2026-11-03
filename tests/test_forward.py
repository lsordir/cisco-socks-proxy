"""Real local TCP traffic, including half-close, through the publication gate."""
import asyncio
import gc
import subprocess
import tempfile
import time
from pathlib import Path
import sys
import unittest
import weakref
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'))
from forward import Connections, handle


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_connections_survive_garbage_collection(self):
        futures = []
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))

        async def waiting_connection(reader, writer):
            try:
                pending = loop.create_future()
                futures.append(weakref.ref(pending))
                await pending
                writer.write(b'still-connected\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        connections = Connections(waiting_connection)
        server = await asyncio.start_server(connections, '127.0.0.1', 0)
        clients = []
        try:
            for _ in range(64):
                clients.append(await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1]))
            end = loop.time() + 5
            while len(futures) < 64 and loop.time() < end:
                await asyncio.sleep(0.01)
            self.assertEqual(len(futures), 64)
            for _ in range(3):
                gc.collect()
                await asyncio.sleep(0)
            self.assertTrue(all(ref() is not None for ref in futures))
            for ref in futures:
                ref().set_result(None)
            received = await asyncio.wait_for(asyncio.gather(
                *(reader.readline() for reader, _ in clients)), 5)
            self.assertEqual(received, [b'still-connected\n'] * 64)
            self.assertFalse(errors)
        finally:
            for task in tuple(connections.tasks):
                task.cancel()
            await asyncio.gather(*connections.tasks, return_exceptions=True)
            for _, writer in clients:
                writer.close()
                await writer.wait_closed()
            server.close()
            await server.wait_closed()
            loop.set_exception_handler(previous)

    async def test_failed_relay_cancels_the_other_pending_reader(self):
        client_reader = object()
        remote_reader = object()
        stopped = asyncio.Event()
        from unittest.mock import AsyncMock
        client_writer, upstream_writer = AsyncMock(), AsyncMock()
        client_writer.close = unittest.mock.Mock()
        upstream_writer.close = unittest.mock.Mock()

        async def pump(reader, writer):
            if reader is client_reader:
                await asyncio.sleep(0)
                raise ConnectionResetError()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        with patch('forward.asyncio.open_connection', return_value=(remote_reader, upstream_writer)), \
                patch('forward.relay', side_effect=pump):
            await asyncio.wait_for(handle(client_reader, client_writer), 2)
        self.assertTrue(stopped.is_set())
        client_writer.close.assert_called_once()
        upstream_writer.close.assert_called_once()

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


class ForwardShutdownTests(unittest.TestCase):
    def test_sigterm_exits_cleanly_instead_of_leaving_pending_tasks(self):
        control = Path(__file__).resolve().parents[1] / 'docker/image/vpn-socks/control'
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / 'ready'
            code = ('import sys, asyncio; sys.path.insert(0, sys.argv[1]); '
                    'from forward import handle, run_server; '
                    'asyncio.run(run_server(handle, 0, sys.argv[2]))')
            proc = subprocess.Popen([sys.executable, '-c', code, str(control), str(ready)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                end = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                proc.terminate()
                _, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertNotIn('Task was destroyed', stderr)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()


if __name__ == '__main__':
    unittest.main()
