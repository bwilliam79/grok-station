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


# A release that changes what a bot runs (0.13.37 did) has no older release with the same content in the table.
# The same-content scenarios then add one older line with this release's content and update pin, in memory only,
# so the rule is still checked. out["prev_synthetic"] says when that happened.
SYNTH_RELEASE = "0.12.99"
_SYNTH = []
_read_table = bot_content.read_table


def _read_table_with_synth(path=None):
    return _read_table(path) + (list(_SYNTH) if path is None else [])


bot_content.read_table = _read_table_with_synth


def prev_same():
    """The newest older release whose home content equals this release's (what same_content accepts)."""
    tab = table_entries()
    cur = [e for e in tab if e["release"] == REL]
    if not cur:
        raise SystemExit("admin/bot-releases.txt has no line for %s (run the manifest step first)" % REL)
    want = cur[-1]["fp"]["home"]
    olds = [e for e in tab if e["release"] != REL and e["fp"]["home"] == want
            and ga.version_order(e["release"], REL) == "newer"]
    if not olds and not _SYNTH and ga.version_order(SYNTH_RELEASE, REL) == "newer":
        _SYNTH.append(dict(cur[-1], release=SYNTH_RELEASE))
        out["prev_synthetic"] = SYNTH_RELEASE
        return prev_same()
    return olds[-1] if olds else None


def old_instr(slug, entry):
    """The hash a home bot on entry["release"] signs: the template with that header and that release's update pin.
    0.13.37: every bot is home (the cloud template is gone)."""
    rest = gs_routine.TEMPLATES["home"].split("\n", 1)[1]
    text = gs_routine.render("Grok Station instructions, release %s.\n" % entry["release"] + rest, "home", slug,
                             entry["update_commit"], entry["update_sha"], owner=ga.setup_owner_name())
    return gs_routine.instr_hash(text)


def station_pub():
    return "".join(r.botreply.station_pub_b64(r.botreply.load_or_create_station(r.SECRETS_DIR)).split())


def current_text(slug):
    b = {"slug": slug}  # 0.13.37: no key_mode; every bot is home
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
    lit = dict(re.findall(r"^(ROUTINE_COMMIT|HASH_ROUTINE_HOME|UPDATE_COMMIT|HASH_UPDATE|RELEASE) (\S+)$",
                          text, re.M))
    m = re.search(r"/grok-station/([0-9a-f]{40})/scripts/install-home\.sh' && printf '%s  %s\\n' '([0-9a-f]{64})'", text)
    lit["INSTALL_COMMIT"], lit["HASH_INSTALL"] = m.groups()
    return lit



# ---- 0.13.37: per-slug forget and Set up mode ----
PAIR_KEY = base64.b64encode(b"\x09" * 32).decode()


def no_password():
    H.need_password = lambda self, j, always=False, session_ok=False: True


def pendings_for(slug):
    found = []
    for name in sorted(os.listdir(r.PENDING_DIR)) if os.path.isdir(r.PENDING_DIR) else []:
        if not name.endswith(".json"):
            continue
        with open(os.path.join(r.PENDING_DIR, name)) as fh:
            doc = json.load(fh)
        if doc.get("persona") == slug or (doc.get("kind") == "setup_relay" and doc.get("target") == slug):
            found.append({"rid": doc.get("request_id"), "kind": doc.get("kind"),
                          "mode": (doc.get("frozen") or {}).get("mode")})
    return found


def traces(slug):
    """Everything a Set up or a Test leaves on the station for slug."""
    status = {}
    try:
        with open(r.status_path()) as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        status = {}
    return {"setup_relay": r.setup_state_load().get(slug), "pendings": pendings_for(slug),
            "bot_status": slug in status if isinstance(status, dict) else False,
            "admin_row": (ga.setup_load().get("rows") or {}).get(slug)}


def setup_rid(slug):
    row = r.setup_state_load().get(slug) or {}
    return row.get("rid")


def send_and_post_key(slug):
    """Set up through the main bot, then the target posts its key (what the verified setup_pair post records)."""
    code, body, posts = setup(slug)
    rid = setup_rid(slug)
    posted = bool(rid) and r.setup_note_review(rid, slug, PAIR_KEY, "Den iMac")
    r._note_status(slug, answered=True)
    return {"code": code, "error": (body or {}).get("error"), "posts": len(posts), "rid": rid, "review": posted,
            "mode": posts[0]["json"].get("mode") if posts else None}


