"""In-process ASGI socket driver: real router/worker lifecycle, no network."""
import asyncio
import json


class AsgiSocket:
    def __init__(self, app, path="/ws/assistant", *, timeout=5, send_hook=None):
        self.app, self.path = app, path
        self.timeout = timeout
        self.send_hook = send_hook
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.task = None

    async def connect(self):
        scope = dict(type="websocket", asgi={"version": "3.0"}, scheme="ws", http_version="1.1",
                     path=self.path, raw_path=self.path.encode(), root_path="", query_string=b"",
                     headers=[], server=("127.0.0.1", 8000), client=("127.0.0.1", 1234), subprotocols=[])
        await self.incoming.put({"type": "websocket.connect"})
        async def send(message):
            await self.outgoing.put(message)
            if self.send_hook is not None:
                await self.send_hook(message)
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, send))
        assert (await self.outgoing.get())["type"] == "websocket.accept"
        return self

    async def send_json(self, payload):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(payload)})

    async def send_bytes(self, data):
        await self.incoming.put({"type": "websocket.receive", "bytes": data})

    async def event(self):
        message = await asyncio.wait_for(self.outgoing.get(), self.timeout)
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    async def closed(self, code=1000):
        message = await self.outgoing.get()
        assert message["type"] == "websocket.close" and message["code"] == code
        await self.task

    async def disconnect(self):
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})

    async def question(self, *, pcm=b"\0\0" * 100, top_k=5):
        from backend.tests.test_server_transport import start, stop
        await self.connect()
        await self.send_json(start(top_k))
        events = [await self.event()]
        from kulai_memory.api.pcm_wav import MAX_BINARY_FRAME_BYTES
        for offset in range(0, len(pcm), MAX_BINARY_FRAME_BYTES):
            await self.send_bytes(pcm[offset:offset + MAX_BINARY_FRAME_BYTES])
        await self.send_json(stop())
        while True:
            message = await self.outgoing.get()
            if message["type"] == "websocket.close":
                assert message["code"] == 1000
                await self.task
                return events
            events.append(json.loads(message["text"]))
