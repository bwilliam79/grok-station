/* grok-station admin (ADMIN-UI.md / design.md). Plain JS, no build, no storage: secrets never touch the browser beyond the
   field they are typed into, and every field is cleared after submit. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const root = () => $("#root");
let S = {state: null, page: "home", timers: [], stepupUntil: null, avatarV: 0, };
const PERSONA = s => s ? s[0].toUpperCase() + s.slice(1) : "";
const WORD = n => String(n == null ? "" : n).split("_").filter(Boolean).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
const ENROLL_ACTIVE = ["recording", "uploading", "training"];

async function api(method, path, body) {
  const opt = {method, credentials: "same-origin", headers: {"X-GS-Admin": "1"}};
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  else if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.body = "{}"; }
  let r;
  try { r = await fetch(path, opt); } catch (e) { return {status: 0, data: {error: "The station isn't answering. Check it's on and on this network."}}; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* image or empty */ }
  if (r.status === 401 && !/^\/api\/(login|setup)/.test(path)) { boot(); }
  return {status: r.status, ok: r.ok, data};
}
async function apiRaw(method, path, bytes, ctype, password) {
  const opt = {method, credentials: "same-origin", headers: {"X-GS-Admin": "1", "Content-Type": ctype || "application/octet-stream"}, body: bytes};
  if (password) opt.headers["X-GS-Password"] = password;
  let r;
  try { r = await fetch(path, opt); } catch (e) { return {status: 0, data: {error: "The station isn't answering. Check it's on and on this network."}}; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401) boot();
  return {status: r.status, ok: r.ok, data};
}
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.innerHTML = `<div class="banner ${kind}" role="status">${esc(msg)}</div>`;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.innerHTML = ""), 4000);
}
async function copyText(text) {
  let value = String(text || "");
  text = "";
  try { await navigator.clipboard.writeText(value); toast("Copied."); }
  catch (e) { toast("Select it and copy it.", "warn"); }
  value = "";
}
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
function every(ms, fn) { fn(); S.timers.push(setInterval(fn, ms)); }
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now"; if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " d ago";
}
function meter(db) {  // design.md: Too quiet / Good / Too loud
  if (db == null) return `<span class="pill off"><i></i>No level yet</span>`;
  const pct = Math.max(0, Math.min(100, (db + 70) / 70 * 100));
  const lab = db > -6 ? ["Too loud", "warn"] : db < -60 ? ["Too quiet", "off"] : ["Good", "ok"];
  return `<div class="inline"><div class="meterbar" style="flex:1;min-width:120px"><b style="width:${pct.toFixed(0)}%"></b></div>
    <span class="pill ${lab[1]}"><i></i>${lab[0]}</span><span class="help mono">${db.toFixed(0)} dBFS</span></div>`;
}
function errBox(el, msg) { el.innerHTML = msg ? `<div class="err" role="alert">${esc(msg)}</div>` : ""; }
function busyBtn(btn, on, label) { btn.disabled = on; if (label) btn.textContent = label; }

/* ---------------------------------------------------------------- boot + sign in */
async function boot() {
  clearTimers();
  const r = await api("GET", "/api/state");
  if (!r.ok) { root().innerHTML = `<div class="center"><div class="card auth"><h2>Can't reach the station</h2><p class="sub">${esc(r.data.error || "Try again.")}</p></div></div>`; return; }
  S.state = r.data;
  noteStepup(r.data);
  if (!r.data.signed_in) return r.data.setup_required ? codeScreen() : loginScreen();
  if (r.data.wizard && !r.data.wizard.done) return wizard(r.data.wizard.step || 2);
  shell();
}
function lockMsg(sec) { return `Too many tries. Sign-in is locked for ${Math.ceil(sec / 60)} more min.`; }

function codeScreen() {  // 01a: first run / reset
  const reset = S.state.mode === "reset";
  root().innerHTML = `<div class="center"><form class="card auth amber" id="f" autocomplete="off">
    <img class="mk" src="/assets/mark.svg" alt=""><div class="wordmark">Grok Station</div>
    <h2 style="font-size:24px">${reset ? "Set a new password" : "Set up this station"}</h2>
    <p class="sub">Enter the 6-character setup code shown on the station screen. It isn't case-sensitive and works once.</p>
    <div class="field"><label for="code">Setup code</label><input class="inp code" id="code" maxlength="7" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" required></div>
    <div id="e"></div>
    <div class="field"><button class="btn p" id="go">Continue</button></div>
    <p class="note">There is no default password. The code is on the station's screen; it changes each time the station starts until setup is done.</p>
  </form></div>`;
  if (S.state.locked_s) errBox($("#e"), lockMsg(S.state.locked_s));
  $("#code").focus();
  $("#f").onsubmit = async ev => {
    ev.preventDefault();
    const code = $("#code").value.replace(/[\s-]/g, "").toUpperCase();
    busyBtn($("#go"), true);
    const r = await api("POST", "/api/setup/code", {code});
    busyBtn($("#go"), false);
    if (!r.ok) return errBox($("#e"), r.status === 429 ? lockMsg(r.data.locked_s || 900) : r.data.error);
    passwordScreen();
  };
}
function strength(pw) {
  let s = 0; if (pw.length >= 10) s++; if (pw.length >= 14) s++; if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++; if (/[^A-Za-z]/.test(pw)) s++;
  return s;
}
function pwFields(id1, id2) {
  return `<div class="field"><label for="${id1}">New password</label><input class="inp" type="password" id="${id1}" minlength="10" autocomplete="new-password" required>
    <div class="strength" id="${id1}-s"><i></i><i></i><i></i><i></i></div><p class="note">At least 10 characters. A short sentence works well.</p></div>
    <div class="field"><label for="${id2}">Type it again</label><input class="inp" type="password" id="${id2}" autocomplete="new-password" required></div>`;
}
function wirePw(id1, id2, btn) {
  const upd = () => {
    const v = $("#" + id1).value, n = strength(v);
    [...$("#" + id1 + "-s").children].forEach((i, k) => i.classList.toggle("on", k < n));
    btn.disabled = v.length < 10 || v !== $("#" + id2).value;
  };
  $("#" + id1).oninput = upd; $("#" + id2).oninput = upd; upd();
}
function passwordScreen() {  // wizard step 1 (forced)
  root().innerHTML = `<div class="center"><form class="card auth" id="f" autocomplete="off">
    <div class="phonestep" style="display:block">Step 1 of 6 · Code and password</div>
    <h2 style="font-size:24px">Choose the admin password</h2>
    <p class="sub">You'll sign in as <b>admin</b> with this password. It's stored on the station as a salted hash only.</p>
    ${pwFields("p1", "p2")}<div id="e"></div>
    <div class="field"><button class="btn p" id="go" disabled>Save and continue</button></div></form></div>`;
  wirePw("p1", "p2", $("#go"));
  $("#f").onsubmit = async ev => {
    ev.preventDefault();
    const password = $("#p1").value;
    busyBtn($("#go"), true);
    const r = await api("POST", "/api/setup/password", {password});
    $("#p1").value = $("#p2").value = "";
    if (!r.ok) { busyBtn($("#go"), false); return errBox($("#e"), r.data.error || "Couldn't save. The setup code may have expired; reload to start again."); }
    boot();
  };
}
function loginScreen() {  // 01b + 01c
  root().innerHTML = `<div class="center"><form class="card auth" id="f">
    <img class="mk" src="/assets/mark.svg" alt=""><div class="wordmark">Grok Station</div><h2 style="font-size:24px">Sign in</h2><p class="sub">${esc(S.state.host)}</p>
    <div class="field"><label for="p">Password</label><div class="inline" style="flex-wrap:nowrap"><input class="inp" type="password" id="p" autocomplete="current-password" required>
      <button type="button" class="btn g sm" id="show">Show</button></div></div>
    <div id="e"></div><div class="field"><button class="btn p" id="go">Sign in</button></div>
    <p class="note"><a href="#" id="forgot">Forgot your password?</a></p>
    <div id="help" hidden class="banner off" style="display:block;margin-top:10px">
      <p>Reset needs the station itself. Either way only the password is wiped; your other settings stay.</p>
      <p style="margin-top:8px"><b>1. SD card:</b> power off, add an empty file named <span class="mono">grok-station-reset</span> to the card's boot partition, power on. The file is deleted after use.</p>
      <p style="margin-top:8px"><b>2. SSH:</b> run <span class="mono">sudo grok-station reset-admin</span>.</p>
      <p style="margin-top:8px">The station then shows a new setup code. Come back here and enter it.</p></div>
  </form></div>`;
  if (S.state.locked_s) errBox($("#e"), lockMsg(S.state.locked_s));
  $("#p").focus();
  $("#show").onclick = () => { const p = $("#p"); p.type = p.type === "password" ? "text" : "password"; $("#show").textContent = p.type === "password" ? "Show" : "Hide"; };
  $("#forgot").onclick = ev => { ev.preventDefault(); $("#help").hidden = !$("#help").hidden; };
  $("#f").onsubmit = async ev => {
    ev.preventDefault();
    busyBtn($("#go"), true);
    const r = await api("POST", "/api/login", {password: $("#p").value});
    $("#p").value = "";
    busyBtn($("#go"), false);
    if (!r.ok) return errBox($("#e"), r.status === 429 ? lockMsg(r.data.locked_s || 900) : r.data.error);
    boot();
  };
}

/* ---------------------------------------------------------------- shared panels (used by the wizard and the pages) */
async function wifiPanel(el, opts = {}) {
  const st = await api("GET", "/api/network");
  const n = st.data || {};
  const ips = Object.entries(n.ips || {}).filter(([k]) => !/^(docker|veth|br-)/.test(k)).map(([k, v]) => `<span class="chip mono">${esc(k)} ${esc(v)}</span>`).join("") || "none";
  el.innerHTML = `<div class="card"><h2>Network</h2>
    <div class="row"><div><div class="lbl">Connection</div><div class="help">${n.hotspot ? "Setup hotspot is on" : n.wifi ? `Wi-Fi <b>${esc(n.wifi.ssid)}</b> · signal ${esc(n.wifi.signal)}%` : n.wired ? "Wired" : "Not connected"}</div></div>
      <span class="right pill ${n.connected ? "ok" : "warn"}"><i></i>${n.connected ? "Online" : "Offline"}</span></div>
    <div class="row"><div><div class="lbl">IP addresses</div><div class="help">${ips}</div></div></div>
    ${opts.hostname === false ? "" : `<div class="row" style="flex-wrap:wrap"><div style="flex:1;min-width:200px"><label class="lbl" for="hn">Station name</label>
      <div class="help">The page moves to <span class="mono" id="hnurl">${esc(n.hostname)}.local</span></div></div>
      <input class="inp" id="hn" value="${esc(n.hostname)}" maxlength="32" style="max-width:220px"><button class="btn sm" id="hnsave">Rename</button></div>`}
    <div class="row"><div class="lbl">Wi-Fi networks</div><button class="btn sm right" id="scan">Scan</button></div>
    <div id="nets"><p class="help">Scan to see networks in range.</p></div>
    <div id="join" hidden></div><div id="ne"></div>
    ${(n.saved || []).length ? `<div class="row"><div class="lbl">Saved networks</div></div>` + n.saved.map(s => `<div class="row"><span>${esc(s.ssid)}</span>
      ${s.managed ? `<span class="right help">Set at install (kept)</span>` : `<button class="btn dg sm right" data-forget="${esc(s.ssid)}">Forget</button>`}</div>`).join("") : ""}
    <p class="note">Your current connection stays on while the station tries a new network; if joining fails it goes back to the old one.</p></div>`;
  const hs = $("#hnsave", el);
  if (hs) hs.onclick = async () => {
    const h = $("#hn", el).value.trim().toLowerCase();
    if (!confirm(`Rename the station to ${h}? This page will move to http://${h}.local`)) return;
    const r = await api("POST", "/api/network/hostname", {hostname: h});
    if (!r.ok) return errBox($("#ne", el), r.data.error);
    toast("Renamed. Opening the new address…"); setTimeout(() => (location.href = r.data.url), 2500);
  };
  el.querySelectorAll("[data-forget]").forEach(b => b.onclick = async () => {
    if (!confirm(`Forget ${b.dataset.forget}?`)) return;
    const r = await api("POST", "/api/network/forget", {ssid: b.dataset.forget});
    r.ok ? (toast("Forgotten."), wifiPanel(el, opts)) : errBox($("#ne", el), r.data.error);
  });
  $("#scan", el).onclick = async () => {
    busyBtn($("#scan", el), true, "Scanning…");
    const r = await api("GET", "/api/network/scan");
    busyBtn($("#scan", el), false, "Scan");
    if (!r.ok) return errBox($("#ne", el), r.data.error);
    const list = (r.data.networks || []).map(w => `<div class="row"><span>${esc(w.ssid)}</span><span class="help">${w.secure ? "🔒 " : ""}${w.signal}%</span>
      ${w.in_use ? `<span class="right pill ok"><i></i>Connected</span>` : `<button class="btn sm right" data-join="${esc(w.ssid)}" data-sec="${w.secure ? 1 : 0}">Join</button>`}</div>`).join("");
    $("#nets", el).innerHTML = (list || `<p class="help">No networks found.</p>`) + `<div class="row"><button class="btn g sm" data-join="" data-sec="1">Hidden network…</button></div>`;
    el.querySelectorAll("[data-join]").forEach(b => b.onclick = () => joinForm(el, b.dataset.join, b.dataset.sec === "1", opts));
  };
}
function joinForm(el, ssid, secure, opts) {
  const j = $("#join", el); j.hidden = false;
  j.innerHTML = `<form class="card" style="background:var(--gs-surface-2);margin-top:12px" autocomplete="off">
    ${ssid ? `<h2>Join ${esc(ssid)}</h2>` : `<h2>Hidden network</h2><div class="field"><label for="js">Network name</label><input class="inp" id="js" maxlength="32" required></div>`}
    ${secure ? `<div class="field"><label for="jp">Password</label><input class="inp" type="password" id="jp" minlength="8" maxlength="63" autocomplete="off" required></div>` : ""}
    <div class="field inline"><button class="btn p" id="jgo">Join</button><button type="button" class="btn g" id="jx">Cancel</button></div><div id="je"></div></form>`;
  $("#jx", j).onclick = () => { j.hidden = true; j.innerHTML = ""; };
  $("form", j).onsubmit = async ev => {
    ev.preventDefault();
    const body = {ssid: ssid || $("#js", j).value, hidden: !ssid};
    if (secure) body.password = $("#jp", j).value;
    busyBtn($("#jgo", j), true, "Joining…");
    const r = await api("POST", "/api/network/join", body);
    if ($("#jp", j)) $("#jp", j).value = "";
    if (!r.ok) { busyBtn($("#jgo", j), false, "Join"); return errBox($("#je", j), r.data.error); }
    toast("Joined " + body.ssid + "."); j.hidden = true; wifiPanel(el, opts); opts.onJoin && opts.onJoin();
  };
}