def setup_payload(slug):
    """A fresh Set up: what was posted and what was frozen in the pending request."""
    code, body, posts = setup(slug)
    rid = setup_rid(slug)
    frozen = None
    if rid:
        with open(os.path.join(r.PENDING_DIR, rid + ".json")) as fh:
            frozen = json.load(fh).get("frozen")
    p = posts[0]["json"] if posts else {}
    return {"code": code, "error": (body or {}).get("error"), "posts": len(posts), "mode": p.get("mode"),
            "routine_sha256": p.get("routine_sha256"), "frozen_mode": (frozen or {}).get("mode"),
            "frozen_equals_post": frozen == p if p else False, "has_cloud": "cloud" in json.dumps(p)}


def imported_state(name):
    row = (r._load_imported().get("rows") or {}).get(name.casefold())
    return row.get("state") if isinstance(row, dict) else None


def setup_view(slug):
    """The Bots page's Set up row for slug: the admin row status and the relay state it shows ({} = no row)."""
    rows = (ga.bots_setup_field() or {}).get("rows") or {}
    row = rows.get(slug) or {}
    return {k: row.get(k) for k in ("status", "relay", "relay_fp") if k in row}


def main_current():
    status("juno", REL, gs_routine.instr_hash(current_text("juno")))


out = {"scenario": SCENARIO, "release": REL, "loaded": LOADED}

if SCENARIO == "main_behind_same_content":
    prev = prev_same()
    out["prev"] = prev and prev["release"]
    status("juno", prev["release"], old_instr("juno", prev))
    status("kai", prev["release"], old_instr("kai", prev))
    out["same_content_rule"] = ga.same_content({"slug": "juno"}, prev["release"],
                                               old_instr("juno", prev), STATION, FP, station_pub())
    out["page"] = page()
    code, body, posts = setup("otto")
    out["gate"] = {"code": code, "error": (body or {}).get("error"), "setup_relay_posts": len(posts)}
    got = ga.relay_call("POST", "/admin/bots/setup-relay", {"actor": ga.RELAY_ACTOR, "slug": "juno", "target": "otto",
                                                            "mode": "home", "path": "first"})
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
                                                                "target": "otto", "mode": "home", "path": "first"})
        return {"code": got[0], "error": got[1].get("error"),
                "posts": len([p for p in POSTS if p["json"].get("kind") == "setup_relay"]) - n}
    got = ga.relay_call("PUT", "/admin/bots/juno/key", {"actor": ga.RELAY_ACTOR, "public_key": KEY,
                                                        "expect_old_fp": r.botreply.key_fingerprint(KEY).replace(" ", "")})
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
    cur_fp = [e for e in tab if e["release"] == REL][-1]["fp"]["home"]  # prev_same() checked it is listed
    # A republished release adds a second row. Different content means an older release, not that row.
    diff = [e for e in tab if e["release"] != REL and e["fp"]["home"] != cur_fp][-1]
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

elif SCENARIO == "remove_then_rescan":
    # 0.13.37: Remove a home bot that was set up, rescan, Set up again: a net-new row and home-mode details.
    no_password()
    main_current()
    out["first"] = send_and_post_key("pip")
    out["before"] = traces("pip")
    code, body = admin("bots_remove", "pip", {})
    out["remove"] = code
    out["after_remove"] = traces("pip")
    out["merge"] = r.merge_imported(["Juno", "Otto", "Kai", "Pip"], "juno")
    out["imported_state"] = imported_state("Pip")
    out["view"] = setup_view("pip")
    out["again"] = setup_payload("pip")
    out["literals"] = literals()

elif SCENARIO == "dismiss_then_rescan":
    # 0.13.37: a Not set up name with a pending Set up, dismissed, then listed again: net-new, no old request.
    no_password()
    main_current()
    out["merge1"] = r.merge_imported(["Juno", "Otto", "Kai", "Pip", "Nix"], "juno")
    out["first"] = send_and_post_key("nix")
    out["before"] = traces("nix")
    r.merge_imported(["Juno", "Otto", "Kai", "Pip"], "juno")  # the fifth bot drops off: the row is missing, so it can be dismissed
    code, body = admin("bots_dismiss", "nix")
    out["dismiss"] = [code, (body or {}).get("error")]
    out["after_dismiss"] = traces("nix")
    out["merge2"] = r.merge_imported(["Juno", "Otto", "Kai", "Pip", "Nix"], "juno")
    out["imported_state"] = imported_state("Nix")
    out["view"] = setup_view("nix")
    out["accept_old"] = r.setup_pair_decide("nix", "accept")
    out["again"] = setup_payload("nix")

