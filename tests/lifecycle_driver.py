#!/usr/bin/env python3
"""Lifecycle scenario driver. Run by tests/test_lifecycle_scenarios.py in a fresh process:

    python3 lifecycle_driver.py <installed root> <work dir> <scenario>

It imports the admin page, the relay and the wake listener from the INSTALLED tree (<root>/opt/grok-station, made
by tools/install-files.sh in tests/lifecycle_sim.py), never from the repo. The relay's admin API runs for real on a
loopback port and the admin page reaches it with its own relay_call. The only fakes are the bots' webhooks (a
recorder in place of the relay's aiohttp ClientSession, so nothing leaves this machine), the clock where a
scenario needs one, and the signed reply transport: a bot's update status is recorded with relay.note_update, the
function the verified reply path calls. Prints one JSON object.
"""
import asyncio, base64, hashlib, json, os, re, socket, sys, threading, time

ROOT, WORK, SCENARIO = sys.argv[1], sys.argv[2], sys.argv[3]
OPT = os.path.join(ROOT, "opt", "grok-station")
sys.path[:] = [p for p in sys.path if p and not os.path.abspath(p).startswith(os.path.dirname(os.path.abspath(__file__)))]
for d in ("wake", "relay", "admin"):
    sys.path.insert(0, os.path.join(OPT, d))
sys.dont_write_bytecode = True
STATION = "https://grok-station.local"
FP = ":".join(["AB"] * 32)
KEY = base64.b64encode(b"\x07" * 32).decode()


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _port()
J = lambda *p: os.path.join(WORK, *p)
for d in ("state", "secrets", "tls", "admin-state", "admin-run", "enroll", "tmp"):
    os.makedirs(J(d), exist_ok=True)
with open(J("secrets", "admin_token"), "w") as fh:
    fh.write("lifecycle-admin-" + "x" * 16 + "\n")
os.chmod(J("secrets", "admin_token"), 0o600)
with open(J("tls", "fingerprint"), "w") as fh:
    fh.write(FP + "\n")
with open(J("tls", "host"), "w") as fh:
    fh.write("grok-station\n")
os.environ.update({
    "GROK_STATE_DIR": J("state"), "GROK_SECRETS_DIR": J("secrets"), "RELAY_TLS_DIR": J("tls"),
    "RELAY_TOKEN": "lifecycle-relay-token", "XAI_API_KEY": "lifecycle-key", "ADMIN_STATION_ADDRESS": STATION,
    "ADMIN_STATE_DIR": J("admin-state"), "ADMIN_RUN_DIR": J("admin-run"), "ADMIN_TLS_DIR": J("tls"),
    "ADMIN_VERSION_FILE": os.path.join(OPT, "VERSION"), "ADMIN_HELPER": "/bin/false",
    "ADMIN_LOCAL_RELAY_TOKEN_FILE": J("secrets", "admin_token"), "RELAY_PORT": str(PORT),
    "ADMIN_RELAY_ENV": J("missing-relay.env"), "WAKE_ENROLL_DIR": J("enroll"), "ADMIN_PERSONAS": J("p.json"),
    "WAKE_CONFIG": J("wakewords.json"), "TMPDIR": J("tmp"),
})

import grok_admin as ga  # noqa: E402  (installed tree)
import relay as r  # noqa: E402
import botreply  # noqa: E402
import gs_routine  # noqa: E402
import bot_content  # noqa: E402

LOADED = {m: getattr(sys.modules.get(m), "__file__", "") for m in ("grok_admin", "relay", "botreply", "gs_routine",
                                                                   "gs_render", "bot_content", "handoff", "safe_io")}
with open(os.path.join(OPT, "VERSION")) as _fh:
    REL = _fh.read().strip()

# ---- bots: personas, main bot, webhooks, pinned keys ----
BOTS = ("juno", "otto", "kai", "pip")
r.VOICES.clear()
r.VOICES.update({"ara"})
os.makedirs(os.path.dirname(r.PERSONAS_FILE), exist_ok=True)
with open(r.PERSONAS_FILE, "w") as fh:
    json.dump({"personas": [{"slug": s, "name": s.title(), "voice": "ara", "prompt": "Helper.", "aliases": [],
                             "facts": []} for s in BOTS]}, fh)
r._personas["mtime"] = None
os.makedirs(os.path.dirname(r.overlay_path()), exist_ok=True)
with open(r.overlay_path(), "w") as fh:
    json.dump({"list_source": "juno"}, fh)