/* ---------------------------------------------------------------- voice + updates */
async function refreshStepup() {
  const r = await api("GET", "/api/state");
  if (r.ok) noteStepup(r.data);
}
function withPassword(title, text, label, send) {
  const go = async pw => {
    const r = await send(pw);
    if (r && r.ok && pw) await refreshStepup();
    return r;
  };
  if (stepupOpen()) go("").then(r => { if (r && r.status === 403) askPassword(title, text, label, go); });
  else askPassword(title, text, label, go);
}
async function paintRelay(el) {
  if (!el) return;
  const r = await api("GET", "/api/relay");
  if (!document.body.contains(el)) return;
  const ready = r.ok && r.data && r.data.state === "ready";
  el.innerHTML = `<div class="card"><h2>Voice</h2>
    <p class="sub">${ready ? "Voice: ready (local). The voice relay runs on this station." : "xAI key not set. Voice starts once it is saved."}</p>
    <div id="xkroot" style="margin-top:12px"></div></div>`;
  await xaiKeyPanel($("#xkroot", el), () => paintRelay(el));
}
function localTime(ts) {
  const ms = typeof ts === "number" ? (ts > 1e12 ? ts : ts * 1000) : Date.parse(String(ts || ""));
  return Number.isNaN(ms) ? String(ts || "") : new Date(ms).toLocaleString();
}
const UPDATE_EVENTS = {"update-check": "Checked", "update-check-failed": "Check failed", "update-start": "Update started",
  "update-failed": "Update failed", "update-autocheck": "Daily check"};
function historyHTML(rows) {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) return `<p class="help">No update history yet.</p>`;
  return list.slice().reverse().map(raw => {
    const parts = String(raw || "").trim().split(/\s+/);
    const ev = parts[3] || "", detail = parts.slice(4).join(" ");
    const what = (UPDATE_EVENTS[ev] || ev) + (detail && detail !== "-" ? " · " + detail : "");
    return `<div class="row"><span class="help">${esc(localTime(parts[0]))}</span><span>${esc(what)}</span></div>`;
  }).join("");
}
async function refreshUpdateBadge() {
  const nodes = document.querySelectorAll("[data-upbadge]");
  if (!nodes.length) return;
  const r = await api("GET", "/api/updates");
  const n = r.ok && r.data && r.data.update_available ? 1 : 0;
  nodes.forEach(b => { b.hidden = !n; b.textContent = n ? "1" : ""; });
}
const JOB_TEXT = {running: "Updating… The admin page restarts during the update and comes back by itself.",
  succeeded: "The last update finished.", failed: "The last update failed. The log is below."};
async function paintUpdates(el) {
  const r = await api("GET", "/api/updates");
  if (!document.body.contains(el)) return;
  const d = r.ok && r.data ? r.data : {};
  const job = d.job || {}, running = job.state === "running";
  const auto = !!d.autocheck;
  el.innerHTML = `${d.reboot_required ? `<div class="banner warn" style="flex-wrap:wrap">The last update changed boot options. Reboot the station (System, Reboot) to finish.</div>` : ""}
    ${d.cant_check ? `<div class="banner warn" style="flex-wrap:wrap">Couldn't reach GitHub to check for updates. Check the station's internet connection.</div>` : ""}
    ${!r.ok ? `<div class="banner bad" style="flex-wrap:wrap">${esc((r.data && r.data.error) || "Can't load updates.")}</div>` : ""}
    <div class="card"><h2>Station</h2>
      <div class="row"><span>Installed</span><span class="right mono">${esc(d.installed || "—")}</span></div>
      <div class="row"><span>Available</span><span class="right mono">${esc(d.available || "—")}${d.available && !d.update_available ? " (up to date)" : ""}</span></div>
      ${d.checked_at ? `<p class="note">Checked ${esc(localTime(d.checked_at))}. Updates come from the main branch on GitHub.</p>` : ""}
      ${JOB_TEXT[job.state] ? `<p class="note">${esc(JOB_TEXT[job.state])}</p>` : ""}
      <div class="field inline"><button type="button" class="btn p" id="upd"${d.update_available && !running ? "" : " disabled"}>Update</button>
        <button type="button" class="btn" id="checknow"${running ? " disabled" : ""}>Check now</button></div>
      <div id="ue"></div>
      ${(running || job.state === "failed") && (job.log || []).length ? `<pre class="mono help" style="white-space:pre-wrap;margin-top:12px">${esc(job.log.join("\n"))}</pre>` : ""}
      <div class="row"><button type="button" class="tog${auto ? " on" : ""}" role="switch" id="auto" aria-checked="${auto ? "true" : "false"}" aria-label="Daily check"></button>
        <div><div class="lbl">Daily check</div><div class="help">Off until you turn it on. A check looks for a newer version. It does not install anything.</div></div></div></div>
    <div class="card"><h2>History</h2>${historyHTML(d.history)}</div>`;
  if (running) setTimeout(() => { if (document.body.contains(el)) paintUpdates(el); }, 4000);
  const post = (btn, label, busy, path, title, text, okMsg) => {
    const send = async pw => {
      if (btn && btn.isConnected) busyBtn(btn, true, busy);
      const body = {};
      if (pw) body.password = pw;
      const res = await api("POST", path, body);
      if (res.status === 403) { if (btn && btn.isConnected) busyBtn(btn, false, label); return res; }
      if (!res.ok) errBox($("#ue", el), (res.data && res.data.error) || "That didn't work.");
      else if (okMsg) toast(okMsg);
      await paintUpdates(el);
      refreshUpdateBadge();
      return res;
    };
    withPassword(title, text, label, send);
  };
  const up = $("#upd", el);
  if (up) up.onclick = () => post(up, "Update", "Starting…", "/api/updates/apply", "Install the update?",
    "Enter the admin password. The station pulls the latest version and re-runs the installer; voice pauses for a few minutes.", "Update started.");
  $("#checknow", el).onclick = () => post($("#checknow", el), "Check now", "Checking…", "/api/updates/check", "Check for updates?",
    "Enter the admin password to look for a newer version. Nothing is installed.", "");
  $("#auto", el).onclick = () => {
    const on = !$("#auto", el).classList.contains("on");
    const send = async pw => {
      const body = {on};
      if (pw) body.password = pw;
      const res = await api("PUT", "/api/updates/autocheck", body);
      if (res.ok) {
        const live = $("#auto", el);
        if (live) { live.classList.toggle("on", on); live.setAttribute("aria-checked", on ? "true" : "false"); }
        toast(on ? "Daily check on." : "Daily check off.");
      } else if (res.status !== 403) toast((res.data && res.data.error) || "Couldn't save that.", "bad");
      return res;
    };
    withPassword("Daily check", "Enter the admin password to change the daily check. A check does not install anything.", "Save", send);
  };
}
async function pageUpdates(el) {
  el.innerHTML = `<div id="updatesroot"><p class="help">Loading…</p></div>`;
  await paintUpdates($("#updatesroot", el));
  refreshUpdateBadge();
}

async function timePanel(el, opts = {}) {
  const [g, z] = await Promise.all([api("GET", "/api/settings/time"), api("GET", "/api/timezones")]);
  const v = (g.data || {}).values || {};
  el.innerHTML = `<div class="card"><h2>Time and location</h2><p class="sub">Used for the clock, the weather on the station, and Grok's time and weather answers.</p>
    <div class="field"><label for="tz">Time zone</label><select class="sel" id="tz">${(z.data.zones || []).map(x => `<option${x === v.timezone ? " selected" : ""}>${esc(x)}</option>`).join("")}</select></div>
    <div class="field"><label for="town">Town or ZIP</label><div class="inline" style="flex-wrap:nowrap"><input class="inp" id="town" placeholder="Town or ZIP code"><button class="btn sm" id="find">Find</button></div>
      <div id="found" class="note">${v.location ? `Current location: ${esc(v.location)}` : "No location yet."}</div></div>
    <div class="field"><label for="un">Temperature</label><select class="sel" id="un"><option value="fahrenheit">°F</option><option value="celsius"${v.GROK_STATION_UNITS === "celsius" ? " selected" : ""}>°C</option></select></div>
    <div id="te"></div><div class="field inline"><button class="btn p" id="tsave">Save</button></div></div>`;
  let pick = null;
  $("#find", el).onclick = async () => {
    const r = await api("GET", "/api/geocode?q=" + encodeURIComponent($("#town", el).value));
    if (!r.ok) return errBox($("#te", el), r.data.error);
    const res = r.data.results || [];
    if (!res.length) return ($("#found", el).textContent = "Nothing found. Try a nearby town.");
    $("#found", el).innerHTML = res.map((x, i) => `<label class="row" style="cursor:pointer"><input type="radio" name="pl" value="${esc(x.id)}"${i ? "" : " checked"}> ${esc(x.label)}</label>`).join("");
    pick = res[0];
    el.querySelectorAll("[name=pl]").forEach(rb => rb.onchange = () => { pick = res.find(x => x.id === rb.value) || null; });
  };
  $("#tsave", el).onclick = async () => {
    const body = {timezone: $("#tz", el).value, GROK_STATION_UNITS: $("#un", el).value};
    if (pick && pick.id) body.place = pick.id;
    const r = await api("PUT", "/api/settings/time", body);
    if (!r.ok) return errBox($("#te", el), r.data.error);
    if (r.data.location) $("#found", el).textContent = "Current location: " + r.data.location;
    if (r.data.timezone && [...$("#tz", el).options].some(o => o.value === r.data.timezone)) $("#tz", el).value = r.data.timezone;
    pick = null;
    errBox($("#te", el), ""); toast("Saved."); opts.onOk && opts.onOk();
  };
}

async function audioPanel(el) {
  const [a, s] = await Promise.all([api("GET", "/api/audio"), api("GET", "/api/settings/audio")]);
  const v = (s.data || {}).values || {}, d = a.data || {};
  const devs = [...new Set([...(d.inputs || []), ...(d.outputs || []), v.WAKE_DEVICE_NAME].filter(Boolean))];
  // no saved choice yet: the listener's default matches "EMEET" in the card name
  const cur = v.WAKE_DEVICE_NAME || devs.find(x => /emeet/i.test(x)) || "";
  el.innerHTML = `<div class="card"><h2>Audio</h2>
    <div class="field"><label for="dev">Microphone and speaker</label><select class="sel" id="dev">${devs.map(x => `<option value="${esc(x)}"${x === cur ? " selected" : ""}>${esc(x)}${(d.in_use || []).includes(x) ? " (in use by the station)" : ""}</option>`).join("")}</select>
      <p class="note">The station uses one sound device for both (the speakerphone).</p></div>
    <div class="field"><label>Microphone level (live)</label><div id="lvl">${meter(d.level_dbfs)}</div></div>
    <div class="field"><label for="rate">Recording rate</label><select class="sel" id="rate"><option value="16000">16 kHz</option><option value="48000"${v.WAKE_CAPTURE_RATE === "48000" ? " selected" : ""}>48 kHz (device only does 48k)</option></select></div>
    <div class="field"><label for="wg">Wake-word mic boost <span class="help" id="wgv"></span></label><input type="range" id="wg" min="1" max="9" step="0.5" value="${esc(v.WAKE_GAIN || "1")}"></div>
    <div class="field"><label for="vg">Voice mic boost <span class="help" id="vgv"></span></label><input type="range" id="vg" min="1" max="9" step="0.5" value="${esc(v.VOICE_GAIN || "1")}"></div>
    <div id="ae"></div>
    <div class="field inline"><button class="btn p" id="asave">Save</button><button class="btn" id="atest">Play test sound</button></div>
    <p class="note">Saving restarts the voice listener (after any conversation or enrollment that's running).</p></div>`;
  const lab = () => { $("#wgv", el).textContent = "×" + $("#wg", el).value; $("#vgv", el).textContent = "×" + $("#vg", el).value; };
  $("#wg", el).oninput = $("#vg", el).oninput = lab; lab();
  every(1500, async () => { if (!document.body.contains(el)) return; const r = await api("GET", "/api/audio"); if (r.ok && $("#lvl", el)) $("#lvl", el).innerHTML = meter(r.data.level_dbfs); });
  $("#atest", el).onclick = async () => { const r = await api("POST", "/api/audio/test", {}); r.ok ? toast("Playing on the station.") : toast(r.data.error, "warn"); };
  $("#asave", el).onclick = async () => {
    const r = await api("PUT", "/api/settings/audio", {WAKE_DEVICE_NAME: $("#dev", el).value, WAKE_CAPTURE_RATE: $("#rate", el).value,
      WAKE_GAIN: (+$("#wg", el).value).toFixed(1), VOICE_GAIN: (+$("#vg", el).value).toFixed(1)});
    if (!r.ok) return errBox($("#ae", el), r.data.error);
    errBox($("#ae", el), ""); toast(r.data.pending || "Saved.");
  };
}