elif SCENARIO == "re_setup_while_pending":
    # 0.13.37: Set up again while a posted key waits for Accept. The page asks first; once replaced, the old
    # request and its posted key can't be accepted or posted to.
    no_password()
    main_current()
    out["first"] = send_and_post_key("otto")
    old = out["first"]["rid"]
    doc = ga.setup_load()
    row = doc["rows"]["otto"]
    for k in ("sent_at", "at"):
        if isinstance(row.get(k), (int, float)):
            row[k] -= 3600  # past the resend wait, so the second Set up is a real new request
    ga.setup_store(doc)
    out["second"] = setup_payload("otto")
    new = setup_rid("otto")
    out["rids_differ"] = bool(new) and new != old
    out["old_pending"] = os.path.exists(os.path.join(r.PENDING_DIR, old + ".json"))
    out["old_doc"] = r._pending_doc(old)
    out["late_post_old"] = r.setup_note_review(old, "otto", PAIR_KEY, "Den iMac")
    out["accept"] = r.setup_pair_decide("otto", "accept")
    out["pinned"] = [r.pinned_fingerprint("otto"), r._pubkey_fp(KEY), r._pubkey_fp(PAIR_KEY)]
    out["pendings"] = pendings_for("otto")

elif SCENARIO == "stale_cloud_request":
    # 0.13.37 upgrade: a Set up request frozen as cloud by 0.13.36 (posted key waiting, never accepted) is retired.
    no_password()
    main_current()
    now = time.time()
    vals = r.setup_station_values()

    def old_cloud_pending(target):
        rid = r.handoff.new_request_id()
        reply_by = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 900))
        frozen = r.handoff.setup_relay_payload(rid, "juno", reply_by, target, vals[0], vals[1], vals[2], "home", REL)
        frozen["mode"] = "cloud"  # what 0.13.36 froze for a bot with no stored key location
        doc = {"request_id": rid, "persona": "juno", "nonce": "n" * 16, "expires_ms": int((now + 900) * 1000),
               "kind": "setup_relay", "target": target, "frozen": frozen, "path": "first"}
        os.makedirs(r.PENDING_DIR, exist_ok=True)
        with open(os.path.join(r.PENDING_DIR, rid + ".json"), "w") as fh:
            json.dump(doc, fh)
        st = r.setup_state_load()
        st[target] = {"state": "review", "rid": rid, "at": int(now), "expires_ms": int((now + 900) * 1000),
                      "computer": "Den iMac", "pending_pub": PAIR_KEY, "fp": r._pubkey_fp(PAIR_KEY), "path": "first"}
        r.setup_state_save(st)
        return rid
    rid_otto = old_cloud_pending("otto")
    doc = ga.setup_load()
    doc.setdefault("rows", {})["otto"] = {"name": "Otto", "wake": "Hey Otto", "computer": "", "status": "sent",
                                         "sent_at": int(now), "fails": 0, "message": "", "unknown": False}
    ga.setup_store(doc)
    out["before"] = {"view": setup_view("otto"), "public": (r.setup_state_public().get("otto") or {}).get("state")}
    out["retired"] = r.retire_cloud_setup_requests()
    out["after"] = {"pending": os.path.exists(os.path.join(r.PENDING_DIR, rid_otto + ".json")),
                    "public": (r.setup_state_public().get("otto") or {}).get("state"),
                    "view": setup_view("otto"), "pending_pub": "pending_pub" in (r.setup_state_load().get("otto") or {})}
    out["accept"] = r.setup_pair_decide("otto", "accept")
    out["accept_admin"] = admin("bots_setup_pair", {"slug": "otto", "decision": "accept",
                                                  "expect_old_fp": r.botreply.key_fingerprint(KEY).replace(" ", ""),
                                                  "expect_new_fp": r._pubkey_fp(PAIR_KEY)})[0]
    out["late_post"] = r.setup_note_review(rid_otto, "otto", PAIR_KEY, "Den iMac")
    # A stale request that appears without a restart is refused when it is read.
    rid_kai = old_cloud_pending("kai")
    out["read_kai"] = r._pending_doc(rid_kai)
    out["kai_file"] = os.path.exists(os.path.join(r.PENDING_DIR, rid_kai + ".json"))
    doc = ga.setup_load()
    for row in (doc.get("rows") or {}).values():
        for k in ("sent_at", "at"):
            if isinstance(row.get(k), (int, float)):
                row[k] -= 3600
    ga.setup_store(doc)
    out["fresh"] = setup_payload("otto")
