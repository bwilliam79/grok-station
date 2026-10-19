#!/usr/bin/env python3
"""grok-station voice relay: holds the xAI key so the Pi never does.

The Pi opens ws://<relay>:8790/ws with "Authorization: Bearer $RELAY_TOKEN" and streams mic audio
as binary PCM16 24 kHz mono frames. The relay opens one xAI realtime session per connection
(server VAD) and sends back binary frames of reply audio (PCM16 24 kHz mono) plus small JSON events:
  {"type":"ready"} {"type":"speech_started"} {"type":"speech_stopped"} {"type":"heard","text":...}
  {"type":"reply_delta","text":...} {"type":"reply","text":...} {"type":"done"}
  {"type":"timeout"} {"type":"error","message":...}
One question and answer per connection: the session closes after response.done, after NO_SPEECH
seconds without speech, or at MAX_SESSION seconds, whichever comes first. GET /health for checks.
"""
import asyncio, base64, hmac, json, os, time
from aiohttp import web, ClientSession, WSMsgType

E = os.environ.get
KEY, TOKEN = E("XAI_API_KEY", ""), E("RELAY_TOKEN", "")
MODEL, VOICE = E("XAI_MODEL", "grok-voice-3.0"), E("XAI_VOICE", "eve")
PORT = int(E("RELAY_PORT", "8790"))
NO_SPEECH, MAX_SESSION = float(E("NO_SPEECH", "10")), float(E("MAX_SESSION", "60"))
INSTRUCTIONS = E("INSTRUCTIONS", (
    "You are Grok, a friendly voice assistant on a small display in a family home. "
    "Answer in one to three short spoken sentences, plainly and warmly. No lists, markdown, or emoji."))
URL = f"wss://api.x.ai/v1/realtime?model={MODEL}"
stats = {"sessions": 0, "seconds": 0.0, "busy": 0}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def health(_):
    return web.json_response({"ok": True, "key": bool(KEY), "model": MODEL, **{k: round(v, 1) for k, v in stats.items()}})


async def session(req):
    if not TOKEN or not hmac.compare_digest(req.headers.get("Authorization", "").encode(), f"Bearer {TOKEN}".encode()):
        return web.json_response({"error": "unauthorized"}, status=401)
    if not KEY:
        return web.json_response({"error": "no xai key configured"}, status=503)
    if stats["busy"]:
        return web.json_response({"error": "busy"}, status=409)  # one device, one session at a time
    pi = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await pi.prepare(req)
    stats["busy"] += 1
    t0, n_up, n_down, why = time.monotonic(), 0, 0, "?"
    try:
        async with ClientSession() as http, http.ws_connect(URL, headers={"Authorization": f"Bearer {KEY}"},
                                                           heartbeat=20, max_msg_size=1 << 24) as up:
            await up.send_json({"type": "session.update", "session": {
                "voice": VOICE, "instructions": INSTRUCTIONS, "turn_detection": {"type": "server_vad"},
                "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}},
                          "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}})
            heard = asyncio.Event()

            async def pi_to_xai():
                nonlocal n_up
                async for m in pi:
                    if m.type == WSMsgType.BINARY and m.data:
                        n_up += len(m.data)
                        await up.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(m.data).decode()})
                return "pi closed"

            async def xai_to_pi():
                nonlocal n_down
                reply = []
                async for m in up:
                    if m.type != WSMsgType.TEXT:
                        continue
                    ev = json.loads(m.data)
                    t = ev.get("type", "")
                    if t in ("session.created", "session.updated", "response.done", "error"):
                        log("xai", t)  # event type only; no content, no key
                    if t == "session.updated":
                        await pi.send_json({"type": "ready"})
                    elif t in ("response.output_audio.delta", "response.audio.delta"):
                        b = base64.b64decode(ev.get("delta", ""))
                        n_down += len(b)
                        await pi.send_bytes(b)
                    elif t == "input_audio_buffer.speech_started":
                        heard.set()
                        await pi.send_json({"type": "speech_started"})
                    elif t == "input_audio_buffer.speech_stopped":
                        await pi.send_json({"type": "speech_stopped"})
                    elif t == "conversation.item.input_audio_transcription.completed":
                        await pi.send_json({"type": "heard", "text": ev.get("transcript", "")})
                    elif t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                        reply.append(ev.get("delta", ""))
                        await pi.send_json({"type": "reply_delta", "text": ev.get("delta", "")})
                    elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                        await pi.send_json({"type": "reply", "text": ev.get("transcript") or "".join(reply)})
                    elif t == "response.done":
                        await pi.send_json({"type": "done"})
                        return "response done"
                    elif t == "error":
                        log("xai error", (ev.get("error") or {}).get("code") or (ev.get("error") or {}).get("type"))
                        await pi.send_json({"type": "error", "message": str((ev.get("error") or {}).get("message", ""))[:200]})
                return "xai closed"

            async def no_speech():
                try:
                    await asyncio.wait_for(heard.wait(), NO_SPEECH)
                    await asyncio.sleep(1e9)
                except asyncio.TimeoutError:
                    await pi.send_json({"type": "timeout"})
                    return "no speech"

            tasks = [asyncio.create_task(c()) for c in (pi_to_xai, xai_to_pi, no_speech)]
            done, pending = await asyncio.wait(tasks, timeout=MAX_SESSION, return_when=asyncio.FIRST_COMPLETED)
            why = next(iter(done)).result() if done else "max session"
            if not done:
                await pi.send_json({"type": "timeout"})
            for t in pending:
                t.cancel()
    except Exception as e:
        why = f"error {type(e).__name__}"
        if not pi.closed:
            await pi.send_json({"type": "error", "message": "relay could not reach xAI"})
    finally:
        stats["busy"] -= 1
        dt = time.monotonic() - t0
        stats["sessions"] += 1
        stats["seconds"] += dt
        log(f"session end: {why}; {dt:.1f}s; up {n_up / 48000:.1f}s audio; down {n_down / 48000:.1f}s audio; "
            f"total {stats['sessions']} sessions {stats['seconds'] / 60:.2f} min")
        await pi.close()
    return pi


if __name__ == "__main__":
    app = web.Application()
    app.add_routes([web.get("/health", health), web.get("/ws", session)])
    log(f"relay on :{PORT} model={MODEL} voice={VOICE} key={'set' if KEY else 'MISSING'}")
    web.run_app(app, port=PORT, print=None, access_log=None)