const BUILTIN_TIP = "Hey Grok is the built-in wake word. It is always on.";
const SENS_TIP = "Higher wakes more easily but may wake by mistake. Lower means fewer false wakes but it may miss you.";
function wordLabel(w) { return w.label || WORD(w.name); }
function enrollStatusText(row) {
  if (!row) return "";
  if (row.state === "processing" || ENROLL_ACTIVE.includes(row.state)) return "Processing…";
  if (row.state === "published" || row.state === "ready") return "Ready";
  if (row.state === "failed_quality") return row.message || "Not good enough yet (below the gate). Record more clips, 20-30, somewhere quiet.";
  return "";
}
async function wakePanel(el, opts = {}) {
  const [r, st] = await Promise.all([api("GET", "/api/wakewords"), api("GET", "/api/enroll/status")]);
  const words = (r.data || {}).words || [];
  const train = (st.data && st.data.words) || {};
  el.innerHTML = `<div class="card"><div class="inline"><h2>Wake words</h2><button class="btn p sm right" id="rec">Record a wake word</button></div>
    <p class="sub">Each word opens one teammate. Built-in words work right away; new ones need about a minute of your voice.</p>
    ${words.map(w => {
      const label = wordLabel(w);
      const tog = w.builtin
        ? `<span class="tipwrap" tabindex="0" role="note" aria-label="${esc(BUILTIN_TIP)}"><button class="tog on" role="switch" aria-checked="true" aria-label="${esc(label)} on" disabled aria-disabled="true"></button><span class="tip">${esc(BUILTIN_TIP)}</span></span>`
        : `<button class="tog${w.enabled ? " on" : ""}" role="switch" aria-checked="${w.enabled}" aria-label="${esc(label)} on" data-tog="${esc(w.name)}"${!w.has_model ? " disabled" : ""}></button>`;
      const job = enrollStatusText(train[w.name]);
      return `<div class="row" style="flex-wrap:wrap">
      ${tog}
      <div style="min-width:160px"><div class="lbl">${esc(label)}</div><div class="help">Opens ${esc(PERSONA(w.persona))}${w.builtin ? " · built in, always on" : ""}</div></div>
      <span class="pill ${w.builtin ? "ok" : w.enrolled ? "ok" : w.has_model ? (w.provisional ? "busy" : "ok") : "warn"}"><i></i>${w.builtin ? "Built in" : w.enrolled ? "Your voice" : w.has_model ? (w.provisional ? "Starter model" : "Ready") : "Needs your voice"}</span>
      ${job ? `<span class="help">${esc(job)}</span>` : ""}
      ${w.metrics ? `<span class="help mono">recall ${esc(w.metrics.recall)} · ${esc(w.metrics.fa_per_hour)}/h false</span>` : ""}
      ${`<label class="inline right help tipwrap" tabindex="0" style="min-width:220px">Sensitivity<span class="tip">${esc(SENS_TIP)}</span> <input type="range" min="5" max="95" value="${Math.round(w.sensitivity * 100)}" data-sens="${esc(w.name)}"${w.has_model ? "" : " disabled"}></label>
      <button class="btn sm" data-enroll="${esc(w.name)}">Record voice</button>`}</div>`;
    }).join("")}
    <div id="we"></div></div>`;
  el.querySelectorAll("[data-tog]").forEach(b => b.onclick = async () => {
    const on = !b.classList.contains("on");
    const res = await api("PUT", "/api/wakewords/" + b.dataset.tog, {enabled: on});
    res.ok ? (b.classList.toggle("on", on), b.setAttribute("aria-checked", on), toast(`${wordLabel({name: b.dataset.tog})} ${on ? "on" : "off"}.`)) : errBox($("#we", el), res.data.error);
  });
  el.querySelectorAll("[data-sens]").forEach(s => s.onchange = async () => {
    const res = await api("PUT", "/api/wakewords/" + s.dataset.sens, {sensitivity: +s.value / 100});
    res.ok ? toast("Saved.") : errBox($("#we", el), res.data.error);
  });
  const names = words.filter(w => !w.builtin).map(w => w.name);
  el.querySelectorAll("[data-enroll]").forEach(b => b.onclick = () => enrollDrawer(b.dataset.enroll, names, () => wakePanel(el, opts)));
  $("#rec", el).onclick = () => enrollDrawer(null, names, () => wakePanel(el, opts));
}

function enrollProgressHTML(word, id) {
  const label = {recording: "Recording", uploading: "Uploading", training: "Training", processing: "Processing…", ready: "Ready", published: "Ready", failed: "Didn't work", failed_quality: "Not good enough yet"}[word.state] || "Waiting";
  const kind = word.state === "failed" || word.state === "failed_quality" ? "bad" : word.state === "ready" || word.state === "published" ? "ok" : "busy";
  const msg = word.message ? ` · ${esc(word.message)}` : "";
  return `<div class="banner ${kind}"><span class="pill ${kind}"><i></i>${label}</span> ${esc(word.label || WORD(id))}${msg}</div>`;
}

function watchEnroll(d, id, box) {
  const tick = async () => {
    if (!document.body.contains(d)) { clearInterval(d.t); return; }
    const s = await api("GET", "/api/enroll/status");
    const w = ((s.data && s.data.words) || {})[id];
    if (!w) return;
    box.innerHTML = enrollProgressHTML(w, id);
    if (w.state === "ready" || w.state === "published" || w.state === "failed" || w.state === "failed_quality") {
      clearInterval(d.t);
      const go = $("#dgo", d);
      if (go) go.textContent = w.state === "ready" || w.state === "published" ? "Done" : "Record voice";
    }
  };
  tick();
  d.t = setInterval(tick, 2000);
}

async function enrollDrawer(phrase, names, done, persona) {
  if (!phrase) return enrollNew(done, persona || "");
  return enrollExisting(phrase, names, done);
}

async function enrollNew(done, persona) {  // free-text phrase + which bot it opens
  const [spR, botR] = await Promise.all([api("GET", "/api/wakewords"), api("GET", "/api/bots")]);
  const sp = ((spR.data || {}).speakers) || [];
  const bots = [{slug: "grok", name: "Grok"}].concat(((botR.data || {}).bots) || []);
  if (persona && !bots.some(b => b.slug === persona)) bots.push({slug: persona, name: persona});
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<div class="drawer" role="dialog" aria-label="Record a wake word"><div class="inline"><h2 style="font:600 22px var(--disp)">Record a wake word</h2><button class="btn g sm right" id="dx">Close</button></div>
    <div class="field"><label for="ph">Wake word</label><input class="inp" id="ph" maxlength="48" placeholder="hey kitchen" autocomplete="off" spellcheck="false">
      <p class="note" id="phnote">Two to four words. It doesn't have to contain the bot's name — one bot can have several.</p></div>
    <div class="field"><label for="bot">Opens</label><select class="sel" id="bot">${bots.map(b => `<option value="${esc(b.slug)}"${b.slug === (persona || "grok") ? " selected" : ""}>${esc(b.name)}</option>`).join("")}</select></div>
    <div class="field"><label for="spk">Whose voice?</label><select class="sel" id="spk"><option value="">Choose…</option>${sp.map(s => `<option value="${esc(s.id)}">${esc(s.name)}${s.guest ? " (guest)" : ""}</option>`).join("")}<option value="+">+ Add speaker</option></select>
      <p class="note">Speaker names stay on this station. The training computer only sees an ID like <span class="mono">spk-3f9a</span>. Ask guests first; their clips are deleted after training.</p>
      <div id="addsp" hidden class="inline" style="margin-top:8px"><input class="inp" id="spn" maxlength="32" placeholder="Name" style="flex:1"><label class="help"><input type="checkbox" id="spg"> Guest</label><button class="btn sm" id="spa">Add</button></div></div>
    <div class="field"><label for="cnt">Clips <span class="help" id="cntv"></span></label><input type="range" id="cnt" min="20" max="30" step="1" value="25"></div>
    <div id="de"></div>
    <div class="field"><button class="btn p" id="dgo">Start on the station</button></div>
    <div id="prog"></div>
    <p class="note">The station guides you: say the word after each chime, at normal volume, from where you usually stand.</p></div>`;
  document.body.appendChild(d);
  const close = () => { clearInterval(d.t); d.remove(); done && done(); };
  $("#dx", d).onclick = close;
  const cl = () => ($("#cntv", d).textContent = `${$("#cnt", d).value} (about ${Math.max(1, Math.round($("#cnt", d).value * 6 / 60))} min)`);
  $("#cnt", d).oninput = cl; cl();
  $("#spk", d).onchange = () => ($("#addsp", d).hidden = $("#spk", d).value !== "+");
  $("#spa", d).onclick = async () => {
    const r = await api("POST", "/api/speakers", {name: $("#spn", d).value, guest: $("#spg", d).checked});
    if (!r.ok) return errBox($("#de", d), r.data.error);
    const o = document.createElement("option"); o.value = r.data.id; o.textContent = $("#spn", d).value;
    $("#spk", d).insertBefore(o, $("#spk", d).lastElementChild); $("#spk", d).value = r.data.id; $("#addsp", d).hidden = true;
  };
  let checkTimer = 0, phraseBad = false;
  $("#ph", d).oninput = () => {
    clearTimeout(checkTimer);
    const v = $("#ph", d).value;
    checkTimer = setTimeout(async () => {
      if (!$("#ph", d) || $("#ph", d).value !== v) return;
      if (!v.trim()) { phraseBad = false; errBox($("#de", d), ""); $("#phnote", d).textContent = "Two to four words. It doesn't have to contain the bot's name — one bot can have several."; return; }
      const r = await api("POST", "/api/enroll/check", {phrase: v});
      if (!$("#ph", d) || $("#ph", d).value !== v) return;
      phraseBad = !r.ok;
      if (r.ok) { errBox($("#de", d), ""); $("#phnote", d).textContent = `Saves as ${r.data.id}.`; }
      else { errBox($("#de", d), (r.data && r.data.error) || "That phrase won't work."); $("#phnote", d).textContent = ""; }
    }, 300);
  };
  $("#dgo", d).onclick = async () => {
    const spk = $("#spk", d).value;
    if (phraseBad) return;
    if (!spk || spk === "+") return errBox($("#de", d), "Pick whose voice this is.");
    busyBtn($("#dgo", d), true, "Starting…");
    const r = await api("POST", "/api/enroll", {phrase: $("#ph", d).value, persona: $("#bot", d).value, count: +$("#cnt", d).value, speaker: spk});
    if (!(r.status === 202 || r.ok)) { busyBtn($("#dgo", d), false, "Start on the station"); return errBox($("#de", d), r.data.error || "The station couldn't start recording."); }
    errBox($("#de", d), ""); $("#dgo", d).textContent = "Recording on the station…";
    watchEnroll(d, (r.data && r.data.id) || "", $("#prog", d));
  };
}

async function enrollExisting(phrase, names, done) {  // record more clips of a word already on the station
  const sp = (await api("GET", "/api/wakewords")).data.speakers || [];
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<div class="drawer" role="dialog" aria-label="Record a wake word"><div class="inline"><h2 style="font:600 22px var(--disp)">Record a wake word</h2><button class="btn g sm right" id="dx">Close</button></div>
    <div class="field"><label for="ph">Wake word</label><select class="sel" id="ph">${names.map(n => `<option value="${esc(n)}"${n === phrase ? " selected" : ""}>${esc(WORD(n))}</option>`).join("")}</select></div>
    <div class="field"><label for="spk">Whose voice?</label><select class="sel" id="spk"><option value="">Choose…</option>${sp.map(s => `<option value="${esc(s.id)}">${esc(s.name)}${s.guest ? " (guest)" : ""}</option>`).join("")}<option value="+">+ Add speaker</option></select>
      <p class="note">Speaker names stay on this station. The training computer only sees an ID like <span class="mono">spk-3f9a</span>. Ask guests first; their clips are deleted after training.</p>
      <div id="addsp" hidden class="inline" style="margin-top:8px"><input class="inp" id="spn" maxlength="32" placeholder="Name" style="flex:1"><label class="help"><input type="checkbox" id="spg"> Guest</label><button class="btn sm" id="spa">Add</button></div></div>
    <div class="field"><label for="cnt">Clips <span class="help" id="cntv"></span></label><input type="range" id="cnt" min="20" max="30" step="1" value="25"></div>
    <div id="de"></div>
    <div class="field"><button class="btn p" id="dgo">Start on the station</button></div>
    <div id="prog"></div>
    <p class="note">The station guides you: say the word after each chime, at normal volume, from where you usually stand.</p></div>`;
  document.body.appendChild(d);
  const close = () => { clearInterval(d.t); d.remove(); done && done(); };
  $("#dx", d).onclick = close;
  const cl = () => ($("#cntv", d).textContent = `${$("#cnt", d).value} (about ${Math.max(1, Math.round($("#cnt", d).value * 6 / 60))} min)`);
  $("#cnt", d).oninput = cl; cl();
  $("#spk", d).onchange = () => ($("#addsp", d).hidden = $("#spk", d).value !== "+");
  $("#spa", d).onclick = async () => {
    const r = await api("POST", "/api/speakers", {name: $("#spn", d).value, guest: $("#spg", d).checked});
    if (!r.ok) return errBox($("#de", d), r.data.error);
    const o = document.createElement("option"); o.value = r.data.id; o.textContent = $("#spn", d).value;
    $("#spk", d).insertBefore(o, $("#spk", d).lastElementChild); $("#spk", d).value = r.data.id; $("#addsp", d).hidden = true;
  };
  $("#dgo", d).onclick = async () => {
    const spk = $("#spk", d).value;
    if (!spk || spk === "+") return errBox($("#de", d), "Pick whose voice this is.");
    busyBtn($("#dgo", d), true, "Starting…");
    const r = await api("POST", "/api/enroll", {phrase: $("#ph", d).value, count: +$("#cnt", d).value, speaker: spk});
    if (!(r.status === 202 || r.ok)) { busyBtn($("#dgo", d), false, "Start on the station"); return errBox($("#de", d), r.data.error || "The station couldn't start recording."); }
    errBox($("#de", d), ""); $("#dgo", d).textContent = "Recording on the station…";
    watchEnroll(d, (r.data && r.data.id) || $("#ph", d).value, $("#prog", d));
  };
}