assert r.write_webhooks({s: {"url": "https://bots.invalid/%s" % s, "token": "tok-%s-%s" % (s, "z" * 12), "saved_at": 10}
                         for s in BOTS})
for s in BOTS:
    assert botreply.pin_key(r.bot_keys_path(), s, KEY)

# ---- fake bot webhooks ----
POSTS, OFFLINE = [], set()


class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, **k):
        slug = url.rsplit("/", 1)[-1]
        if slug in OFFLINE:
            raise OSError("host offline")
        POSTS.append({"bot": slug, "json": json})
        return _Resp(200)


r.ClientSession = _Session


# ---- the relay's admin API on loopback ----
def _serve(ready):
    from aiohttp import web
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.add_routes(r.ADMIN_ROUTES)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    ready.set()
    loop.run_forever()


_ready = threading.Event()
threading.Thread(target=_serve, args=(_ready,), daemon=True).start()
assert _ready.wait(20)


# ---- the admin page, called the way its HTTP handler calls itself ----
class H(ga.Handler):
    def __init__(self):
        self.out = []

    def send(self, code, body):
        self.out.append((code, body))
        return code


def admin(method, *args):
    h = H()
    code = getattr(ga.Handler, method)(h, *args)
    return code, (h.out[-1][1] if h.out else None)


def page():
    """/api/finish-update: the Bots rows, the Bots-menu dot (behind) and the main-bot callout read this."""
    code, body = admin("api_get_finish_update", "/api/finish-update", {}, {})
    rows = {b["slug"]: b for g in body["computers"] for b in g["bots"]}
    return {"code": code, "behind": body["behind"], "release": body["release"],
            "states": {s: rows[s]["state"] for s in rows}, "fail": {s: rows[s]["fail"] for s in rows},
            "wait_left": {s: rows[s]["wait_left"] for s in rows}, "instr": {s: rows[s]["instr"] for s in rows},
            "have_release": {s: rows[s]["have_release"] for s in rows}}


def table_entries():
    return bot_content.read_table()


def prev_same():
    """The newest older release whose cloud content equals this release's (what same_content accepts)."""
    tab = table_entries()
    cur = [e for e in tab if e["release"] == REL]
    if not cur:
        raise SystemExit("admin/bot-releases.txt has no line for %s (run the manifest step first)" % REL)
    want = cur[-1]["fp"]["cloud"]
    olds = [e for e in tab if e["release"] != REL and e["fp"]["cloud"] == want
            and ga.version_order(e["release"], REL) == "newer"]
    return olds[-1] if olds else None


def old_instr(slug, entry):
    """The hash a cloud bot on entry["release"] signs: the template with that header and that release's update pin."""
    rest = gs_routine.TEMPLATES["cloud"].split("\n", 1)[1]
    pub = "".join(r.botreply.station_pub_b64(r.botreply.load_or_create_station(r.SECRETS_DIR)).split())
    text = gs_routine.render("Grok Station instructions, release %s.\n" % entry["release"] + rest, "cloud", slug,
                             entry["update_commit"], entry["update_sha"], STATION, FP, pub)
    return gs_routine.instr_hash(text)


def station_pub():
    return "".join(r.botreply.station_pub_b64(r.botreply.load_or_create_station(r.SECRETS_DIR)).split())


def current_text(slug):
    b = {"slug": slug, "key_mode": "cloud"}
    return ga.render_bot_routine(b, STATION, FP, station_pub())


def status(slug, release, instr):
    line = "%s instr=%s helper=- computer=%s" % (release, instr, hashlib.sha256(slug.encode()).hexdigest()[:16])
    assert r.note_update(slug, line), line
    return line


def setup(slug):
    n = len(POSTS)
    code, body = admin("bots_setup_send", {"slug": slug})
    return code, body, [p for p in POSTS[n:] if p["json"].get("kind") == "setup_relay"]


def literals():
    with open(os.path.join(OPT, "admin", "setup-params.txt")) as fh:
        text = fh.read()
    lit = dict(re.findall(r"^(ROUTINE_COMMIT|HASH_ROUTINE_HOME|HASH_ROUTINE_CLOUD|UPDATE_COMMIT|HASH_UPDATE|RELEASE) (\S+)$",
                          text, re.M))
    m = re.search(r"/grok-station/([0-9a-f]{40})/scripts/install-home\.sh' && printf '%s  %s\\n' '([0-9a-f]{64})'", text)
    lit["INSTALL_COMMIT"], lit["HASH_INSTALL"] = m.groups()
    return lit


out = {"scenario": SCENARIO, "release": REL, "loaded": LOADED}

