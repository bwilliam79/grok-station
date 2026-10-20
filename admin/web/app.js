/* grok-station admin (ADMIN-UI.md / design.md). Plain JS, no build, no storage: secrets never touch the browser beyond the
   field they are typed into, and every field is cleared after submit. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const root = () => $("#root");
let S = {state: null, page: "home", timers: []};
const PERSONA = s => s ? s[0].toUpperCase() + s.slice(1) : "";
const WORD = n => "Hey " + PERSONA(String(n).replace(/^hey_/, "").replace(/_/g, " "));

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
function toast(msg, kind = "ok") {
  const t = $("#toast");
  t.innerHTML = `<div class="banner ${kind}" role="status">${esc(msg)}</div>`;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.innerHTML = ""), 4000);
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
    <div class="phonestep" style="display:block">Step 1 of 7 · Code and password</div>
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
    <div class="field"><label for="u">Username</label><input class="inp" id="u" value="admin" autocomplete="username" autocapitalize="none" required></div>
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
    const r = await api("POST", "/api/login", {username: $("#u").value.trim(), password: $("#p").value});
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

async function relayPanel(el, opts = {}) {  // secrets pattern §8
  const r = await api("GET", "/api/secrets");
  const s = r.data || {}, t = s.relay_token || {};
  el.innerHTML = `<div class="card"><h2>Voice relay</h2><p class="sub">The station connects to the voice service over mutual TLS with its own token.</p>
    <div class="field"><label for="ru">Relay address</label><input class="inp mono" id="ru" value="${esc((s.relay_url || {}).value)}" placeholder="wss://voice-host:8443/" spellcheck="false"></div>
    <div class="row" style="margin-top:14px"><div class="secret">
      <span class="lbl">🔒 Station token</span>
      ${t.set ? `<span class="mono">••••${esc(t.last4 || "")}</span><span class="pill ok"><i></i>Saved</span><span class="help">${t.saved_at ? "saved " + ago(t.saved_at) : ""}${t.last_test ? " · checked " + ago(t.last_test.at) : ""}</span>`
              : `<span class="pill warn"><i></i>Not set</span>`}</div>
      <div class="right inline"><button class="btn sm" id="rt">Test</button><button class="btn sm" id="rr">Replace</button></div></div>
    <div id="rep" hidden><div class="field"><label for="tok">New token</label><input class="inp mono" type="password" id="tok" autocomplete="off" spellcheck="false">
      <p class="note" id="toklen">Paste the token from the voice service computer. Only its length is shown.</p></div></div>
    <div class="field inline"><button class="btn p" id="rs">Test and save</button><button class="btn g" id="rc" hidden>Cancel</button></div>
    <div id="res"></div>
    <p class="note">The old token keeps working until the new one tests OK. Tokens never appear in logs, the page address or your browser storage.</p></div>`;
  const tok = $("#tok", el);
  tok.oninput = () => ($("#toklen", el).textContent = tok.value ? `${tok.value.length} characters` : "Paste the token from the voice service computer. Only its length is shown.");
  $("#rr", el).onclick = () => { $("#rep", el).hidden = false; $("#rc", el).hidden = false; tok.focus(); };
  $("#rc", el).onclick = () => { tok.value = ""; $("#rep", el).hidden = true; $("#rc", el).hidden = true; };
  const show = (res, saved) => {
    const c = res.data.checks || {};
    const item = (ok, txt) => `<span class="pill ${ok ? "ok" : ok === false ? "bad" : "off"}"><i></i>${txt}</span>`;
    $("#res", el).innerHTML = `<div class="banner ${res.ok ? "ok" : "bad"}" style="flex-wrap:wrap">${esc(res.ok ? (saved ? "Saved and connected." : "Connection works.") : res.data.error)}
      ${item(c.tls, "Secure connection")} ${item(c.token, "Station token")} ${item(c.xai_key, "xAI key on the voice service")}
      ${c.client_cert_expires ? `<span class="help">Certificate valid until ${esc(c.client_cert_expires)}</span>` : ""}</div>`;
  };
  $("#rt", el).onclick = async () => { busyBtn($("#rt", el), true, "Testing…"); const res = await api("POST", "/api/secrets/relay_token/test", {}); busyBtn($("#rt", el), false, "Test"); show(res); };
  $("#rs", el).onclick = async () => {
    const body = {url: $("#ru", el).value.trim()};
    if (tok.value) body.value = tok.value;
    busyBtn($("#rs", el), true, "Testing…");
    const res = await api("POST", "/api/secrets/relay_token", body);
    tok.value = "";
    busyBtn($("#rs", el), false, "Test and save");
    show(res, res.ok);
    if (res.ok) { opts.onOk && opts.onOk(); setTimeout(() => relayPanel(el, opts), 2500); }
  };
}
function xaiPanel(el) {
  el.innerHTML = `<div class="card"><h2>xAI key</h2>
    <div class="row"><div class="secret"><span class="lbl">🔒 xAI API key</span><span class="help">Stored on the voice service computer, never on the station.</span></div></div>
    <p class="sub" style="margin-top:8px">The station never holds the key. Change it in the relay's secrets on the voice service computer, then use <b>Test</b> on the relay card: it confirms the key is present there.</p></div>`;
}

async function timePanel(el, opts = {}) {
  const [g, z] = await Promise.all([api("GET", "/api/settings/time"), api("GET", "/api/timezones")]);
  const v = (g.data || {}).values || {};
  el.innerHTML = `<div class="card"><h2>Time and location</h2><p class="sub">Used for the clock, the weather on the station, and Grok's time and weather answers.</p>
    <div class="field"><label for="tz">Time zone</label><select class="sel" id="tz">${(z.data.zones || []).map(x => `<option${x === v.timezone ? " selected" : ""}>${esc(x)}</option>`).join("")}</select></div>
    <div class="field"><label for="town">Town or ZIP</label><div class="inline" style="flex-wrap:nowrap"><input class="inp" id="town" placeholder="Town or ZIP code"><button class="btn sm" id="find">Find</button></div>
      <div id="found" class="note">${v.GROK_STATION_LAT ? `Current location: ${esc(v.GROK_STATION_LAT)}, ${esc(v.GROK_STATION_LON)}` : "No location yet."}</div></div>
    <div class="field"><label for="un">Temperature</label><select class="sel" id="un"><option value="fahrenheit">°F</option><option value="celsius"${v.GROK_STATION_UNITS === "celsius" ? " selected" : ""}>°C</option></select></div>
    <div id="te"></div><div class="field inline"><button class="btn p" id="tsave">Save</button></div></div>`;
  let pick = null;
  $("#find", el).onclick = async () => {
    const r = await api("GET", "/api/geocode?q=" + encodeURIComponent($("#town", el).value));
    if (!r.ok) return errBox($("#te", el), r.data.error);
    const res = r.data.results || [];
    if (!res.length) return ($("#found", el).textContent = "Nothing found. Try a nearby town.");
    $("#found", el).innerHTML = res.map((x, i) => `<label class="row" style="cursor:pointer"><input type="radio" name="pl" value="${i}"${i ? "" : " checked"}> ${esc(x.name)}${x.admin1 ? ", " + esc(x.admin1) : ""} ${esc(x.country || "")}</label>`).join("");
    pick = res[0];
    el.querySelectorAll("[name=pl]").forEach(rb => rb.onchange = () => { pick = res[+rb.value]; if (pick.timezone) $("#tz", el).value = pick.timezone; });
    if (pick.timezone) $("#tz", el).value = pick.timezone;
  };
  $("#tsave", el).onclick = async () => {
    const body = {timezone: $("#tz", el).value, GROK_STATION_UNITS: $("#un", el).value};
    if (pick) { body.GROK_STATION_LAT = String(pick.lat); body.GROK_STATION_LON = String(pick.lon); }
    const r = await api("PUT", "/api/settings/time", body);
    if (!r.ok) return errBox($("#te", el), r.data.error);
    errBox($("#te", el), ""); toast("Saved."); opts.onOk && opts.onOk();
  };
}

async function audioPanel(el) {
  const [a, s] = await Promise.all([api("GET", "/api/audio"), api("GET", "/api/settings/audio")]);
  const v = (s.data || {}).values || {}, d = a.data || {};
  const devs = [...new Set([...(d.inputs || []), ...(d.outputs || []), v.WAKE_DEVICE_NAME].filter(Boolean))];
  el.innerHTML = `<div class="card"><h2>Audio</h2>
    <div class="field"><label for="dev">Microphone and speaker</label><select class="sel" id="dev">${devs.map(x => `<option${x === v.WAKE_DEVICE_NAME ? " selected" : ""}>${esc(x)}</option>`).join("")}</select>
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

async function wakePanel(el, opts = {}) {
  const r = await api("GET", "/api/wakewords");
  const words = (r.data || {}).words || [];
  el.innerHTML = `<div class="card"><div class="inline"><h2>Wake words</h2><button class="btn p sm right" id="rec">Record a wake word</button></div>
    <p class="sub">Each word opens one teammate. Built-in words work right away; new ones need about a minute of your voice.</p>
    ${words.map(w => `<div class="row" style="flex-wrap:wrap">
      <button class="tog${w.enabled ? " on" : ""}" role="switch" aria-checked="${w.enabled}" aria-label="${esc(WORD(w.name))} on" data-tog="${esc(w.name)}"${w.backup ? ` disabled aria-disabled="true" title="Jarvis is the station's default wake word and can't be turned off"` : !w.has_model ? " disabled" : ""}></button>
      <div style="min-width:160px"><div class="lbl">${esc(WORD(w.name))}</div><div class="help">Opens ${esc(PERSONA(w.persona))}${w.backup ? " · backup word, always on" : ""}</div></div>
      <span class="pill ${w.backup ? "off" : w.enrolled ? "ok" : w.has_model ? (w.provisional ? "busy" : "ok") : "warn"}"><i></i>${w.backup ? "Backup" : w.enrolled ? "Your voice" : w.has_model ? (w.provisional ? "Starter model" : "Ready") : "Needs your voice"}</span>
      ${w.metrics ? `<span class="help mono">recall ${esc(w.metrics.recall)} · ${esc(w.metrics.fa_per_hour)}/h false</span>` : ""}
      ${w.backup ? "" : `<label class="inline right help" style="min-width:220px">Sensitivity <input type="range" min="5" max="95" value="${Math.round(w.sensitivity * 100)}" data-sens="${esc(w.name)}"${w.has_model ? "" : " disabled"}></label>
      <button class="btn sm" data-enroll="${esc(w.name)}">Record voice</button>`}</div>`).join("")}
    ${opts.skip ? "" : `<p class="note">Hey Jarvis is the backup word: it always works, even if a new word isn't trained yet.</p>`}<div id="we"></div></div>`;
  el.querySelectorAll("[data-tog]").forEach(b => b.onclick = async () => {
    const on = !b.classList.contains("on");
    const res = await api("PUT", "/api/wakewords/" + b.dataset.tog, {enabled: on});
    res.ok ? (b.classList.toggle("on", on), b.setAttribute("aria-checked", on), toast(`${WORD(b.dataset.tog)} ${on ? "on" : "off"}.`)) : errBox($("#we", el), res.data.error);
  });
  el.querySelectorAll("[data-sens]").forEach(s => s.onchange = async () => {
    const res = await api("PUT", "/api/wakewords/" + s.dataset.sens, {sensitivity: +s.value / 100});
    res.ok ? toast("Saved.") : errBox($("#we", el), res.data.error);
  });
  const names = words.filter(w => !w.backup).map(w => w.name);
  el.querySelectorAll("[data-enroll]").forEach(b => b.onclick = () => enrollDrawer(b.dataset.enroll, names, () => wakePanel(el, opts)));
  $("#rec", el).onclick = () => enrollDrawer(null, names, () => wakePanel(el, opts));
}

async function enrollDrawer(phrase, names, done) {  // §9: phrase+teammate, speaker (Pi-only names), clip count, record
  const sp = (await api("GET", "/api/wakewords")).data.speakers || [];
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<div class="drawer" role="dialog" aria-label="Record a wake word"><div class="inline"><h2 style="font:600 22px var(--disp)">Record a wake word</h2><button class="btn g sm right" id="dx">Close</button></div>
    <div class="field"><label for="ph">Wake word</label><select class="sel" id="ph">${names.map(n => `<option value="${esc(n)}"${n === phrase ? " selected" : ""}>${esc(WORD(n))}</option>`).join("")}</select>
      <p class="note">New words are added on the voice service computer first; then they show up here.</p></div>
    <div class="field"><label for="spk">Whose voice?</label><select class="sel" id="spk"><option value="">Choose…</option>${sp.map(s => `<option value="${esc(s.id)}">${esc(s.name)}${s.guest ? " (guest)" : ""}</option>`).join("")}<option value="+">+ Add speaker</option></select>
      <p class="note">Speaker names stay on this station. The training computer only sees an ID like <span class="mono">spk-3f9a</span>. Ask guests first; their clips are deleted after training.</p>
      <div id="addsp" hidden class="inline" style="margin-top:8px"><input class="inp" id="spn" maxlength="32" placeholder="Name" style="flex:1"><label class="help"><input type="checkbox" id="spg"> Guest</label><button class="btn sm" id="spa">Add</button></div></div>
    <div class="field"><label for="cnt">Clips <span class="help" id="cntv"></span></label><input type="range" id="cnt" min="10" max="40" step="1" value="10"></div>
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
    d.t = setInterval(async () => {
      const s = await api("GET", "/api/enroll");
      const last = s.data.last || {};
      $("#prog", d).innerHTML = s.data.enrolling ? `<div class="banner ok"><span class="pill ok"><i></i>Recording</span> ${esc(WORD(last.phrase || ""))} · ${esc(last.count || "")} clips. Follow the station screen.</div>`
        : `<div class="banner busy"><span class="pill busy"><i></i>Queued for training</span> Results appear on this page when training finishes.</div>`;
      if (!s.data.enrolling) { clearInterval(d.t); $("#dgo", d).textContent = "Done"; }
    }, 2000);
  };
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

/* ---------------------------------------------------------------- wizard (design.md §8.2 order) */
const STEPS = ["Code and password", "Wi-Fi", "xAI key", "Voice relay", "Time and location", "Wake words", "Audio test"];
async function wizard(step) {
  clearTimers();
  step = Math.max(2, Math.min(8, step));
  const hotspot = S.state.mode === "hotspot";
  if (step === 8) {
    root().innerHTML = `<div class="center"><div class="card auth"><div class="mk"></div><h2 style="font-size:24px">All set</h2>
      <p class="sub">Say <b>Hey Grok</b> to try it. Optional next: <a href="#" data-p="bots">Bots</a> · <a href="#" data-p="display">Display</a>.</p>
      <div class="field"><button class="btn p" id="fin">Go to Home</button></div></div></div>`;
    const fin = async p => { await api("POST", "/api/wizard", {step: 8, done: true}); S.page = p; boot(); };
    $("#fin").onclick = () => fin("home");
    document.querySelectorAll("[data-p]").forEach(a => a.onclick = ev => { ev.preventDefault(); fin(a.dataset.p); });
    return;
  }
  root().innerHTML = `<div class="wiz"><nav class="rail" aria-label="Setup steps">${STEPS.map((t, i) => `<div class="st${i + 1 < step ? " done" : ""}${i + 1 === step ? " on" : ""}"><b>${i + 1 < step ? "✓" : i + 1}</b>${t}</div>`).join("")}</nav>
    <div class="pane"><div class="phonestep">Step ${step} of 7 · ${STEPS[step - 1]}</div><div class="meterbar phonestep" style="height:6px"><b style="width:${step / 7 * 100}%"></b></div>
      <h1 style="font:600 28px var(--disp);margin-bottom:16px">${STEPS[step - 1]}</h1><div id="wp"></div>
      <div class="inline" style="margin-top:20px">${step > 2 ? `<button class="btn g" id="back">Back</button>` : ""}
        <span class="right inline">${step === 2 && !hotspot ? `<button class="btn g" id="skip">Skip (wired)</button>` : ""}${step === 6 ? `<button class="btn g" id="skip">Skip, record later</button>` : ""}
        <button class="btn p" id="next">Next</button></span></div></div></div>`;
  const wp = $("#wp"), next = $("#next");
  const go = async s => { await api("POST", "/api/wizard", {step: s, done: false}); wizard(s); };
  if ($("#back")) $("#back").onclick = () => go(step - 1);
  if ($("#skip")) $("#skip").onclick = () => go(step + 1);
  next.onclick = () => go(step + 1);
  if (step === 2) {
    const st = (await api("GET", "/api/network")).data || {};
    next.disabled = hotspot && !(st.wifi);
    await wifiPanel(wp, {hostname: true, onJoin: () => (next.disabled = false)});
    if (hotspot) wp.insertAdjacentHTML("afterbegin", `<div class="banner warn" style="margin-bottom:12px">The station is running its own setup hotspot. Join your home Wi-Fi to continue; the hotspot then turns off and this page moves to your network.</div>`);
  } else if (step === 3) {
    xaiPanel(wp);
  } else if (step === 4) {
    const s = (await api("GET", "/api/secrets")).data || {};
    next.disabled = !(s.relay_token || {}).set;
    await relayPanel(wp, {onOk: () => (next.disabled = false)});
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
  audio: '<path d="M11 5L6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>', display: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  time: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', network: '<path d="M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/>',
  system: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  more: '<circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/>'};
const svg = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>`;
const PAGES = {home: ["Home", pageHome], voice: ["xAI key and relay", pageVoice], wake: ["Wake words", el => wakePanel(el)], bots: ["Bots", pageBots],
  audio: ["Audio", el => audioPanel(el)], display: ["Display", el => displayPanel(el)], time: ["Time and location", el => timePanel(el)],
  network: ["Network", el => wifiPanel(el)], system: ["System", pageSystem], more: ["More", pageMore]};
const NAV = [["", [["home", "Home"]]], ["Voice service", [["voice", "xAI key and relay"]]],
  ["Station", [["wake", "Wake words"], ["bots", "Bots"], ["audio", "Audio"], ["display", "Display"], ["time", "Time and location"], ["network", "Network"]]],
  ["", [["system", "System"]]]];
const TABS = [["home", "Home", "home"], ["wake", "Voice", "wake"], ["audio", "Station", "audio"], ["system", "System", "system"], ["more", "More", "more"]];

function closeFloats() {
  document.querySelectorAll("body > .drawer, body > .sheet").forEach(n => { if (n._cancel) n._cancel(); n.remove(); });
}
function shell() {
  closeFloats();
  const page = (location.hash.slice(1) in PAGES) ? location.hash.slice(1) : S.page;
  S.page = page;
  root().innerHTML = `<div class="app"><aside class="side"><div class="brand"><img class="mk" src="/assets/mark.svg" alt=""><div><b>Grok Station</b><span>● ${esc(S.state.host)}</span></div></div>
    ${NAV.map(([g, items]) => (g ? `<div class="grp">${g}</div>` : "") + items.map(([k, t]) => `<a class="nav${k === page ? " on" : ""}" href="#${k}" title="${t}">${svg(k)}<span>${t}</span></a>`).join("")).join("")}
    <div class="foot">Signed in as admin · <a href="#" id="out">Sign out</a><br>Version <span class="mono">${esc(S.state.version)}</span></div></aside>
    <main class="main"><header class="top"><h1>${PAGES[page][0]}</h1></header><div class="body" id="pg"></div></main></div>
    <nav class="phonebar">${TABS.map(([k, t, i]) => `<button data-tab="${k}" class="${k === page ? "on" : ""}">${svg(i)}${t}</button>`).join("")}</nav>`;
  $("#out").onclick = async ev => { ev.preventDefault(); await api("POST", "/api/logout", {}); boot(); };
  document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => (location.hash = b.dataset.tab));
  clearTimers();
  PAGES[page][1]($("#pg"));
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
      ["Voice relay", h.relay ? "Connected" : "Tap Test on the relay page", "voice"],
      ["Network", n.wifi ? `Wi-Fi ${esc(n.wifi.ssid)} · ${n.wifi.signal}%` : n.wired ? "Wired" : "Offline", "network"],
      ["Services", up ? "All running" : Object.entries(svc).filter(([, v]) => v !== "active").map(([k]) => esc(k.replace("grok-station-", ""))).join(", ") + " stopped", "system"],
      ["Wake words", `${(h.words || []).length} on`, "wake"]].map(([t, v, p]) => `<a class="tile" href="#${p}" style="text-decoration:none;color:inherit"><div class="help">${t}</div><div style="font:600 17px var(--disp);margin-top:4px">${v}</div></a>`).join("");
  };
  every(10000, load);
  every(5000, () => { const p = $("#pv", el); if (p) { p.src = "/api/preview?t=" + Date.now(); $("#pva", el).textContent = "Updated " + new Date().toLocaleTimeString(); } });
}
async function pageVoice(el) {
  el.innerHTML = `<div class="cols"><div id="x"></div><div id="r"></div></div>`;
  xaiPanel($("#x", el)); relayPanel($("#r", el));
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
function slugify(name) {
  let s = String(name || "").normalize("NFKD").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (!/^[a-z]/.test(s)) s = ("b" + s).replace(/^-+/, "");
  s = s.slice(0, 24).replace(/-+$/g, "");
  if (s === "grok") s = "grok-bot";
  return s.length >= 2 ? s : "";
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
function avatar(slug, name, lg) {
  const letter = esc(String(name || slug || "?").trim().slice(0, 1).toUpperCase() || "?");
  return `<span class="avwrap${lg ? " lg" : ""}"><img class="av" alt="" src="/api/portrait/${esc(slug)}" onerror="this.remove()"><span class="monoav">${letter}</span></span>`;
}
function askPassword(title, text, label, send) {
  const d = document.createElement("div");
  d.className = "sheet";
  d.innerHTML = `<form class="card" role="dialog" autocomplete="off" style="width:min(400px,100%)">
    <h2>${esc(title)}</h2><p class="sub">${esc(text)}</p>
    <div class="field"><label for="pwv">Admin password</label><input class="inp" type="password" id="pwv" autocomplete="current-password" required></div>
    <div id="pwe"></div>
    <div class="field inline"><button class="btn p" id="pwgo">${esc(label)}</button><button class="btn g" type="button" id="pwno">Cancel</button></div></form>`;
  document.body.appendChild(d);
  const close = () => d.remove();
  $("#pwno", d).onclick = close;
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
  list.innerHTML = bots.map(b => `<div class="botrow">${avatar(b.slug, b.name)}
    <div style="flex:1;min-width:0"><div class="lbl">${esc(b.name)}</div><div style="margin-top:4px">${botPill(b)}</div></div>
    <button class="btn sm" data-bot="${esc(b.slug)}">Configure</button></div>`).join("") || `<p class="help">No bots yet.</p>`;
  list.querySelectorAll("[data-bot]").forEach(btn => btn.onclick = () => openBot(bots.find(b => b.slug === btn.dataset.bot), el));
}
async function refreshBots(el) {
  const r = await api("GET", "/api/bots");
  if (!r.ok || !$("#blist", el)) return null;
  const msg = $("#bmsg", el);
  if (msg) { msg.hidden = !r.data.message; if (r.data.message) msg.textContent = r.data.message; }
  paintBots(el, r.data.bots || []);
  return r.data;
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
      <div style="min-width:0"><div class="lbl">${esc(bot.name)}</div><div style="margin-top:4px">${botPill(bot)}</div>
        ${bot.voice ? `<div class="help">Voice: ${esc(bot.voice)}</div>` : ""}</div>
      <button class="btn g sm right" id="dx">Close</button></div>
    <p class="note">Copy these from the bot's routine panel in Grok Bot.</p>
    <div id="saved"${saved ? "" : " hidden"} class="secret"><span class="dots">•••• ${esc((bot.webhook && bot.webhook.last4) || "")}</span>
      <span class="pill ok"><i></i>Saved</span><button class="btn sm" id="repl" type="button">Replace</button></div>
    <div id="fields"${saved ? " hidden" : ""}>
      <div class="field"><label for="burl">Webhook URL</label><input class="inp" id="burl" type="url" inputmode="url" autocomplete="off" spellcheck="false" autocapitalize="off"></div>
      <div class="field"><label for="bkey">Webhook key</label><input class="inp" id="bkey" type="password" autocomplete="off" spellcheck="false" autocapitalize="off"></div>
      <p class="note" id="pasted" hidden>Pasted · not saved</p>
    </div>
    <div class="field inline"><button class="btn" id="test" type="button">Test</button><button class="btn p" id="save" type="button" disabled>Save</button></div>
    <div id="tbox"></div>
    ${saved ? `<div class="field"><button class="btn dg" id="delhook" type="button">Remove…</button></div>` : ""}
    <div class="botfoot"><button class="btn g sm" id="ren" type="button">Rename</button><button class="btn dg sm" id="delbot" type="button">Remove bot</button></div>`;
  document.body.appendChild(d);
  const fieldsOn = () => { $("#saved", d).hidden = true; $("#fields", d).hidden = false; };
  const sync = () => {
    const url = $("#burl", d).value.trim(), key = $("#bkey", d).value, dirty = url.length + key.length > 0;
    $("#pasted", d).hidden = !dirty;
    $("#save", d).disabled = !(url && key);
    $("#test", d).disabled = !!testId || (dirty ? !(url && key) : !(bot.webhook && bot.webhook.configured));
  };
  if (!saved) fieldsOn();
  $("#repl", d) && ($("#repl", d).onclick = () => { $("#burl", d).value = ""; $("#bkey", d).value = ""; fieldsOn(); sync(); });
  $("#burl", d).oninput = $("#bkey", d).oninput = sync;
  sync();
  $("#dx", d).onclick = async () => { await cancelTest(); d.remove(); };
  const showResult = (res) => {
    stopPoll();
    testId = null;
    const btn = $("#test", d);
    if (btn) { btn.textContent = "Test"; btn.disabled = false; }
    sync();
    const box = $("#tbox", d);
    if (!box) return;
    if (res.state === "ok") {
      const secs = res.answered_in_s == null ? "" : oneDecimal(res.answered_in_s) + " s";
      box.innerHTML = `<div class="banner ok"><span class="pill ok"><i></i>Answered in ${esc(secs)}</span></div>`;
      return;
    }
    if (res.state === "cancelled") { box.innerHTML = `<p class="note">Cancelled.</p>`; return; }
    const err = res.error || "No answer after 2 min";
    const hint = !res.error || res.error === "No answer after 2 min";
    box.innerHTML = `<div class="banner bad"><span class="pill bad"><i></i>${esc(err)}</span></div>` +
      (hint ? `<p class="note">Check both values are from this bot's routine panel, then test again.</p>` : "");
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
    const url = $("#burl", d).value.trim();
    const key = $("#bkey", d).value;
    if (!(url && key)) return;
    askPassword("Save webhook?", "Enter the admin password to save this webhook.", "Save", async pw => {
      $("#bkey", d).value = "";
      sync();
      const r = await api("PUT", `/api/bots/${encodeURIComponent(bot.slug)}/webhook`, {url, key, password: pw});
      if (r.ok) {
        bot.webhook = {configured: true, last4: r.data.last4, saved_at: Math.floor(Date.now() / 1000)};
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
    });
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
    box.innerHTML = `<form class="card" role="dialog" autocomplete="off" style="width:min(400px,100%)">
      <h2>Rename</h2><p class="sub">${esc(bot.name)}</p>
      <div class="field"><label for="rname">Name</label><input class="inp" id="rname" maxlength="40" required value="${esc(bot.name)}"></div>
      <div class="field"><label for="rpw">Admin password</label><input class="inp" type="password" id="rpw" autocomplete="current-password" required></div>
      <div id="rpe"></div>
      <div class="field inline"><button class="btn p" id="rgo">Rename</button><button class="btn g" type="button" id="rno">Cancel</button></div></form>`;
    document.body.appendChild(box);
    $("#rno", box).onclick = () => box.remove();
    box.querySelector("form").onsubmit = async ev => {
      ev.preventDefault();
      const name = $("#rname", box).value.trim();
      const pw = $("#rpw", box).value;
      $("#rpw", box).value = "";
      const r = await api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, {name, password: pw});
      if (!r.ok) return errBox($("#rpe", box), r.data.error);
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
function addBot(voices, el) {
  closeFloats();
  const d = document.createElement("div");
  d.className = "sheet";
  const opts = (voices || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  d.innerHTML = `<form class="card" role="dialog" autocomplete="off" style="width:min(440px,100%)">
    <h2>Add bot</h2><p class="sub">A new teammate. You'll get a voice token to paste into its routine, once.</p>
    <div class="field"><label for="aname">Name</label><input class="inp" id="aname" maxlength="40" required autocomplete="off"></div>
    <div class="field"><label for="aslug">Slug</label><input class="inp mono" id="aslug" maxlength="24" pattern="[a-z][a-z0-9-]{1,23}" required autocomplete="off" spellcheck="false"></div>
    <div class="field"><label for="avoice">Voice</label><select class="sel" id="avoice" required>${opts || `<option value="">No voices yet</option>`}</select></div>
    <div class="field"><label for="apw">Admin password</label><input class="inp" type="password" id="apw" autocomplete="current-password" required></div>
    <div id="ape"></div>
    <div class="field inline"><button class="btn p" id="ago">Add bot</button><button class="btn g" type="button" id="ano">Cancel</button></div></form>`;
  document.body.appendChild(d);
  let slugEdited = false;
  $("#aname", d).oninput = () => { if (!slugEdited) $("#aslug", d).value = slugify($("#aname", d).value); };
  $("#aslug", d).oninput = () => { slugEdited = true; };
  $("#ano", d).onclick = () => d.remove();
  d.querySelector("form").onsubmit = async ev => {
    ev.preventDefault();
    const name = $("#aname", d).value.trim();
    const slug = $("#aslug", d).value.trim();
    const voice = $("#avoice", d).value;
    const pw = $("#apw", d).value;
    $("#apw", d).value = "";
    busyBtn($("#ago", d), true);
    const r = await api("POST", "/api/bots", {name, slug, voice, password: pw});
    if (!r.ok) { busyBtn($("#ago", d), false, "Add bot"); return errBox($("#ape", d), r.data.error); }
    refreshBots(el);
    const token = r.data.voice_token || "";
    d.querySelector(".card").innerHTML = `<h2>Voice token</h2>
      <p class="sub">Give this to ${esc(name)}'s routine as its voice token. It won't be shown again.</p>
      <div class="tokenbox" id="tok">${esc(token)}</div>
      <div class="field inline"><button class="btn sm" id="copy" type="button">Copy</button><button class="btn g sm" id="done" type="button">Close</button></div>`;
    $("#copy", d).onclick = async () => {
      const t = $("#tok", d).textContent;
      try { await navigator.clipboard.writeText(t); toast("Copied."); }
      catch (e) { toast("Select the token and copy it.", "warn"); }
    };
    $("#done", d).onclick = () => { d.remove(); refreshBots(el); };
  };
}
async function pageBots(el) {
  const [r, g] = await Promise.all([api("GET", "/api/bots"), api("GET", "/api/guest")]);
  const data = r.ok ? r.data : {bots: [], voices: [], message: (r.data && r.data.error) || "Can't reach the voice service right now."};
  const guest = !!(g.data || {}).guest;
  const bots = data.bots || [];
  const n = s => bots.filter(b => (b.status || {}).state === s).length;
  el.innerHTML = `${data.message ? `<div class="banner warn" id="bmsg">${esc(data.message)}</div>` : `<div class="banner warn" id="bmsg" hidden></div>`}
    <div class="card"><div class="inline"><span class="lbl" id="bcount">${n("connected")} connected · ${n("error")} errors · ${n("not_configured")} not configured</span>
      <button class="btn p sm right" id="add">Add bot</button></div>
      <div id="blist"></div></div>
    <div class="card"><div class="row"><button class="tog${guest ? " on" : ""}" role="switch" aria-checked="${guest}" id="guest" aria-label="Guest mode"></button>
      <div><div class="lbl">Guest mode</div><div class="help">On while guests are over.</div></div></div></div>`;
  paintBots(el, bots);
  $("#add", el).onclick = () => addBot(data.voices || [], el);
  $("#guest", el).onclick = async () => {
    const on = !$("#guest", el).classList.contains("on");
    const x = await api("PUT", "/api/guest", {guest: on});
    if (x.ok) { $("#guest", el).classList.toggle("on", on); $("#guest", el).setAttribute("aria-checked", String(on)); toast(on ? "Guest mode on." : "Guest mode off."); }
    else toast((x.data && x.data.error) || "Couldn't change guest mode.", "bad");
  };
}
async function pageSystem(el) {
  el.innerHTML = `<div class="cols"><div class="card" id="ver"><h2>Updates</h2><p class="help">Checking…</p></div><div class="card" id="hl"><h2>Health</h2></div></div>
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
  const u = await api("GET", "/api/system/update");
  $("#ver", el).innerHTML = `<h2>Updates</h2><div class="row"><span>Installed</span><span class="right mono">${esc(s.version)}</span></div>` + (u.ok ? `
    <div class="row"><span>Latest</span><span class="right mono">${esc(u.data.latest.sha.slice(0, 7))}</span></div><p class="note">What's new: ${esc(u.data.latest.message)}</p>
    <div class="inline" style="margin-top:10px">${u.data.update_ready ? `<span class="pill warn"><i></i>Update ready</span><button class="btn p sm" id="inst">Install</button>` : `<span class="pill ok"><i></i>Up to date</span>`}</div>` : `<p class="help">${esc(u.data.error)}</p>`);
  if ($("#inst", el)) $("#inst", el).onclick = async () => {
    if (!confirm("Install the update? Voice restarts for a few seconds.")) return;
    busyBtn($("#inst", el), true, "Installing…");
    const r = await api("POST", "/api/system/update", {sha: u.data.latest.sha});
    r.ok ? toast("Installed. The page reloads in a moment.") : (busyBtn($("#inst", el), false, "Install"), toast(r.data.error, "bad"));
    if (r.ok) setTimeout(() => location.reload(), 8000);
  };
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
  el.innerHTML = `<div class="card">${[["voice", "xAI key and relay"], ["bots", "Bots"], ["display", "Display"], ["time", "Time and location"], ["network", "Network"]].map(([k, t]) =>
    `<a class="row" href="#${k}" style="color:inherit;text-decoration:none">${t}<span class="right help">›</span></a>`).join("")}
    <div class="row"><button class="btn g" id="again">Run setup again</button></div><div class="row"><button class="btn g" id="out2">Sign out</button></div></div>`;
  $("#again", el).onclick = async () => { await api("POST", "/api/wizard", {step: 2, done: false}); boot(); };
  $("#out2", el).onclick = async () => { await api("POST", "/api/logout", {}); boot(); };
}

boot();
