#!/usr/bin/env python3
"""grok-station voice relay: holds the xAI key so the Pi never does.

The Pi opens ws://<relay>:8790/ws with "Authorization: Bearer $RELAY_TOKEN" and streams mic audio
as binary PCM16 24 kHz mono frames. The relay opens one xAI realtime session per connection
(server VAD) and sends back binary frames of reply audio (PCM16 24 kHz mono) plus small JSON events:
  {"type":"ready"} {"type":"speech_started"} {"type":"speech_stopped"} {"type":"heard","text":...}
  {"type":"response_started"} {"type":"reply_delta","text":...} {"type":"reply","text":...} {"type":"done"}
  {"type":"timeout","speech":bool} {"type":"xai_error","message":...} (non-fatal) {"type":"error","message":...}
One question and answer per connection: the session closes after response.done, after NO_SPEECH seconds
with no speech in progress and no response (measured from session start or the last speech_stopped, so a
short noise that gets no reply doesn't end the turn), or at MAX_SESSION seconds. GET /health for checks.
"""
import asyncio, base64, datetime, hmac, json, os, time
from zoneinfo import ZoneInfo
from aiohttp import web, ClientSession, WSMsgType

E = os.environ.get
KEY, TOKEN = E("XAI_API_KEY", ""), E("RELAY_TOKEN", "")
MODEL, VOICE = E("XAI_MODEL", "grok-voice-3.0"), E("XAI_VOICE", "eve")
PORT = int(E("RELAY_PORT", "8790"))
NO_SPEECH, MAX_SESSION = float(E("NO_SPEECH", "10")), float(E("MAX_SESSION", "60"))
INSTRUCTIONS = E("INSTRUCTIONS", (
    "You are Grok, a friendly voice assistant on a small display in a family home. "
    "Answer in one to three short spoken sentences, plainly and warmly. No lists, markdown, or emoji."))
TZ = E("TZ_NAME", "America/Chicago")                  # for the "current date and time" line in the instructions
KEYTERMS = [t.strip()[:50] for t in E("KEYTERMS", "Grok").split(",") if t.strip()][:100]  # transcription bias
URL = f"wss://api.x.ai/v1/realtime?model={MODEL}"
QUIET = {"response.output_audio.delta", "response.audio.delta", "response.output_audio_transcript.delta",
         "response.audio_transcript.delta", "conversation.item.input_audio_transcription.updated"}
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
            now = datetime.datetime.now(ZoneInfo(TZ))
            instructions = f"{INSTRUCTIONS} The current local date and time is {now:%A, %B %-d, %Y, %-I:%M %p} ({TZ})."
            audio_in = {"format": {"type": "audio/pcm", "rate": 24000}}
            if KEYTERMS:
                audio_in["transcription"] = {"keyterms": KEYTERMS}
            await up.send_json({"type": "session.update", "session": {
                "voice": VOICE, "instructions": instructions, "turn_detection": {"type": "server_vad"},
                "audio": {"input": audio_in, "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}})
            st = {"speaking": False, "responding": False, "speech": False, "quiet_since": time.monotonic()}

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
                    if t not in QUIET:
                        log("xai", t)  # event type only; no content, no key
                    if t == "session.updated":
                        await pi.send_json({"type": "ready"})
                    elif t in ("response.output_audio.delta", "response.audio.delta"):
                        b = base64.b64decode(ev.get("delta", ""))
                        n_down += len(b)
                        await pi.send_bytes(b)
                    elif t == "input_audio_buffer.speech_started":
                        st.update(speaking=True, speech=True)
                        await pi.send_json({"type": "speech_started"})
                    elif t == "input_audio_buffer.speech_stopped":
                        st.update(speaking=False, quiet_since=time.monotonic())
                        await pi.send_json({"type": "speech_stopped"})
                    elif t == "response.created":
                        st["responding"] = True
                        await pi.send_json({"type": "response_started"})
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
                        await pi.send_json({"type": "xai_error", "message": str((ev.get("error") or {}).get("message", ""))[:200]})
                return "xai closed"

            async def no_speech():
                while st["speaking"] or st["responding"] or time.monotonic() - st["quiet_since"] < NO_SPEECH:
                    await asyncio.sleep(0.25)
                await pi.send_json({"type": "timeout", "speech": st["speech"]})
                return "no reply to noise" if st["speech"] else "no speech"

            tasks = [asyncio.create_task(c()) for c in (pi_to_xai, xai_to_pi, no_speech)]
            done, pending = await asyncio.wait(tasks, timeout=MAX_SESSION, return_when=asyncio.FIRST_COMPLETED)
            why = next(iter(done)).result() if done else "max session"
            if not done:
                await pi.send_json({"type": "timeout", "speech": st["speech"]})
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
    log(f"relay on :{PORT} model={MODEL} voice={VOICE} key={'set' if KEY else 'MISSING'} keyterms={len(KEYTERMS)} tz={TZ}")
    web.run_app(app, port=PORT, print=None, access_log=None)
