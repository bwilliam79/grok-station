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
function legacyCopy(value) {
  // Works on plain HTTP and on a self-signed cert, where navigator.clipboard is missing or refuses.
  const ta = document.createElement("textarea");
  ta.value = value; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
async function copyText(text, btn) {
  let value = String(text || "");
  text = "";
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(value); ok = true; } catch (e) { ok = false; }
  }
  if (!ok) ok = legacyCopy(value);
  value = "";
  if (btn) {
    const tip = btn.parentElement && btn.parentElement.querySelector(".copied");
    if (tip) { tip.textContent = ok ? "Copied" : "Select it and copy it"; tip.hidden = false; clearTimeout(tip._t); tip._t = setTimeout(() => (tip.hidden = true), 1500); }
  } else if (ok) toast("Copied."); else toast("Select it and copy it.", "warn");
  return ok;
}
const COPY_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></g></svg>';
// A read-only value: plain monospace text, never an input. A copy icon button sits beside it.
function staticValue(id, value, label, missing) {
  if (!value) return `<div class="static"><span class="static-missing" id="${id}">${esc(missing || "Not available")}</span></div>`;
  return `<div class="static"><code class="static-val mono" id="${id}">${esc(value)}</code>
    <button type="button" class="iconbtn" data-copy="${id}" title="Copy ${esc(label)}" aria-label="Copy ${esc(label)}">${COPY_ICON}</button>
    <span class="copied" role="status" hidden></span></div>`;
}
document.addEventListener("click", ev => {
  const btn = ev.target && ev.target.closest ? ev.target.closest("[data-copy]") : null;
  if (!btn) return;
  const src = document.getElementById(btn.getAttribute("data-copy"));
  if (src) copyText(src.textContent, btn);
});
const STATION_KEY_MISSING = "Station key unavailable, see logs";
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
  // Rows come from the server as {at, event, version}: versions where known, never commit ids.
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) return `<p class="help">No update history yet.</p>`;
  return list.slice().reverse().map(r => {
    const row = r && typeof r === "object" ? r : {};
    const what = (UPDATE_EVENTS[row.event] || row.event || "") + (row.version ? " · " + row.version : "");
    return `<div class="row"><span class="help">${esc(localTime(row.at))}</span><span>${esc(what)}</span></div>`;
  }).join("");
}
function historyCard(rows, open) {
  return `<div class="card hist"><h2><button type="button" class="disclose" id="histtog" aria-expanded="${open ? "true" : "false"}" aria-controls="histbody">History<svg class="chev" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button></h2>
    <div id="histbody"${open ? "" : " hidden"}>${historyHTML(rows)}</div></div>`;
}
async function refreshUpdateBadge() {
  const nodes = document.querySelectorAll("[data-upbadge]");
  if (!nodes.length) return;
  const r = await api("GET", "/api/updates");
  const n = r.ok && r.data && r.data.update_available ? 1 : 0;
  nodes.forEach(b => { b.hidden = !n; b.textContent = n ? "1" : ""; });
}
const JOB_TEXT = {running: "Updating… The admin page restarts during the update and comes back by itself.",
  succeeded: "The last update finished.", failed: "The update didn't finish. Details are in the station's logs."};