if SCENARIO == "main_behind_same_content":
    prev = prev_same()
    out["prev"] = prev and prev["release"]
    status("juno", prev["release"], old_instr("juno", prev))
    status("kai", prev["release"], old_instr("kai", prev))
    out["same_content_rule"] = ga.same_content({"slug": "juno", "key_mode": "cloud"}, prev["release"],
                                               old_instr("juno", prev), STATION, FP, station_pub())
    out["page"] = page()
    code, body, posts = setup("otto")
    out["gate"] = {"code": code, "error": (body or {}).get("error"), "setup_relay_posts": len(posts)}
    got = ga.relay_call("POST", "/admin/bots/setup-relay", {"actor": ga.RELAY_ACTOR, "slug": "juno", "target": "otto",
                                                            "mode": "cloud", "path": "first"})
    out["relay_direct"] = {"code": got[0], "error": got[1].get("error"), "posts": len([p for p in POSTS if p["json"].get("kind") == "setup_relay"])}

elif SCENARIO == "main_current_setup_proceeds":
    text = current_text("juno")
    status("juno", REL, gs_routine.instr_hash(text))
    out["page"] = page()
    code, body, posts = setup("otto")
    out["gate"] = {"code": code, "error": (body or {}).get("error"), "setup_relay_posts": len(posts)}
    out["payload"] = posts[0]["json"] if posts else None
    out["literals"] = literals()
    pins = ga.deploy_pins()
    out["main_update_pin"] = {"commit": pins.get("commit"), "sha": pins.get("files", {}).get("scripts/gs-update.sh"),
                              "in_text": pins.get("commit") in text and pins.get("files", {}).get("scripts/gs-update.sh") in text}
    out["station"] = {"station": STATION, "pin": FP.replace(":", "").lower(), "pub": station_pub()}

elif SCENARIO == "main_paired_no_status":
    # 0.13.35: a main bot with no signed status yet. The relay goes ahead only if its key was pinned while this
    # release was current; a stamp from an older release (or none) is refused like a held older status.
    def relay_direct():
        n = len([p for p in POSTS if p["json"].get("kind") == "setup_relay"])
        got = ga.relay_call("POST", "/admin/bots/setup-relay", {"actor": ga.RELAY_ACTOR, "slug": "juno",
                                                                "target": "otto", "mode": "cloud", "path": "first"})
        return {"code": got[0], "error": got[1].get("error"),
                "posts": len([p for p in POSTS if p["json"].get("kind") == "setup_relay"]) - n}
    got = ga.relay_call("PUT", "/admin/bots/juno/key", {"actor": ga.RELAY_ACTOR, "public_key": KEY})
    out["pin"] = got[0]
    out["stamp"] = r.paired_releases().get("juno")
    out["held"] = r.bot_updates().get("juno")
    out["relay_this_release"] = relay_direct()
    r._save_paired({"juno": prev_same()["release"]})
    out["relay_older_stamp"] = relay_direct()
    r._save_paired({})
    out["relay_no_stamp"] = relay_direct()
    out["page"] = page()
    code, body, posts = setup("otto")
    out["gate"] = {"code": code, "error": (body or {}).get("error"), "setup_relay_posts": len(posts)}

elif SCENARIO == "mixed_releases":
    prev = prev_same()
    tab = table_entries()
    cur_fp = [e for e in tab if e["release"] == REL][-1]["fp"]["cloud"]  # prev_same() checked it is listed
    diff = [e for e in tab if e["fp"]["cloud"] != cur_fp][-1]
    status("juno", REL, gs_routine.instr_hash(current_text("juno")))
    status("kai", prev["release"], old_instr("kai", prev))
    status("pip", diff["release"], old_instr("pip", diff))
    out["releases"] = {"kai": prev["release"], "pip": diff["release"]}
    out["page"] = page()
    code, body, posts = setup("otto")
    out["gate"] = {"code": code, "error": (body or {}).get("error"), "setup_relay_posts": len(posts)}