function openVoiceStatus(bot, el) {  // reopen the in-progress (or failed) attempt; does not start a new recording
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<div class="drawer" role="dialog" aria-label="Wake word status"><div class="inline"><h2 style="font:600 22px var(--disp)">${esc(bot.name || "Wake word")}</h2><button class="btn g sm right" id="dx">Close</button></div>
    <div id="vbox"></div>
    <div class="field"><button class="btn" id="again" type="button" hidden>Record again</button></div>
    <p class="note">Closing this leaves the recording running. The button keeps its place after a reload.</p></div>`;
  document.body.appendChild(d);
  const tick = async () => {
    if (!document.body.contains(d)) { clearInterval(d.t); return; }
    const s = await api("GET", "/api/enroll/status");
    const words = (s.data && s.data.words) || {};
    const rows = Object.entries(words).filter(([, w]) => (w.persona || "grok") === bot.slug);
    const active = rows.filter(([, w]) => w.state === "processing" || ENROLL_ACTIVE.includes(w.state));
    const failed = rows.filter(([, w]) => w.state === "failed" || w.state === "failed_quality");
    const show = active[0] || failed[failed.length - 1];
    const box = $("#vbox", d);
    if (!box) return;
    if (!show) { box.innerHTML = `<p class="note">Nothing in progress.</p>`; $("#again", d).hidden = true; return; }
    const [id, w] = show;
    box.innerHTML = enrollProgressHTML(w, id);
    const again = $("#again", d);
    again.hidden = w.state !== "failed" && w.state !== "failed_quality";
    again.onclick = () => { clearInterval(d.t); d.remove(); enrollDrawer(null, [], () => refreshBots(el), bot.slug); };
  };
  tick();
  d.t = setInterval(tick, 2000);
  $("#dx", d).onclick = () => { clearInterval(d.t); d.remove(); if (el) refreshBots(el); };
}

async function displayPanel(el) {
  const g = await api("GET", "/api/settings/display");
  const v = (g.data || {}).values || {};
  const [nf, nt] = (v.GROK_STATION_NIGHT || "22-6").split("-");
  const hours = sel => [...Array(24).keys()].map(h => `<option value="${h}"${String(h) === sel ? " selected" : ""}>${((h + 11) % 12) + 1} ${h < 12 ? "AM" : "PM"}</option>`).join("");
  el.innerHTML = `<div class="card"><h2>Display</h2>
    <div class="row"><button class="tog${v.GROK_STATION_NIGHT === "" ? "" : " on"}" role="switch" id="non" aria-label="Night clock"></button><div><div class="lbl">Night clock</div><div class="help">A dim clock overnight.</div></div></div>
    <div class="row inline"><label class="help">From <select class="sel" id="nf" style="width:130px">${hours(nf)}</select></label><label class="help">to <select class="sel" id="nt" style="width:130px">${hours(nt)}</select></label></div>
    <div class="field"><label for="hold">Message hold time <span class="help" id="holdv"></span></label><input type="range" id="hold" min="5" max="120" value="${esc(v.GROK_STATION_HOLD || "30")}"></div>
    <div class="row"><button class="tog${v.GROK_STATION_FADE === "0" ? "" : " on"}" role="switch" id="fade" aria-label="Fade"></button><div class="lbl">Fade between screens</div></div>
    <div class="field"><label for="dun">Temperature</label><select class="sel" id="dun"><option value="fahrenheit">°F</option><option value="celsius"${v.GROK_STATION_UNITS === "celsius" ? " selected" : ""}>°C</option></select></div>
    <div id="de"></div><div class="field inline"><button class="btn p" id="dsave">Save</button></div>
    <p class="note">Brightness isn't adjustable from here yet: it depends on the monitor (HDMI monitors need DDC/CI support).</p></div>
    <div class="card"><h2>Live preview</h2><img class="thumb" id="pv" alt="What the station screen shows now"></div>`;
  $("#non", el).onclick = $("#fade", el).onclick = ev => ev.currentTarget.classList.toggle("on");
  const hl = () => ($("#holdv", el).textContent = $("#hold", el).value + " s"); $("#hold", el).oninput = hl; hl();
  every(5000, () => { const p = $("#pv", el); if (p) p.src = "/api/preview?t=" + Date.now(); });
  $("#dsave", el).onclick = async () => {
    const r = await api("PUT", "/api/settings/display", {GROK_STATION_NIGHT: $("#non", el).classList.contains("on") ? `${$("#nf", el).value}-${$("#nt", el).value}` : "",
      GROK_STATION_HOLD: $("#hold", el).value, GROK_STATION_FADE: $("#fade", el).classList.contains("on") ? "1" : "0", GROK_STATION_UNITS: $("#dun", el).value});
    r.ok ? (errBox($("#de", el), ""), toast("Saved. The screen restarts.")) : errBox($("#de", el), r.data.error);
  };
}

/* ---------------------------------------------------------------- wizard. Backend steps still run 2..8. */
const STEPS = ["Code and password", "Wi-Fi", "xAI key", "Time and location", "Wake words", "Audio test"];
const WIZ_VIEW = {2: 2, 3: 3, 4: 3, 5: 4, 6: 5, 7: 6};
const WIZ_NEXT = {2: 3, 3: 5, 4: 5, 5: 6, 6: 7, 7: 8};
const WIZ_BACK = {3: 2, 4: 2, 5: 3, 6: 5, 7: 6};
/* xAI key: write-only. The page never receives the key back, only whether one is set. */
async function xaiKeyPanel(el, onSaved, replacing) {
  const st = (await api("GET", "/api/xai-key")).data || {};
  const form = `<form id="xk" autocomplete="off"><div class="field"><label for="xkv">${st.set ? "New xAI API key" : "xAI API key"} <span class="help">${st.set ? "Replaces the current key." : "Not set yet."}</span></label>
    <input class="inp" type="password" id="xkv" autocomplete="off" spellcheck="false" placeholder="xai-..."></div>
    <div id="xke"></div><div class="inline" style="margin-top:14px"><button class="btn p" id="xks">Save key</button>${st.set ? `<button type="button" class="btn g" id="xkc">Cancel</button>` : ""}</div></form>`;
  const status = `<div class="row" id="xkst"><span class="dot ok"></span><b style="color:var(--ok, #2e9e5b)">Key set</b>${st.last4 ? ` <span class="mono">ending ••••${esc(st.last4)}</span>` : ""}
    <button type="button" class="btn g sm" id="xkr" style="margin-left:auto">Replace key</button></div>`;
  el.innerHTML = `${st.set && !replacing ? status : form}
    <p class="note">The key is saved on this station for the voice relay only. It is never shown again. Voice starts once it is saved.</p>`;
  if (st.set && !replacing) { $("#xkr", el).onclick = () => xaiKeyPanel(el, onSaved, true); return; }
  if ($("#xkc", el)) $("#xkc", el).onclick = () => xaiKeyPanel(el, onSaved, false);
  if (replacing) $("#xkv", el).focus();
  $("#xk", el).onsubmit = async ev => {
    ev.preventDefault();
    const inp = $("#xkv", el), btn = $("#xks", el);
    busyBtn(btn, true, "Saving…");
    const r = await api("POST", "/api/xai-key", {key: inp.value.trim()});
    inp.value = "";
    busyBtn(btn, false, "Save key");
    if (!r.ok) return errBox($("#xke", el), r.data.error || "Couldn't save the key.");
    if (S.state) S.state.xai_key_set = true;
    toast("Key saved. Voice is on.");
    await xaiKeyPanel(el, onSaved, false);
    if (onSaved) onSaved();
  };
}

async function wizard(step) {
  clearTimers();
  step = Math.max(2, Math.min(8, step));
  const hotspot = S.state.mode === "hotspot";
  if (step === 8) {
    root().innerHTML = `<div class="center"><div class="card auth"><div class="mk"></div><h2 style="font-size:24px">All set</h2>
      <p class="sub">Say <b>Hey Grok</b> to try it. Optional next: <a href="#" data-p="voice">Voice</a> · <a href="#" data-p="bots">Bots</a> · <a href="#" data-p="display">Display</a>.</p>
      <div class="field"><button class="btn p" id="fin">Go to Home</button></div></div></div>`;
    const fin = async p => { await api("POST", "/api/wizard", {step: 8, done: true}); S.page = p; boot(); };
    $("#fin").onclick = () => fin("home");
    document.querySelectorAll("[data-p]").forEach(a => a.onclick = ev => { ev.preventDefault(); fin(a.dataset.p); });
    return;
  }
  const view = WIZ_VIEW[step] || 2;
  const skip = step === 2 && !hotspot ? `<button class="btn g" id="skip">Skip (wired)</button>`
    : (step === 3 || step === 4) ? `<button class="btn g" id="skip">Set up later</button>`
    : step === 6 ? `<button class="btn g" id="skip">Skip, record later</button>` : "";
  root().innerHTML = `<div class="wiz"><nav class="rail" aria-label="Setup steps">${STEPS.map((t, i) => `<div class="st${i + 1 < view ? " done" : ""}${i + 1 === view ? " on" : ""}"><b>${i + 1 < view ? "✓" : i + 1}</b>${t}</div>`).join("")}</nav>
    <div class="pane"><div class="phonestep">Step ${view} of 6 · ${STEPS[view - 1]}</div><div class="meterbar phonestep" style="height:6px"><b style="width:${view / 6 * 100}%"></b></div>
      <h1 style="font:600 28px var(--disp);margin-bottom:16px">${STEPS[view - 1]}</h1><div id="wp"></div>
      <div class="inline" style="margin-top:20px">${view > 2 ? `<button class="btn g" id="back">Back</button>` : ""}
        <span class="right inline">${skip}<button class="btn p" id="next">Next</button></span></div></div></div>`;
  const wp = $("#wp"), next = $("#next");
  const go = async s => { await api("POST", "/api/wizard", {step: s, done: false}); wizard(s); };
  if ($("#back")) $("#back").onclick = () => go(WIZ_BACK[step] || Math.max(2, step - 1));
  if ($("#skip")) $("#skip").onclick = () => go(WIZ_NEXT[step] || step + 1);
  next.onclick = () => go(WIZ_NEXT[step] || step + 1);
  if (step === 2) {
    const st = (await api("GET", "/api/network")).data || {};
    next.disabled = hotspot && !(st.wifi);
    await wifiPanel(wp, {hostname: true, onJoin: () => (next.disabled = false)});
    if (hotspot) wp.insertAdjacentHTML("afterbegin", `<div class="banner warn" style="margin-bottom:12px">The station is running its own setup hotspot. Join your home Wi-Fi to continue; the hotspot then turns off and this page moves to your network.</div>`);
  } else if (step === 3 || step === 4) {
    next.disabled = false;
    await xaiKeyPanel(wp, () => (next.disabled = false));
    next.disabled = !(S.state && S.state.xai_key_set);
  } else if (step === 5) {
    await timePanel(wp, {onOk: () => {}});
  } else if (step === 6) {
    await wakePanel(wp, {skip: true});
  } else if (step === 7) {
    await audioPanel(wp);
  }
}

/* ---------------------------------------------------------------- app shell + pages */
const ICON = {
  home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  voice: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  wake: '<path d="M4 12h2l2-6 4 12 3-9 2 3h3"/>', bots: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6 6 0 0 1 3.5 5.5"/>',
  privacy: '<path d="M12 3l8 3v6c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V6z"/>',
  audio: '<path d="M11 5L6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>', display: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  time: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', network: '<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/>',
  system: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  updates: '<path d="M21 12a9 9 0 1 1-2.6-6.3L21 8"/><path d="M21 3v5h-5"/>',
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>'};
const svg = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>`;
const PAGES = {home: ["Home", pageHome], voice: ["Voice", pageVoice], wake: ["Wake words", el => wakePanel(el)], bots: ["Bots", pageBots],
  privacy: ["Privacy", pagePrivacy],
  audio: ["Audio", el => audioPanel(el)], display: ["Display", el => displayPanel(el)], time: ["Time and location", el => timePanel(el)],
  network: ["Network", el => wifiPanel(el)], updates: ["Updates", pageUpdates], system: ["System", pageSystem], more: ["More", pageMore]};
const NAV = [["", [["home", "Home"]]], ["Voice service", [["voice", "Voice"]]],
  ["Station", [["wake", "Wake words"], ["bots", "Bots"], ["privacy", "Privacy"], ["audio", "Audio"], ["display", "Display"], ["time", "Time and location"], ["network", "Network"]]],
  ["System", [["updates", "Updates"], ["system", "System"]]]];
const TABS = [["home", "Home", "home"], ["wake", "Voice", "wake"], ["audio", "Station", "audio"], ["system", "System", "system"], ["more", "More", "more"]];

function closeFloats() {
  document.querySelectorAll("body > .drawer, body > .sheet").forEach(n => { if (n._cancel) n._cancel(); n.remove(); });
}
function shell() {
  closeFloats();
  const page = (location.hash.slice(1) in PAGES) ? location.hash.slice(1) : S.page;
  S.page = page;
  root().innerHTML = `<div class="app"><aside class="side"><div class="brand"><img class="mk" src="/assets/mark.svg" alt=""><div><b>Grok Station</b><span>● ${esc(S.state.host)}</span></div></div>
    ${NAV.map(([g, items]) => (g ? `<div class="grp">${g}</div>` : "") + items.map(([k, t]) => `<a class="nav${k === page ? " on" : ""}" href="#${k}" title="${t}">${svg(k)}<span>${t}</span>${k === "updates" ? `<b class="b count" id="upbadge" data-upbadge hidden></b>` : ""}</a>`).join("")).join("")}
    <div class="foot">Signed in as admin · <a href="#" id="out">Sign out</a><br>Version <span class="mono">${esc(S.state.version)}</span></div></aside>
    <main class="main"><header class="top"><h1>${PAGES[page][0]}</h1><div class="r" id="topr"></div></header><div class="body" id="pg"></div></main></div>
    <nav class="phonebar">${TABS.map(([k, t, i]) => `<button data-tab="${k}" class="${k === page ? "on" : ""}">${svg(i)}${t}</button>`).join("")}</nav>`;
  $("#out").onclick = async ev => { ev.preventDefault(); await api("POST", "/api/logout", {}); boot(); };
  document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => (location.hash = b.dataset.tab));
  clearTimers();
  PAGES[page][1]($("#pg"));
  refreshUpdateBadge();
}
window.addEventListener("hashchange", () => { if (S.state && S.state.signed_in && !(S.state.wizard && !S.state.wizard.done)) shell(); });

