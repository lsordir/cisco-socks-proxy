"""Expose a selected worker's SOCKS port; unselected probes stay internal."""
import asyncio
import logging
import os
import signal
from pathlib import Path


class Connections:
    """Keep connection tasks alive until completion, including during GC."""
    def __init__(self, handler):
        self.handler = handler
        self.tasks = set()

    def __call__(self, reader, writer):
        task = asyncio.create_task(self.handler(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.done)

    def done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logging.getLogger(__name__).error('Proxy connection failed',
                    exc_info=(type(error), error, error.__traceback__))


async def relay(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def handle(reader, writer, targets=None):
    upstream = None
    pumps = []
    try:
        # Choose once per TCP connection. Never move an established stream or
        # replay SOCKS/application bytes onto another VPN after a failure.
        for host, port in targets if targets is not None else [('127.0.0.1', 1080)]:
            try:
                remote, upstream = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5)
                break
            except (OSError, asyncio.TimeoutError):
                continue
        if upstream is None:
            return
        pumps = [asyncio.create_task(relay(reader, upstream)),
                 asyncio.create_task(relay(remote, writer))]
        await asyncio.gather(*pumps)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        # gather() does not cancel the other relay when one fails. Always
        # cancel and reap outstanding readers before releasing their streams.
        for pump in pumps:
            if not pump.done():
                pump.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        for stream in (writer, upstream):
            if stream is not None:
                stream.close()
        for stream in (writer, upstream):
            if stream is not None:
                try:
                    await asyncio.wait_for(stream.wait_closed(), timeout=2)
                except (OSError, asyncio.TimeoutError):
                    pass


async def run_server(handler, port, ready_file=None):
    connections = Connections(handler)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    server = await asyncio.start_server(connections, '0.0.0.0', port)
    try:
        if ready_file:
            Path(ready_file).touch()
        await stopped.wait()
    finally:
        server.close()
        await server.wait_closed()
        pending = tuple(connections.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


async def main():
    await run_server(handle, 1082, os.environ['VPN_PUBLISH_FILE'])


if __name__ == '__main__':
    asyncio.run(main())
