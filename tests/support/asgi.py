"""Exercise the real ASGI app without opening network sockets.

Windows asyncio normally creates a connected pair of local TCP sockets even for
an in-process request. This test loop uses a threading.Event for wakeups. The
ASGI transport needs timers and worker-thread notifications, but never socket I/O.
"""
import asyncio
import os
import selectors
import threading

import httpx


class _EventSelector(selectors.SelectSelector):
    def __init__(self):
        super().__init__()
        self.ready = threading.Event()

    def select(self, timeout=None):
        if self.get_map():
            raise RuntimeError("The ASGI test transport must not perform socket I/O")
        self.ready.wait(timeout)
        self.ready.clear()
        return []


class _MemoryLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(_EventSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        self._selector.ready.set()


class LocalASGI:
    def __init__(self, app, *, host="127.0.0.1", base_url="http://localhost"):
        self.app, self.host, self.base_url = app, host, base_url

    def request(self, method, path, **kwargs):
        async def deliver():
            transport = httpx.ASGITransport(app=self.app, client=(self.host, 49152))
            async with httpx.AsyncClient(transport=transport,base_url=self.base_url) as client:
                return await client.request(method,path,**kwargs)
        with asyncio.Runner(loop_factory=_MemoryLoop if os.name == "nt" else None) as runner:
            return runner.run(deliver())

    def get(self, path, **kwargs):
        return self.request("GET",path,**kwargs)

    def post(self, path, **kwargs):
        return self.request("POST",path,**kwargs)