async function pageHome(el) {
  el.innerHTML = `<div class="cols"><div class="card" id="hero"><p class="help">Loading…</p></div><div class="card"><h2>On the station now</h2><img class="thumb" id="pv" alt="Live preview of the station screen"><p class="note" id="pva"></p></div></div>
    <div class="tiles" id="tiles"></div>`;
  const load = async () => {
    const r = await api("GET", "/api/home");
    if (!r.ok || !$("#hero", el)) return;
    const h = r.data, w = h.wake || {};
    const svc = h.services || {}, up = Object.values(svc).every(x => x === "active");
    const ready = w && w.setup == null && svc["grok-station-wake"] === "active";
    const state = w.session ? ["In a conversation", "busy"] : w.enrolling ? ["Recording a wake word", "busy"] : ready ? ["Ready to talk", "ok"] : ["Voice is off", "warn"];
    $("#hero", el).innerHTML = `<div class="hero"><div class="ring ${state[1] === "warn" ? "amber" : ""}"></div><div><div style="font:600 22px var(--disp)">${state[0]}</div>
      <div class="help">${h.network && h.network.connected ? "Online" : "Offline"} · last heard ${h.last_heard ? ago(h.last_heard.at) + " (" + esc(PERSONA(h.last_heard.who)) + ")" : "not yet"}</div></div></div>
      <div style="margin-top:12px">${(h.words || []).map(x => `<span class="chip">${esc(WORD(x))}</span>`).join("")}</div>
      <div class="field"><label>Microphone</label>${meter(w.level_dbfs)}</div>
      <div class="inline" style="margin-top:12px"><button class="btn sm" id="ts">Test speaker</button><button class="btn sm" id="rf">Refresh</button></div>
      <p class="note">As of ${new Date(h.as_of * 1000).toLocaleTimeString()}</p>`;
    $("#ts", el).onclick = async () => { const x = await api("POST", "/api/audio/test", {}); x.ok ? toast("Playing on the station.") : toast(x.data.error, "warn"); };
    $("#rf", el).onclick = load;
    const n = h.network || {};
    $("#tiles", el).innerHTML = [
      ["Voice", h.xai_key ? "Ready (local)" : "xAI key not set", "voice"],
      ["Network", n.wifi ? `Wi-Fi ${esc(n.wifi.ssid)} · ${n.wifi.signal}%` : n.wired ? "Wired" : "Offline", "network"],
      ["Services", up ? "All running" : Object.entries(svc).filter(([, v]) => v !== "active").map(([k]) => esc(k.replace("grok-station-", ""))).join(", ") + " stopped", "system"],
      ["Wake words", `${(h.words || []).length} on`, "wake"]].map(([t, v, p]) => `<a class="tile" href="#${p}" style="text-decoration:none;color:inherit"><div class="help">${t}</div><div style="font:600 17px var(--disp);margin-top:4px">${v}</div></a>`).join("");
  };
  every(10000, load);
  every(5000, () => { const p = $("#pv", el); if (p) { p.src = "/api/preview?t=" + Date.now(); $("#pva", el).textContent = "Updated " + new Date().toLocaleTimeString(); } });
}
async function pageVoice(el) {
  el.innerHTML = `<div id="relayroot"></div>`;
  await paintRelay($("#relayroot", el));
}
function clockTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  let h = d.getHours();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return h + ":" + String(d.getMinutes()).padStart(2, "0") + " " + ap;
}
function mss(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}
function oneDecimal(n) { return (Math.round(Number(n) * 10) / 10).toFixed(1); }
const BOT_ID_RE = /^[a-z][a-z0-9-]{1,23}$/;
const BOT_ID_ERROR = "Use 2–24 characters: a letter first, then lowercase letters, digits, or hyphens. The voice service doesn't allow a longer id.";
const BOT_ID_HELP = "Short lowercase ID for this bot, like nix. Used for wake-word routing, portraits and replies. Can't be changed later.";
function botIdFromName(name) {
  let s = String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/-{2,}/g, "-");
  if (s.length > 24) s = s.slice(0, 24).replace(/-+$/g, "");
  return s;
}
function botIdProblem(slug) {
  if (slug === "grok") return '"grok" is reserved.';
  if (!BOT_ID_RE.test(slug)) return BOT_ID_ERROR;
  return "";
}
function botPill(b) {
  const st = b.status || {};
  if (st.state === "connected") {
    const when = st.last_answered_at ? "Last answered " + clockTime(st.last_answered_at) : "Connected";
    return `<span class="pill ok"><i></i>${esc(when)}</span>`;
  }
  if (st.state === "error") {
    const msg = String(st.last_error || "Error");
    return `<span class="pill bad"><i></i>${esc(msg.length > 80 ? msg.slice(0, 79) + "…" : msg)}</span>`;
  }
  return `<span class="pill off"><i></i>Paste its webhook to connect</span>`;
}
function stepupText() {
  const left = Number(S.stepupUntil) - Date.now() / 1000;
  if (!(left > 0)) return "";
  return "Unlocked for " + Math.ceil(left / 60) + " min";
}
function noteStepup(data) {
  if (data && Object.prototype.hasOwnProperty.call(data, "stepup_until")) S.stepupUntil = data.stepup_until;
  const n = $("#sulock");
  if (!n) return;
  const t = stepupText();
  n.textContent = t;
  n.hidden = !t;
}
function stepupOpen() { return !!stepupText(); }
/* sha256 of UTF-8 bytes, hex. Same formula as app/avatars.py: int(sha256(slug.encode()).hexdigest(), 16) % 18 + 1. */
function sha256Hex(bytes) {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);
  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const len = bytes.length;
  const bitHi = Math.floor(len / 0x20000000);
  const bitLo = (len << 3) >>> 0;
  let n = len + 9;
  if (n % 64) n += 64 - (n % 64);
  const buf = new Uint8Array(n);
  buf.set(bytes);
  buf[len] = 0x80;
  const view = new DataView(buf.buffer);
  view.setUint32(n - 8, bitHi, false);
  view.setUint32(n - 4, bitLo, false);
  const w = new Uint32Array(64);
  const rotr = (x, r) => (x >>> r) | (x << (32 - r));
  for (let off = 0; off < n; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
      const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7].map(x => x.toString(16).padStart(8, "0")).join("");
}
function utf8Bytes(s) {
  const out = [];
  for (let i = 0; i < s.length; i++) {
    let c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length) {
      const c2 = s.charCodeAt(++i);
      c = 0x10000 + ((c & 0x3ff) << 10) + (c2 & 0x3ff);
    }
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 0x3f), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
  }
  return new Uint8Array(out);
}
function defaultAvatarIndex(slug) {
  const hex = sha256Hex(utf8Bytes(String(slug)));
  let n = 0;
  for (let i = 0; i < hex.length; i++) n = (n * 16 + parseInt(hex[i], 16)) % 18;
  return n + 1;
}
function defaultAvatarSrc(slug) {
  return "/avatars/default-" + String(defaultAvatarIndex(slug)).padStart(2, "0") + ".svg";
}
function avatarOnError(img) {
  const wrap = img && img.parentNode;
  const fb = wrap && wrap.getAttribute("data-fallback");
  if (fb && img.dataset.fb !== "1") {
    img.dataset.fb = "1";
    img.src = fb;
    return;
  }
  img.remove();
}
function bindAvatars(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("img.av").forEach(img => { img.onerror = () => avatarOnError(img); });
}
function voiceOptions(voices, current, id) {
  const list = Array.isArray(voices) ? voices.slice() : [];
  if (current && list.indexOf(current) < 0) list.unshift(current);
  const opts = list.map(v => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(v)}</option>`).join("");
  const required = id === "avoice" ? " required" : "";
  return `<select class="sel" id="${id}"${required}>${opts || `<option value="">No voices yet</option>`}</select>`;
}
function avatar(slug, name, lg) {
  const letter = esc(String(name || slug || "?").trim().slice(0, 1).toUpperCase() || "?");
  const v = S.avatarV ? "?v=" + S.avatarV : "";
  const fb = esc(defaultAvatarSrc(slug));
  return `<span class="avwrap${lg ? " lg" : ""}" data-slug="${esc(slug)}" data-fallback="${fb}"><img class="av" alt="" src="/api/portrait/${esc(slug)}${v}" onerror="avatarOnError(this)"><span class="monoav">${letter}</span></span>`;
}
function askPassword(title, text, label, send, onCancel) {
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<form class="card" role="dialog" autocomplete="off" style="width:min(400px,100%)">
    <h2>${esc(title)}</h2><p class="sub">${esc(text)}</p>
    <div class="field"><label for="pwv">Admin password</label><input class="inp" type="password" id="pwv" autocomplete="current-password" required></div>
    <div id="pwe"></div>
    <div class="field inline"><button class="btn p" id="pwgo">${esc(label)}</button><button class="btn g" type="button" id="pwno">Cancel</button></div></form>`;
  document.body.appendChild(d);
  const close = () => d.remove();
  $("#pwno", d).onclick = () => { close(); if (onCancel) onCancel(); };
  d.querySelector("form").onsubmit = async ev => {
    ev.preventDefault();
    const inp = $("#pwv", d);
    const pw = inp.value;
    inp.value = "";
    busyBtn($("#pwgo", d), true);
    const r = await send(pw);
    busyBtn($("#pwgo", d), false, label);
    if (r && r.ok) close();
    else errBox($("#pwe", d), (r && r.data && r.data.error) || "Couldn't do that.");
  };
  $("#pwv", d).focus();
}
function paintBots(el, bots) {
  const n = s => bots.filter(b => (b.status || {}).state === s).length;
  const count = $("#bcount", el);
  if (count) count.textContent = `${n("connected")} connected · ${n("error")} errors · ${n("not_configured")} not configured`;
  const list = $("#blist", el);
  if (!list) return;
  const voice = el._voice || {};
  list.innerHTML = bots.map(b => {
    const info = voice[b.slug] || {};
    return `<div class="botrow">${avatar(b.slug, b.name)}
    <div style="flex:1;min-width:0"><div class="lbl">${esc(b.name)}</div><div style="margin-top:4px">${botPill(b)}</div></div>
    <button class="btn sm" type="button" data-voice="${esc(b.slug)}">${esc(info.label || "Record voice")}</button>
    ${(b.webhook && b.webhook.configured) ? "" : `<button class="btn sm" type="button" data-hook="${esc(b.slug)}">Connect webhook</button>`}
    <button class="btn sm" type="button" data-bot="${esc(b.slug)}">Configure</button></div>`;
  }).join("") || `<p class="help">No bots yet.</p>`;
  bindAvatars(list);
  list.querySelectorAll("[data-bot]").forEach(btn => btn.onclick = () => openBot(bots.find(b => b.slug === btn.dataset.bot), el));
  list.querySelectorAll("[data-hook]").forEach(btn => btn.onclick = () => connectWebhookWizard(bots.find(b => b.slug === btn.dataset.hook), el));
  list.querySelectorAll("[data-voice]").forEach(btn => btn.onclick = () => {
    const bot = bots.find(b => b.slug === btn.dataset.voice);
    const info = voice[bot.slug] || {};
    if (info.processing || info.state === "failed" || info.state === "failed_quality") openVoiceStatus(bot, el);
    else enrollDrawer(null, [], () => refreshBots(el), bot.slug);
  });
}
async function refreshBots(el) {
  const [r, s] = await Promise.all([api("GET", "/api/bots"), api("GET", "/api/enroll/status")]);
  if (!r.ok || !$("#blist", el)) return null;
  noteStepup(r.data);
  el._voices = (r.data && r.data.voices) || [];
  el._voice = (s.data && s.data.buttons) || {};
  const msg = $("#bmsg", el);
  if (msg) { msg.hidden = !r.data.message; if (r.data.message) msg.textContent = r.data.message; }
  paintBots(el, r.data.bots || []);
  return r.data;
}
function bustPortraits(el) {
  S.avatarV = Date.now();
  // A portrait that failed to load earlier removed its <img>; put one back so a new upload shows.
  document.querySelectorAll(".avwrap[data-slug]").forEach(w => {
    let img = w.querySelector("img.av");
    if (!img) {
      img = document.createElement("img");
      img.className = "av"; img.alt = "";
      w.prepend(img);
    }
    delete img.dataset.fb;
    img.onerror = () => avatarOnError(img);
    img.src = "/api/portrait/" + encodeURIComponent(w.dataset.slug) + "?v=" + S.avatarV;
  });
  return refreshBots(el);
}
function openBot(bot, el) {
  if (!bot) return;
  closeFloats();
  const d = document.createElement("div");
  d.className = "drawer bots";
  d.id = "botdrawer";
  d.setAttribute("role", "dialog");
  d.setAttribute("aria-label", "Configure " + (bot.name || "bot"));
  let testTimer = null, testId = null;
  const stopPoll = () => { if (testTimer) { clearInterval(testTimer); testTimer = null; } };
  const cancelTest = async () => {
    stopPoll();
    const id = testId;
    testId = null;
    if (id) await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}/test/${encodeURIComponent(id)}`, {});
  };
  d._cancel = cancelTest;
  const saved = !!(bot.webhook && bot.webhook.configured);
  d.innerHTML = `<div class="inline">${avatar(bot.slug, bot.name, true)}
      <div style="min-width:0"><div class="lbl">${esc(bot.name)}</div><div style="margin-top:4px">${botPill(bot)}</div></div>
      <button class="btn g sm right" id="dx">Close</button></div>
    <div class="field"><label for="bvoice">Voice</label>
      <div class="inline">${voiceOptions(el._voices, bot.voice, "bvoice")}<button class="btn sm" id="vsave" type="button">Save</button></div></div>
    <div class="field inline">
      <label class="btn sm" for="photo">Upload photo</label>
      <input type="file" id="photo" accept="image/png,image/jpeg,image/webp" hidden>
      <button class="btn sm" id="refav" type="button">Refresh avatar</button>
    </div>
    <p class="note">png, jpeg, or webp, up to 2 MB.</p>
    <p class="note">Copy these from the bot's routine panel in Grok Bot.</p>
    <div id="saved"${saved ? "" : " hidden"} class="secret"><span class="dots">•••• ${esc((bot.webhook && bot.webhook.last4) || "")}</span>
      <span class="pill ok"><i></i>Saved</span><button class="btn sm" id="repl" type="button">Replace</button></div>
    <div id="fields"${saved ? " hidden" : ""}>
      <div class="field"><label for="burl">Webhook URL</label><input class="inp" id="burl" type="url" inputmode="url" autocomplete="off" spellcheck="false" autocapitalize="off"></div>
      <div class="field"><label for="bkey">Webhook key</label><input class="inp" id="bkey" type="password" autocomplete="off" spellcheck="false" autocapitalize="off"></div>
      <p class="note" id="pasted" hidden>Pasted · not saved</p>
    </div>
    <div class="field inline"><button class="btn" id="test" type="button">Test</button><button class="btn p" id="save" type="button">Save</button></div>
    <div id="tbox"></div>
    ${saved ? `<div class="field"><button class="btn dg" id="delhook" type="button">Remove…</button></div>` : ""}
    <div class="botfoot"><button class="btn g sm" id="ren" type="button">Rename</button><button class="btn dg sm" id="delbot" type="button">Remove bot</button></div>`;
  document.body.appendChild(d);
  bindAvatars(d);
  const fieldsOn = () => { $("#saved", d).hidden = true; $("#fields", d).hidden = false; };
  const sync = () => {
    const url = $("#burl", d).value.trim(), key = $("#bkey", d).value, dirty = url.length + key.length > 0;
    $("#pasted", d).hidden = !dirty;
    $("#test", d).disabled = !!testId || (dirty ? !(url && key) : !(bot.webhook && bot.webhook.configured));
  };
  if (!saved) fieldsOn();
  $("#repl", d) && ($("#repl", d).onclick = () => { $("#burl", d).value = ""; $("#bkey", d).value = ""; fieldsOn(); sync(); });
  $("#burl", d).oninput = $("#bkey", d).oninput = sync;
  sync();
  $("#dx", d).onclick = async () => { await cancelTest(); d.remove(); };
  $("#vsave", d).onclick = () => {
    const voice = ($("#bvoice", d) && $("#bvoice", d).value) || "";
    if (!voice) return toast("Pick a voice.", "bad");
    const send = async pw => {
      const body = {voice};
      if (pw) body.password = pw;
      const r = await api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body);
      if (r.ok) {
        bot.voice = voice;
        toast("Voice saved.");
        refreshBots(el);
      }
      return r;
    };
    if (stepupOpen()) send("").then(r => {
      if (r && r.status === 403) askPassword("Change voice?", "Enter the admin password to save this voice.", "Save", send);
      else if (r && !r.ok) toast((r.data && r.data.error) || "Couldn't save the voice.", "bad");
    });
    else askPassword("Change voice?", "Enter the admin password to save this voice.", "Save", send);
  };
  $("#photo", d).onchange = () => {
    const file = $("#photo", d).files && $("#photo", d).files[0];
    $("#photo", d).value = "";
    if (!file) return;
    const okType = file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp";
    if (!okType || file.size > 2000000) return toast("Use a png, jpeg, or webp up to 2 MB.", "bad");
    const send = async pw => apiRaw("POST", `/api/bots/${encodeURIComponent(bot.slug)}/avatar`, await file.arrayBuffer(), file.type, pw);
    const done = r => {
      if (r && (r.ok || r.status === 200)) { toast("Photo saved."); bustPortraits(el); }
      return r;
    };
    if (stepupOpen()) send("").then(r => { if (r && r.status === 403) askPassword("Upload photo?", "Enter the admin password to save this photo.", "Upload", async pw => done(await send(pw))); else done(r); });
    else askPassword("Upload photo?", "Enter the admin password to save this photo.", "Upload", async pw => done(await send(pw)));
  };
  $("#refav", d).onclick = async () => {
    const btn = $("#refav", d);
    busyBtn(btn, true, "Refreshing…");
    const r = await api("POST", `/api/bots/${encodeURIComponent(bot.slug)}/avatar-request`, {});
    busyBtn(btn, false, "Refresh avatar");
    if (!(r.ok || r.status === 202)) return toast((r.data && r.data.error) || "Couldn't refresh the avatar.", "bad");
    toast("Refreshing avatar.");
    setTimeout(() => { if (document.body.contains(el)) bustPortraits(el); }, 30000);
  };
  const showResult = (res) => {
    stopPoll();
    testId = null;
    const btn = $("#test", d);
    if (btn) { btn.textContent = "Test"; btn.disabled = false; }
    sync();
    const box = $("#tbox", d);
    if (!box) return;
    box.innerHTML = testResultHTML(res);
  };
  const startPoll = (id) => {
    testId = id;
    let sec = 0, busy = false;
    const paint = () => {
      const clock = $("#tclock", d), bar = $("#tfill", d);
      if (clock) clock.textContent = mss(sec);
      if (bar) bar.style.width = Math.min(100, sec / 120 * 100) + "%";
    };
    $("#tbox", d).innerHTML = `<div class="twait"><div class="inline"><span class="spin"></span><b>Waiting for ${esc(bot.name)}… <span id="tclock">0:00</span></b></div>
      <div class="tbar"><i id="tfill"></i></div>
      <p class="note">Bots can take a while to answer. Gives up after 2 min.</p>
      <a href="#" id="tcancel">Cancel</a></div>`;
    $("#tcancel", d).onclick = async ev => { ev.preventDefault(); await cancelTest(); showResult({state: "cancelled"}); };
    testTimer = setInterval(async () => {
      if (!$("#tclock", d)) { stopPoll(); return; }
      sec += 1;
      paint();
      if (busy) return;
      busy = true;
      try {
        const r = await api("GET", `/api/bots/${encodeURIComponent(bot.slug)}/test/${encodeURIComponent(id)}`);
        if (!$("#tclock", d)) return;
        if (!r.ok) { showResult({state: "failed", error: (r.data && r.data.error) || "No answer after 2 min"}); return; }
        if (typeof r.data.elapsed_s === "number") { sec = Math.max(sec, r.data.elapsed_s); paint(); }
        if (r.data.state === "waiting" && sec >= 125) { showResult({state: "failed", error: "No answer after 2 min"}); return; }
        if (r.data.state !== "waiting") showResult(r.data);
      } finally { busy = false; }
    }, 1000);
    S.timers.push(testTimer);
  };
  $("#test", d).onclick = async () => {
    if (testId) return;
    const url = $("#fields", d).hidden ? "" : $("#burl", d).value.trim();
    const key = $("#fields", d).hidden ? "" : $("#bkey", d).value;
    const body = {};
    if (url || key) { body.url = url; body.key = key; }
    const btn = $("#test", d);
    btn.disabled = true;
    btn.textContent = "Testing…";
    const r = await api("POST", `/api/bots/${encodeURIComponent(bot.slug)}/test`, body);
    if (!r.ok || !r.data.test_id) {
      btn.textContent = "Test";
      sync();
      showResult({state: "failed", error: (r.data && r.data.error) || "Couldn't start the test."});
      return;
    }
    startPoll(r.data.test_id);
  };
  $("#save", d).onclick = () => {
    const urlEl = $("#burl", d), keyEl = $("#bkey", d);
    const url = urlEl.value.trim();
    const key = keyEl.value;
    let box = $("#berr", d);
    if (!box) {
      box = document.createElement("div");
      box.id = "berr";
      $("#save", d).parentElement.insertAdjacentElement("afterend", box);
    }
    if (!(url && key)) {
      if ($("#fields", d).hidden) { $("#saved", d).hidden = true; $("#fields", d).hidden = false; }
      errBox(box, "Paste both the webhook URL and the key.");
      (url ? keyEl : urlEl).focus();
      return;
    }
    errBox(box, "");
    const send = async pw => {
      $("#bkey", d).value = "";
      sync();
      const body = {url, key};
      if (pw) body.password = pw;
      const r = await api("PUT", `/api/bots/${encodeURIComponent(bot.slug)}/webhook`, body);
      if (r.ok) {
        bot.webhook = {configured: true, last4: r.data.last4, saved_at: Math.floor(Date.now() / 1000)};
        showRoutine(bot, r.data.routine);
        $("#burl", d).value = "";
        const dots = $(".dots", d);
        if (dots) dots.textContent = "•••• " + (r.data.last4 || "");
        $("#saved", d).hidden = false;
        $("#fields", d).hidden = true;
        if (!$("#delhook", d)) {
          const holder = document.createElement("div");
          holder.className = "field";
          holder.innerHTML = `<button class="btn dg" id="delhook" type="button">Remove…</button>`;
          $("#tbox", d).insertAdjacentElement("afterend", holder);
          $("#delhook", d).onclick = removeHook;
        }
        toast("Saved.");
        refreshBots(el);
      }
      return r;
    };
    if (stepupOpen()) send("").then(r => { if (r && r.status === 403) askPassword("Save webhook?", "Enter the admin password to save this webhook.", "Save", send); });
    else askPassword("Save webhook?", "Enter the admin password to save this webhook.", "Save", send);
  };
  const removeHook = () => askPassword(`Remove ${bot.name}'s bot?`, `This deletes the webhook address and key. ${bot.name} stays on the station.`, "Remove", async pw => {
    const r = await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}/webhook`, {password: pw});
    if (r.ok) {
      bot.webhook = {configured: false, last4: null, saved_at: null};
      toast("Webhook removed.");
      d.remove();
      refreshBots(el);
    }
    return r;
  });
  if ($("#delhook", d)) $("#delhook", d).onclick = removeHook;
  $("#ren", d).onclick = () => {
    const box = document.createElement("div");
    box.className = "sheet";
    const open = stepupOpen();
    box.innerHTML = `<form class="card" role="dialog" autocomplete="off" style="width:min(400px,100%)">
      <h2>Rename</h2><p class="sub">${esc(bot.name)}</p>
      <div class="field"><label for="rname">Name</label><input class="inp" id="rname" maxlength="40" required value="${esc(bot.name)}"></div>
      ${open ? `<p class="note stepup">${esc(stepupText())}</p>` : `<div class="field"><label for="rpw">Admin password</label><input class="inp" type="password" id="rpw" autocomplete="current-password" required></div>`}
      <div id="rpe"></div>
      <div class="field inline"><button class="btn p" id="rgo">Rename</button><button class="btn g" type="button" id="rno">Cancel</button></div></form>`;
    document.body.appendChild(box);
    $("#rno", box).onclick = () => box.remove();
    box.querySelector("form").onsubmit = async ev => {
      ev.preventDefault();
      const name = $("#rname", box).value.trim();
      const pwEl = $("#rpw", box);
      const pw = pwEl ? pwEl.value : "";
      if (pwEl) pwEl.value = "";
      const body = {name};
      if (pw) body.password = pw;
      const r = await api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body);
      if (!r.ok) {
        if (r.status === 403 && !$("#rpw", box)) {
          const holder = document.createElement("div");
          holder.className = "field";
          holder.innerHTML = `<label for="rpw">Admin password</label><input class="inp" type="password" id="rpw" autocomplete="current-password" required>`;
          $("#rpe", box).before(holder);
        }
        return errBox($("#rpe", box), r.data.error);
      }
      box.remove();
      toast("Renamed.");
      d.remove();
      refreshBots(el);
    };
  };
  $("#delbot", d).onclick = () => askPassword(`Remove ${bot.name}?`, "This removes the bot from the station. A registry teammate is hidden. An added bot is deleted.", "Remove bot", async pw => {
    const r = await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}`, {password: pw});
    if (r.ok) { toast("Removed."); d.remove(); refreshBots(el); }
    return r;
  });
}
const ADD_STEP_LABEL = "1 Bot · 2 Voice token · 3 Webhook · 4 Done";
// Values the bot's routine needs, shown once after a webhook save. The reply token is not shown again.
function routineText(bot, v) {
  return [
    `Station: ${v.station_url}` + ((v.station_urls || []).length > 1 ? ` (or ${v.station_urls.slice(1).join(", ")})` : ""),
    `Cert SHA-256: ${v.fingerprint || "see the admin page"}`,
    `Reply token (Authorization: Bearer ...): ${v.reply_token}`,
    `1. When the webhook fires, read request_id from the body.`,
    `2. GET ${v.station_url}/voice/request/<request_id> with the reply token. The JSON has "question".`,
    `3. POST ${v.reply_url} with the reply token and JSON {"request_id":"<request_id>","text":"<answer, max 400 chars>","tier":0,"action_class":"answer"}.`,
    `Answer within 2 minutes. Persona: ${bot.slug}.`,
  ].join("\n");
}
function showRoutine(bot, v) {
  if (!v || !v.reply_token) return;
  const d = document.createElement("div");
  d.className = "modal";
  d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Routine values"><h2>Paste this into ${esc(bot.name || bot.slug)}'s routine</h2>
    <p class="note">The reply token is shown only now. Saving the webhook again makes a new one.</p>
    <p class="note">The station is reachable on your home network only. A routine running in the cloud can't reach it unless you give it a way in (see the README, "Connecting a bot").</p>
    <textarea class="inp mono" id="rtext" rows="9" readonly>${esc(routineText(bot, v))}</textarea>
    <div class="row"><button class="btn" id="rcopy" type="button">Copy</button><button class="btn" id="rdone" type="button">Done</button></div></div>`;
  document.body.appendChild(d);
  $("#rcopy", d).onclick = () => { navigator.clipboard && navigator.clipboard.writeText($("#rtext", d).value); toast("Copied."); };
  $("#rdone", d).onclick = () => d.remove();
}

const HOOK_LATER = "Not connected yet - use Connect webhook on the Bots page";
const TOKEN_WHAT = "This is the bot's own key to talk to the station's voice service.";
const TOKEN_WHERE = "Paste it into the bot's Grok Bot routine settings as its voice token. The routine sends it as Authorization: Bearer <voice token> when it replies or uploads an avatar.";
const TOKEN_ONCE = "It won't be shown again.";
function addStepper(n) {
  const parts = ADD_STEP_LABEL.split(" · ");
  return `<p class="stepper" aria-label="${ADD_STEP_LABEL}">${parts.map((t, i) => {
    const cls = i + 1 === n ? "on" : i + 1 < n ? "done" : "";
    return `<span class="${cls}"${i + 1 === n ? ' aria-current="step"' : ""}>${t}</span>`;
  }).join('<span class="sep"> · </span>')}</p>`;
}
function httpsWebhook(url) {
  const v = String(url || "").trim();
  if (!/^https:\/\//i.test(v)) return false;
  try {
    const u = new URL(v);
    return u.protocol === "https:" && !u.username && !u.password && !!u.hostname;
  } catch (e) { return false; }
}
function shownLast4(v) {
  return typeof v === "string" && /^[\x21-\x7e]{1,4}$/.test(v) ? v : "";
}
function dropToken(root) {
  const node = root && $("#tok", root);
  if (node) node.textContent = "";
  try {
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.removeAllRanges) sel.removeAllRanges();
  } catch (e) { /* selection can throw while a document is closing */ }
}
function testResultHTML(res) {
  if (res.state === "ok") {
    const secs = res.answered_in_s == null ? "" : oneDecimal(res.answered_in_s) + " s";
    return `<div class="banner ok"><span class="pill ok"><i></i>Answered in ${esc(secs)}</span></div>`;
  }
  if (res.state === "cancelled") return `<p class="note">Cancelled.</p>`;
  const err = res.error || "No answer after 2 min";
  const hint = !res.error || res.error === "No answer after 2 min";
  return `<div class="banner bad"><span class="pill bad"><i></i>${esc(err)}</span></div>` +
    (hint ? `<p class="note">Check both values are from this bot's routine panel, then test again.</p>` : "");
}
function mountWebhookStep(root, bot, el, onFinish) {
  let connected = !!(bot.webhook && bot.webhook.configured);
  let last4 = connected ? shownLast4(bot.webhook && bot.webhook.last4) : "";
  let key = "";
  let testTimer = null, testId = null, saving = false;
  const stopPoll = () => { if (testTimer) { clearInterval(testTimer); testTimer = null; } };
  const cancelTest = async () => {
    stopPoll();
    const id = testId;
    testId = null;
    if (id) await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}/test/${encodeURIComponent(id)}`, {});
  };
  root.innerHTML = `<h2>Connect webhook</h2>
    <ol class="ol">
      <li>Open this bot's routine in Grok Bot.</li>
      <li>Find its webhook panel.</li>
      <li>Copy the webhook URL and its key.</li>
    </ol>
    <p class="note">The station sends the key as the <span class="mono">Authorization</span> header when it calls the webhook.</p>
    <div id="hsaved" class="secret" hidden><span class="dots" id="hookdots"></span><span class="pill ok"><i></i>Saved</span></div>
    <div id="hookfields">
      <div class="field"><label for="hurl">Webhook URL</label><input class="inp" id="hurl" type="url" inputmode="url" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="https://"></div>
      <div class="field"><label for="hkey">Key</label><input class="inp" id="hkey" type="password" autocomplete="off" spellcheck="false" autocapitalize="off"></div>
    </div>
    <div id="herr"></div>
    <div class="field inline">
      <button class="btn p" id="hsave" type="button">Save</button>
      <button class="btn" id="hping" type="button" disabled>Send test ping</button>
      <button class="btn p" id="hnext" type="button" hidden>Continue</button>
    </div>
    <div id="hbox"></div>
    <div class="field"><a href="#" class="skipnow" id="hskip">Skip for now</a></div>`;
  const showSaved = () => {
    const fields = $("#hookfields", root), saved = $("#hsaved", root);
    if (fields) fields.hidden = true;
    if (saved) saved.hidden = false;
    const dots = $("#hookdots", root);
    if (dots) dots.textContent = "•••• " + (last4 || "");
    const urlEl = $("#hurl", root), keyEl = $("#hkey", root);
    if (urlEl) urlEl.value = "";
    if (keyEl) keyEl.value = "";
    const ping = $("#hping", root);
    if (ping && !testId) ping.disabled = false;
    const next = $("#hnext", root);
    if (next) next.hidden = false;
    const saveBtn = $("#hsave", root);
    if (saveBtn) saveBtn.hidden = true;
  };
  if (connected) showSaved();
  const showResult = (res) => {
    stopPoll();
    testId = null;
    const btn = $("#hping", root);
    if (btn) { btn.disabled = !connected; btn.textContent = "Send test ping"; }
    const box = $("#hbox", root);
    if (box) box.innerHTML = testResultHTML(res);
  };
  const startPoll = (id) => {
    testId = id;
    let sec = 0, busy = false;
    const btn = $("#hping", root);
    if (btn) { btn.disabled = true; btn.textContent = "Send test ping"; }
    const box = $("#hbox", root);
    if (!box) return;
    box.innerHTML = `<div class="twait"><div class="inline"><span class="spin"></span><b>Waiting for ${esc(bot.name)}… <span id="hclock">0:00</span></b></div>
      <div class="tbar"><i id="hfill"></i></div>
      <p class="note">Bots can take a while to answer. Gives up after 2 min.</p>
      <a href="#" id="hcancel">Cancel</a></div>`;
    const cancel = $("#hcancel", root);
    if (cancel) cancel.onclick = async ev => { ev.preventDefault(); await cancelTest(); showResult({state: "cancelled"}); };
    testTimer = setInterval(async () => {
      if (!$("#hclock", root)) { stopPoll(); return; }
      sec += 1;
      const clock = $("#hclock", root), bar = $("#hfill", root);
      if (clock) clock.textContent = mss(sec);
      if (bar) bar.style.width = Math.min(100, sec / 120 * 100) + "%";
      if (busy) return;
      busy = true;
      try {
        const r = await api("GET", `/api/bots/${encodeURIComponent(bot.slug)}/test/${encodeURIComponent(id)}`);
        if (testId !== id || !$("#hclock", root)) return;
        if (!r.ok) { showResult({state: "failed", error: (r.data && r.data.error) || "No answer after 2 min"}); return; }
        if (typeof r.data.elapsed_s === "number") { sec = Math.max(sec, r.data.elapsed_s); if (clock) clock.textContent = mss(sec); if (bar) bar.style.width = Math.min(100, sec / 120 * 100) + "%"; }
        if (r.data.state === "waiting" && sec >= 125) { showResult({state: "failed", error: "No answer after 2 min"}); return; }
        if (r.data.state !== "waiting") showResult(r.data);
      } finally { busy = false; }
    }, 1000);
    S.timers.push(testTimer);
  };
  const finish = async () => {
    key = "";
    const urlEl = $("#hurl", root), keyEl = $("#hkey", root);
    if (urlEl) urlEl.value = "";
    if (keyEl) keyEl.value = "";
    await cancelTest();
    onFinish(connected, last4);
  };
  $("#hskip", root).onclick = ev => { ev.preventDefault(); finish(); };
  const next = $("#hnext", root);
  if (next) next.onclick = () => finish();
  $("#hping", root).onclick = async () => {
    if (testId || !connected) return;
    const btn = $("#hping", root);
    btn.disabled = true;
    const r = await api("POST", `/api/bots/${encodeURIComponent(bot.slug)}/test`, {});
    if (!r.ok || !r.data || !r.data.test_id) {
      if (btn) btn.disabled = !connected;
      showResult({state: "failed", error: (r.data && r.data.error) || "Couldn't start the test."});
      return;
    }
    startPoll(r.data.test_id);
  };
  $("#hsave", root).onclick = () => {
    if (saving) return;
    const urlEl = $("#hurl", root), keyEl = $("#hkey", root);
    const url = urlEl ? urlEl.value.trim() : "";
    const typed = keyEl ? keyEl.value.trim() : "";
    if (!httpsWebhook(url)) {
      key = "";
      errBox($("#herr", root), "The webhook URL has to start with https://.");
      if (urlEl) urlEl.focus();
      return;
    }
    if (!typed) {
      key = "";
      errBox($("#herr", root), "Paste the webhook key.");
      if (keyEl) keyEl.focus();
      return;
    }
    key = typed;
    errBox($("#herr", root), "");
    const send = async pw => {
      saving = true;
      const btn = $("#hsave", root);
      if (btn) busyBtn(btn, true, "Saving…");
      if (keyEl) keyEl.value = "";
      const body = {url, key};
      if (pw) body.password = pw;
      const r = await api("PUT", `/api/bots/${encodeURIComponent(bot.slug)}/webhook`, body);
      body.key = "";
      body.password = "";
      saving = false;
      if (btn) busyBtn(btn, false, "Save");
      if (r && r.ok) {
        key = "";
        connected = true;
        last4 = shownLast4(r.data && r.data.last4);
        bot.webhook = {configured: true, last4: last4 || null, saved_at: Math.floor(Date.now() / 1000)};
        showSaved();
        showRoutine(bot, r.data && r.data.routine);
        errBox($("#herr", root), "");
        toast("Saved.");
        refreshBots(el);
      }
      return r;
    };
    const forgetKey = () => { key = ""; };
    if (stepupOpen()) {
      send("").then(r => {
        if (r && r.status === 403) askPassword("Save webhook?", "Enter the admin password to save this webhook.", "Save", send, forgetKey);
        else if (r && !r.ok) { forgetKey(); errBox($("#herr", root), (r.data && r.data.error) || "Couldn't save."); }
      });
    } else askPassword("Save webhook?", "Enter the admin password to save this webhook.", "Save", send, forgetKey);
  };
  return {cancel: () => { key = ""; return cancelTest(); }};
}
function connectWebhookWizard(bot, el) {
  if (!bot || !bot.slug) return;
  closeFloats();
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Connect webhook"><div id="hookmount"></div></div>`;
  document.body.appendChild(d);
  const finish = () => {
    if (d._cancel) d._cancel();
    d._cancel = null;
    d.remove();
    refreshBots(el);
  };
  const ctl = mountWebhookStep($("#hookmount", d), bot, el, finish);
  d._cancel = () => ctl.cancel();
}
function addBot(voices, el) {
  closeFloats();
  const d = document.createElement("div");
  d.className = "sheet";
  d._cancel = () => {};
  document.body.appendChild(d);
  const step4 = (bot, connected, last4) => {
    d._cancel = () => {};
    d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Done">${addStepper(4)}
      <h2>Done</h2><p id="donesum"></p><p class="note" id="donenote"></p>
      <div class="field"><button class="btn p" id="adddone" type="button">Close</button></div></div>`;
    const sum = $("#donesum", d);
    if (connected) {
      sum.textContent = "Webhook connected.";
      if (last4) {
        const mask = document.createElement("span");
        mask.className = "dots";
        mask.textContent = " •••• " + last4;
        sum.appendChild(mask);
      }
    } else sum.textContent = HOOK_LATER;
    $("#donenote", d).textContent = (bot.name || "This bot") + " is on the station.";
    $("#adddone", d).onclick = () => { d.remove(); refreshBots(el); };
  };
  const step3 = (bot) => {
    dropToken(d);
    d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Connect webhook">${addStepper(3)}<div id="hookmount"></div></div>`;
    const ctl = mountWebhookStep($("#hookmount", d), bot, el, (connected, last4) => step4(bot, connected, last4));
    d._cancel = () => ctl.cancel();
  };
  const step2 = (bot, token) => {
    d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Voice token">${addStepper(2)}
      <h2>Voice token</h2>
      <p id="tokwhat"></p>
      <p id="tokwhere"></p>
      <p class="note" id="tokonce"></p>
      <div class="tokenbox" id="tok"></div>
      <div class="field"><button class="btn sm" id="copytok" type="button">Copy</button></div>
      <div class="field"><label class="inline" for="savedtok"><input type="checkbox" id="savedtok"> I've saved it</label></div>
      <div class="field"><button class="btn p" id="toknext" type="button" disabled>Continue</button></div></div>`;
    $("#tokwhat", d).textContent = TOKEN_WHAT;
    $("#tokwhere", d).textContent = TOKEN_WHERE;
    $("#tokonce", d).textContent = TOKEN_ONCE;
    const node = $("#tok", d);
    node.textContent = token;
    token = "";
    d._cancel = () => dropToken(d);
    $("#copytok", d).onclick = () => {
      const live = $("#tok", d);
      const value = live ? live.textContent : "";
      if (live && window.getSelection && document.createRange) {
        const range = document.createRange();
        range.selectNodeContents(live);
        const sel = window.getSelection();
        if (sel) { sel.removeAllRanges(); sel.addRange(range); }
      }
      copyText(value);
    };
    $("#savedtok", d).onchange = () => { $("#toknext", d).disabled = !$("#savedtok", d).checked; };
    $("#toknext", d).onclick = () => {
      if (!$("#savedtok", d).checked) return;
      dropToken(d);
      step3(bot);
    };
  };
  const step1 = () => {
    d._cancel = () => {};
    d.innerHTML = `<form class="card wizcard" role="dialog" autocomplete="off" aria-label="Add bot">${addStepper(1)}
      <h2>Add bot</h2><p class="sub">A new teammate. You'll get a voice token to paste into its routine, once.</p>
      <div class="field"><label for="aname">Name</label><input class="inp" id="aname" maxlength="40" required autocomplete="off"></div>
      <div class="field"><label for="aslug">Bot ID <span class="tipwrap info" tabindex="0" role="note" aria-label="${esc(BOT_ID_HELP)}">i<span class="tip">${esc(BOT_ID_HELP)}</span></span></label>
        <input class="inp mono" id="aslug" maxlength="24" autocomplete="off" spellcheck="false" required>
        <p class="note">${esc(BOT_ID_HELP)}</p>
        <p class="err" id="asluge" hidden></p></div>
      <div class="field"><label for="avoice">Voice</label>${voiceOptions(voices, "", "avoice")}</div>
      ${stepupOpen() ? `<p class="note stepup">${esc(stepupText())}</p>` : `<div class="field"><label for="apw">Admin password</label><input class="inp" type="password" id="apw" autocomplete="current-password" required></div>`}
      <div id="ape"></div>
      <div class="field inline"><button class="btn p" id="ago" type="submit">Add bot</button><button class="btn g" type="button" id="ano">Cancel</button></div></form>`;
    let slugEdited = false;
    const showId = () => {
      const slug = $("#aslug", d).value.trim();
      const problem = slug ? botIdProblem(slug) : "";
      const e = $("#asluge", d);
      if (e) { e.hidden = !problem; e.textContent = problem; }
      return problem;
    };
    $("#aname", d).oninput = () => { if (!slugEdited) $("#aslug", d).value = botIdFromName($("#aname", d).value); showId(); };
    $("#aslug", d).oninput = () => { slugEdited = true; showId(); };
    $("#ano", d).onclick = () => d.remove();
    d.querySelector("form").onsubmit = async ev => {
      ev.preventDefault();
      const name = $("#aname", d).value.trim();
      const slug = $("#aslug", d).value.trim();
      const problem = showId();
      if (problem) return;
      const voice = $("#avoice", d).value;
      const pwEl = $("#apw", d);
      let pw = pwEl ? pwEl.value : "";
      if (pwEl) pwEl.value = "";
      busyBtn($("#ago", d), true);
      const body = {name, slug, voice};
      if (pw) body.password = pw;
      pw = "";
      const r = await api("POST", "/api/bots", body);
      body.password = "";
      if (!r.ok) {
        if ($("#ago", d)) busyBtn($("#ago", d), false, "Add bot");
        if (r.status === 403 && !$("#apw", d)) {
          const holder = document.createElement("div");
          holder.className = "field";
          holder.innerHTML = `<label for="apw">Admin password</label><input class="inp" type="password" id="apw" autocomplete="current-password" required>`;
          $("#ape", d).before(holder);
        }
        return errBox($("#ape", d), (r.data && r.data.error) || "Couldn't add that bot.");
      }
      await refreshBots(el);
      const bot = {slug, name, voice, webhook: {configured: false, last4: null}};
      let token = String((r.data && r.data.voice_token) || "");
      if (r.data) r.data.voice_token = "";
      if (!token) {
        if ($("#ago", d)) busyBtn($("#ago", d), false, "Add bot");
        errBox($("#ape", d), "The voice service didn't return a voice token.");
        return;
      }
      if (!document.body.contains(d)) { token = ""; return; }
      step2(bot, token);
      token = "";
    };
  };
  step1();
}
async function pageBots(el) {
  const [r, s] = await Promise.all([api("GET", "/api/bots"), api("GET", "/api/enroll/status")]);
  const data = r.ok ? r.data : {bots: [], voices: [], message: (r.data && r.data.error) || "Can't reach the voice service right now."};
  const bots = data.bots || [];
  const n = st => bots.filter(b => (b.status || {}).state === st).length;
  const slot = $("#topr");
  if (slot) slot.innerHTML = `<button class="btn sm" id="avatars" type="button">Refresh avatars</button>`;
  el._voice = (s.data && s.data.buttons) || {};
  el._voices = data.voices || [];
  el.innerHTML = `${data.message ? `<div class="banner warn" id="bmsg">${esc(data.message)}</div>` : `<div class="banner warn" id="bmsg" hidden></div>`}
    <div class="card"><div class="inline"><span class="lbl" id="bcount">${n("connected")} connected · ${n("error")} errors · ${n("not_configured")} not configured</span>
      <span class="stepup" id="sulock" hidden></span>
      <button class="btn p sm right" id="add">Add bot</button></div>
      <div id="blist"></div></div>`;
  paintBots(el, bots);
  if (r.ok) noteStepup(data);
  every(15000, () => { if ($("#sulock", el)) noteStepup({stepup_until: S.stepupUntil}); });
  const av = $("#avatars");
  if (av) av.onclick = async () => {
    busyBtn(av, true, "Refreshing…");
    const x = await api("POST", "/api/bots/refresh-avatars", {});
    busyBtn(av, false, "Refresh avatars");
    if (!(x.ok || x.status === 202)) return toast((x.data && x.data.error) || "Couldn't refresh avatars.", "bad");
    await bustPortraits(el);
    toast(x.data && x.data.running ? "Avatar refresh already running." : "Refreshing avatars.");
    setTimeout(() => { if (document.body.contains(el)) bustPortraits(el); }, 2000);
  };
  every(3000, () => { if (document.body.contains(el) && $("#blist", el)) refreshBots(el); });
  $("#add", el).onclick = () => addBot(data.voices || [], el);
}
const PII_TEXT = "Always blocked, in every mode: passwords, keys, tokens, SSNs, and card numbers. When this is on, replies also block personal info: household names, emails, phones, addresses, IPs, MACs, and host names.";
async function pagePrivacy(el) {
  const r = await api("GET", "/api/privacy");
  const on = !(r.data && r.data.pii_mode === false);
  const msg = (r.data && r.data.message) || "";
  el.innerHTML = `<div class="card"><div class="row"><button class="tog${on ? " on" : ""}" role="switch" aria-checked="${on}" id="pii" aria-label="Block personal info in replies"></button>
      <div><div class="lbl">Block personal info in replies</div><div class="help">${esc((r.data && r.data.text) || PII_TEXT)}</div></div></div>
      ${msg ? `<p class="note">${esc(msg)}</p>` : ""}
      <div id="pe"></div></div>`;
  $("#pii", el).onclick = async () => {
    const next = !$("#pii", el).classList.contains("on");
    const send = async pw => {
      const body = {pii_mode: next};
      if (pw) body.password = pw;
      return api("PUT", "/api/privacy", body);
    };
    const apply = res => {
      if (!res || !res.ok) { errBox($("#pe", el), (res && res.data && res.data.error) || "Couldn't save."); return res; }
      $("#pii", el).classList.toggle("on", next);
      $("#pii", el).setAttribute("aria-checked", String(next));
      errBox($("#pe", el), "");
      toast(next ? "Personal info blocked in replies." : "Personal info can appear in replies.");
      return res;
    };
    if (stepupOpen()) {
      const res = await send("");
      if (res.status === 403) askPassword("Change privacy?", "Enter the admin password to change this.", "Save", async pw => apply(await send(pw)));
      else apply(res);
    } else askPassword("Change privacy?", "Enter the admin password to change this.", "Save", async pw => apply(await send(pw)));
  };
}
async function pageSystem(el) {
  el.innerHTML = `<div class="card" id="hl"><h2>Health</h2></div>
    <div class="card" id="tls"><h2>Security certificate</h2></div>
    <div class="card"><div class="inline"><h2>Logs</h2><span class="right inline"><select class="sel" id="lp" style="width:200px"><option value="grok-station-wake">Voice listener</option><option value="grok-station">Screen</option><option value="grok-station-admin">Admin page</option></select>
      <select class="sel" id="ls" style="width:120px"><option value="15m">15 min</option><option value="1h" selected>1 hour</option><option value="1d">1 day</option></select><button class="btn sm" id="lr">Show</button><a class="btn sm" id="ld">Download</a></span></div>
      <div class="log" id="log" style="margin-top:12px">Pick a part and press Show.</div></div>
    <div class="card"><h2>Restart</h2><div class="inline" style="margin-top:8px"><button class="btn" id="rv">Restart voice only</button><button class="btn dg" id="rb">Reboot station…</button><button class="btn g" id="again">Run setup again</button></div><div id="se"></div></div>
    <div class="card"><h2>Admin password</h2><form id="pw" autocomplete="off"><div class="field"><label for="old">Current password</label><input class="inp" type="password" id="old" autocomplete="current-password" required></div>
      ${pwFields("np1", "np2")}<div id="pe"></div><div class="field"><button class="btn p" id="pgo" disabled>Change password</button></div></form></div>`;
  wirePw("np1", "np2", $("#pgo", el));
  $("#pw", el).onsubmit = async ev => {
    ev.preventDefault();
    const r = await api("POST", "/api/password", {old: $("#old", el).value, new: $("#np1", el).value});
    $("#old", el).value = $("#np1", el).value = $("#np2", el).value = "";
    r.ok ? toast("Password changed. Other devices are signed out.") : errBox($("#pe", el), r.data.error);
  };
  const s = (await api("GET", "/api/system")).data || {};
  const hh = s.health || {};
  const t = s.tls || {};
  $("#tls", el).innerHTML = `<h2>Security certificate</h2><p class="help">This station made its own certificate for ${esc(t.url || "this page")}. If your browser warns about it, check that the browser's SHA-256 fingerprint matches this one exactly (it's also shown on the station screen for 10 minutes after a new certificate is made), then trust it.</p>
    <div class="row"><span>SHA-256</span><span class="right mono" style="word-break:break-all;max-width:70%;text-align:right">${t.fingerprint ? esc(t.fingerprint) : "n/a"}</span></div>`;
  $("#hl", el).innerHTML = `<h2>Health</h2>
    <div class="row"><span>CPU temperature</span><span class="right mono">${hh.cpu_c != null ? hh.cpu_c + " °C" : "n/a"}</span></div>
    <div class="row"><span>Uptime</span><span class="right mono">${hh.uptime_s != null ? Math.floor(hh.uptime_s / 86400) + "d " + Math.floor(hh.uptime_s % 86400 / 3600) + "h" : "n/a"}</span></div>
    <div class="row"><span>Memory used</span><span class="right mono">${hh.mem_used_pct != null ? hh.mem_used_pct + "%" : "n/a"}</span></div>
    <div class="row"><span>Disk free</span><span class="right mono">${hh.disk_free_gb != null ? hh.disk_free_gb + " GB" : "n/a"}</span></div>
    ${Object.entries(s.services || {}).map(([k, v]) => `<div class="row"><span>${esc(k)}</span><span class="right pill ${v === "active" ? "ok" : "bad"}"><i></i>${esc(v)}</span></div>`).join("")}
    <div class="row"><span>Network</span><span class="right help">${s.network && s.network.wifi ? "Wi-Fi " + esc(s.network.wifi.ssid) + " · " + s.network.wifi.signal + "%" : s.network && s.network.wired ? "Wired" : "Offline"}</span></div>`;
  const logs = async () => {
    const q = `part=${$("#lp", el).value}&since=${$("#ls", el).value}`;
    $("#ld", el).href = "/api/system/logs?download=1&" + q;
    const r = await api("GET", "/api/system/logs?" + q);
    $("#log", el).textContent = r.ok ? (r.data.lines || []).join("\n") || "Nothing in that time." : r.data.error;
  };
  $("#lr", el).onclick = logs;
  $("#again", el).onclick = async () => { await api("POST", "/api/wizard", {step: 2, done: false}); boot(); };
  $("#rv", el).onclick = async () => { const r = await api("POST", "/api/system/restart-voice", {}); r.ok ? toast("Voice restarted.") : errBox($("#se", el), r.data.error); };
  $("#rb", el).onclick = () => {
    const d = document.createElement("div"); d.className = "sheet";
    d.innerHTML = `<div class="card" role="dialog"><h2>Reboot the station?</h2><p class="sub">It's back in about a minute. A conversation in progress is cut off.</p>
      <div id="re"></div><div class="field inline"><button class="btn d" id="ry">Reboot</button><button class="btn g" id="rn">Cancel</button></div></div>`;
    document.body.appendChild(d);
    $("#rn", d).onclick = () => d.remove();
    $("#ry", d).onclick = async () => {
      let r = await api("POST", "/api/system/reboot", {confirm: true});
      if (r.status === 409 && confirm("A conversation or enrollment is running. Reboot anyway?")) r = await api("POST", "/api/system/reboot", {confirm: true, force: true});
      if (r.ok) { d.remove(); toast("Rebooting. This page reconnects when it's back."); } else errBox($("#re", d), r.data.error);
    };
  };
}
function pageMore(el) {
  const items = [["voice", "Voice"], ["updates", "Updates"], ["bots", "Bots"], ["privacy", "Privacy"], ["display", "Display"], ["time", "Time and location"], ["network", "Network"]];
  el.innerHTML = `<div class="card">${items.map(([k, t]) => `<a class="row" href="#${k}" style="color:inherit;text-decoration:none">${esc(t)}<span class="right inline">${k === "updates" ? `<b class="count" data-upbadge hidden></b>` : ""}<span class="help">›</span></span></a>`).join("")}
    <div class="row"><button class="btn g" id="again">Run setup again</button></div><div class="row"><button class="btn g" id="out2">Sign out</button></div></div>`;
  $("#again", el).onclick = async () => { await api("POST", "/api/wizard", {step: 2, done: false}); boot(); };
  $("#out2", el).onclick = async () => { await api("POST", "/api/logout", {}); boot(); };
  refreshUpdateBadge();
}

boot();