elif SCENARIO == "update_timeout_offline":
    status("juno", REL, gs_routine.instr_hash(current_text("juno")))
    status("kai", "0.0.1", "0" * 16)
    p0 = page()
    sha = p0["instr"]["kai"]
    send = lambda: admin("api_post_finish_update", "/api/finish-update/send", {}, {"slug": "kai", "message_sha": sha})
    steps = {"before": p0["states"]["kai"]}
    c, b = send()
    steps["send"] = c
    steps["update_posts"] = [p["json"].get("release") for p in POSTS if p["json"].get("kind") == "update_bot"]
    p1 = page()
    steps["waiting"] = [p1["states"]["kai"], p1["wait_left"]["kai"]]
    c, b = send()
    steps["second_press"] = [c, (b or {}).get("error")]
    # The bot's computer is off: no reply. Ten minutes and a second pass.
    doc = ga.read_finish()
    doc["sent"]["kai"]["at"] -= ga.FINISH_WAIT_S + 1
    ga.write_json(ga.path(ga.FINISH), doc)
    r._update_asked["kai"] = time.time() - r.HELPER_ASK_GAP_S - 1
    p2 = page()
    steps["after_10_min"] = [p2["states"]["kai"], p2["fail"]["kai"], p2["wait_left"]["kai"]]
    OFFLINE.add("kai")
    c, b = send()
    steps["offline_press"] = [c, (b or {}).get("error")]
    p3 = page()
    steps["offline_row"] = [p3["states"]["kai"], p3["fail"]["kai"]]
    OFFLINE.discard("kai")
    c, b = send()
    steps["back_online_press"] = c
    out["steps"] = steps
    out["windows"] = {"FINISH_WAIT_S": ga.FINISH_WAIT_S, "HELPER_ASK_GAP_S": r.HELPER_ASK_GAP_S,
                      "UPDATE_REPLY_BY_S": botreply.UPDATE_REPLY_BY_S}

elif SCENARIO == "status_lint_digits":
    rid = "vs_" + "e" * 22
    line = "%s instr=b839678994eacb57 helper=- computer=93205df0855d1aad" % REL
    pend = {"request_id": rid, "persona": "kai", "nonce": "n" * 16, "expires_ms": int(time.time() * 1000) + 60000,
            "pii_mode": True, "kind": "update_bot"}
    got = botreply._accept_answer({"action_class": "answer", "kind": "reply", "request_id": rid, "text": line, "tier": 0},
                                  "kai", (lambda _rid: pend), set(), (), (), int(time.time() * 1000), pii=True)
    out["accepted"] = isinstance(got, dict) and got.get("text") == line
    out["free_text_ssn_blocked"] = r.handoff.lint_reply("the number is 839-67-8994", pii=True) is not None
    out["recorded"] = r.note_update("kai", got["text"]) if out["accepted"] else False
    out["page"] = page()

elif SCENARIO == "utc_stamps":
    import fewshot as fs
    import grok_wake as gw
    import time as realtime
    LOADED.update({"fewshot": fs.__file__, "grok_wake": gw.__file__})

    class Clock:  # 8 hours behind UTC, like a station in America/Los_Angeles
        def __init__(self, t):
            self.t = t

        def time(self):
            return self.t

        def gmtime(self, t=None):
            return realtime.gmtime(self.t if t is None else t)

        def localtime(self, t=None):
            return realtime.gmtime((self.t if t is None else t) - 8 * 3600)

        def strftime(self, fmt, tt=None):
            return realtime.strftime(fmt, self.localtime() if tt is None else tt)

        def monotonic(self):
            return realtime.monotonic()

        def sleep(self, s):
            pass

    clock = Clock(1_700_000_000)
    phrase = "hey_kai"
    os.makedirs(os.path.join(ga.ENROLL_INBOX, phrase, "20000101-000000"), exist_ok=True)
    ga.time, ga.wake, ga.phrases_to_clear = clock, (lambda *a, **k: (200, {})), (lambda slug: [phrase])
    code, body = ga.clear_bot_training("kai")
    with open(os.path.join(ga.ENROLL_INBOX, phrase, "cleared.json")) as fh:
        cleared = json.load(fh)["session"]
    clock.t += 60  # one minute after Clear, a new recording
    gw.time, gw.ENROLL_DIR, gw.post_state = clock, J("listener-enroll"), (lambda *a, **k: None)
    gw.ENROLL_STATE["busy"], gw.ENROLL_STATE["last"] = False, None

    def read16(raw=False):
        raise EOFError("stop")
    try:
        gw.enroll({"phrase": phrase, "persona": "kai", "count": 5}, read16, "default")
    except EOFError:
        pass
    ses = (gw.ENROLL_STATE.get("last") or {}).get("session")
    out.update({"clear_code": code, "cleared": cleared, "session": ses,
                "kept": fs.current_training_set([(ses, "clip")], cleared, 20) == [(ses, "clip")],
                "local_would_be": realtime.strftime("%Y%m%d-%H%M%S", clock.localtime())})
else:
    raise SystemExit("unknown scenario " + SCENARIO)

out["loaded"] = LOADED
print(json.dumps(out, default=str))
