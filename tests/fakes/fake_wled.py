"""Fake WLED JSON API. aiohttp on 127.0.0.1, ephemeral port. Records each request."""
import asyncio, json

from aiohttp import web


class FakeWLED:
    def __init__(self, state=None, mode="ok"):
        self.state = {"on": True, "bri": 128}
        if state:
            self.state.update(state)
        self.mode = mode  # ok, redirect, slow, big, exact
        self.location = ""
        self.info = {"name": "WLED", "ver": "16.0.0"}
        self.presets = {"3": {"n": "party"}}
        self.requests = []
        self.runner = None
        self.site = None
        self.port = None

    def _app(self):
        app = web.Application()
        app.router.add_get("/json/info", self._info)
        app.router.add_get("/json/state", self._state_get)
        app.router.add_post("/json/state", self._state_post)
        app.router.add_get("/presets.json", self._presets)
        return app

    def _remember(self, request, raw):
        body = None
        if raw:
            try:
                body = json.loads(raw.decode())
            except (UnicodeError, ValueError):
                body = raw.decode("utf-8", "replace")
        self.requests.append({"method": request.method, "path": request.path, "body": body})
        return body

    async def _reply(self, request, mutate):
        raw = await request.read()
        body = self._remember(request, raw)
        if self.mode == "redirect":
            return web.Response(status=302, headers={"Location": self.location})
        if self.mode == "slow":
            await asyncio.sleep(5)
        if self.mode == "big":
            return web.Response(status=200, body=b"x" * (70 * 1024), content_type="application/json")
        if self.mode == "exact":
            return web.Response(status=200, body=b"x" * (64 * 1024), content_type="application/json")
        if mutate and isinstance(body, dict):
            for key in ("on", "bri", "ps", "seg"):
                if key in body:
                    self.state[key] = body[key]
        return web.json_response(self.state)

    async def _info(self, request):
        raw = await request.read()
        self._remember(request, raw)
        return web.json_response(self.info)

    async def _presets(self, request):
        raw = await request.read()
        self._remember(request, raw)
        return web.json_response(self.presets)

    async def _state_get(self, request):
        return await self._reply(request, False)

    async def _state_post(self, request):
        return await self._reply(request, True)

    async def start(self):
        self.runner = web.AppRunner(self._app())
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site.port
        return self.port

    async def close(self):
        runner, self.runner = self.runner, None
        if runner is not None:
            await runner.cleanup()

    @property
    def host(self):
        return "127.0.0.1:%d" % self.port
