"""Fake Homebridge config-ui-x. aiohttp on 127.0.0.1, ephemeral port. Records each request.

The access log is off so a token in the Authorization header is not written to stderr.
"""
import asyncio, base64, json, threading, time

from aiohttp import web


def jwt_token(exp, marker="TESTHBTOKEN"):
    """Unsigned JWT. The relay reads exp and does not check the signature."""
    def part(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return part({"alg": "none", "typ": "JWT"}) + "." + part({"exp": exp}) + "." + marker


def sample_accessories():
    """Controllable rows plus the kinds this release must not offer."""
    def row(typ, uid, name, char, value):
        return {"type": typ, "uniqueId": uid, "serviceName": name,
                "serviceCharacteristics": [{"type": char, "value": value, "perms": ["pr", "pw"]}]}
    return [
        row("Lightbulb", "light-1", "Porch Light", "On", False),
        row("Lightbulb", "bool-1", "Bool Light", "On", True),
        row("Lightbulb", "a+b", "Plus Light", "On", 0),
        row("Switch", "sw-1", "Hall Switch", "On", 1),
        row("Outlet", "out-1", "Coffee Maker", "On", 0),
        row("Fan", "fan-1", "Garage Fan", "Active", 0),
        row("Fanv2", "fan-2", "Desk Fan", "On", 0),
        row("Television", "tv-1", "Living Room TV", "Active", 0),
        row("Camera", "cam-1", "Driveway", "On", False),
        row("Doorbell", "bell-1", "Front Bell", "On", False),
        row("MotionSensor", "mot-1", "Hall Motion", "On", False),
        row("SecuritySystem", "sec-1", "Alarm", "On", False),
        row("LockMechanism", "lock-1", "Front Door", "On", False),
        row("GarageDoorOpener", "gar-1", "Garage", "On", False),
        row("Thermostat", "therm-1", "Hall Thermostat", "On", False),
        {"type": "Lightbulb", "serviceName": "No Id", "serviceCharacteristics": [{"type": "On", "value": 0}]},
    ]


class FakeHomebridge:
    def __init__(self):
        self.mode = "ok"  # ok, admin, users500, users401, otp, badlogin, redirect, plain, missing, slow, big
        self.users_status = 403
        self.acc_status = 200
        self.delay = 5
        self.otp_code = "123456"
        self.token_exp = None
        self.marker = "TESTHBTOKEN"
        self.login_ok_left = None  # None means every login can succeed
        self.put_401_left = 0
        self.wrap = False
        self.location = "http://127.0.0.1/secret"
        self.accessories = sample_accessories()
        self.requests = []
        self.logins = 0
        self.tokens = []
        self.runner = None
        self.site = None
        self.port = None
        self._loop = None
        self._thread = None

    def _token(self):
        exp = self.token_exp if self.token_exp is not None else int(time.time()) + 3600
        token = jwt_token(exp, self.marker)
        self.tokens.append(token)
        return token

    def _app(self):
        app = web.Application()
        app.router.add_get("/api/auth/settings", self._settings)
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_get("/api/accessories", self._accessories)
        app.router.add_put("/api/accessories/{uid}", self._put)
        app.router.add_get("/api/users", self._users)
        app.router.add_get("/secret", self._secret)
        return app

    def _remember(self, request, raw):
        body = None
        if raw:
            try:
                body = json.loads(raw.decode())
            except (UnicodeError, ValueError):
                body = raw.decode("utf-8", "replace")
        self.requests.append({
            "method": request.method,
            "path": request.path,
            "raw_path": request.raw_path,
            "body": body,
            "host": request.headers.get("Host", ""),
        })
        return body

    async def _settings(self, request):
        raw = await request.read()
        self._remember(request, raw)
        if self.mode == "redirect":
            return web.Response(status=302, headers={"Location": self.location})
        if self.mode == "slow":
            await asyncio.sleep(self.delay)
        if self.mode == "big":
            return web.Response(status=200, body=b'{"pad":"' + b"x" * (70 * 1024) + b'"}', content_type="application/json")
        if self.mode == "plain":
            return web.Response(status=200, text="not-json")
        if self.mode == "missing":
            return web.json_response({"error": "missing"}, status=404)
        return web.json_response({"form": "login"})

    async def _login(self, request):
        raw = await request.read()
        body = self._remember(request, raw)
        self.logins += 1
        if self.mode == "slow":
            await asyncio.sleep(self.delay)
        if self.mode == "badlogin" or self.login_ok_left == 0:
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.login_ok_left is not None:
            self.login_ok_left -= 1
        if self.mode == "otp" and (not isinstance(body, dict) or body.get("otp") != self.otp_code):
            return web.json_response({"otpRequired": True, "message": "OTP required"}, status=401)
        return web.json_response({"access_token": self._token()}, status=201)

    async def _accessories(self, request):
        raw = await request.read()
        self._remember(request, raw)
        if self.acc_status != 200:
            return web.json_response({"error": "no"}, status=self.acc_status)
        if self.wrap:
            return web.json_response({"accessories": self.accessories})
        return web.json_response(self.accessories)

    async def _put(self, request):
        raw = await request.read()
        self._remember(request, raw)
        if self.put_401_left > 0:
            self.put_401_left -= 1
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"ok": True})

    async def _users(self, request):
        raw = await request.read()
        self._remember(request, raw)
        code = 200 if self.mode == "admin" else 500 if self.mode == "users500" else 401 if self.mode == "users401" else self.users_status
        if code == 200:
            return web.json_response([{"admin": True}], status=200)
        return web.json_response({"error": "no"}, status=code)

    async def _secret(self, request):
        raw = await request.read()
        self._remember(request, raw)
        return web.json_response({"secret": True})

    def reset(self):
        self.mode = "ok"
        self.users_status = 403
        self.acc_status = 200
        self.delay = 5
        self.token_exp = None
        self.login_ok_left = None
        self.put_401_left = 0
        self.wrap = False
        self.accessories = sample_accessories()
        self.requests = []
        self.logins = 0
        self.tokens = []

    async def start(self):
        self.runner = web.AppRunner(self._app(), access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site.port
        return self.port

    async def close(self):
        runner, self.runner = self.runner, None
        if runner is not None:
            await runner.cleanup()

    def start_thread(self):
        ready = threading.Event()
        error = []

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self.start())
            except Exception as exc:
                error.append(exc)
                ready.set()
                return
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("fake homebridge did not start")
        if error:
            raise error[0]
        return self.port

    def close_thread(self):
        loop = self._loop
        if loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self.close(), loop)
        fut.result(5)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self._loop = None

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port
