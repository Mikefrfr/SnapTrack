"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LABEL = {renewal:"Renews", warranty_end:"Warranty ends", return_deadline:"Return by", due_date:"Payment due", trial_end:"Trial ends", other:"Deadline"};
const CATS = ["groceries","dining","shopping","electronics","utilities","subscription","travel","health","home","other"];
let ME = null, CFG = {}, RECEIPTS = [], mode = "login";

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (r.status === 401 && ME) { ME = null; showAuth(); throw new Error("Session expired, please log in again"); }
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch {}
    if (typeof m !== "string") m = "Please check what you entered";
    throw new Error(m);
  }
  return r.json();
}
const send = (p, body, method = "POST") => api(p, {method, headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});

const daysLeft = d => { const t = new Date(); t.setHours(0,0,0,0); return Math.round((new Date(d + "T00:00:00") - t) / 864e5); };
const money = (v, cur) => v == null ? "—" : `${Number(v).toFixed(2)} ${cur || ""}`.trim();
function pill(d) {
  const n = daysLeft(d.date);
  const cls = n < 0 ? "past" : n <= 3 ? "urgent" : n <= 14 ? "soon" : "";
  const when = n < 0 ? "passed" : n === 0 ? "today" : n === 1 ? "tomorrow" : `in ${n}d`;
  return `<span class="pill ${cls}">${esc(LABEL[d.kind] || "Deadline")} · ${esc(d.date)} (${when})</span>`;
}
const nextDeadline = r => r.deadlines.filter(d => daysLeft(d.date) >= 0).sort((a, b) => a.date.localeCompare(b.date))[0];

/* ---------- views ---------- */
function showAuth() { $("#app").hidden = true; $("#auth").hidden = false; $("#code-wrap").hidden = !CFG.signup_code_required || mode === "login"; }
function showApp() { $("#auth").hidden = true; $("#app").hidden = false; load(); }
function setMode(m) {
  mode = m;
  $("#tab-login").classList.toggle("on", m === "login");
  $("#tab-signup").classList.toggle("on", m === "signup");
  $("#auth-btn").textContent = m === "login" ? "Log in" : "Create account";
  $("#pw").autocomplete = m === "login" ? "current-password" : "new-password";
  $("#code-wrap").hidden = !CFG.signup_code_required || m === "login";
  $("#auth-msg").textContent = "";
}
$("#tab-login").onclick = () => setMode("login");
$("#tab-signup").onclick = () => setMode("signup");
$("#auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  $("#auth-msg").textContent = "";
  try {
    await send(mode === "login" ? "/api/login" : "/api/register", {identifier: $("#ident").value, password: $("#pw").value, code: $("#code").value});
    $("#pw").value = "";
    ME = await api("/api/me");
    showApp();
  } catch (err) { $("#auth-msg").textContent = err.message; }
});

/* ---------- gallery ---------- */
async function load() {
  const [rs, sm] = await Promise.all([api("/api/receipts"), api("/api/summary")]);
  RECEIPTS = rs;
  $("#summary").textContent = sm.totals.length ? "This month: " + sm.totals.map(t => `${t.total.toFixed(2)} ${t.cur}`.trim()).join("  ·  ") : "";
  const cats = [...new Set(rs.map(r => r.category).filter(Boolean))].sort();
  const cur = $("#cat").value;
  $("#cat").innerHTML = `<option value="">All categories</option>` + cats.map(c => `<option ${c === cur ? "selected" : ""}>${esc(c)}</option>`).join("");
  render();
}
function render() {
  const q = $("#search").value.trim().toLowerCase(), cat = $("#cat").value, only = $("#onlydl").checked;
  const list = RECEIPTS.filter(r =>
    (!q || [r.store, r.category, r.doc_type, r.notes].join(" ").toLowerCase().includes(q)) &&
    (!cat || r.category === cat) && (!only || nextDeadline(r)));
  $("#empty").hidden = RECEIPTS.length > 0;
  $("#grid").innerHTML = list.map(r => {
    const u = nextDeadline(r);
    const img = r.mime.startsWith("image/") ? `<img loading="lazy" src="/api/receipts/${r.id}/thumb" alt="">` : `<div class="pdf">PDF</div>`;
    return `<button class="gcard" data-open="${r.id}"><div class="gimg">${img}</div><div class="ginfo">
      <div class="store">${esc(r.store || "Unknown store")}</div><div class="muted">${esc(money(r.total, r.currency))}</div>
      ${u ? pill(u) : '<span class="pill">no deadline</span>'}</div></button>`;
  }).join("");
}
["#search", "#cat", "#onlydl"].forEach(s => $(s).addEventListener("input", render));