const PAGE_INSTALLED = {v: null};  // version this page was loaded against; a change means the admin was updated
async function paintUpdates(el) {
  const r = await api("GET", "/api/updates");
  if (!document.body.contains(el)) return;
  if (r.ok && r.data) el._snap = r.data;
  if (r.ok && r.data && r.data.installed) {
    // The update replaced this page's own code. Reload so the new page (not the old script) draws the result.
    if (PAGE_INSTALLED.v && PAGE_INSTALLED.v !== r.data.installed) { location.reload(); return; }
    PAGE_INSTALLED.v = r.data.installed;
  }
  const offline = !!(el._applying && r.status === 0);
  const d = r.ok && r.data ? r.data : (offline && el._snap ? el._snap : {});
  const job = d.job || {};
  if (el._applying && r.ok && (job.state === "succeeded" || job.state === "failed")) el._applying = false;
  if (el._applying && r.ok && job.state !== "running") el._applyWait = (el._applyWait || 0) + 1;
  else if (!offline) el._applyWait = 0;
  if (el._applying && el._applyWait > 15) { el._applying = false; el._applyStuck = true; }
  const spinning = !!(el._applying || job.state === "running");
  const stuck = !!el._applyStuck;
  el._applyStuck = false;
  const auto = !!d.autocheck;
  const failLine = job.state === "failed" ? (job.error || JOB_TEXT.failed) : "";
  const noteLine = offline ? "Reconnecting…" : (job.state === "succeeded" ? JOB_TEXT.succeeded : (spinning ? JOB_TEXT.running : ""));
  el.innerHTML = `${d.reboot_required ? `<div class="banner warn" style="flex-wrap:wrap">The last update changed boot options. Reboot the station (System, Reboot) to finish.</div>` : ""}
    ${d.cant_check ? `<div class="banner warn" style="flex-wrap:wrap">Couldn't reach GitHub to check for updates. Check the station's internet connection.</div>` : ""}
    ${!r.ok && !offline ? `<div class="banner bad" style="flex-wrap:wrap">${esc((r.data && r.data.error) || "Can't load updates.")}</div>` : ""}
    <div class="card"><h2>Station</h2>
      ${d.update_available ? `<div class="banner up">Update available: ${esc(d.installed || "—")} → <span class="ver-new">${esc(d.available)}</span></div>` : ""}
      ${d.update_blocked ? `<div class="banner bad">${esc(d.update_blocked)}</div>` : ""}
      ${d.up_to_date && !d.cant_check ? `<div class="uptodate">Up to date</div>` : ""}
      <div class="row"><span>Installed</span><span class="right mono">${esc(d.installed || "—")}</span></div>
      <div class="row"><span>Available</span><span class="right">${d.update_available ? `<span class="ver-new">${esc(d.available)}</span>` : `<span class="mono">${esc(d.available || "—")}</span>`}</span></div>
      ${d.checked_at ? `<p class="note">Checked ${esc(localTime(d.checked_at))}. Updates come from the main branch on GitHub.</p>` : ""}
      ${noteLine ? `<p class="note">${esc(noteLine)}</p>` : ""}
      ${failLine ? `<p class="err" role="alert">${esc(failLine)}</p>` : ""}
      ${stuck ? `<p class="err" role="alert">The update didn't start.</p>` : ""}
      <div class="field inline updbtns">${spinning
        ? `<span class="spin" role="status" aria-label="Updating"></span>`
        : (d.update_available ? `<button type="button" class="btn p" id="upd">Update</button>` : "")}
        <button type="button" class="btn" id="checknow"${spinning ? " disabled" : ""}>Check now</button></div>
      <div id="ue"></div>
      <div class="row"><button type="button" class="tog${auto ? " on" : ""}" role="switch" id="auto" aria-checked="${auto ? "true" : "false"}" aria-label="Daily check"></button>
        <div><div class="lbl">Daily check</div><div class="help">Off until you turn it on. A check looks for a newer version. It does not install anything.</div></div></div></div>
    ${historyCard(d.history, !!el._histOpen)}`;
  const tog = $("#histtog", el);
  if (tog) tog.onclick = () => {  // a <button>: Enter and Space work, and it is in the tab order
    el._histOpen = !el._histOpen;
    tog.setAttribute("aria-expanded", el._histOpen ? "true" : "false");
    $("#histbody", el).hidden = !el._histOpen;
  };
  el._paintGen = (el._paintGen || 0) + 1;
  const gen = el._paintGen;
  if (spinning) setTimeout(() => { if (document.body.contains(el) && el._paintGen === gen) paintUpdates(el); }, 2000);
  // Check and apply do not ask for the password again. The signed-in session is enough.
  // The installer log stays on the station. This page shows a spinner, then one line on failure.
  const run = async (btn, label, busy, path, okMsg) => {
    if (btn && btn.isConnected) busyBtn(btn, true, busy);
    const res = await api("POST", path, {});
    if (!document.body.contains(el)) return res;
    if (path === "/api/updates/apply" && res.status !== 0 && !res.ok) el._applying = false;
    await paintUpdates(el);
    if (!document.body.contains(el)) return res;
    if (!res.ok && res.status !== 0) errBox($("#ue", el), (res.data && res.data.error) || "That didn't work.");
    else if (okMsg) toast(okMsg);
    refreshUpdateBadge();
    return res;
  };
  const up = $("#upd", el);
  if (up) up.onclick = async () => {
    el._applying = true;
    await paintUpdates(el);
    if (!document.body.contains(el)) return;
    return run(null, "Update", "Starting…", "/api/updates/apply", "");
  };
  const check = $("#checknow", el);
  if (check) check.onclick = () => run(check, "Check now", "Checking…", "/api/updates/check", "");
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

function soundLabel(id) {
  if (id === "none") return "None";
  if (id === "chime") return "Chime";
  const m = /^([a-z]+)-(\d+)$/.exec(id);
  if (m) {
    const name = {alarm: "Alarm", notify: "Notify", end: "End"}[m[1]];
    if (name) return name + " " + String(+m[2]);
  }
  return id;
}
function soundSelect(id, opts, cur) {
  return `<select class="sel" id="${id}" style="width:auto;flex:1;min-width:160px">${(opts || []).map(x =>
    `<option value="${esc(x)}"${x === cur ? " selected" : ""}>${esc(soundLabel(x))}</option>`).join("")}</select>`;
}

async function audioPanel(el) {
  const [a, s] = await Promise.all([api("GET", "/api/audio"), api("GET", "/api/settings/audio")]);
  const v = (s.data || {}).values || {}, d = a.data || {};
  const sounds = (s.data || {}).sounds || {}, defs = sounds.defaults || {};
  const alarmOpts = sounds.alarm || [], listenOpts = sounds.listen || ["none"], endOpts = sounds.end || ["none"];
  const alarmCur = alarmOpts.includes(v.ALARM_SOUND) ? v.ALARM_SOUND : (defs.alarm != null ? defs.alarm : (alarmOpts[0] || ""));
  const listenCur = listenOpts.includes(v.LISTEN_TONE) ? v.LISTEN_TONE : (defs.listen || "none");
  const endCur = endOpts.includes(v.END_TONE) ? v.END_TONE : (defs.end || "none");
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
    <div class="field"><label for="alm">Alarm sound</label><div class="inline">${soundSelect("alm", alarmOpts, alarmCur)}<button class="btn" id="aprev">Preview</button></div>
      <p class="note">Plays on the station when a timer finishes.</p></div>
    <div class="field"><label for="ltn">Listen tone</label><div class="inline">${soundSelect("ltn", listenOpts, listenCur)}<button class="btn" id="lprev">Preview</button></div>
      <p class="note">Plays when the station wakes.</p></div>
    <div class="field"><label for="etn">End tone</label><div class="inline">${soundSelect("etn", endOpts, endCur)}<button class="btn" id="eprev">Preview</button></div>
      <p class="note">Plays when a conversation ends.</p></div>
    <div id="ae"></div>
    <div class="field inline"><button class="btn p" id="asave">Save</button><button class="btn" id="atest">Play test sound</button></div>
    <p class="note">Saving restarts the voice listener (after any conversation or enrollment that's running).</p></div>`;
  const lab = () => { $("#wgv", el).textContent = "×" + $("#wg", el).value; $("#vgv", el).textContent = "×" + $("#vg", el).value; };
  $("#wg", el).oninput = $("#vg", el).oninput = lab; lab();
  every(1500, async () => { if (!document.body.contains(el)) return; const r = await api("GET", "/api/audio"); if (r.ok && $("#lvl", el)) $("#lvl", el).innerHTML = meter(r.data.level_dbfs); });
  const preview = async (sel) => {
    const id = $(sel, el).value;
    if (!id || id === "none") return toast("Nothing to play.");
    const r = await api("POST", "/api/audio/test", {id});
    r.ok ? toast("Playing on the station.") : toast(r.data.error || "Couldn't play that.", "warn");
  };
  $("#aprev", el).onclick = () => preview("#alm");
  $("#lprev", el).onclick = () => preview("#ltn");
  $("#eprev", el).onclick = () => preview("#etn");
  $("#atest", el).onclick = async () => { const r = await api("POST", "/api/audio/test", {}); r.ok ? toast("Playing on the station.") : toast(r.data.error, "warn"); };
  $("#asave", el).onclick = async () => {
    const r = await api("PUT", "/api/settings/audio", {WAKE_DEVICE_NAME: $("#dev", el).value, WAKE_CAPTURE_RATE: $("#rate", el).value,
      WAKE_GAIN: (+$("#wg", el).value).toFixed(1), VOICE_GAIN: (+$("#vg", el).value).toFixed(1),
      ALARM_SOUND: $("#alm", el).value, LISTEN_TONE: $("#ltn", el).value, END_TONE: $("#etn", el).value});
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
  const dim = (g.data || {}).dim || {};
  const sensor = dim.sensor || null;
  const [nf, nt] = (v.GROK_STATION_NIGHT || "22-6").split("-");
  const hours = sel => [...Array(24).keys()].map(h => `<option value="${h}"${String(h) === sel ? " selected" : ""}>${((h + 11) % 12) + 1} ${h < 12 ? "AM" : "PM"}</option>`).join("");
  const pct = (s, fallback) => { const n = parseInt(s, 10); return Number.isFinite(n) && n >= 0 && n <= 100 ? String(n) : String(fallback); };
  const hhmm = (s, fallback) => /^\d{2}:\d{2}$/.test(s || "") ? s : fallback;
  const off = (s, fallback) => { const n = parseInt(s, 10); return Number.isFinite(n) && n >= -180 && n <= 180 ? String(n) : String(fallback); };
  const modes = ["off", "schedule", "sun", "auto-sensor"];
  const mode = modes.includes(v.GROK_STATION_DIM_MODE) ? v.GROK_STATION_DIM_MODE : "off";
  const day = pct(v.GROK_STATION_DIM_DAY, 90), night = pct(v.GROK_STATION_DIM_NIGHT, 20);
  const attn = (box, msg) => { box.innerHTML = msg ? `<div class="banner warn" role="alert"><span class="pill warn">Needs attention</span> ${esc(msg)}</div>` : ""; };
  el.innerHTML = `<div class="cols"><div class="card"><h2>Display</h2>
    <div class="row"><button class="tog${v.GROK_STATION_NIGHT === "" ? "" : " on"}" role="switch" id="non" aria-label="Night clock"></button><div><div class="lbl">Night clock</div><div class="help">A dim clock overnight. With dimming on, it follows that window.</div></div></div>
    <div class="row inline"><label class="help">From <select class="sel" id="nf" style="width:130px">${hours(nf)}</select></label><label class="help">to <select class="sel" id="nt" style="width:130px">${hours(nt)}</select></label></div>
    <div class="field"><label for="hold">Message hold time <span class="help" id="holdv"></span></label><input type="range" id="hold" min="5" max="120" value="${esc(v.GROK_STATION_HOLD || "30")}"></div>
    <div class="row"><button class="tog${v.GROK_STATION_FADE === "0" ? "" : " on"}" role="switch" id="fade" aria-label="Fade"></button><div class="lbl">Fade between screens</div></div>
    <div class="field"><label for="dun">Temperature</label><select class="sel" id="dun"><option value="fahrenheit">°F</option><option value="celsius"${v.GROK_STATION_UNITS === "celsius" ? " selected" : ""}>°C</option></select></div>
    <div class="field"><label for="dmode">Brightness</label><select class="sel" id="dmode">
      <option value="off">Off</option><option value="schedule">Schedule</option><option value="sun">Sunrise and sunset</option><option value="auto-sensor">Light sensor</option></select>
      <div class="help">Off leaves the backlight alone. Schedule and sunrise need no sensor. A light sensor is used only when the kernel already exposes one.</div></div>
    <div class="field"><label for="dday">Day brightness <span class="help" id="ddayv"></span></label><input type="range" id="dday" min="0" max="100" value="${day}"></div>
    <div class="field"><label for="dnight">Night brightness <span class="help" id="dnightv"></span></label><input type="range" id="dnight" min="0" max="100" value="${night}"></div>
    <div class="row inline"><label class="help">Dim from <input class="inp" type="time" id="dfrom" value="${hhmm(v.GROK_STATION_DIM_FROM, "22:00")}"></label><label class="help">until <input class="inp" type="time" id="dto" value="${hhmm(v.GROK_STATION_DIM_TO, "06:00")}"></label></div>
    <div class="row inline"><label class="help">Sunrise offset <input class="inp" type="number" id="dsr" min="-180" max="180" step="1" value="${off(v.GROK_STATION_DIM_SUNRISE, 0)}"></label><label class="help">Sunset offset <input class="inp" type="number" id="dss" min="-180" max="180" step="1" value="${off(v.GROK_STATION_DIM_SUNSET, 0)}"></label></div>
    <div class="row inline"><label class="help">Latitude <input class="inp" id="dlat" maxlength="8" inputmode="decimal" placeholder="12.34" value="${esc(v.GROK_STATION_DIM_LAT || "")}"></label><label class="help">Longitude <input class="inp" id="dlon" maxlength="8" inputmode="decimal" placeholder="-56.78" value="${esc(v.GROK_STATION_DIM_LON || "")}"></label></div>
    <p class="note">Blank latitude and longitude use the station location, rounded to 2 decimals. Sensor: ${sensor ? esc(sensor.name) : "none"}. ${dim.backlight ? "Backlight: " + esc(dim.backlight.name) : "No backlight file; a dark layer is drawn instead."}</p>
    <div id="de"></div><div class="field inline"><button class="btn p" id="dsave">Save</button></div></div>
    <div class="card"><h2>Live preview</h2><img class="thumb" id="pv" alt="What the station screen shows now">
      <div class="nightprev" id="nprev"><div class="tm">11:24</div><div class="dt">Thu, Oct 22</div><div class="dim-overlay" id="nov"></div></div>
      <p class="note">Example night clock. The black layer is the night brightness.</p></div></div>`;
  $("#dmode", el).value = mode;
  $("#non", el).onclick = $("#fade", el).onclick = ev => ev.currentTarget.classList.toggle("on");
  const hl = () => ($("#holdv", el).textContent = $("#hold", el).value + " s"); $("#hold", el).oninput = hl; hl();
  const levels = () => { $("#ddayv", el).textContent = $("#dday", el).value + "%"; $("#dnightv", el).textContent = $("#dnight", el).value + "%";
    const n = parseInt($("#dnight", el).value, 10) || 0; $("#nov", el).style.opacity = String((100 - n) / 100); };
  $("#dday", el).oninput = $("#dnight", el).oninput = levels; levels();
  const noReading = dim.lux == null;
  const showSense = () => {
    if ($("#dmode", el).value !== "auto-sensor" || !noReading) { attn($("#de", el), ""); return; }
    attn($("#de", el), sensor
      ? "The light sensor isn't reporting a reading. Dimming uses the schedule, then sunrise, then the day level."
      : "No light sensor was found. Dimming uses the schedule, then sunrise, then the day level.");
  };
  $("#dmode", el).onchange = showSense; showSense();
  every(5000, () => { const p = $("#pv", el); if (p) p.src = "/api/preview?t=" + Date.now(); });
  $("#dsave", el).onclick = async () => {
    const r = await api("PUT", "/api/settings/display", {GROK_STATION_NIGHT: $("#non", el).classList.contains("on") ? `${$("#nf", el).value}-${$("#nt", el).value}` : "",
      GROK_STATION_HOLD: $("#hold", el).value, GROK_STATION_FADE: $("#fade", el).classList.contains("on") ? "1" : "0", GROK_STATION_UNITS: $("#dun", el).value,
      GROK_STATION_DIM_MODE: $("#dmode", el).value, GROK_STATION_DIM_DAY: $("#dday", el).value, GROK_STATION_DIM_NIGHT: $("#dnight", el).value,
      GROK_STATION_DIM_FROM: $("#dfrom", el).value, GROK_STATION_DIM_TO: $("#dto", el).value,
      GROK_STATION_DIM_LAT: $("#dlat", el).value.trim(), GROK_STATION_DIM_LON: $("#dlon", el).value.trim(),
      GROK_STATION_DIM_SUNRISE: $("#dsr", el).value, GROK_STATION_DIM_SUNSET: $("#dss", el).value});
    if (r.ok) { showSense(); toast("Saved. The screen restarts."); }
    else attn($("#de", el), (r.data && r.data.error) || "Couldn't save.");
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
const BOT_ID_HELP = "Short lowercase ID for this bot, like grok. Used for wake-word routing, portraits and replies. Can't be changed later.";
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
function pubKeyOk(text) {
  const s = String(text || "").replace(/\s+/g, "");
  if (!s || s.length > 80 || !/^[A-Za-z0-9+/]+={0,2}$/.test(s)) return false;
  try { return atob(s).length === 32; } catch (e) { return false; }
}
function rttLine(stats) {
  const s = stats || {};
  const avg = typeof s.avg_rtt_ms === "number" ? s.avg_rtt_ms : null;
  const n = typeof s.timeouts === "number" ? s.timeouts : 0;
  if (avg == null && n === 0) return "No round trips yet";
  const secs = avg == null ? "—" : oneDecimal(avg / 1000);
  return `Avg round trip ${secs} s (last 10) · ${n} ${n === 1 ? "timeout" : "timeouts"}`;
}
function applyBotMeta(el, data) {
  if (!el || !data) return;
  el._stationPub = typeof data.station_pub === "string" ? data.station_pub : "";
  el._killed = data.killed === true;
  el._stats = data.stats || {avg_rtt_ms: null, timeouts: 0};
  const killed = $("#bkilled", el);
  if (killed) killed.hidden = !el._killed;
  const overall = $("#rttall", el);
  if (overall) overall.textContent = "Overall · " + rttLine(el._stats);
}
function pairFields(stationPub, paired) {
  return `<div class="field"><label>Station public key</label>
      ${staticValue("spub", stationPub, "station public key", STATION_KEY_MISSING)}
      <p class="note">The bot seals replies to this key. Never port-forward the admin port.</p></div>
    <div class="field"><label for="bpub">Bot public key</label>
      <input class="inp mono" id="bpub" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="Base64, 32 bytes">
      <p class="note">From grok-station-reply keygen. The private key stays on the bot.</p></div>
    <div id="perr"></div>
    <div class="field inline"><button class="btn" id="pair" type="button">Pair key</button>
      ${paired ? `<button class="btn dg" id="revoke" type="button">Revoke</button>` : ""}</div>`;
}
function wirePair(root, bot, el) {
  const revokeOne = () => askPassword(`Revoke ${bot.name}?`, "This drops the pinned key. Replies from this bot are rejected until you pair it again.", "Revoke", async pw => {
    const r = await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}/key`, {password: pw});
    if (r && r.ok) {
      bot.paired = false;
      toast("Revoked.");
      const btn = $("#revoke", root);
      if (btn) btn.remove();
      refreshBots(el);
    }
    return r;
  });
  const pair = $("#pair", root);
  if (pair) pair.onclick = () => {
    const inp = $("#bpub", root);
    const pub = inp ? inp.value.trim() : "";
    if (!pubKeyOk(pub)) {
      errBox($("#perr", root), "Paste the bot's public key: base64 of 32 bytes.");
      if (inp) inp.focus();
      return;
    }
    errBox($("#perr", root), "");
    const send = async pw => {
      if (inp) inp.value = "";
      const body = {public_key: pub};
      if (pw) body.password = pw;
      const r = await api("PUT", `/api/bots/${encodeURIComponent(bot.slug)}/key`, body);
      body.public_key = "";
      body.password = "";
      if (r && r.ok) {
        bot.paired = true;
        toast("Paired.");
        const holder = pair.parentElement;
        if (holder && !$("#revoke", root)) {
          const b = document.createElement("button");
          b.className = "btn dg";
          b.id = "revoke";
          b.type = "button";
          b.textContent = "Revoke";
          holder.appendChild(b);
          b.onclick = revokeOne;
        }
        refreshBots(el);
      }
      return r;
    };
    if (stepupOpen()) send("").then(r => {
      if (r && r.status === 403) askPassword("Pair key?", "Enter the admin password to pin this key.", "Pair key", send);
      else if (r && !r.ok) errBox($("#perr", root), (r && r.data && r.data.error) || "Couldn't pair.");
    });
    else askPassword("Pair key?", "Enter the admin password to pin this key.", "Pair key", send);
  };
  const rev = $("#revoke", root);
  if (rev) rev.onclick = revokeOne;
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
function voiceOptions(voices, current, id, blankLabel, attrs) {
  const list = Array.isArray(voices) ? voices.slice() : [];
  const cur = typeof current === "string" ? current : "";
  if (cur && list.indexOf(cur) < 0) list.unshift(cur);
  const blank = blankLabel || "Station default";
  let opts = `<option value=""${cur ? "" : " selected"}>${esc(blank)}</option>`;
  opts += list.map(v => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(v)}</option>`).join("");
  return `<select class="sel" id="${id}"${attrs || ""}>${opts}</select>`;
}
function voiceChoice(bot, el) {
  const v = (bot && bot.voice) || "";
  const d = (el && el._defaultVoice) || "";
  if (!v || (d && v.toLowerCase() === d.toLowerCase())) return "";
  return v;
}
function stationVoiceLabel(el) {
  const v = el && el._defaultVoice;
  return v ? "Station default (" + v + ")" : "Station default";
}
function keyFingerprint(text) {
  if (!pubKeyOk(text)) return "";
  const raw = atob(String(text).replace(/\s+/g, ""));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return sha256Hex(bytes).slice(0, 16).replace(/(.{4})/g, "$1 ").trim();
}
function dialogX() {
  return `<button class="x" type="button" id="dx" aria-label="Close">×</button>`;
}
function bindSheet(d, close) {
  const x = $("#dx", d);
  if (x) x.onclick = ev => { ev.preventDefault(); close(); };
  if (!d._esc) {
    d._esc = ev => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      ev.stopPropagation();
      close();
    };
    d.addEventListener("keydown", d._esc);
  }
  const card = d.querySelector("[role=dialog]");
  if (card && !card.hasAttribute("tabindex")) card.tabIndex = -1;
  const field = d.querySelector("input:not([readonly]), select, textarea");
  const btn = d.querySelector("button.btn.p, button.btn.d");
  const target = field || btn || card;
  if (target && target.focus) target.focus();
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
function confirmRemove(bot, onDone) {
  const d = document.createElement("div");
  d.className = "sheet";
  const open = stepupOpen();
  d.innerHTML = `<form class="card" role="dialog" autocomplete="off" aria-label="Remove bot" style="width:min(400px,100%)">
    ${dialogX()}
    <h2>Remove ${esc(bot.name)}?</h2>
    <p class="sub">This removes the bot from the station. A registry teammate is hidden. An added bot is deleted.</p>
    ${open ? `<p class="note stepup">${esc(stepupText())}</p>` : `<div class="field"><label for="rmpw">Admin password</label><input class="inp" type="password" id="rmpw" autocomplete="current-password" required></div>`}
    <div id="rme"></div>
    <div class="field"><button class="btn d" id="rmgo" type="submit">Remove bot</button></div></form>`;
  document.body.appendChild(d);
  const close = () => d.remove();
  bindSheet(d, close);
  d.querySelector("form").onsubmit = async ev => {
    ev.preventDefault();
    const pwEl = $("#rmpw", d);
    let pw = pwEl ? pwEl.value : "";
    if (pwEl) pwEl.value = "";
    const body = {};
    if (pw) body.password = pw;
    pw = "";
    const btn = $("#rmgo", d);
    if (btn) busyBtn(btn, true);
    const r = await api("DELETE", `/api/bots/${encodeURIComponent(bot.slug)}`, body);
    body.password = "";
    if (!r.ok) {
      if (btn && btn.isConnected) busyBtn(btn, false, "Remove bot");
      if (r.status === 403 && !$("#rmpw", d) && $("#rme", d)) {
        const holder = document.createElement("div");
        holder.className = "field";
        holder.innerHTML = `<label for="rmpw">Admin password</label><input class="inp" type="password" id="rmpw" autocomplete="current-password" required>`;
        $("#rme", d).before(holder);
        const again = $("#rmpw", d);
        if (again) again.focus();
      }
      errBox($("#rme", d), (r.data && r.data.error) || "Couldn't remove that bot.");
      return;
    }
    refreshStepup();
    close();
    toast("Removed.");
    if (onDone) onDone();
  };
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
    <div style="flex:1;min-width:0"><div class="lbl">${esc(b.name)}</div><div style="margin-top:4px">${botPill(b)}</div>
    <div class="help">${esc(rttLine(b.stats))}</div></div>
    ${voiceOptions(el._voices, voiceChoice(b, el), "vv-" + b.slug, "", ` data-vsel="${esc(b.slug)}" aria-label="Voice"`)}
    <button class="btn sm" type="button" data-voice="${esc(b.slug)}">${esc(info.label || "Record voice")}</button>
    ${(b.webhook && b.webhook.configured) ? "" : `<button class="btn sm" type="button" data-hook="${esc(b.slug)}">Connect webhook</button>`}
    <button class="btn sm" type="button" data-bot="${esc(b.slug)}">Configure</button></div>`;
  }).join("") || `<p class="help">No bots yet.</p>`;
  bindAvatars(list);
  list.querySelectorAll("[data-vsel]").forEach(sel => {
    sel.dataset.prev = sel.value;
    sel.onfocus = () => { sel.dataset.prev = sel.value; };
    sel.onchange = () => saveRowVoice(sel, bots.find(b => b.slug === sel.dataset.vsel), el);
  });
  list.querySelectorAll("[data-bot]").forEach(btn => btn.onclick = () => openBot(bots.find(b => b.slug === btn.dataset.bot), el));
  list.querySelectorAll("[data-hook]").forEach(btn => btn.onclick = () => connectWebhookWizard(bots.find(b => b.slug === btn.dataset.hook), el));
  list.querySelectorAll("[data-voice]").forEach(btn => btn.onclick = () => {
    const bot = bots.find(b => b.slug === btn.dataset.voice);
    const info = voice[bot.slug] || {};
    if (info.processing || info.state === "failed" || info.state === "failed_quality") openVoiceStatus(bot, el);
    else enrollDrawer(null, [], () => refreshBots(el), bot.slug);
  });
}
function saveRowVoice(sel, bot, el) {
  if (!bot || !sel) return;
  const voice = sel.value;
  const prev = sel.dataset.prev || "";
  const send = async pw => {
    const body = {voice};
    if (pw) body.password = pw;
    const r = await api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body);
    if (r && r.ok) {
      bot.voice = voice || (el && el._defaultVoice) || "";
      sel.dataset.prev = voice;
      toast("Voice saved.");
      if (el) el._holdPaint = false;
      refreshBots(el);
    }
    return r;
  };
  const revert = () => {
    if (el) el._holdPaint = false;
    sel.value = prev;
  };
  if (el) el._holdPaint = true;
  if (stepupOpen()) send("").then(r => {
    if (r && r.status === 403) askPassword("Change voice?", "Enter the admin password to save this voice.", "Save", async pw => {
      const res = await send(pw);
      if (!(res && res.ok)) revert();
      return res;
    }, revert);
    else if (!(r && r.ok)) {
      revert();
      toast((r && r.data && r.data.error) || "Couldn't save the voice.", "bad");
    }
  });
  else askPassword("Change voice?", "Enter the admin password to save this voice.", "Save", async pw => {
    const res = await send(pw);
    if (!(res && res.ok)) revert();
    return res;
  }, revert);
}
async function refreshBots(el) {
  const [r, s] = await Promise.all([api("GET", "/api/bots"), api("GET", "/api/enroll/status")]);
  if (!r.ok || !$("#blist", el)) return null;
  noteStepup(r.data);
  el._voices = (r.data && r.data.voices) || [];
  el._defaultVoice = (r.data && r.data.default_voice) || "";
  el._voice = (s.data && s.data.buttons) || {};
  applyBotMeta(el, r.data);
  const focused = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.vsel;
  if (el._holdPaint || focused) return r.data;
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
    <div class="field inline"><button class="btn" id="test" type="button">Test relay</button><button class="btn p" id="save" type="button">Save</button></div>
    <div id="tbox"></div>
    ${saved ? `<div class="field"><button class="btn dg" id="delhook" type="button">Remove…</button></div>` : ""}
    ${pairFields(el._stationPub, bot.paired)}
    <div class="botfoot"><button class="btn g sm" id="ren" type="button">Rename</button><button class="btn dg sm" id="delbot" type="button">Remove bot</button></div>`;
  document.body.appendChild(d);
  bindAvatars(d);
  wirePair(d, bot, el);
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
    const send = async pw => {
      const body = {voice};
      if (pw) body.password = pw;
      const r = await api("PATCH", `/api/bots/${encodeURIComponent(bot.slug)}`, body);
      if (r.ok) {
        bot.voice = voice || (el && el._defaultVoice) || "";
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
    if (btn) { btn.textContent = "Test relay"; btn.disabled = false; }
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
      if (bar) bar.style.width = Math.min(100, sec / 30 * 100) + "%";
    };
    $("#tbox", d).innerHTML = `<div class="twait"><div class="inline"><span class="spin"></span><b>Waiting for ${esc(bot.name)}… <span id="tclock">0:00</span></b></div>
      <div class="tbar"><i id="tfill"></i></div>
      <p class="note">Gives up after 30 seconds.</p>
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
        if (!r.ok) { showResult({state: "failed", error: (r.data && r.data.error) || "No answer"}); return; }
        if (typeof r.data.elapsed_s === "number") { sec = Math.max(sec, r.data.elapsed_s); paint(); }
        if (r.data.state === "waiting" && sec >= 35) { showResult({state: "failed", error: "No answer"}); return; }
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
      btn.textContent = "Test relay";
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
  $("#delbot", d).onclick = () => confirmRemove(bot, async () => {
    await cancelTest();
    d.remove();
    refreshBots(el);
  });
}
const ADD_STEP_LABEL = "1 Bot · 2 Pair keys · 3 Webhook · 4 Done";
const HOOK_LATER = "Not connected yet - use Connect webhook on the Bots page";
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
function webhookProblem(url, key) {
  const v = String(url || "").trim();
  let u;
  try { u = new URL(v); } catch (e) { u = null; }
  const port = u ? u.port : "";
  const pathAt = v.indexOf("/", v.indexOf("://") + 3);
  if (!u || !httpsWebhook(v) || v.length > 512 || /\s/.test(v) || pathAt < 0 || u.hostname !== "api2.cursor.sh" || (port && port !== "443") || u.hash)
    return "That webhook address isn't allowed.";
  if (!/^[\x21-\x7e]{8,400}$/.test(String(key || ""))) return "Paste the webhook key.";
  return "";
}
function shownLast4(v) {
  return typeof v === "string" && /^[\x21-\x7e]{1,4}$/.test(v) ? v : "";
}
function testResultHTML(res) {
  if (res.state === "ok") {
    const secs = res.answered_in_s == null ? "" : oneDecimal(res.answered_in_s) + " s";
    return `<div class="banner ok"><span class="pill ok"><i></i>Answered in ${esc(secs)}</span></div>`;
  }
  if (res.state === "cancelled") return `<p class="note">Cancelled.</p>`;
  const err = res.error || "No answer";
  const hint = !res.error || res.error === "No answer";
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
      <p class="note">Gives up after 30 seconds.</p>
      <a href="#" id="hcancel">Cancel</a></div>`;
    const cancel = $("#hcancel", root);
    if (cancel) cancel.onclick = async ev => { ev.preventDefault(); await cancelTest(); showResult({state: "cancelled"}); };
    testTimer = setInterval(async () => {
      if (!$("#hclock", root)) { stopPoll(); return; }
      sec += 1;
      const clock = $("#hclock", root), bar = $("#hfill", root);
      if (clock) clock.textContent = mss(sec);
      if (bar) bar.style.width = Math.min(100, sec / 30 * 100) + "%";
      if (busy) return;
      busy = true;
      try {
        const r = await api("GET", `/api/bots/${encodeURIComponent(bot.slug)}/test/${encodeURIComponent(id)}`);
        if (testId !== id || !$("#hclock", root)) return;
        if (!r.ok) { showResult({state: "failed", error: (r.data && r.data.error) || "No answer"}); return; }
        if (typeof r.data.elapsed_s === "number") { sec = Math.max(sec, r.data.elapsed_s); if (clock) clock.textContent = mss(sec); if (bar) bar.style.width = Math.min(100, sec / 30 * 100) + "%"; }
        if (r.data.state === "waiting" && sec >= 35) { showResult({state: "failed", error: "No answer"}); return; }
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
  const draft = {name: "", slug: "", voice: "", pub: "", url: "", key: ""};
  let created = false;
  const wipe = () => { draft.pub = ""; draft.key = ""; draft.url = ""; };
  d._cancel = wipe;
  const shut = () => { wipe(); d.remove(); if (created) refreshBots(el); };
  document.body.appendChild(d);
  const showPw = () => {
    if ($("#apw", d) || !$("#ape", d)) return;
    const holder = document.createElement("div");
    holder.className = "field";
    holder.innerHTML = `<label for="apw">Admin password</label><input class="inp" type="password" id="apw" autocomplete="current-password" required>`;
    $("#ape", d).before(holder);
    const again = $("#apw", d);
    if (again) again.focus();
  };
  const stepDone = (connected, last4) => {
    refreshBots(el);
    d.innerHTML = `<div class="card wizcard" role="dialog" aria-label="Done">${dialogX()}${addStepper(4)}
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
    } else if (sum) sum.textContent = HOOK_LATER;
    const note = $("#donenote", d);
    if (note) note.textContent = (draft.name || "This bot") + " is on the station.";
    bindSheet(d, shut);
    const done = $("#adddone", d);
    if (done) done.onclick = shut;
  };
  const step4 = () => {
    const fp = draft.pub ? keyFingerprint(draft.pub) : "";
    const hook = !!(draft.url && draft.key);
    const open = stepupOpen();
    d.innerHTML = `<form class="card wizcard" role="dialog" autocomplete="off" aria-label="Add bot">${dialogX()}${addStepper(4)}
      <h2>Add bot</h2><p class="sub">Check this, then add the bot.</p>
      <div class="row"><span>Name</span><span class="right">${esc(draft.name)}</span></div>
      <div class="row"><span>ID</span><span class="right mono">${esc(draft.slug)}</span></div>
      <div class="row"><span>Voice</span><span class="right">${esc(draft.voice ? draft.voice : stationVoiceLabel(el))}</span></div>
      <div class="row"><span>Key</span><span class="right mono">${fp ? esc(fp) : "no"}</span></div>
      <div class="row"><span>Webhook</span><span class="right">${hook ? "yes" : "no"}</span></div>
      ${open ? `<p class="note stepup">${esc(stepupText())}</p>` : `<div class="field"><label for="apw">Admin password</label><input class="inp" type="password" id="apw" autocomplete="current-password" required></div>`}
      <div id="ape"></div>
      <div class="field"><button class="btn p" id="ago" type="submit">Add bot</button></div></form>`;
    bindSheet(d, shut);
    d.querySelector("form").onsubmit = async ev => {
      ev.preventDefault();
      const pwEl = $("#apw", d);
      let pw = pwEl ? pwEl.value : "";
      if (pwEl) pwEl.value = "";
      const btn = $("#ago", d);
      if (btn) busyBtn(btn, true);
      const fail = (r, msg) => {
        pw = "";
        if (!document.body.contains(d)) { if (created) refreshBots(el); return; }
        if (btn && btn.isConnected) busyBtn(btn, false, "Add bot");
        if (r && r.status === 403) showPw();
        const box = $("#ape", d);
        if (box) errBox(box, (r && r.data && r.data.error) || msg);
      };
      try {
        if (!created) {
          const body = {name: draft.name, slug: draft.slug};
          if (draft.voice) body.voice = draft.voice;
          if (pw) body.password = pw;
          const r = await api("POST", "/api/bots", body);
          body.password = "";
          if (r && r.data) r.data.voice_token = "";
          if (!r.ok) return fail(r, "Couldn't add that bot.");
          created = true;
        }
        if (draft.pub) {
          const body = {public_key: draft.pub};
          if (pw) body.password = pw;
          const r = await api("PUT", `/api/bots/${encodeURIComponent(draft.slug)}/key`, body);
          body.public_key = "";
          body.password = "";
          if (!r.ok) return fail(r, "The bot was added, but the key wasn't pinned.");
          draft.pub = "";
        }
        let connected = false, last4 = "";
        if (draft.url && draft.key) {
          const body = {url: draft.url, key: draft.key};
          if (pw) body.password = pw;
          const r = await api("PUT", `/api/bots/${encodeURIComponent(draft.slug)}/webhook`, body);
          last4 = r && r.ok ? shownLast4(r.data && r.data.last4) : "";
          body.key = "";
          body.password = "";
          if (!r.ok) return fail(r, "The bot was added, but the webhook wasn't saved.");
          draft.key = "";
          draft.url = "";
          connected = true;
        }
        pw = "";
        refreshStepup();
        if (!document.body.contains(d)) { refreshBots(el); return; }
        stepDone(connected, last4);
      } finally { pw = ""; }
    };
  };
  const step3 = () => {
    d.innerHTML = `<form class="card wizcard" role="dialog" autocomplete="off" aria-label="Connect webhook">${dialogX()}${addStepper(3)}
      <h2>Connect webhook</h2>
      <p class="sub">Optional. Copy the webhook URL and its key from this bot's routine panel.</p>
      <div class="field"><label for="aurl">Webhook URL</label><input class="inp" id="aurl" type="url" inputmode="url" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="https://" maxlength="512"></div>
      <div class="field"><label for="akey">Key</label><input class="inp" id="akey" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" maxlength="400"></div>
      <div id="awe"></div>
      <div class="field"><button class="btn p" id="anext" type="submit">Next</button></div>
      <div class="field"><a href="#" class="skipnow" id="askip">Skip for now</a></div></form>`;
    const urlEl = $("#aurl", d), keyEl = $("#akey", d);
    if (urlEl) urlEl.value = draft.url;
    bindSheet(d, shut);
    const skip = $("#askip", d);
    if (skip) skip.onclick = ev => { ev.preventDefault(); draft.url = ""; draft.key = ""; step4(); };
    d.querySelector("form").onsubmit = ev => {
      ev.preventDefault();
      const url = urlEl ? urlEl.value.trim() : "";
      const key = keyEl ? keyEl.value : "";
      if (!url && !String(key).trim()) { draft.url = ""; draft.key = ""; if (keyEl) keyEl.value = ""; step4(); return; }
      const problem = webhookProblem(url, String(key).trim());
      if (problem) { errBox($("#awe", d), problem); return; }
      draft.url = url;
      draft.key = String(key).trim();
      if (keyEl) keyEl.value = "";
      step4();
    };
  };
  const step2 = () => {
    d.innerHTML = `<form class="card wizcard" role="dialog" autocomplete="off" aria-label="Pair keys">${dialogX()}${addStepper(2)}
      <h2>Pair keys</h2>
      <p class="sub">On the bot's machine, run grok-station-reply keygen. Paste the public line here.</p>
      <div class="field"><label>Station public key</label>
        ${staticValue("astapub", el._stationPub, "station public key", STATION_KEY_MISSING)}
        <p class="note">The bot seals replies to this key. Never port-forward the admin port.</p></div>
      <div class="field"><label for="apub">Bot public key</label>
        <input class="inp mono" id="apub" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="Base64, 32 bytes" maxlength="120">
        <p class="note">From grok-station-reply keygen. The private key stays on the bot.</p></div>
      <div id="ape2"></div>
      <div class="field"><button class="btn p" id="apairnext" type="submit">Next</button></div>
      <div class="field"><a href="#" class="skipnow" id="apairskip">Skip for now</a></div></form>`;
    bindSheet(d, shut);
    const skip = $("#apairskip", d);
    if (skip) skip.onclick = ev => { ev.preventDefault(); draft.pub = ""; step3(); };
    d.querySelector("form").onsubmit = ev => {
      ev.preventDefault();
      const inp = $("#apub", d);
      const pub = inp ? inp.value.trim() : "";
      if (!pub) { draft.pub = ""; step3(); return; }
      if (!pubKeyOk(pub)) {
        errBox($("#ape2", d), "Paste the bot's public key: base64 of 32 bytes.");
        if (inp) inp.focus();
        return;
      }
      draft.pub = pub;
      if (inp) inp.value = "";
      step3();
    };
  };
  const step1 = () => {
    d.innerHTML = `<form class="card wizcard" role="dialog" autocomplete="off" aria-label="Add bot">${dialogX()}${addStepper(1)}
      <h2>Add bot</h2><p class="sub">A new teammate. Next you'll pair its key, then connect its webhook.</p>
      <div class="field"><label for="aname">Name</label><input class="inp" id="aname" maxlength="40" required autocomplete="off"></div>
      <div class="field"><label for="aslug">Bot ID <span class="tipwrap info" tabindex="0" role="note" aria-label="${esc(BOT_ID_HELP)}">i<span class="tip">${esc(BOT_ID_HELP)}</span></span></label>
        <input class="inp mono" id="aslug" maxlength="24" autocomplete="off" spellcheck="false" required>
        <p class="note">${esc(BOT_ID_HELP)}</p>
        <p class="err" id="asluge" hidden></p></div>
      <div class="field"><label for="avoice">Voice</label>${voiceOptions(voices, "", "avoice", stationVoiceLabel(el))}</div>
      <div class="field"><button class="btn p" type="submit">Next</button></div></form>`;
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
    bindSheet(d, shut);
    d.querySelector("form").onsubmit = ev => {
      ev.preventDefault();
      const name = $("#aname", d).value.trim();
      const slug = $("#aslug", d).value.trim();
      const problem = showId();
      if (!name || problem) return;
      draft.name = name;
      draft.slug = slug;
      draft.voice = ($("#avoice", d) && $("#avoice", d).value) || "";
      step2();
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
  el._defaultVoice = data.default_voice || "";
  el.innerHTML = `${data.message ? `<div class="banner warn" id="bmsg">${esc(data.message)}</div>` : `<div class="banner warn" id="bmsg" hidden></div>`}
    <div class="banner warn" id="bkilled"${data.killed ? "" : " hidden"}>All bots are revoked. Pair a key again to accept replies.</div>
    <div class="card"><div class="inline"><span class="lbl" id="bcount">${n("connected")} connected · ${n("error")} errors · ${n("not_configured")} not configured</span>
      <span class="stepup" id="sulock" hidden></span>
      <button class="btn dg sm" id="revokeall" type="button">Revoke all bots</button>
      <button class="btn p sm right" id="add">Add bot</button></div>
      <p class="note" id="rttall"></p>
      <div id="blist"></div></div>`;
  applyBotMeta(el, data);
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
  $("#add", el).onclick = () => addBot(el._voices || data.voices || [], el);
  const kill = $("#revokeall", el);
  if (kill) kill.onclick = () => askPassword("Revoke all bots?", "This drops every pinned key. Replies are rejected until a key is paired again.", "Revoke all bots", async pw => {
    const r = await api("POST", "/api/bots/revoke-all", {password: pw});
    if (r && r.ok) { toast("All bots revoked."); refreshBots(el); }
    return r;
  });
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
      ${pwFields("np1", "np2")}<div id="pe"></div><div class="field"><button class="btn p" id="pgo" disabled>Change password</button></div></form></div>
    <div class="card"><h2>Password grace period</h2>
      <p class="sub">After you enter the admin password, later changes on this sign-in skip it for this many minutes. 0 always asks. Signing out ends it.</p>
      <div class="field"><label for="grace">Minutes</label><input class="inp" id="grace" type="number" inputmode="numeric" min="0" max="120" step="1" value="10"></div>
      <p class="note">Changing this always asks for the password.</p>
      <div id="ge"></div>
      <div class="field"><button class="btn p" id="gsave" type="button">Save</button></div></div>`;
  wirePw("np1", "np2", $("#pgo", el));
  $("#pw", el).onsubmit = async ev => {
    ev.preventDefault();
    const r = await api("POST", "/api/password", {old: $("#old", el).value, new: $("#np1", el).value});
    $("#old", el).value = $("#np1", el).value = $("#np2", el).value = "";
    r.ok ? toast("Password changed. Other devices are signed out.") : errBox($("#pe", el), r.data.error);
  };
  const s = (await api("GET", "/api/system")).data || {};
  const g = s.password_grace_min;
  const gEl = $("#grace", el);
  if (gEl && typeof g === "number" && g >= 0 && g <= 120) gEl.value = String(g);
  const gsave = $("#gsave", el);
  if (gsave) gsave.onclick = () => {
    const raw = (gEl && gEl.value || "").trim();
    const n = /^\d+$/.test(raw) ? Number(raw) : NaN;
    if (!Number.isInteger(n) || n < 0 || n > 120) {
      errBox($("#ge", el), "Pick a grace period from 0 to 120 minutes.");
      return;
    }
    errBox($("#ge", el), "");
    askPassword("Password grace period", "Enter the admin password. Changing this always asks, even during the grace window.", "Save", async pw => {
      const r = await api("PUT", "/api/system/password-grace", {password_grace_min: n, password: pw});
      if (r && r.ok) {
        noteStepup(r.data);
        if (gEl) gEl.value = String(n);
        toast(n === 0 ? "The station will always ask for the password." : "Grace period saved.");
      }
      return r;
    });
  };
  const hh = s.health || {};
  const t = s.tls || {};
  $("#tls", el).innerHTML = `<h2>Security certificate</h2><p class="help">This station made its own certificate for ${esc(t.url || "this page")}. If your browser warns about it, check that the browser's SHA-256 fingerprint matches this one exactly (it's also shown on the station screen for 10 minutes after a new certificate is made), then trust it.</p>
    <div class="row"><span>SHA-256</span><span class="right" style="max-width:70%">${staticValue("tlsfp", t.fingerprint || "", "certificate fingerprint", "n/a")}</span></div>`;
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