elif SCENARIO == "key_migration":
    # GS1337-01: what the relay does at its first 0.13.37 start with pins from 0.13.36. Three bots, one per
    # case: kai explicit key_mode cloud; otto no key_mode but a signed update status from its home helper; pip no
    # key_mode and nothing from a home helper. juno (the main bot) is key_mode home. All four have the same pin.
    no_password()
    main_current()
    with open(r.overlay_path()) as fh:
        ov = json.load(fh)
    ov["key_mode"] = {"juno": "home", "kai": "cloud"}
    with open(r.overlay_path(), "w") as fh:
        json.dump(ov, fh)
    status("otto", REL, gs_routine.instr_hash(current_text("otto")))
    trusted = getattr(r, "trusted_keys", None) or (lambda: botreply.load_keys(r.bot_keys_path()))

    def trust():
        keys = trusted().get("keys") or {}
        return {s: (s in keys and r.bot_paired(s)) for s in BOTS}

    def listed():
        got = ga.relay_call("GET", "/admin/bots")
        rows = {b["slug"]: ga.safe_bot(b) for b in (got[1].get("bots") or [])}
        return {s: (rows.get(s) or {}).get("key_move") for s in BOTS}

    def mig_audit():
        path = r.audit_path()
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [l.split(" ", 2)[2] for l in fh.read().splitlines() if " key-migration " in l]

    # 0.13.36's save_overlay dropped key_mode on the first save; it must survive until the migration has read it.
    got = ga.relay_call("PATCH", "/admin/bots/kai", {"voice": "ara", "actor": ga.RELAY_ACTOR})
    with open(r.overlay_path()) as fh:
        out["key_mode_before_migration"] = (got[0], json.load(fh).get("key_mode"))
    out["trusted_before"] = trust()
    run = getattr(r, "run_key_migration", None)
    out["verdicts"] = {s: v["verdict"] for s, v in (run() or {}).items()} if run else None
    out["evidence"] = {s: v["evidence"] for s, v in (r.run_key_migration(preview=True) or {}).items()} if run else None
    out["second_run"] = run() if run else "no migration"
    mpath = os.path.join(r.bots_dir(), "key-migration.json")
    out["file"] = json.load(open(mpath)) if os.path.exists(mpath) else None
    out["audit"] = mig_audit()
    out["trusted_after"] = trust()
    out["listed"] = listed()
    out["icon"] = r.icon_view()
    got = ga.relay_call("PATCH", "/admin/bots/kai", {"voice": "ara", "actor": ga.RELAY_ACTOR})
    with open(r.overlay_path()) as fh:
        out["key_mode_after_migration"] = (got[0], json.load(fh).get("key_mode"))
    # kai: Configure > Replace key with a new key from its home computer.
    new_key = base64.b64encode(b"\x0b" * 32).decode()
    old_fp = r.botreply.key_fingerprint(KEY).replace(" ", "")
    out["kai_same_key"] = admin("bots_key_put", "kai", {"public_key": KEY, "expect_old_fp": old_fp})[0]
    out["kai_same_key_listed"] = listed()["kai"]
    out["kai_replace"] = admin("bots_key_put", "kai", {"public_key": new_key, "expect_old_fp": old_fp})[0]
    # pip: Set up again; its home helper posts a key and the admin accepts it.
    out["pip_setup"] = send_and_post_key("pip")
    out["pip_accept"] = admin("bots_setup_pair", {"slug": "pip", "decision": "accept", "expect_old_fp": old_fp,
                                                "expect_new_fp": r._pubkey_fp(PAIR_KEY)})
    out["trusted_end"] = trust()
    out["listed_end"] = listed()
    out["audit_end"] = mig_audit()[len(out["audit"]):]
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
elif SCENARIO == "accept_notes_setup":
    # 0.13.39: Accept records the frozen release, so a webhook bot with no signed status is done, not copy.
    # Reject records nothing. Remove drops the stamp, so the id is not carried as already set up.
    no_password()
    main_current()
    out["before"] = page()["states"]["otto"]
    posted = send_and_post_key("otto")
    out["review"] = posted["review"]
    rid = posted["rid"]
    frozen_rel = None
    if rid:
        with open(os.path.join(r.PENDING_DIR, rid + ".json")) as fh:
            frozen_rel = (json.load(fh).get("frozen") or {}).get("release")
    out["frozen_release"] = frozen_rel
    old_fp = r.botreply.key_fingerprint(KEY).replace(" ", "")
    code, body = admin("bots_setup_pair", {"slug": "otto", "decision": "accept", "expect_old_fp": old_fp,
                                         "expect_new_fp": r._pubkey_fp(PAIR_KEY)})
    out["accept"] = code
    stamp = (ga.read_finish().get("setup") or {}).get("otto") or {}
    out["stamp_release"] = stamp.get("release")
    out["after"] = page()["states"].get("otto")
    send_and_post_key("kai")
    admin("bots_setup_pair", {"slug": "kai", "decision": "reject"})
    out["kai_stamp"] = (ga.read_finish().get("setup") or {}).get("kai")
    out["remove"] = admin("bots_remove", "otto", {})[0]
    out["stamp_after_remove"] = (ga.read_finish().get("setup") or {}).get("otto")