/* ---------- detail ---------- */
async function openDetail(id, edit = false) {
  const r = await api("/api/receipts/" + id);
  const media = r.mime.startsWith("image/")
    ? `<a href="/api/receipts/${r.id}/file" target="_blank"><img src="/api/receipts/${r.id}/file" alt="Uploaded document"></a>`
    : `<a href="/api/receipts/${r.id}/file" target="_blank">Open PDF</a>`;
  let body;
  if (edit) {
    body = `<form id="edit-form" data-id="${r.id}">
      <label>Store</label><input name="store" type="text" value="${esc(r.store)}">
      <label>Total</label><input name="total" type="number" step="0.01" value="${r.total ?? ""}">
      <label>Currency</label><input name="currency" type="text" value="${esc(r.currency)}">
      <label>Category</label><select name="category">${CATS.map(c => `<option ${c === r.category ? "selected" : ""}>${c}</option>`).join("")}</select>
      <label>Purchase date</label><input name="purchase_date" type="date" value="${esc(r.purchase_date)}">
      <label>Notes</label><textarea name="notes" rows="3">${esc(r.notes)}</textarea>
      <div class="actions row"><button class="primary" type="submit">Save</button><button type="button" class="ghost" data-open="${r.id}">Cancel</button></div></form>`;
  } else {
    const items = r.items.length ? `<h3>Items</h3><table><tr><th>Item</th><th class="num">Qty</th><th class="num">Price</th></tr>${
      r.items.map(i => `<tr><td>${esc(i.name)}</td><td class="num">${esc(i.qty ?? "")}</td><td class="num">${i.price == null ? "" : Number(i.price).toFixed(2)}</td></tr>`).join("")}</table>` : "";
    const dls = r.deadlines.map(d => `<div class="dl-item"><span>${pill(d)} <span class="muted small">${esc(d.label)}</span></span>
      <button class="ghost" data-deldl="${d.id}" data-rid="${r.id}">Remove</button></div>`).join("") || `<p class="muted small">No deadlines found.</p>`;
    body = `<dl>
      <dt>Total</dt><dd>${esc(money(r.total, r.currency))}</dd>
      <dt>Tax</dt><dd>${esc(r.tax == null ? "—" : Number(r.tax).toFixed(2))}</dd>
      <dt>Category</dt><dd>${esc(r.category || "—")}</dd>
      <dt>Type</dt><dd>${esc(r.doc_type || "—")}</dd>
      <dt>Document no.</dt><dd>${esc(r.document_number || "—")}</dd>
      <dt>Purchase date</dt><dd>${esc(r.purchase_date || "—")}</dd>
      <dt>Notes</dt><dd>${esc(r.notes || "—")}</dd>
      <dt>Uploaded</dt><dd>${esc(r.created_at)} UTC</dd></dl>
      ${items}<h3>Deadlines &amp; alerts</h3>${dls}
      <div class="add-dl" data-rid="${r.id}">
        <select id="n-kind">${Object.entries(LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input id="n-date" type="date"><input id="n-label" type="text" placeholder="Note (optional)">
        <button class="ghost" data-adddl="${r.id}">+ Add deadline</button></div>
      <div class="actions row"><button class="ghost" data-edit="${r.id}">Edit details</button>
        <button class="ghost danger" data-del="${r.id}">Delete</button></div>`;
  }
  $("#detail-body").innerHTML = `<div class="dlg-head"><h2>${esc(r.store || "Unknown store")}</h2><button class="ghost" data-close="detail">✕</button></div>
    <div class="media">${media}</div>${body}`;
  if (!$("#detail").open) $("#detail").showModal();
}

