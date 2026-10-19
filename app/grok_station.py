#!/usr/bin/env python3
"""grok-station: tiny LAN HTTP listener + fullscreen KMS/DRM text display.

POST /show {"text": "..."} with "Authorization: Bearer $GROK_STATION_TOKEN".
"""
import hmac, ipaddress, json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")  # no SDL display; we write /dev/fb0 directly
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame

TOKEN = os.environ.get("GROK_STATION_TOKEN", "")
PORT = int(os.environ.get("GROK_STATION_PORT", "8787"))
SNAP = os.environ.get("GROK_STATION_SNAPSHOT", "/run/grok-station/last.png")
FB = os.environ.get("GROK_STATION_FB", "/dev/fb0")
MAX_CHARS = 500
SHOW = pygame.USEREVENT + 1


class Handler(BaseHTTPRequestHandler):
    server_version = "grok-station"

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"ok": True}) if self.path == "/health" else self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:")).is_private:
            return self._reply(403, {"error": "lan only"})
        if self.path != "/show":
            return self._reply(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(auth.encode(), f"Bearer {TOKEN}".encode()):
            return self._reply(401, {"error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 8192:
                return self._reply(413, {"error": "too large"})
            text = str(json.loads(self.rfile.read(n) or b"{}").get("text", "")).strip()[:MAX_CHARS]
        except Exception:
            return self._reply(400, {"error": "bad json"})
        if not text:
            return self._reply(400, {"error": "text required"})
        pygame.event.post(pygame.event.Event(SHOW, text=text))
        print(f"show from {self.client_address[0]}: {len(text)} chars", flush=True)
        self._reply(200, {"ok": True})

    def log_message(self, *a):
        pass


def wrap(font, text, width):
    lines = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if font.size(trial)[0] <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def render(screen, text, stamp):
    w, h = screen.get_size()
    screen.fill((0, 0, 0))
    if text is None:
        f = pygame.font.Font(None, h // 20)
        s = f.render("grok-station", True, (40, 40, 40))
        screen.blit(s, s.get_rect(center=(w // 2, h // 2)))
    else:
        size = h // 6
        while True:  # shrink until the wrapped text fits
            f = pygame.font.Font(None, size)
            lines = wrap(f, text, int(w * 0.9))
            if (len(lines) * f.get_linesize() <= h * 0.8 and max(f.size(l)[0] for l in lines) <= w * 0.9) or size <= 24:
                break
            size = int(size * 0.9)
        y = (h - len(lines) * f.get_linesize()) // 2
        for l in lines:
            s = f.render(l, True, (255, 255, 255))
            screen.blit(s, s.get_rect(midtop=(w // 2, y)))
            y += f.get_linesize()
        sf = pygame.font.Font(None, h // 30)
        s = sf.render(stamp, True, (90, 90, 90))
        screen.blit(s, s.get_rect(bottomright=(w - 20, h - 15)))
    with open(FB, "wb") as fb:  # 16bpp RGB565 surface matches the vc4 fbdev layout
        fb.write(screen.get_buffer().raw)
    try:
        pygame.image.save(screen, SNAP)
    except Exception:
        pass


def fb_surface():
    sysfs = "/sys/class/graphics/" + os.path.basename(FB)
    w, h = map(int, open(f"{sysfs}/virtual_size").read().split(","))
    bpp = int(open(f"{sysfs}/bits_per_pixel").read())
    stride = int(open(f"{sysfs}/stride").read())
    screen = pygame.Surface((w, h), depth=bpp)
    if screen.get_pitch() != stride:
        raise SystemExit(f"fb stride {stride} != surface pitch {screen.get_pitch()}")
    return screen


def main():
    pygame.display.init()  # dummy driver: needed for the event queue only
    pygame.font.init()
    screen = fb_surface()
    print(f"display {screen.get_size()} fb={FB}", flush=True)
    render(screen, None, "")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    while True:
        ev = pygame.event.wait(60000)  # idle wake once a minute; no busy loop
        if ev.type == SHOW:
            render(screen, ev.text, time.strftime("%a %b %-d  %-I:%M %p"))
            print("rendered", flush=True)
        elif ev.type == pygame.QUIT:
            break


if __name__ == "__main__":
    main()