elif SCENARIO == "missing_key_postback":
    # 0.13.41: a setup request that times out with no key post-back fails out loud.
    import contextlib
    import io
    now = time.time()
    now_ms = int(now * 1000)
    rid = r.handoff.new_request_id()
    r.setup_state_save({
        "otto": {"state": "relayed", "rid": rid, "at": int(now - 1000), "expires_ms": now_ms - 1000, "path": "held"},
        "kai": {"state": "review", "rid": r.handoff.new_request_id(), "at": int(now - 1000),
                "expires_ms": now_ms - 1000, "path": "first", "computer": "Den iMac",
                "pending_pub": PAIR_KEY, "fp": r._pubkey_fp(PAIR_KEY)},
        "pip": {"state": "relayed", "rid": r.handoff.new_request_id(), "at": int(now),
                "expires_ms": now_ms + 600000, "path": "held"},
        "wren": {"state": "sent", "rid": r.handoff.new_request_id(), "at": int(now - 1000),
                 "expires_ms": now_ms - 1000, "path": "first"},
        "noor": {"state": "sent", "rid": r.handoff.new_request_id(), "at": int(now - 1000),
                 "expires_ms": 0, "path": "first"},
    })
    doc = ga.setup_load()
    doc.setdefault("rows", {})
    for slug, name in (("otto", "Otto"), ("kai", "Kai"), ("pip", "Pip"), ("wren", "Wren"), ("noor", "Noor")):
        doc["rows"][slug] = {"name": name, "wake": "Hey " + name, "computer": "", "status": "sent",
                             "sent_at": int(now - 1000), "fails": 0, "message": "", "unknown": False}
    ga.setup_store(doc)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        public = r.setup_state_public(now_ms=now_ms)
    with contextlib.redirect_stdout(io.StringIO()):
        again = r.setup_state_public(now_ms=now_ms)
    out["otto"] = public.get("otto")
    out["kai"] = public.get("kai")
    out["pip"] = public.get("pip")
    out["wren"] = public.get("wren")
    out["noor"] = public.get("noor")
    out["log"] = buf.getvalue()
    out["again_state"] = (again.get("otto") or {}).get("state")
    view = ga.setup_public(ga.setup_load(), now, public)
    out["row"] = (view.get("rows") or {}).get("otto")
    params = open(os.path.join(OPT, "admin", "setup-params.txt"), encoding="utf-8").read()
    js = open(os.path.join(OPT, "admin", "web", "app.js"), encoding="utf-8").read()
    out["step"] = "6. Required: post your public key back to this open setup request." in params
    out["page"] = "did not post its key back. Setup timed out." in js
else:
    raise SystemExit("unknown scenario " + SCENARIO)

out["loaded"] = LOADED
print(json.dumps(out, default=str))