/* ---------- upload ---------- */
async function bitmapTo(file, max, quality) {
  const b = await createImageBitmap(file);
  const s = Math.min(1, max / Math.max(b.width, b.height));
  const c = document.createElement("canvas");
  c.width = Math.round(b.width * s); c.height = Math.round(b.height * s);
  c.getContext("2d").drawImage(b, 0, 0, c.width, c.height);
  return new Promise(r => c.toBlob(r, "image/jpeg", quality));
}
$("#add").onclick = () => $("#file").click();
$("#file").onchange = async e => {
  const m = $("#msg");
  for (const f of e.target.files) {
    m.className = "muted"; m.textContent = `Reading ${f.name}…`;
    try {
      const fd = new FormData();
      let main = f;
      if (f.type.startsWith("image/") && f.type !== "image/gif") {
        try {
          main = new File([await bitmapTo(f, 1800, .85)], "receipt.jpg", {type: "image/jpeg"});
          fd.append("thumb", new File([await bitmapTo(f, 360, .7)], "t.jpg", {type: "image/jpeg"}));
        } catch { main = f; }
      }
      fd.append("file", main);
      const r = await api("/api/upload", {method: "POST", body: fd});
      m.className = "good";
      m.textContent = `Saved: ${r.store || "document"} ${money(r.total, r.currency)} · ${r.deadlines.length} deadline(s) found`;
    } catch (err) { m.className = "err"; m.textContent = "Failed: " + err.message; }
    await load();
  }
  e.target.value = "";
};

/* ---------- settings ---------- */
$("#open-settings").onclick = () => {
  $("#who").textContent = "Signed in as " + ME.identifier;
  $("#s-email").value = ME.alert_email; $("#s-topic").value = ME.ntfy_topic; $("#s-msg").textContent = "";
  $("#s-status").textContent = `AI: ${ME.ai ? "ready" : "MISSING API KEY"} · Server email: ${ME.smtp ? "configured" : "not configured"} · Alerts ${ME.alert_days.join(", ")} days before a deadline`;
  $("#settings").showModal();
};
async function saveSettings() {
  await send("/api/me", {alert_email: $("#s-email").value, ntfy_topic: $("#s-topic").value}, "PUT");
  ME.alert_email = $("#s-email").value.trim().toLowerCase(); ME.ntfy_topic = $("#s-topic").value.trim();
}
$("#s-save").onclick = async () => { const m = $("#s-msg"); try { await saveSettings(); m.className = "good"; m.textContent = "Saved ✓"; } catch (e) { m.className = "err"; m.textContent = e.message; } };
$("#s-test").onclick = async () => { const m = $("#s-msg"); m.className = "muted"; m.textContent = "Sending…"; try { await saveSettings(); await api("/api/test-alert", {method: "POST"}); m.className = "good"; m.textContent = "Test sent ✓"; } catch (e) { m.className = "err"; m.textContent = e.message; } };
$("#logout").onclick = async () => { await api("/api/logout", {method: "POST"}); ME = null; RECEIPTS = []; $("#settings").close(); $("#grid").innerHTML = ""; showAuth(); };

/* ---------- clicks & forms ---------- */
document.addEventListener("click", async e => {
  const t = e.target.closest("button, a"); if (!t) return;
  const d = t.dataset;
  try {
    if (d.open) openDetail(d.open);
    else if (d.close) $("#" + d.close).close();
    else if (d.edit) openDetail(d.edit, true);
    else if (d.del && confirm("Delete this receipt and its file?")) { await api("/api/receipts/" + d.del, {method: "DELETE"}); $("#detail").close(); load(); }
    else if (d.deldl) { await api("/api/deadlines/" + d.deldl, {method: "DELETE"}); await load(); openDetail(d.rid); }
    else if (d.adddl) {
      const date = $("#n-date").value; if (!date) { alert("Pick a date first"); return; }
      await send(`/api/receipts/${d.adddl}/deadlines`, {kind: $("#n-kind").value, label: $("#n-label").value, date});
      await load(); openDetail(d.adddl);
    }
  } catch (err) { alert(err.message); }
});
document.addEventListener("submit", async e => {
  if (e.target.id !== "edit-form") return;
  e.preventDefault();
  const f = e.target, v = n => f.elements[n].value;
  try {
    await send("/api/receipts/" + f.dataset.id, {store: v("store"), total: v("total") === "" ? null : parseFloat(v("total")),
      currency: v("currency"), category: v("category"), purchase_date: v("purchase_date") || null, notes: v("notes")}, "PUT");
    await load(); openDetail(f.dataset.id);
  } catch (err) { alert(err.message); }
});

/* ---------- boot ---------- */
(async () => {
  CFG = await api("/api/config");
  try { ME = await api("/api/me"); showApp(); } catch { showAuth(); }
})();
