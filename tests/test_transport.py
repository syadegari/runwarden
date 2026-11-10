"""Exercise real framing and peer authentication where Unix sockets are permitted."""

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest

from rmgr.daemon import API, connection
from rmgr.models import Config, GIB, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store
from tests.test_scheduler import FakeBackend


class TransportTests(unittest.TestCase):
    def setUp(self):
        try:
            first, second = socket.socketpair()
            try:
                first.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                first.sendall(b"probe")
                second.recv(5)
            finally:
                first.close()
                second.close()
        except OSError as exc:
            self.skipTest(f"Unix sockets unavailable in this environment: {exc}")

    def test_json_submit_and_malformed_request(self):
        asyncio.run(self.exercise_transport())

    async def exercise_transport(self):
        with tempfile.TemporaryDirectory(prefix="rmgr-ipc-") as directory:
            home = Path(directory)
            prepare_home(home)
            store = Store(home)
            scheduler = Scheduler(store, Config([0], GIB), FakeBackend())
            api = API(scheduler, home, {})
            try:
                for payload, expected in ((b'{"op":"health"}\n', True), (b'not-json\n', False)):
                    server_socket, client_socket = socket.socketpair()
                    server_reader, server_writer = await asyncio.open_connection(sock=server_socket)
                    handler = asyncio.create_task(connection(server_reader, server_writer, api))
                    reader, writer = await asyncio.open_connection(sock=client_socket)
                    writer.write(payload)
                    await writer.drain()
                    reply = json.loads(await reader.readline())
                    self.assertEqual(reply["ok"], expected)
                    writer.close()
                    await writer.wait_closed()
                    await handler
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
