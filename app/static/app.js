const $ = (id) => document.getElementById(id);
const esc = (v) => v == null ? "" : String(v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => n == null ? "" : Number(n).toLocaleString(undefined,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = (iso) => iso ? new Date(iso + "Z").toLocaleString() : "";
const MAX_ROWS = 1000;

let meta = null;       // benchmarks, frameworks and upload kinds from the server
let result = null;     // audit result currently shown
let checkNames = {};

// --- Requests -------------------------------------------------------------

async function request(url, options = {}) {
  const res = await fetch(url, options);
  if (res.status === 401 && !url.startsWith("/api/auth/")) {
    showLogin(false);
    throw new Error("Please sign in.");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const detail = (await res.json()).detail;
      msg = typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ")
        : msg;
    } catch {}
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (url) => (await request(url)).json();
const sendJSON = async (method, url, body) => {
  const res = await request(url, { method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  return res.status === 204 ? null : res.json();
};
const postForm = (url, fd) => request(url, { method: "POST", body: fd });

function showError(msg) {
  $("error").textContent = msg || "";
  $("error").hidden = !msg;
}
async function guard(fn) {
  showError("");
  try { return await fn(); } catch (err) { showError(err.message); }
}

// Like guard, with the button disabled until the request finishes, so a double-click
// doesn't send it twice. `ready` says whether the button should be enabled afterwards.
async function busy(button, fn, ready = () => true) {
  button.busy = (button.busy || 0) + 1;
  button.disabled = true;
  try { return await guard(fn); } finally {
    button.busy -= 1;
    if (!button.busy) button.disabled = !ready();
  }
}

function formObject(form) {
  const out = {};
  for (const [k, v] of new FormData(form)) if (typeof v === "string") out[k] = v.trim();
  return out;
}

async function saveFile(res, fallback) {
  const header = res.headers.get("Content-Disposition") || "";
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/)?.[1];
  const name = (encoded && decodeURIComponent(encoded)) || header.match(/filename="([^"]+)"/)?.[1] || fallback;
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  URL.revokeObjectURL(url);
}

// --- Column mapping component ----------------------------------------------

function Mapper(root, onChange = () => {}) {
  let file = null;
  root.innerHTML = `
    <div class="mapper" hidden>
      <div class="grid" data-sheet-wrap hidden style="margin-top:14px">
        <div><label>Sheet</label><select class="map" data-sheet></select></div>
      </div>
      <h2 style="margin-top:18px">Column mapping</h2>
      <p class="muted">Check which column in your file holds each field. Map either a signed Amount column, or Debit and Credit columns.</p>
      <div class="mapping" data-mapping></div>
      <p class="muted" data-note></p>
      <div class="table-wrap"><table class="preview-table" data-preview></table></div>
    </div>`;
  const q = (sel) => root.querySelector(sel);

  async function load(f, sheet = "") {
    file = f;
    q(".mapper").hidden = true;
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    if (sheet) fd.append("sheet", sheet);
    const p = await (await postForm("/api/preview", fd)).json();
    q("[data-sheet-wrap]").hidden = p.sheets.length < 2;
    q("[data-sheet]").innerHTML = p.sheets.map((s) =>
      `<option ${s === p.sheet ? "selected" : ""}>${esc(s)}</option>`).join("");
    const options = (selected) => '<option value="">(not in file)</option>' + p.columns.map((c) =>
      `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`).join("");
    q("[data-mapping]").innerHTML = p.fields.map((f) => `
      <div>
        <label>${esc(f.label)}</label>
        <select class="map" data-field="${f.id}">${options(p.suggested[f.id])}</select>
      </div>`).join("");
    q("[data-note]").textContent =
      `First ${p.rows.length} of ${p.row_count.toLocaleString()} rows in the file:`;
    q("[data-preview]").innerHTML = "<tr>" + p.columns.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr>" +
      p.rows.map((r) => "<tr>" + r.map((v) => `<td title="${esc(v)}">${esc(v)}</td>`).join("") + "</tr>").join("");
    q(".mapper").hidden = false;
  }

  q("[data-sheet]").addEventListener("change", () => guard(() => load(file, q("[data-sheet]").value)));
  q("[data-mapping]").addEventListener("change", onChange);

  return {
    load: (f) => guard(() => load(f)),
    reset: () => { file = null; q(".mapper").hidden = true; },
    addTo(fd) {
      if (q(".mapper").hidden) return fd;
      const mapping = {};
      root.querySelectorAll("select[data-field]").forEach((s) => { if (s.value) mapping[s.dataset.field] = s.value; });
      fd.set("mapping", JSON.stringify(mapping));
      if (!q("[data-sheet-wrap]").hidden) fd.set("sheet", q("[data-sheet]").value);
      return fd;
    },
  };
}

// --- Views and routing ------------------------------------------------------

// Lowest rank that can open each view; anything else falls back to engagements.
const VIEW_RANK = { engagements: 1, users: 4, activity: 4 };

function route() {
  if (!me) return;
  let [, view = "engagements", id] = location.hash.split("/");
  if (me.role === "client") {
    view = "portal";  // client users only have the portal
    if (location.hash !== "#/portal") history.replaceState(null, "", "#/portal");
  } else if (!(me.rank >= (VIEW_RANK[view] || Infinity))) {
    view = "engagements";
    id = undefined;
    history.replaceState(null, "", "#/engagements");
  }
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  $("results").hidden = true;
  showError("");
  if (view === "engagements") guard(() => showEngagements(id ? Number(id) : null));
  if (view === "users") guard(showUsers);
  if (view === "activity") guard(showActivity);
  if (view === "portal") guard(showPortal);
}
window.addEventListener("hashchange", route);

// --- Engagements view -------------------------------------------------------

let currentEngagement = null;
let newEngagementClient = null;

async function showEngagements(engagementId) {
  await renderClients(engagementId);
  $("eng-new").hidden = true;
  if (engagementId) {
    await openEngagement(engagementId);
  } else {
    currentEngagement = null;
    $("eng-detail").hidden = true;
    $("eng-empty").hidden = false;
  }
}

async function renderClients(activeId) {
  const clients = await getJSON("/api/clients");
  $("client-list").innerHTML = clients.length ? clients.map((c) => `
    <div class="client">
      <div class="client-name"><span>${esc(c.name)}</span>
        <button type="button" data-new-eng="${c.id}" class="link min-2" data-client-name="${esc(c.name)}">+ engagement</button></div>
      ${c.industry ? `<div class="muted" style="font-size:13px">${esc(c.industry)}</div>` : ""}
      <ul>${c.engagements.map((e) => `<li><a href="#/engagements/${e.id}" class="${e.id === activeId ? "active" : ""}">
        ${esc(e.name)} <span class="muted">· ${esc(e.period_end)}</span></a></li>`).join("")}</ul>
    </div>`).join("") : '<p class="muted">No clients yet.</p>';
  $("new-client").open = !clients.length;
}

$("client-list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-new-eng]");
  if (!btn) return;
  newEngagementClient = Number(btn.dataset.newEng);
  $("eng-new-client").textContent = btn.dataset.clientName;
  $("eng-form").reset();
  $("eng-empty").hidden = $("eng-detail").hidden = true;
  $("eng-new").hidden = false;
  $("eng-name").focus();
});

$("client-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard(async () => {
    await sendJSON("POST", "/api/clients", formObject($("client-form")));
    $("client-form").reset();
    await renderClients(currentEngagement?.id);
  });
});

$("eng-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard(async () => {
    const body = formObject($("eng-form"));
    if (!body.period_start) delete body.period_start;
    const eng = await sendJSON("POST", `/api/clients/${newEngagementClient}/engagements`, body);
    location.hash = `#/engagements/${eng.id}`;
  });
});

async function openEngagement(id) {
  const e = await getJSON(`/api/engagements/${id}`);
  if (currentEngagement?.id !== e.id) {
    // Check ids differ between engagements, so don't carry that filter over.
    regOffset = 0; regSelected.clear(); $("reg-check").value = "";
    // Uploads differ too, so let the server pick this engagement's latest trial balances.
    fsMapEdits = {}; $("fs-tb").innerHTML = $("fs-prior").innerHTML = "";
    // Hide the last engagement's statements and findings while this one's load, so a quick
    // click can't change an entry or finding of the engagement just left.
    fs = null; fsEngagementId = null; renderStatementsPanel();
    reg = null; $("reg-cards").innerHTML = $("reg-table").innerHTML = $("reg-count").textContent = "";
    $("reg-bulk").hidden = true;
    resetEntryForm();
    $("rv-form").reset();
    rv = null; rvDrafts = {};  // don't show (or act on) the last engagement's sign-offs
    $("rv-cards").innerHTML = $("rv-sections").innerHTML = $("rv-notes").innerHTML = "";
    $("rq-form").reset();
    rq = null; rqDrafts = {};
    $("rq-cards").innerHTML = $("rq-list").innerHTML = $("rq-contacts").textContent = "";
  }
  currentEngagement = e;
  $("eng-empty").hidden = true;
  $("eng-detail").hidden = false;
  $("eng-title").textContent = `${e.client.name} · ${e.name}`;
  $("eng-meta").textContent = `${e.period_start ? e.period_start + " to " : "Period ending "}${e.period_end} · ${e.framework}`;
  renderMateriality(e.materiality);
  renderUploads(e.uploads);
  renderRuns(e.runs);
  renderComparisonForm(e);
  renderComparisons(e.comparisons);
  renderSampleForm(e);
  renderSamples(e.samples);
  if (currentFinding && currentFinding.engagement_id !== e.id) closeFinding();
  guard(loadReview);  // on its own, so a slow or failing panel above doesn't hold it up
  guard(loadRequests);
  await loadStatements();
  await loadRegister();
}

$("eng-delete").addEventListener("click", () => {
  const e = currentEngagement;
  if (!e || !confirm(`Delete ${e.name} for ${e.client.name}, with all its files and runs?`)) return;
  guard(async () => {
    await sendJSON("DELETE", `/api/engagements/${e.id}`);
    location.hash = "#/engagements";
  });
});

// Materiality

function benchmarkInfo(id) { return meta.benchmarks.find((b) => b.id === id); }

function renderMateriality(m) {
  const f = $("mat-form");
  f.benchmark.value = m?.benchmark || "pbt";
  f.benchmark_amount.value = m?.benchmark_amount ?? "";
  f.benchmark_pct.value = m?.benchmark_pct ?? benchmarkInfo(f.benchmark.value).default_pct;
  f.performance_pct.value = m?.performance_pct ?? meta.default_performance_pct;
  f.trivial_pct.value = m?.trivial_pct ?? meta.default_trivial_pct;
  f.rationale.value = m?.rationale || "";
  $("mat-saved").textContent = m ? "Saved." : "Not set yet; the materiality check is skipped until it is.";
  updateMatRange();
  previewMateriality();
}

function updateMatRange() {
  $("mat-range").textContent = `(typical ${benchmarkInfo($("mat-benchmark").value).range})`;
}

function materialityBody() {
  const o = formObject($("mat-form"));
  const num = (v) => v === "" ? null : Number(v);
  return { benchmark: o.benchmark, benchmark_amount: num(o.benchmark_amount),
    benchmark_pct: num(o.benchmark_pct), performance_pct: num(o.performance_pct),
    trivial_pct: num(o.trivial_pct), rationale: o.rationale };
}

async function previewMateriality() {
  const body = materialityBody();
  if (!body.benchmark_amount) { $("mat-cards").innerHTML = ""; return; }
  try {
    const m = await sendJSON("POST", "/api/materiality/calculate", body);
    $("mat-cards").innerHTML = [
      ["Overall materiality", m.overall_materiality],
      ["Performance materiality", m.performance_materiality],
      ["Clearly trivial", m.trivial_threshold],
    ].map(([k, v]) => `<div class="card"><div class="v">${money(v)}</div><div class="k">${k}</div></div>`).join("");
  } catch (err) {
    $("mat-cards").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
}

$("mat-benchmark").addEventListener("change", () => {
  $("mat-pct").value = benchmarkInfo($("mat-benchmark").value).default_pct;
  updateMatRange();
  previewMateriality();
});
$("mat-form").addEventListener("input", (e) => {
  $("mat-saved").textContent = "";
  if (e.target.name !== "rationale") previewMateriality();
});
$("mat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard(async () => {
    const eng = await sendJSON("PUT", `/api/engagements/${currentEngagement.id}/materiality`, materialityBody());
    currentEngagement = eng;
    renderMateriality(eng.materiality);
  });
});

// Uploads

const uploadMapper = Mapper($("up-mapper"));
$("up-file").addEventListener("change", () => uploadMapper.load($("up-file").files[0]));

$("upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard(async () => {
    const fd = uploadMapper.addTo(new FormData($("upload-form")));
    await postForm(`/api/engagements/${currentEngagement.id}/uploads`, fd);
    $("fs-tb").innerHTML = $("fs-prior").innerHTML = "";  // show the new file's statements
    $("upload-form").reset();
    uploadMapper.reset();
    await openEngagement(currentEngagement.id);
  });
});

function renderUploads(uploads) {
  $("uploads").innerHTML = uploads.length ? "<tr><th>File</th><th>Type</th><th>Rows</th><th>Uploaded</th><th>Import</th><th></th></tr>" +
    uploads.map((u) => `<tr>
      <td>${esc(u.filename)}<div class="muted" style="font-size:12px" title="SHA-256">${esc(u.sha256.slice(0, 12))}…</div></td>
      <td>${esc(u.kind_label)}</td>
      <td class="num">${u.import.rows_used.toLocaleString()}</td>
      <td class="nowrap">${esc(when(u.uploaded_at))}</td>
      <td class="muted">${u.import.warnings.length ? esc(u.import.warnings.join(" ")) : "OK"}</td>
      <td><div class="cell-actions">
        <button type="button" class="small" data-run="${u.id}">Run audit</button>
        <button type="button" class="small secondary danger min-2" data-delete-upload="${u.id}">Delete</button>
      </div></td></tr>`).join("")
    : '<tr><td class="muted">No files uploaded yet.</td></tr>';
}

$("uploads").addEventListener("click", (e) => {
  const run = e.target.closest("[data-run]");
  const del = e.target.closest("[data-delete-upload]");
  if (run) {
    guard(async () => {
      run.disabled = true;
      run.textContent = "Running…";
      const r = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/runs`, {
        upload_id: Number(run.dataset.run),
        cutoff_days: Number($("run-cutoff").value || 0),
        round_threshold: Number($("run-round").value || 1000),
      });
      await openEngagement(currentEngagement.id);
      showRun(r);
    });
  } else if (del && confirm("Delete this file and the audit runs made from it?")) {
    guard(async () => {
      await sendJSON("DELETE", `/api/uploads/${del.dataset.deleteUpload}`);
      await openEngagement(currentEngagement.id);
    });
  }
});

// Runs

function renderRuns(runs) {
  $("runs").innerHTML = runs.length ? "<tr><th>Run</th><th>When</th><th>File</th><th>Rows</th><th>High</th><th>Medium</th><th>Low</th><th></th></tr>" +
    runs.map((r) => `<tr>
      <td>#${r.id}</td><td class="nowrap">${esc(when(r.created_at))}</td><td>${esc(r.filename)}</td>
      <td class="num">${r.rows.toLocaleString()}</td>
      <td class="num">${r.findings_by_severity.high}</td><td class="num">${r.findings_by_severity.medium}</td>
      <td class="num">${r.findings_by_severity.low}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-view-run="${r.id}">View</button>
        <a class="button" href="/api/runs/${r.id}/report">Excel</a>
        <a class="button" href="/api/runs/${r.id}/report?format=csv">CSV</a>
      </div></td></tr>`).join("")
    : '<tr><td class="muted">No runs yet. Upload a file and press Run audit.</td></tr>';
}

$("runs").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view-run]");
  if (btn) guard(async () => showRun(await getJSON(`/api/runs/${btn.dataset.viewRun}`)));
});

function showRun(r) {
  showResults(r.result, `Run #${r.id} · ${r.filename}`,
    `<a class="button" href="/api/runs/${r.id}/report">Download Excel</a>
     <a class="button" href="/api/runs/${r.id}/report?format=csv">Download CSV</a>`);
  $("results").scrollIntoView({ behavior: "smooth" });
}

// Prior-year comparison

function renderComparisonForm(e) {
  const opts = (kinds, preferred) => e.uploads
    .filter((u) => kinds.includes(u.kind))
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a file first</option>';
  $("cmp-current").innerHTML = opts(["tb", "gl"]);
  $("cmp-prior").innerHTML = opts(["prior_tb", "tb", "gl"]);
  const prior = e.uploads.filter((u) => u.kind === "prior_tb").pop();
  if (prior) $("cmp-prior").value = prior.id;
  const pm = e.materiality?.performance_materiality;
  $("cmp-amount").placeholder = pm ? `${money(pm)} (performance materiality)` : "Amount";
  $("cmp-result").hidden = true;
}

$("cmp-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const c = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/comparisons`, {
      current_upload_id: Number($("cmp-current").value),
      prior_upload_id: Number($("cmp-prior").value),
      threshold_amount: $("cmp-amount").value ? Number($("cmp-amount").value) : null,
      threshold_pct: Number($("cmp-pct").value || 0),
    });
    await openEngagement(currentEngagement.id);
    showComparison(c);
  });
});

function renderComparisons(list) {
  $("comparisons").innerHTML = list.length ? "<tr><th>#</th><th>When</th><th>This year</th><th>Prior year</th><th>Threshold</th><th>Flagged</th><th></th></tr>" +
    list.map((c) => `<tr><td>${c.id}</td><td class="nowrap">${esc(when(c.created_at))}</td>
      <td>${esc(c.current_filename)}</td><td>${esc(c.prior_filename)}</td>
      <td class="num">${money(c.settings.threshold_amount)} / ${c.settings.threshold_pct}%</td>
      <td class="num">${c.summary.flagged} of ${c.summary.accounts}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-view-cmp="${c.id}">View</button>
        <a class="button" href="/api/comparisons/${c.id}/report">Excel</a>
      </div></td></tr>`).join("") : "";
}

$("comparisons").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-view-cmp]");
  if (btn) guard(async () => showComparison(await getJSON(`/api/comparisons/${btn.dataset.viewCmp}`)));
});

let comparison = null;

function showComparison(c) {
  comparison = c;
  const s = c.result.summary;
  $("cmp-result").hidden = false;
  $("cmp-title").textContent = `Comparison #${c.id}: ${c.current_filename} vs ${c.prior_filename}`;
  $("cmp-actions").innerHTML = `<a class="button" href="/api/comparisons/${c.id}/report">Download Excel</a>`;
  $("cmp-cards").innerHTML = [
    ["Accounts compared", s.accounts], ["Flagged", s.flagged],
    ["New accounts", s.new_accounts], ["No longer in file", s.removed_accounts],
    ["Threshold", money(c.result.settings.threshold_amount)],
  ].map(([k, v]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  renderComparisonTable();
  $("cmp-result").scrollIntoView({ behavior: "smooth" });
}

function renderComparisonTable() {
  const only = $("cmp-flagged-only").checked;
  const rows = comparison.result.rows.filter((r) => !only || r.flagged);
  $("cmp-table").innerHTML = "<tr><th>Account</th><th>Name</th><th>This year</th><th>Prior year</th><th>Change</th><th>%</th><th>Why flagged</th></tr>" +
    rows.map((r) => `<tr><td>${esc(r.account)}</td><td>${esc(r.name)}</td>
      <td class="num">${money(r.current)}</td><td class="num">${money(r.prior)}</td>
      <td class="num">${money(r.change)}</td>
      <td class="num">${r.change_pct == null ? "–" : (r.change_pct > 0 ? "+" : "") + r.change_pct + "%"}</td>
      <td>${r.reasons.map((x) => `<div>${esc(x)}</div>`).join("")}</td></tr>`).join("");
}
$("cmp-flagged-only").addEventListener("change", renderComparisonTable);

// Lead sheets and draft statements

let fs = null;          // latest /statements response
let fsEngagementId = null;  // the engagement `fs` belongs to
let fsMapEdits = {};    // unsaved mapping changes: account -> line id ("" to unmap)
const signed = (n) => n == null ? "" : n < 0 ? `(${money(-n)})` : money(n);
const drcr = (n) => Math.abs(n) < 0.005 ? "0.00" : `${money(Math.abs(n))} ${n > 0 ? "Dr" : "Cr"}`;

function fsQuery() {
  // Only ask for files that still exist; otherwise the server picks the latest of each kind.
  const ids = new Set(currentEngagement.uploads.map((u) => String(u.id)));
  const q = new URLSearchParams();
  if (ids.has($("fs-tb").value)) q.set("upload_id", $("fs-tb").value);
  if (ids.has($("fs-prior").value)) q.set("prior_upload_id", $("fs-prior").value);
  return q.toString();
}

const FS_EMPTY = $("fs-empty").textContent;

async function loadStatements() {
  const id = currentEngagement.id;
  let data, error = null;
  try {
    data = await getJSON(`/api/engagements/${id}/statements?${fsQuery()}`);
  } catch (err) {
    error = err.message;  // shown in the panel, so the rest of the engagement still loads
  }
  if (currentEngagement?.id !== id) return;  // the user has opened another engagement
  fs = error ? null : data;
  fsEngagementId = error ? null : id;
  renderStatementsPanel(error);
}

function renderStatementsPanel(error = null) {
  const ready = !!fs?.tb;
  $("fs-empty").textContent = error ? `The lead sheets can't be built from this trial balance: ${error}` : FS_EMPTY;
  $("fs-empty").hidden = ready;
  $("fs-body").hidden = $("fs-export").hidden = !ready;
  if (!ready) return;
  $("fs-tb").innerHTML = fs.tb_choices.map((u) =>
    `<option value="${u.id}"${u.id === fs.tb.id ? " selected" : ""}>${esc(u.filename)} (${esc(when(u.uploaded_at))})</option>`).join("");
  $("fs-prior").innerHTML = fs.prior_choices.length ? fs.prior_choices.map((u) =>
    `<option value="${u.id}"${u.id === fs.prior_tb?.id ? " selected" : ""}>${esc(u.filename)} (${esc(when(u.uploaded_at))})</option>`).join("")
    : '<option value="">None uploaded</option>';
  $("fs-prior").disabled = !fs.prior_choices.length;
  $("fs-export").href = `/api/engagements/${currentEngagement.id}/statements/export?${fsQuery()}`;
  const st = fs.statements;
  const assets = st.balance_sheet.find((r) => r.label === "Total assets");
  const cards = [
    [signed(st.profit.current), "Profit for the year (adjusted)"],
    [signed(assets.current), "Total assets"],
    [fs.unmapped.length, "Unmapped accounts"],
    [fs.entries.length, "Adjusting entries"],
  ];
  if (Math.abs(st.difference.current) >= 0.005) cards.push([signed(st.difference.current), "Balance sheet out by"]);
  $("fs-cards").innerHTML = cards.map(([v, k]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${k}</div></div>`).join("");
  renderMapping();
  renderEntries();
  renderLeadSheets();
  renderStatementTables();
  renderUncorrected();
}

["fs-tb", "fs-prior"].forEach((id) => $(id).addEventListener("change", () => guard(loadStatements)));

$("fs-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-tab]");
  if (!tab) return;
  $("fs-tabs").querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b === tab));
  document.querySelectorAll("#fs-body .tab").forEach((p) => { p.hidden = p.dataset.pane !== tab.dataset.tab; });
});

// Mapping

function lineOptions(selected) {
  return '<option value="">Not mapped</option>' + fs.sections.map((s) => `<optgroup label="${esc(s.label)}">` +
    fs.lines.filter((l) => l.section === s.id).map((l) =>
      `<option value="${l.id}"${l.id === selected ? " selected" : ""}>${esc(l.label)}</option>`).join("") + "</optgroup>").join("");
}

function renderMapping() {
  const lineLabel = Object.fromEntries(fs.lines.map((l) => [l.id, l.label]));
  const mapped = fs.accounts.filter((a) => a.line).length;
  $("fs-map-note").textContent = `${mapped} of ${fs.accounts.length} accounts mapped.` +
    (fs.unmapped.length ? " Unmapped accounts are left out of the statements, so the balance sheet won't balance until they are mapped." : "");
  const onlyUnmapped = $("fs-unmapped-only").checked;
  const rows = fs.accounts.filter((a) => !onlyUnmapped || !a.line || a.account in fsMapEdits);
  $("fs-map").innerHTML = "<tr><th>Account</th><th>Name</th><th class=\"num\">Adjusted balance</th><th>Financial statement line</th></tr>" +
    rows.map((a) => {
      const value = a.account in fsMapEdits ? fsMapEdits[a.account] : a.line || "";
      const hint = !a.line && a.suggested && !(a.account in fsMapEdits)
        ? `<div class="tag">Suggested: ${esc(lineLabel[a.suggested])}</div>` : "";
      return `<tr data-account="${esc(a.account)}"${a.account in fsMapEdits ? ' class="changed"' : ""}>
        <td>${esc(a.account)}${a.in_tb ? "" : ` <span class="tag">(${a.prior != null ? "prior year only" : "from entries"})</span>`}</td><td>${esc(a.name)}</td>
        <td class="num">${drcr(a.adjusted)}</td>
        <td><select class="map" aria-label="Line for account ${esc(a.account)}">${lineOptions(value)}</select>${hint}</td></tr>`;
    }).join("");
  $("fs-save-map").disabled = !canSaveMapping() || $("fs-save-map").busy > 0;
}

$("fs-map").addEventListener("change", (ev) => {
  const row = ev.target.closest("[data-account]");
  if (!row) return;
  const account = row.dataset.account;
  const saved = fs.accounts.find((a) => a.account === account).line || "";
  if (ev.target.value === saved) delete fsMapEdits[account]; else fsMapEdits[account] = ev.target.value;
  row.classList.toggle("changed", account in fsMapEdits);
  $("fs-save-map").disabled = !canSaveMapping() || $("fs-save-map").busy > 0;
});
$("fs-unmapped-only").addEventListener("change", renderMapping);
$("fs-accept").addEventListener("click", () => {
  fs.accounts.filter((a) => !a.line && a.suggested && !(a.account in fsMapEdits))
    .forEach((a) => { fsMapEdits[a.account] = a.suggested; });
  renderMapping();
});
const canSaveMapping = () => Object.keys(fsMapEdits).length > 0;
$("fs-save-map").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  const id = fsEngagementId;
  if (id !== currentEngagement.id) return;
  const mappings = Object.fromEntries(Object.entries(fsMapEdits).map(([k, v]) => [k, v || null]));
  await sendJSON("PUT", `/api/engagements/${id}/mapping`, { mappings });
  if (currentEngagement.id !== id) return;  // the user has opened another engagement
  fsMapEdits = {};
  await loadStatements();
  await loadEngagementActivity();
}, canSaveMapping));

// Adjusting entries

function entryLineRow() {
  return `<tr><td><input name="account" list="aje-accounts" aria-label="Account" maxlength="200"></td>
    <td><input name="debit" type="number" min="0" step="0.01" aria-label="Debit"></td>
    <td><input name="credit" type="number" min="0" step="0.01" aria-label="Credit"></td>
    <td><input name="memo" maxlength="500" aria-label="Memo"></td>
    <td><button type="button" class="small secondary" data-remove-line aria-label="Remove line">×</button></td></tr>`;
}

function resetEntryForm() {
  $("aje-form").reset();
  $("aje-lines").innerHTML = "<tr><th>Account</th><th>Debit</th><th>Credit</th><th>Memo</th><th></th></tr>" + entryLineRow() + entryLineRow();
  updateEntryTotals();
}

function entryLines() {
  return [...$("aje-lines").querySelectorAll("tr")].slice(1).map((tr) => ({
    account: tr.querySelector("[name=account]").value.trim(),
    debit: Number(tr.querySelector("[name=debit]").value) || 0,
    credit: Number(tr.querySelector("[name=credit]").value) || 0,
    memo: tr.querySelector("[name=memo]").value.trim(),
  }));
}

function updateEntryTotals() {
  const lines = entryLines();
  const dr = lines.reduce((s, l) => s + l.debit, 0), cr = lines.reduce((s, l) => s + l.credit, 0);
  const diff = Math.round((dr - cr) * 100) / 100;
  $("aje-totals").textContent = `Debits ${money(dr)} · Credits ${money(cr)}` + (diff ? ` · Out by ${money(Math.abs(diff))}` : " · Balanced");
}

function renderEntries() {
  // Booked entries need a reviewer, as marking one booked does; preparers propose them.
  const kinds = fs.entry_kinds.filter((k) => !k.booked || me.rank >= 2);
  if ([...$("aje-kind").options].map((o) => o.value).join() !== kinds.map((k) => k.id).join()) {
    $("aje-kind").innerHTML = kinds.map((k) => `<option value="${k.id}">${esc(k.label)} (${k.prefix})</option>`).join("");
    resetEntryForm();
  }
  $("aje-accounts").innerHTML = fs.accounts.map((a) => `<option value="${esc(a.account)}" label="${esc(a.name || "")}"></option>`).join("");
  $("aje-list").innerHTML = fs.entries.length
    ? "<tr><th>Entry</th><th>Description</th><th>Lines</th><th class=\"num\">Amount</th><th>By</th><th></th></tr>" + fs.entries.map((e) => `<tr>
        <td class="nowrap"><strong>${esc(e.ref)}</strong><div class="tag">${esc(e.kind_label)}${e.date ? " · " + esc(e.date) : ""}</div></td>
        <td>${esc(e.description)}</td>
        <td class="nowrap">${e.lines.map((l) => `${l.debit ? "Dr" : "&nbsp;&nbsp;&nbsp;&nbsp;Cr"} ${esc(l.account)} ${money(l.debit || l.credit)}${l.memo ? ` <span class="tag">${esc(l.memo)}</span>` : ""}`).join("<br>")}</td>
        <td class="num">${money(e.total)}</td>
        <td class="nowrap">${esc(e.author)}<div class="tag">${esc(when(e.created_at))}</div></td>
        <td><div class="cell-actions">
          ${e.kind === "proposed" ? `<button type="button" class="small secondary min-2" data-book="${e.id}" title="The client has agreed to book it">Mark booked</button>` : ""}
          <button type="button" class="small secondary danger min-2" data-delete-entry="${e.id}">Delete</button>
        </div></td></tr>`).join("")
    : '<tr><td class="muted">No adjusting entries yet.</td></tr>';
}

$("aje-lines").addEventListener("input", updateEntryTotals);
$("aje-lines").addEventListener("click", (ev) => {
  if (!ev.target.closest("[data-remove-line]")) return;
  if ($("aje-lines").querySelectorAll("tr").length > 3) ev.target.closest("tr").remove();
  updateEntryTotals();
});
$("aje-add-line").addEventListener("click", () => {
  $("aje-lines").insertAdjacentHTML("beforeend", entryLineRow());
});
$("aje-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("aje-post"), async () => {
    const o = formObject($("aje-form"));
    const lines = entryLines().filter((l) => l.account || l.debit || l.credit);
    const id = currentEngagement.id;
    await sendJSON("POST", `/api/engagements/${id}/adjusting-entries`,
      { kind: o.kind, description: o.description, date: o.date || null, lines });
    if (currentEngagement.id !== id) return;  // the user has opened another engagement
    resetEntryForm();
    await loadStatements();
    await loadEngagementActivity();
  });
});
$("aje-list").addEventListener("click", (ev) => {
  const book = ev.target.closest("[data-book]"), del = ev.target.closest("[data-delete-entry]");
  if (book) guard(async () => {
    await sendJSON("PATCH", `/api/adjusting-entries/${book.dataset.book}`, { kind: "adjusting" });
    await loadStatements();
    await loadEngagementActivity();
  });
  if (del) {
    const e = fs.entries.find((x) => String(x.id) === del.dataset.deleteEntry);
    if (!confirm(`Delete ${e.ref}: ${e.description}?`)) return;
    guard(async () => {
      await sendJSON("DELETE", `/api/adjusting-entries/${e.id}`);
      await loadStatements();
      await loadEngagementActivity();
    });
  }
});

// Lead sheets, statements and uncorrected misstatements

function renderLeadSheets() {
  const prior = fs.has_prior;
  const head = `<tr><th>Account</th><th>Name</th><th class="num">Unadjusted</th><th class="num">Adjusting</th><th class="num">Reclass</th><th class="num">Adjusted</th>${prior ? '<th class="num">Prior year</th><th class="num">Change</th>' : ""}</tr>`;
  const nums = (r) => `<td class="num">${signed(r.unadjusted)}</td><td class="num">${signed(r.adjusting)}</td>
    <td class="num">${signed(r.reclassification)}</td><td class="num">${signed(r.adjusted)}</td>` +
    (prior ? `<td class="num">${signed(r.prior)}</td><td class="num">${signed(r.adjusted - (r.prior || 0))}</td>` : "");
  $("fs-lead").innerHTML = fs.lead_sheets.length ? head + fs.lead_sheets.map((s) =>
    `<tr class="line-head"><td colspan="${prior ? 8 : 6}">${esc(s.label)} <span class="tag">${esc(s.section_label)}</span></td></tr>` +
    s.accounts.map((a) => `<tr><td>${esc(a.account)}</td><td>${esc(a.name)}</td>${nums(a)}</tr>`).join("") +
    `<tr class="line-total"><td colspan="2">Total ${esc(s.label.toLowerCase())}</td>${nums(s.totals)}</tr>`).join("")
    : '<tr><td class="muted">Map accounts to see lead sheets.</td></tr>';
}

function renderStatementTables() {
  const prior = fs.has_prior;
  const table = (rows) => `<tr><th></th><th class="num">This year</th>${prior ? '<th class="num">Prior year</th>' : ""}</tr>` +
    rows.map((r) => `<tr class="${r.kind}"><td>${esc(r.label)}</td>` + (r.kind === "heading" ? `<td></td>${prior ? "<td></td>" : ""}`
      : `<td class="num">${signed(r.current)}</td>${prior ? `<td class="num">${signed(r.prior)}</td>` : ""}`) + "</tr>").join("");
  $("fs-is").innerHTML = table(fs.statements.income_statement);
  $("fs-bs").innerHTML = table(fs.statements.balance_sheet);
  const diff = fs.statements.difference.current;
  $("fs-diff").hidden = Math.abs(diff) < 0.005;
  const tbOut = Math.abs(fs.totals.adjusted) >= 0.005;
  const n = fs.unmapped.length;
  $("fs-diff").textContent = `The balance sheet is out by ${signed(diff)}. ` + [
    n ? `${n} account${n === 1 ? " isn't" : "s aren't"} mapped yet; map ${n === 1 ? "it" : "them"} on the Account mapping tab.` : "",
    tbOut ? `The adjusted trial balance itself doesn't balance (it totals ${drcr(fs.totals.adjusted)}).` : "",
  ].filter(Boolean).join(" ");
}

function renderUncorrected() {
  const u = fs.uncorrected;
  $("unc-cards").innerHTML = [
    [signed(u.profit), "Effect on profit"], [signed(u.net_assets), "Effect on net assets"],
    [u.overall_materiality == null ? "Not set" : money(u.overall_materiality), "Overall materiality"],
    [u.pct_of_overall == null ? "–" : `${u.pct_of_overall}%`, "Largest effect vs materiality"],
  ].map(([v, k]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${k}</div></div>`).join("");
  const unmapped = u.unclassified.length && u.overall_materiality == null
    ? ` ${u.unclassified.join(", ")} ${u.unclassified.length === 1 ? "isn't" : "aren't"} mapped to a statement line, so ${u.unclassified.length === 1 ? "its" : "their"} effect isn't counted yet.` : "";
  $("unc-conclusion").textContent = (u.conclusion || (u.overall_materiality == null ? "Set materiality above to compare against it." : "")) + unmapped;
  $("unc-table").innerHTML = u.entries.length
    ? '<tr><th>Entry</th><th>Description</th><th class="num">Effect on profit</th><th class="num">Effect on net assets</th></tr>' +
      u.entries.map((e) => `<tr><td>${esc(e.ref)}</td><td>${esc(e.description)}</td><td class="num">${signed(e.profit)}</td><td class="num">${signed(e.net_assets)}</td></tr>`).join("")
    : '<tr><td class="muted">No proposed entries. Post one with type "Proposed" on the Adjusting entries tab.</td></tr>';
}

// Sampling

let samplingOptions = null;

function renderSampleForm(e) {
  $("smp-upload").innerHTML = e.uploads.filter((u) => u.kind !== "prior_tb")
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a file first</option>';
  const pm = e.materiality?.performance_materiality;
  $("smp-tm").placeholder = pm ? `${money(pm)} (performance materiality)` : "Amount";
  $("smp-result").hidden = true;
  updateSampleMethod();
}

function updateSampleMethod() {
  const mus = $("smp-method").value === "mus";
  document.querySelectorAll(".mus-only").forEach((el) => { el.hidden = !mus; });
  $("smp-size").required = !mus;
  $("smp-size-hint").textContent = mus ? "(blank to calculate)" : "";
}
$("smp-method").addEventListener("change", updateSampleMethod);

$("smp-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const num = (id) => $(id).value === "" ? null : Number($(id).value);
  guard(async () => {
    const body = {
      upload_id: Number($("smp-upload").value),
      method: $("smp-method").value,
      population: $("smp-population").value,
      min_amount: num("smp-min") || 0,
      sample_size: num("smp-size"),
      seed: num("smp-seed"),
    };
    if (body.method === "mus") {
      Object.assign(body, {
        confidence: Number($("smp-confidence").value),
        tolerable_misstatement: num("smp-tm"),
        expected_misstatement: num("smp-em") || 0,
      });
    }
    const s = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/samples`, body);
    await openEngagement(currentEngagement.id);
    showSample(s);
  });
});

function renderSamples(list) {
  $("samples").innerHTML = list.length ? "<tr><th>#</th><th>When</th><th>File</th><th>Method</th><th>Items</th><th>Value</th><th>Seed</th><th></th></tr>" +
    list.map((s) => `<tr><td>${s.id}</td><td class="nowrap">${esc(when(s.created_at))}</td>
      <td>${esc(s.filename)}</td><td>${esc(s.method_label)}</td>
      <td class="num">${s.sample_size} of ${s.population.lines}</td>
      <td class="num">${money(s.sample_value)}</td><td class="num">${s.seed}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-view-smp="${s.id}">View</button>
        <a class="button" href="/api/samples/${s.id}/export">Excel</a>
      </div></td></tr>`).join("") : "";
}

$("samples").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-view-smp]");
  if (btn) guard(async () => showSample(await getJSON(`/api/samples/${btn.dataset.viewSmp}`)));
});

function showSample(s) {
  const r = s.result;
  const d = r.details;
  $("smp-result").hidden = false;
  $("smp-title").textContent = `Sample #${s.id}: ${r.method_label} from ${s.filename}`;
  $("smp-actions").innerHTML = `<a class="button" href="/api/samples/${s.id}/export">Download workpaper</a>`;
  const cards = [
    ["Items selected", r.sample_size], ["Value selected", money(r.sample_value)],
    ["Population lines", r.population.lines.toLocaleString()], ["Population value", money(r.population.value)],
    ["Seed", r.seed],
  ];
  if (d.interval != null) cards.push(["Interval", r.method === "mus" ? money(d.interval) : d.interval]);
  if (r.method === "mus") cards.push(["Top stratum items", d.top_stratum_items]);
  $("smp-cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const notes = [r.population.label];
  if (r.params.min_amount) notes.push(`lines of at least ${money(r.params.min_amount)}`);
  if (r.method === "mus") {
    notes.push(`${r.params.confidence}% confidence`);
    if (r.params.tolerable_misstatement) notes.push(`tolerable misstatement ${money(r.params.tolerable_misstatement)}`);
    if (r.params.expected_misstatement) notes.push(`expected misstatement ${money(r.params.expected_misstatement)}`);
    if (d.monetary_units !== r.sample_size) notes.push(`${d.monetary_units} monetary units landed on ${r.sample_size} lines`);
  }
  if (d.note) notes.push(d.note);
  $("smp-note").textContent = notes.join(" · ");
  const hits = r.method === "mus";
  $("smp-table").innerHTML = `<tr><th>Row</th><th>Entry</th><th>Date</th><th>Account</th><th>Description</th><th>Amount</th>${hits ? "<th>Hits</th>" : ""}<th>Why selected</th></tr>` +
    r.items.map((i) => `<tr><td class="num">${i.row}</td><td>${esc(i.entry_id)}</td>
      <td class="nowrap">${esc(i.date)}</td><td>${esc(i.account)}</td><td>${esc(i.description)}</td>
      <td class="num">${money(i.amount)}</td>${hits ? `<td class="num">${i.hits}</td>` : ""}
      <td>${esc(i.reason)}</td></tr>`).join("");
  $("smp-result").scrollIntoView({ behavior: "smooth" });
}

// Findings register

const REG_PAGE = 200;
let reg = null;               // {summary, checks, matching, offset, findings}: one page of the register
let regOffset = 0;
let regSelected = new Set();
let currentFinding = null;

const fileSize = (n) => n < 1024 ? `${n} bytes` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const statusPill = (f) => `<span class="st ${f.status}">${esc(f.status_label)}</span>`;

async function loadRegister() {
  const id = currentEngagement.id;
  const q = new URLSearchParams({ offset: regOffset, limit: REG_PAGE, status: $("reg-status").value,
    severity: $("reg-sev").value, check: $("reg-check").value });
  const data = await getJSON(`/api/engagements/${id}/findings?${q}`);
  if (currentEngagement?.id !== id) return;  // the user has opened another engagement
  reg = data;
  if (!reg.findings.length && regOffset > 0) {  // the page emptied (e.g. after a bulk change)
    regOffset = Math.max(0, regOffset - REG_PAGE);
    return loadRegister();
  }
  $("reg-export").href = `/api/engagements/${id}/findings/export`;
  const s = reg.summary;
  $("reg-cards").innerHTML = [["Open", s.open], ["Reviewed", s.reviewed], ["Cleared", s.cleared], ["Total", s.total]]
    .map(([k, v]) => `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  const chk = $("reg-check").value;
  $("reg-check").innerHTML = '<option value="">All checks</option>' +
    reg.checks.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  if (reg.checks.some((c) => c.id === chk)) $("reg-check").value = chk;
  renderRegister();
  await loadEngagementActivity();
}

async function loadEngagementActivity() {
  renderActivity($("eng-activity"), await getJSON(`/api/activity?engagement_id=${currentEngagement.id}&limit=100`), false);
  await loadReview();  // any change to the file can make a sign-off out of date
}

// Review and sign-off

let rv = null;  // latest /review response
let rvDrafts = {};  // unsent replies by note id, kept when the notes are redrawn
const NOTE_STATUS = { open: "Open", responded: "Answered", cleared: "Cleared" };

async function loadReview() {
  const id = currentEngagement.id;
  const data = await getJSON(`/api/engagements/${id}/review`);
  if (currentEngagement?.id !== id) return;  // the user has opened another engagement
  rv = data;
  renderReview();
}

function signoffCell(s, kind) {
  const so = s[kind];
  const canSign = kind === "prepared" || me.rank >= (s.id === "completion" ? 3 : 2);
  const prepOk = s.prepared && !s.prepared.stale;
  const mine = so && so.user_id === me.id;
  let html = so ? `${esc(so.user_name)}<div class="tag">${esc(when(so.at))}</div>${so.stale ? '<div class="stale">Changed since</div>' : ""}` : '<span class="muted">–</span>';
  const buttons = [];
  if (canSign && (!so || so.stale) && (kind === "prepared" || prepOk)) {
    const blocked = kind === "reviewed" && (s.open_notes ? `Clear its ${s.open_notes} review note(s) first`
      : s.prepared.user_id === me.id ? "You prepared it, so someone else reviews it" : "");
    buttons.push(`<button type="button" class="small${so ? " secondary" : ""}" data-sign="${s.id}" data-kind="${kind}"${blocked ? ` disabled title="${esc(blocked)}"` : ""}>${so ? "Sign again" : "Sign off"}</button>`);
  }
  const giveRank = kind === "reviewed" ? (s.id === "completion" ? 3 : 2) : 1;
  if (so && (mine || me.rank >= Math.max(2, giveRank))) {
    buttons.push(`<button type="button" class="small secondary" data-unsign="${s.id}" data-kind="${kind}">Take back</button>`);
  }
  if (buttons.length) html += `<div class="cell-actions" style="margin-top:4px">${buttons.join("")}</div>`;
  return html;
}

function renderReview() {
  $("rv-cards").innerHTML = [
    [`${rv.reviewed} of ${rv.total}`, "Sections reviewed"],
    [rv.open_notes, "Review notes to clear"],
    [rv.sections.find((s) => s.id === "completion").done ? "Yes" : "No", "Signed off for completion"],
  ].map(([v, k]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${k}</div></div>`).join("");
  $("rv-sections").innerHTML = "<tr><th>Section</th><th>Prepared</th><th>Reviewed</th><th class=\"num\">Notes</th><th>Status</th></tr>" +
    rv.sections.map((s) => `<tr>
      <td><strong>${esc(s.label)}</strong><div class="tag">${esc(s.description)}${s.last_change_at ? ` Last changed ${esc(when(s.last_change_at))}.` : ""}</div></td>
      <td>${signoffCell(s, "prepared")}</td><td>${signoffCell(s, "reviewed")}</td>
      <td class="num">${s.open_notes || ""}</td>
      <td class="nowrap">${s.done ? "✓ " : ""}${esc(s.status)}</td></tr>`).join("");
  const options = rv.sections.map((s) => `<option value="${s.id}">${esc(s.label)}</option>`).join("");
  for (const [id, first] of [["rv-section", ""], ["rv-filter-section", '<option value="">All sections</option>']]) {
    const keep = $(id).value;
    $(id).innerHTML = first + options;
    if (keep) $(id).value = keep;
  }
  renderNotes();
}

function renderNotes() {
  $("rv-notes").querySelectorAll("[data-reply]").forEach((f) => {
    const id = f.closest("[data-note]").dataset.note;
    if (f.text.value.trim()) rvDrafts[id] = f.text.value; else delete rvDrafts[id];
  });
  const preparer = Object.fromEntries(rv.sections.map((s) => [s.id, s.prepared?.user_id]));
  const todo = $("rv-filter").value === "todo", section = $("rv-filter-section").value;
  const notes = rv.notes.filter((n) => (!todo || n.status !== "cleared") && (!section || n.section === section));
  $("rv-notes").innerHTML = notes.length ? notes.map((n) => `<div class="rv-note ${n.status}" data-note="${n.id}">
      <div class="head"><strong class="status-${n.status}">${NOTE_STATUS[n.status]}</strong><span>${esc(n.section_label)}</span>
        <span>${esc(n.author)} · ${esc(when(n.created_at))}</span>${n.cleared_by ? `<span>Cleared by ${esc(n.cleared_by)} · ${esc(when(n.cleared_at))}</span>` : ""}</div>
      <div class="text">${esc(n.text)}</div>
      ${n.replies.map((r) => `<div class="reply">${esc(r.text)}<div class="tag">${esc(r.author)} · ${esc(when(r.at))}</div></div>`).join("")}
      <div class="cell-actions">
        ${n.status === "cleared" ? '<button type="button" class="small secondary min-2" data-note-status="open">Reopen</button>'
          : `<button type="button" class="small min-2" data-note-status="cleared"${preparer[n.section] === me.id ? ' disabled title="You prepared this section, so another reviewer clears its notes"' : ""}>Clear</button>`}
        ${n.status === "responded" ? '<button type="button" class="small secondary min-2" data-note-status="open" title="The answer isn\'t enough">Send back</button>' : ""}
        ${n.author_id === me.id && !n.replies.length ? '<button type="button" class="small secondary danger" data-note-delete>Delete</button>' : ""}
      </div>
      ${n.status !== "cleared" ? '<form data-reply><textarea name="text" rows="1" required maxlength="5000" aria-label="Reply to note" placeholder="Reply"></textarea><button type="submit" class="small secondary">Reply</button></form>' : ""}
    </div>`).join("")
    : `<p class="muted">${rv.notes.length ? "No notes match." : "No review notes yet."}</p>`;
  $("rv-notes").querySelectorAll("[data-reply]").forEach((f) => {
    f.text.value = rvDrafts[f.closest("[data-note]").dataset.note] || "";
  });
}

$("rv-filter").addEventListener("change", renderNotes);
$("rv-filter-section").addEventListener("change", renderNotes);

$("rv-sections").addEventListener("click", (ev) => {
  const sign = ev.target.closest("[data-sign]"), unsign = ev.target.closest("[data-unsign]");
  const id = currentEngagement.id;
  if (sign) busy(sign, async () => {
    await sendJSON("POST", `/api/engagements/${id}/signoffs`, { section: sign.dataset.sign, kind: sign.dataset.kind });
    await loadEngagementActivity();
  });
  if (unsign) {
    const s = rv.sections.find((x) => x.id === unsign.dataset.unsign);
    const both = unsign.dataset.kind === "prepared" && s.reviewed;
    if (!confirm(`Take back the ${unsign.dataset.kind} sign-off on ${s.label}?${both ? " Its review will show as changed." : ""}`)) return;
    busy(unsign, async () => {
      await sendJSON("DELETE", `/api/engagements/${id}/signoffs/${unsign.dataset.unsign}/${unsign.dataset.kind}`);
      await loadEngagementActivity();
    });
  }
});

$("rv-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("rv-submit"), async () => {
    await sendJSON("POST", `/api/engagements/${currentEngagement.id}/review-notes`, formObject($("rv-form")));
    $("rv-text").value = "";
    await loadEngagementActivity();
  });
});

$("rv-notes").addEventListener("click", (ev) => {
  const status = ev.target.closest("[data-note-status]"), del = ev.target.closest("[data-note-delete]");
  const note = ev.target.closest("[data-note]")?.dataset.note;
  if (status) busy(status, async () => {
    await sendJSON("PATCH", `/api/review-notes/${note}`, { status: status.dataset.noteStatus });
    await loadEngagementActivity();
  });
  if (del && confirm("Delete this review note?")) busy(del, async () => {
    await sendJSON("DELETE", `/api/review-notes/${note}`);
    await loadEngagementActivity();
  });
});
$("rv-notes").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-reply]");
  if (!form) return;
  ev.preventDefault();
  const note = form.closest("[data-note]").dataset.note;
  busy(form.querySelector("button"), async () => {
    await sendJSON("POST", `/api/review-notes/${note}/replies`, { text: form.text.value.trim() });
    form.text.value = "";
    delete rvDrafts[note];
    await loadEngagementActivity();
  });
});

// Client document requests: shared by the team's panel and the client portal

let rq = null;          // latest /requests response for the open engagement
let rqDrafts = {};      // unsent messages by request id, kept when the list is redrawn
let portal = null;      // latest /portal/requests response (client users)

function keepDrafts(root) {
  root.querySelectorAll("[data-rq-message]").forEach((f) => {
    const id = f.closest("[data-rq]").dataset.rq;
    if (f.text.value.trim()) rqDrafts[id] = f.text.value; else delete rqDrafts[id];
  });
}
function restoreDrafts(root) {
  root.querySelectorAll("[data-rq-message]").forEach((f) => {
    f.text.value = rqDrafts[f.closest("[data-rq]").dataset.rq] || "";
  });
}

function requestCard(r, team) {
  const open = r.status !== "accepted";
  const files = r.files.map((f) => {
    const canRemove = open && (f.uploaded_by_id === me.id || (team && me.rank >= 2));
    return `<li><button type="button" class="small secondary file" data-rq-file="${f.id}">${esc(f.filename)}</button>
      <span class="tag">${fileSize(f.size)} · ${esc(f.uploaded_by)}${team && f.from_client ? " (client)" : ""} · ${esc(when(f.uploaded_at))}</span>
      ${canRemove ? `<button type="button" class="link danger-link" data-rq-remove="${f.id}">Remove</button>` : ""}</li>`;
  }).join("");
  // One line each: replies keep the message's own line breaks (white-space: pre-wrap).
  const messages = r.messages.map((m) => `<div class="reply${m.from_client ? " client" : ""}">${
    m.status_label ? `<strong class="rq-status-${m.status_to}">${esc(m.status_label)}</strong>${m.text ? ": " : ""}` : ""}${esc(m.text)}<div class="tag">${
    esc(m.author)}${team && m.from_client ? " (client)" : ""} · ${esc(when(m.at))}</div></div>`).join("");
  const actions = [];
  if (open) actions.push(`<label class="button small upload">${r.files.length ? "Add a file" : "Upload a file"}<input type="file" multiple data-rq-upload class="visually-hidden"></label>`);
  if (team && r.status === "submitted") actions.push('<button type="button" class="small" data-rq-accept>Accept files</button>');
  if (team && (!r.files.length || me.rank >= 2)) actions.push('<button type="button" class="small secondary danger" data-rq-delete>Delete request</button>');
  // Reopening accepted files is for reviewers; the server refuses preparers.
  const returnable = team && (r.status === "submitted" || (r.status === "accepted" && me.rank >= 2));
  return `<div class="rq ${r.status}" data-rq="${r.id}">
    <div class="head"><strong>${r.number}. ${esc(r.title)}</strong>
      <span class="st rq-${r.status}">${esc(r.status_label)}</span>
      ${r.due_date ? `<span class="${r.overdue ? "overdue" : "tag"}">Due ${esc(r.due_date)}${r.overdue ? " · overdue" : ""}</span>` : ""}
      ${team ? `<span class="tag">Asked by ${esc(r.created_by)} · ${esc(when(r.created_at))}</span>` : ""}</div>
    ${r.details ? `<div class="text">${esc(r.details)}</div>` : ""}
    ${files ? `<ul class="rq-files">${files}</ul>` : `<p class="tag">${team ? "No files yet." : "Nothing sent yet."}</p>`}
    ${messages}
    ${actions.length ? `<div class="cell-actions">${actions.join("")}</div>` : ""}
    <form data-rq-message><textarea name="text" rows="1" maxlength="5000" aria-label="Message" placeholder="${team ? "Message to the client" : "Message to your auditors"}"></textarea>
      <button type="submit" class="small secondary">Send message</button>
      ${returnable ? '<button type="button" class="small secondary" data-rq-return title="Send this request back to the client with the message">Send back with message</button>' : ""}</form>
  </div>`;
}

// Click, change and submit handlers for a list of request cards. `reload` redraws it.
function wireRequests(root, reload) {
  root.addEventListener("change", (ev) => {
    const input = ev.target.closest("[data-rq-upload]");
    if (!input || !input.files.length) return;
    const id = input.closest("[data-rq]").dataset.rq;
    busy(input, async () => {
      try {
        for (const file of input.files) {
          const fd = new FormData();
          fd.append("file", file);
          await postForm(`/api/requests/${id}/files`, fd);
        }
      } finally { await reload(); }
    });
  });
  root.addEventListener("click", (ev) => {
    const card = ev.target.closest("[data-rq]");
    if (!card) return;
    const id = card.dataset.rq;
    const accept = ev.target.closest("[data-rq-accept]"), del = ev.target.closest("[data-rq-delete]");
    const remove = ev.target.closest("[data-rq-remove]"), ret = ev.target.closest("[data-rq-return]");
    const file = ev.target.closest("[data-rq-file]");
    if (file) busy(file, async () => {  // fetched, so an error shows here instead of replacing the page
      await saveFile(await request(`/api/request-files/${file.dataset.rqFile}`), file.textContent);
    });
    if (accept) busy(accept, async () => {
      await sendJSON("POST", `/api/requests/${id}/status`, { status: "accepted", message: card.querySelector("[data-rq-message]").text.value.trim() });
      delete rqDrafts[id];
      await reload();
    });
    if (ret) {
      const text = card.querySelector("[data-rq-message]").text;
      if (!text.value.trim()) { showError("Write what's missing or wrong in the message box first."); text.focus(); return; }
      busy(ret, async () => {
        await sendJSON("POST", `/api/requests/${id}/status`, { status: "returned", message: text.value.trim() });
        delete rqDrafts[id];
        await reload();
      });
    }
    if (remove && confirm("Remove this file? The request keeps a record of it.")) busy(remove, async () => {
      await sendJSON("DELETE", `/api/request-files/${remove.dataset.rqRemove}`);
      await reload();
    });
    if (del && confirm("Delete this request? The client will no longer see it.")) busy(del, async () => {
      await sendJSON("DELETE", `/api/requests/${id}`);
      delete rqDrafts[id];
      await reload();
    });
  });
  root.addEventListener("submit", (ev) => {
    const form = ev.target.closest("[data-rq-message]");
    if (!form) return;
    ev.preventDefault();
    const id = form.closest("[data-rq]").dataset.rq;
    if (!form.text.value.trim()) return;
    busy(form.querySelector("button[type=submit]"), async () => {
      await sendJSON("POST", `/api/requests/${id}/messages`, { text: form.text.value.trim() });
      form.text.value = "";
      delete rqDrafts[id];
      await reload();
    });
  });
}

// The team's panel on the engagement page

async function loadRequests() {
  const id = currentEngagement.id;
  const data = await getJSON(`/api/engagements/${id}/requests`);
  if (currentEngagement?.id !== id) return;  // the user has opened another engagement
  rq = data;
  renderRequests();
}

function renderRequests() {
  const s = rq.summary;
  $("rq-cards").innerHTML = [
    [s.open + s.returned, "Waiting for the client"], [s.submitted, "Submitted, to look at"],
    [s.accepted, "Accepted"], [s.overdue, "Overdue"],
  ].map(([v, k]) => `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  const active = rq.contacts.filter((c) => c.active);
  $("rq-contacts").textContent = active.length
    ? `Client users who can see these: ${active.map((c) => `${c.name} (${c.email})`).join(", ")}.`
    : "No one at this client has a Client account yet, so they can't see these requests. An admin can add one under Users.";
  keepDrafts($("rq-list"));
  const filter = $("rq-filter").value.split(",").filter(Boolean);
  const list = rq.requests.filter((r) => !filter.length || filter.includes(r.status));
  $("rq-list").innerHTML = list.length ? list.map((r) => requestCard(r, true)).join("")
    : `<p class="muted">${rq.requests.length ? "No requests match." : "No requests yet."}</p>`;
  restoreDrafts($("rq-list"));
}

async function reloadRequests() {
  await loadRequests();
  await loadEngagementActivity();
}

wireRequests($("rq-list"), reloadRequests);
$("rq-filter").addEventListener("change", renderRequests);

$("rq-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  busy($("rq-submit"), async () => {
    const body = formObject($("rq-form"));
    if (!body.due_date) delete body.due_date;
    await sendJSON("POST", `/api/engagements/${id}/requests`, body);
    if (currentEngagement?.id !== id) return;
    $("rq-form").reset();
    await reloadRequests();
  });
});

document.querySelectorAll("[data-rq-export]").forEach((btn) => btn.addEventListener("click", () => busy(btn, async () => {
  const format = btn.dataset.rqExport;
  await saveFile(await request(`/api/engagements/${currentEngagement.id}/requests/export?format=${format}`), `requests.${format}`);
  await loadEngagementActivity();
})));

// The client portal

async function showPortal() {
  portal = await getJSON("/api/portal/requests");
  renderPortal();
}

function renderPortal() {
  if (!portal.client) {
    $("pt-lead").textContent = "Your account isn't linked to a client yet. Ask your auditors to set it up.";
    $("pt-list").innerHTML = "";
    return;
  }
  $("pt-lead").textContent = `Documents your auditors have asked ${portal.client} for. Upload a file against each request; they will accept it or tell you what else they need.`;
  keepDrafts($("pt-list"));
  const todo = $("pt-filter").value === "todo";
  const html = portal.engagements.map((e) => {
    const list = e.requests.filter((r) => !todo || r.status !== "accepted");
    const owed = e.summary.open + e.summary.returned;
    return `<section class="panel">
      <h2>${esc(e.name)} <span class="muted">· period ending ${esc(e.period_end)}</span></h2>
      <p class="tag" style="margin-top:-6px">${owed ? `${owed} still to send` : "Nothing left to send"}${e.summary.overdue ? ` · <span class="overdue">${e.summary.overdue} overdue</span>` : ""} · ${e.summary.accepted} of ${e.summary.total} accepted</p>
      ${list.length ? list.map((r) => requestCard(r, false)).join("") : '<p class="muted">Nothing here right now.</p>'}
    </section>`;
  }).join("");
  $("pt-list").innerHTML = html || '<section class="panel muted">Your auditors haven\'t asked for anything yet.</section>';
  restoreDrafts($("pt-list"));
}

wireRequests($("pt-list"), showPortal);
$("pt-filter").addEventListener("change", renderPortal);

function renderRegister() {
  if (!reg) return;  // this engagement's register is still loading
  const shown = reg.findings;
  const first = reg.offset + 1, last = reg.offset + shown.length;
  $("reg-count").textContent = reg.matching > shown.length
    ? `${first}–${last} of ${reg.matching} findings` : `${reg.matching} findings`;
  $("reg-prev").hidden = $("reg-next").hidden = reg.matching <= REG_PAGE;
  $("reg-prev").disabled = reg.offset === 0;
  $("reg-next").disabled = last >= reg.matching;
  const allChecked = shown.length && shown.every((f) => regSelected.has(f.id));
  $("reg-table").innerHTML = reg.summary.total
    ? `<tr><th><input type="checkbox" id="reg-all" ${allChecked ? "checked" : ""} title="Select all shown"></th><th>Status</th><th>Severity</th><th>Finding</th><th>Entry</th><th>Date</th><th>Account</th><th>Amount</th><th></th></tr>` +
      shown.map((f) => `<tr class="clickable${currentFinding?.id === f.id ? " current" : ""}" data-finding="${f.id}">
        <td><input type="checkbox" data-select="${f.id}" ${regSelected.has(f.id) ? "checked" : ""}></td>
        <td>${statusPill(f)}</td><td><span class="sev ${f.severity}">${f.severity}</span></td>
        <td class="finding-cell"><button type="button" class="link finding-open" aria-label="Open finding ${f.id}: ${esc(f.check_name)}">${esc(f.check_name)}</button>${f.detail && f.detail !== f.check_name ? `<div class="muted">${esc(f.detail)}</div>` : ""}</td>
        <td>${esc(f.entry_id)}</td><td class="nowrap">${esc(f.date)}</td>
        <td>${esc(f.account)}</td><td class="num">${money(f.amount)}</td>
        <td class="nowrap muted">${f.notes ? `${f.notes} note${f.notes > 1 ? "s" : ""}` : ""}${f.notes && f.evidence ? " · " : ""}${f.evidence ? `${f.evidence} file${f.evidence > 1 ? "s" : ""}` : ""}</td>
      </tr>`).join("")
    : '<tr><td class="muted">No findings yet. Run the audit on a file, or add a finding by hand.</td></tr>';
  if (reg.summary.total && !shown.length) $("reg-table").innerHTML += '<tr><td colspan="9" class="muted">No findings match these filters.</td></tr>';
  $("reg-bulk").hidden = !regSelected.size;
  $("reg-selected").textContent = `${regSelected.size} selected`;
}
["reg-status", "reg-sev", "reg-check"].forEach((id) => $(id).addEventListener("change", () => {
  regOffset = 0;
  regSelected.clear();
  guard(loadRegister);
}));
$("reg-prev").addEventListener("click", () => { regOffset = Math.max(0, regOffset - REG_PAGE); guard(loadRegister); });
$("reg-next").addEventListener("click", () => { regOffset += REG_PAGE; guard(loadRegister); });

$("reg-table").addEventListener("click", (ev) => {
  const box = ev.target.closest("input[type=checkbox]");
  if (box) {
    const rows = reg.findings;
    if (box.id === "reg-all") rows.forEach((f) => box.checked ? regSelected.add(f.id) : regSelected.delete(f.id));
    else box.checked ? regSelected.add(Number(box.dataset.select)) : regSelected.delete(Number(box.dataset.select));
    renderRegister();
    return;
  }
  const tr = ev.target.closest("[data-finding]");
  if (tr) guard(async () => openFinding(await getJSON(`/api/findings/${tr.dataset.finding}`)));
});

$("reg-bulk").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-bulk]");
  if (!btn) return;
  guard(async () => {
    await sendJSON("POST", `/api/engagements/${currentEngagement.id}/findings/bulk`, {
      ids: [...regSelected], status: btn.dataset.bulk, note: $("reg-bulk-note").value,
    });
    regSelected.clear();
    $("reg-bulk-note").value = "";
    await loadRegister();
    if (currentFinding) openFinding(await getJSON(`/api/findings/${currentFinding.id}`), false);
  });
});

$("reg-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const body = formObject($("reg-form"));
    for (const k of ["account", "entry_id", "date", "amount"]) if (!body[k]) delete body[k];
    if (body.amount) body.amount = Number(body.amount);
    const f = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/findings`, body);
    $("reg-form").reset();
    $("reg-new").open = false;
    await loadRegister();
    openFinding(f);
  });
});

function openFinding(f, scroll = true) {
  currentFinding = f;
  $("fd").hidden = false;
  $("fd-title").innerHTML = `Finding #${f.id} ${statusPill(f)}`;
  const where = [f.entry_id && `Entry ${f.entry_id}`, f.date, f.account, f.amount != null && money(f.amount),
    f.row && `row ${f.row} of ${f.filename}`].filter(Boolean);
  $("fd-meta").textContent = `${f.check_name}${where.length ? " · " + where.join(" · ") : ""}`;
  $("fd-detail").textContent = [f.description, f.detail].filter(Boolean).join(": ");
  // Preparers can move findings between open and reviewed; clearing and reopening is for reviewers up.
  const statuses = me.rank >= 2 ? ["open", "reviewed", "cleared"] : f.status === "cleared" ? ["cleared"] : ["open", "reviewed"];
  $("fd-status").innerHTML = statuses.map((x) =>
    `<option value="${x}"${x === f.status ? " selected" : ""}>${x[0].toUpperCase() + x.slice(1)}</option>`).join("");
  $("fd-status").disabled = statuses.length === 1;
  $("fd-severity").value = f.severity;
  $("fd-severity").disabled = me.rank < 2;
  $("fd-note").value = "";
  $("fd-delete").hidden = f.source !== "manual";
  $("fd-evidence").innerHTML = f.attachments.length
    ? "<tr><th>File</th><th>Size</th><th>Added</th><th>SHA-256</th><th></th></tr>" + f.attachments.map((a) => `<tr>
        <td>${esc(a.filename)}</td><td class="num">${fileSize(a.size)}</td>
        <td class="nowrap">${esc(when(a.uploaded_at))}${a.author ? " · " + esc(a.author) : ""}</td>
        <td class="muted" title="${esc(a.sha256)}">${esc(a.sha256.slice(0, 12))}…</td>
        <td><div class="cell-actions"><a class="button" href="/api/evidence/${a.id}">Download</a>
          ${f.status === "cleared" ? "" : `<button type="button" class="small secondary danger min-2" data-remove-ev="${a.id}">Remove</button>`}</div></td></tr>`).join("")
    : '<tr><td class="muted">No evidence attached.</td></tr>';
  $("fd-trail").innerHTML = f.trail.length ? [...f.trail].reverse().map((n) => `<li>
      <div class="who">${esc(when(n.created_at))}${n.author ? " · " + esc(n.author) : ""}</div>
      ${n.status_to ? `<div>Status: ${esc(n.status_from)} → <strong>${esc(n.status_to)}</strong></div>` : ""}
      ${n.text ? `<div>${esc(n.text)}</div>` : ""}</li>`).join("")
    : '<li class="muted">Nothing yet.</li>';
  renderRegister();
  renderAssistant();
  if (scroll) $("fd").scrollIntoView({ behavior: "smooth" });
}

// AI assistant (shown only when the server has an API key)

let assistant = { enabled: false };
let aiAnswer = null;  // {findingId, label, text, model}: the answer on screen

function renderAssistant() {
  $("ai").hidden = !assistant.enabled || !currentFinding;
  if (aiAnswer && aiAnswer.findingId !== currentFinding?.id) aiAnswer = null;
  $("ai-out").hidden = !aiAnswer;
  if (!aiAnswer) return;
  $("ai-label").textContent = `${aiAnswer.label} from the AI assistant (${aiAnswer.model}). Check it before using it.`;
  $("ai-text").textContent = aiAnswer.text;
}

$("ai").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-ai]");
  if (!btn) return;
  const id = currentFinding.id;
  const buttons = [...$("ai").querySelectorAll("[data-ai]")];
  buttons.forEach((b) => { b.disabled = true; });  // one question at a time
  busy(btn, async () => {
    try {
      const r = await sendJSON("POST", `/api/findings/${id}/assistant`, { task: btn.dataset.ai });
      if (currentFinding?.id !== id) return;  // the user has opened another finding
      aiAnswer = { findingId: id, ...r };
      renderAssistant();
    } finally {
      buttons.forEach((b) => { if (b !== btn) b.disabled = false; });
      loadEngagementActivity();  // failed requests are logged too
    }
  });
});

$("ai-note").addEventListener("click", () => busy($("ai-note"), async () => {
  const a = aiAnswer;
  const out = await sendJSON("POST", `/api/findings/${a.findingId}/notes`, { text: aiNoteText(a) });
  aiAnswer = null;
  if (currentFinding?.id === a.findingId) await refreshFinding(out);
}));

function aiNoteText(a) {
  return `${a.label} drafted with the AI assistant (${a.model}):\n\n${a.text}`;
}

$("ai-edit").addEventListener("click", () => {
  $("fd-note").value = aiNoteText(aiAnswer);
  $("fd-note").focus();
});

function closeFinding() {
  currentFinding = null;
  $("fd").hidden = true;
}
$("fd-close").addEventListener("click", () => { closeFinding(); renderRegister(); });

async function refreshFinding(f) {
  await loadRegister();
  openFinding(f, false);
}

$("fd-save").addEventListener("click", () => guard(async () => {
  const f = currentFinding;
  const status = $("fd-status").value, severity = $("fd-severity").value, note = $("fd-note").value.trim();
  let out = f;
  if (status !== f.status || severity !== f.severity) {
    out = await sendJSON("PATCH", `/api/findings/${f.id}`, { status, severity, note });
  } else if (note) {
    out = await sendJSON("POST", `/api/findings/${f.id}/notes`, { text: note });
  } else {
    return;
  }
  await refreshFinding(out);
}));

$("fd-delete").addEventListener("click", () => {
  if (!confirm("Delete this finding?")) return;
  guard(async () => {
    await sendJSON("DELETE", `/api/findings/${currentFinding.id}`);
    closeFinding();
    await loadRegister();
  });
});

$("fd-ev-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const fd = new FormData();
    fd.append("file", $("fd-ev-file").files[0]);
    const f = await (await postForm(`/api/findings/${currentFinding.id}/evidence`, fd)).json();
    $("fd-ev-form").reset();
    await refreshFinding(f);
  });
});

$("fd-evidence").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-remove-ev]");
  if (!btn || !confirm("Remove this evidence file? The review trail keeps a record of it.")) return;
  guard(async () => {
    await sendJSON("DELETE", `/api/evidence/${btn.dataset.removeEv}`);
    await refreshFinding(await getJSON(`/api/findings/${currentFinding.id}`));
  });
});

// --- Sign-in, users and activity ------------------------------------------------

let me = null;       // signed-in user
let roles = [];
let appStarted = false;

function setUser(user) {
  me = user;
  lastUser = userKey(user);
  document.body.dataset.rank = user.rank;
  $("user-name").textContent = user.name;
  $("user-role").textContent = user.role_label;
  $("user-chip").hidden = false;
  $("nav").hidden = user.role === "client";
}

function showLogin(needsSetup) {
  me = null;
  // Nothing of the last user's may reach the next one: unsent messages, the portal.
  rqDrafts = {}; rvDrafts = {}; rq = null; portal = null;
  $("pt-list").innerHTML = $("rq-list").innerHTML = "";
  delete document.body.dataset.rank;
  $("user-chip").hidden = $("nav").hidden = true;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== "view-login"; });
  $("results").hidden = true;
  $("login-form").hidden = needsSetup;
  $("setup-form").hidden = !needsSetup;
  (needsSetup ? $("setup-name") : $("login-email")).focus();
}

let lastUser = null;  // who this page was last set up for, and as what
const userKey = (u) => `${u.id}:${u.role}:${u.client_id ?? ""}`;

async function signedIn(res) {
  const user = (await res.json()).user;
  if (lastUser !== null && lastUser !== userKey(user)) {
    location.reload();  // someone else, or a new role: start from a clean page (the cookie is set)
    return;
  }
  setUser(user);
  showError("");
  if (me.role === "client" || meta) route(); else await startApp();
}

$("login-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const res = await request("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formObject($("login-form"))) });
    $("login-password").value = "";
    await signedIn(res);
  });
});

$("setup-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const res = await request("/api/auth/setup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formObject($("setup-form"))) });
    await signedIn(res);
  });
});

$("logout").addEventListener("click", () => guard(async () => {
  await sendJSON("POST", "/api/auth/logout");
  location.hash = "";
  location.reload();  // drop everything the page holds for this user
}));

$("pw-open").addEventListener("click", () => {
  $("pw-form").reset();
  $("pw-error").hidden = true;
  $("pw-dialog").showModal();
});
$("pw-cancel").addEventListener("click", () => $("pw-dialog").close());
$("pw-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await sendJSON("POST", "/api/auth/password", formObject($("pw-form")));
    $("pw-dialog").close();
  } catch (err) {
    $("pw-error").textContent = err.message;
    $("pw-error").hidden = false;
  }
});

let users = [];

let clients = [];  // for linking client users

async function showUsers() {
  [users, clients] = await Promise.all([getJSON("/api/users"), getJSON("/api/clients")]);
  // Team members and client users stay on their side here: a client user needs a client.
  const staff = roles.filter((r) => r.id !== "client");
  const roleOptions = (sel, list) => list.map((r) => `<option value="${r.id}"${r.id === sel ? " selected" : ""}>${esc(r.label)}</option>`).join("");
  const clientOptions = (sel) => clients.map((c) => `<option value="${c.id}"${c.id === sel ? " selected" : ""}>${esc(c.name)}</option>`).join("");
  const keep = $("nu-role").value;
  $("nu-role").innerHTML = roleOptions(keep || "preparer", [...staff, ...roles.filter((r) => r.id === "client")]);
  $("nu-client").innerHTML = '<option value="">Pick a client</option>' + clientOptions(Number($("nu-client").value));
  updateNewUserRole();
  $("users").innerHTML = "<tr><th>Name</th><th>Email</th><th>Role</th><th>Last sign-in</th><th>Status</th><th></th></tr>" +
    users.map((u) => `<tr data-user="${u.id}">
      <td>${esc(u.name)}${u.id === me.id ? ' <span class="muted">(you)</span>' : ""}</td><td>${esc(u.email)}</td>
      <td>${u.role === "client"
        ? `<label class="tag" for="uc-${u.id}">Client of</label><select class="map" id="uc-${u.id}" data-client>${u.client_id ? "" : '<option value="">No client</option>'}${clientOptions(u.client_id)}</select>`
        : `<select class="map" data-role aria-label="Role">${roleOptions(u.role, staff)}</select>`}</td>
      <td class="nowrap">${esc(when(u.last_login_at)) || '<span class="muted">Never</span>'}</td>
      <td>${u.active ? "Active" : '<span class="muted">Deactivated</span>'}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-reset>Reset password</button>
        <button type="button" class="small secondary${u.active ? " danger" : ""}" data-active="${!u.active}">${u.active ? "Deactivate" : "Reactivate"}</button>
      </div></td></tr>`).join("");
}

function updateNewUserRole() {
  const client = $("nu-role").value === "client";
  $("nu-client-wrap").hidden = !client;
  $("nu-client").required = client;
}
$("nu-role").addEventListener("change", updateNewUserRole);

$("users").addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-role], [data-client]");
  if (!sel || !sel.value) return;
  const id = sel.closest("[data-user]").dataset.user;
  const body = "role" in sel.dataset ? { role: sel.value } : { client_id: Number(sel.value) };
  guard(async () => {
    try { await sendJSON("PATCH", `/api/users/${id}`, body); } finally { await showUsers(); }
  });
});

$("users").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-user]");
  if (!row) return;
  const id = row.dataset.user;
  const reset = ev.target.closest("[data-reset]");
  const active = ev.target.closest("[data-active]");
  if (reset) {
    resetUser = users.find((u) => String(u.id) === id);
    $("reset-form").reset();
    $("reset-error").hidden = true;
    $("reset-who").textContent = resetUser.name;
    $("reset-dialog").showModal();
  } else if (active) {
    guard(async () => { await sendJSON("PATCH", `/api/users/${id}`, { active: active.dataset.active === "true" }); await showUsers(); });
  }
});

let resetUser = null;
$("reset-cancel").addEventListener("click", () => $("reset-dialog").close());
$("reset-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await sendJSON("PATCH", `/api/users/${resetUser.id}`, formObject($("reset-form")));
    $("reset-dialog").close();
    showError("");
  } catch (err) {
    $("reset-error").textContent = err.message;
    $("reset-error").hidden = false;
  }
});

$("user-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  guard(async () => {
    const body = formObject($("user-form"));
    if (body.role === "client") body.client_id = Number(body.client_id); else delete body.client_id;
    await sendJSON("POST", "/api/users", body);
    $("user-form").reset();
    await showUsers();
  });
});

function activityDetails(d) {
  const parts = [];
  if (d.sha256) parts.push(`SHA-256 ${d.sha256}`);
  if (d.current_sha256) parts.push(`this year ${d.current_sha256.slice(0, 12)}…, prior ${d.prior_sha256.slice(0, 12)}…`);
  const params = d.settings || d.params;
  if (params) parts.push(Object.entries(params).filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", "));
  return parts.join(" · ");
}

function renderActivity(table, rows, withEngagement) {
  table.innerHTML = rows.length
    ? `<tr><th>When</th><th>Who</th>${withEngagement ? "<th>Engagement</th>" : ""}<th>What</th><th>Details</th></tr>` +
      rows.map((a) => `<tr><td class="nowrap">${esc(when(a.at))}</td>
        <td class="nowrap">${esc(a.user_name) || '<span class="muted">Not signed in</span>'}${a.user_role ? ` <span class="muted">${esc(a.user_role)}</span>` : ""}</td>
        ${withEngagement ? `<td>${a.engagement_id ? `<a href="#/engagements/${a.engagement_id}">${esc(a.engagement_label)}</a>` : ""}</td>` : ""}
        <td>${esc(a.summary)}</td><td class="details-cell">${esc(activityDetails(a.details))}${a.ip ? ` <span title="IP address">${esc(a.ip)}</span>` : ""}</td></tr>`).join("")
    : '<tr><td class="muted">Nothing yet.</td></tr>';
}

async function showActivity() {
  if (!users.length) users = await getJSON("/api/users");
  const sel = $("act-user").value;
  $("act-user").innerHTML = '<option value="">Everyone</option>' +
    users.map((u) => `<option value="${u.id}">${esc(u.name)}</option>`).join("");
  $("act-user").value = sel;
  const q = new URLSearchParams({ limit: 500 });
  if ($("act-user").value) q.set("user_id", $("act-user").value);
  if ($("act-action").value) q.set("action", $("act-action").value);
  renderActivity($("act-table"), await getJSON(`/api/activity?${q}`), true);
}
$("act-user").addEventListener("change", () => guard(showActivity));
$("act-action").addEventListener("change", () => guard(showActivity));

// --- Quick audit view ---------------------------------------------------------

const quickMapper = Mapper($("quick-mapper"), () => { $("dl-xlsx").disabled = $("dl-csv").disabled = true; });

function quickForm(extra = {}) {
  const fd = quickMapper.addTo(new FormData($("form")));
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  return fd;
}

$("file").addEventListener("change", () => {
  $("dl-xlsx").disabled = $("dl-csv").disabled = true;
  quickMapper.load($("file").files[0]);
});

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard(async () => {
    $("run").disabled = true;
    $("run").textContent = "Running…";
    try {
      showResults(await (await postForm("/api/audit", quickForm())).json(), "Results", "");
      $("dl-xlsx").disabled = $("dl-csv").disabled = false;
    } finally {
      $("run").disabled = false;
      $("run").textContent = "Run audit";
    }
  });
});

const quickDownload = (format) => guard(async () =>
  saveFile(await postForm("/api/report", quickForm({ format })), `audit-report.${format}`));
$("dl-xlsx").addEventListener("click", () => quickDownload("xlsx"));
$("dl-csv").addEventListener("click", () => quickDownload("csv"));

// --- Results ------------------------------------------------------------------

function showResults(r, title, actionsHtml) {
  result = r;
  const s = r.summary;
  const imp = r.import;
  checkNames = Object.fromEntries(r.checks.map((c) => [c.id, c.name]));
  $("results").hidden = false;
  $("results-title").textContent = title;
  $("results-actions").innerHTML = actionsHtml;

  const cards = [
    ["Rows read", imp ? imp.rows_read.toLocaleString() : s.rows.toLocaleString()],
    ["Rows analysed", s.rows.toLocaleString()],
    ["Total debits", money(s.total_debits)],
    ["Total credits", money(s.total_credits)],
    ["Rows with findings", s.flagged_rows.toLocaleString()],
    ["High", s.findings_by_severity.high],
    ["Medium", s.findings_by_severity.medium],
    ["Low", s.findings_by_severity.low],
  ];
  const pm = r.materiality?.performance_materiality;
  if (pm) cards.push(["Performance materiality", money(pm)]);
  $("cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("import-warnings").innerHTML = (imp?.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");

  $("checks").innerHTML = "<tr><th>Check</th><th>Status</th><th>Flagged</th><th>Notes</th></tr>" +
    r.checks.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.status)}</td>
      <td class="num">${c.status === "ran" ? c.flagged : "–"}</td><td class="muted">${esc(c.note)}</td></tr>`).join("");

  $("f-sev").value = "";
  $("f-check").innerHTML = '<option value="">All checks</option>' +
    r.checks.filter((c) => c.flagged).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  renderFindings();
  renderBenford();
}

function renderFindings() {
  const sev = $("f-sev").value, chk = $("f-check").value;
  const rows = result.findings.filter((f) => (!sev || f.severity === sev) && (!chk || f.check === chk));
  const shown = rows.slice(0, MAX_ROWS);
  $("f-count").textContent = rows.length > MAX_ROWS
    ? `Showing ${MAX_ROWS} of ${rows.length}; download the report for all.`
    : `${rows.length} findings`;
  $("findings").innerHTML = "<tr><th>Severity</th><th>Check</th><th>Row</th><th>Entry</th><th>Date</th>" +
    "<th>Account</th><th>Description</th><th>Amount</th><th>Detail</th></tr>" +
    shown.map((f) => `<tr><td><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span></td>
      <td>${esc(checkNames[f.check] || f.check)}</td><td class="num">${esc(f.row)}</td><td>${esc(f.entry_id)}</td>
      <td class="nowrap">${esc(f.date)}</td><td>${esc(f.account)}</td><td>${esc(f.description)}</td>
      <td class="num">${money(f.amount)}</td><td>${esc(f.detail)}</td></tr>`).join("");
}
$("f-sev").addEventListener("change", renderFindings);
$("f-check").addEventListener("change", renderFindings);

function renderBenford() {
  const b = result.benford;
  $("benford-note").textContent = b.n < 100
    ? `Only ${b.n} amounts of 10 or more, so this is shown for reference only.`
    : `${b.conformity}, MAD ${b.mad.toFixed(4)} across ${b.n.toLocaleString()} amounts. ` +
      "Bars are actual frequency; the line marks the expected frequency.";
  const max = Math.max(...b.digits.map((d) => Math.max(d.actual, d.expected)), 0.01);
  $("benford").innerHTML = b.digits.map((d) => `
    <div>${d.digit}</div>
    <div class="bar ${d.significant ? "sig" : ""}" title="z = ${d.z}">
      <div class="a" style="width:${(d.actual / max) * 100}%"></div>
      <div class="e" style="left:${(d.expected / max) * 100}%"></div>
    </div>
    <div class="muted">${(d.actual * 100).toFixed(1)}% vs ${(d.expected * 100).toFixed(1)}%</div>`).join("");
}

// --- Start --------------------------------------------------------------------

async function startApp() {
  if (me.role === "client") {  // the rest is for the audit team, and refuses client users
    route();
    return;
  }
  meta = await getJSON("/api/materiality/benchmarks");
  $("mat-benchmark").innerHTML = meta.benchmarks.map((b) => `<option value="${b.id}">${esc(b.label)}</option>`).join("");
  $("eng-framework").innerHTML = meta.frameworks.map((f) => `<option>${esc(f)}</option>`).join("");
  samplingOptions = await getJSON("/api/sampling/options");
  $("smp-method").innerHTML = samplingOptions.methods.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
  $("smp-population").innerHTML = samplingOptions.populations.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("");
  $("smp-confidence").innerHTML = samplingOptions.confidence_levels.map((c) => `<option value="${c}"${c === 95 ? " selected" : ""}>${c}%</option>`).join("");
  $("reg-status").innerHTML = '<option value="">All statuses</option>' +
    (await getJSON("/api/findings/options")).statuses.map((x) => `<option value="${x.id}">${esc(x.label)}</option>`).join("");
  try { assistant = await getJSON("/api/assistant/status"); } catch { assistant = { enabled: false }; }
  $("up-kind").innerHTML = meta.upload_kinds.map((k) => `<option value="${k.id}">${esc(k.label)}</option>`).join("");
  appStarted = true;
  route();
}

(async () => {
  const status = await getJSON("/api/auth/status");
  roles = status.roles;
  if (status.user) {
    setUser(status.user);
    await startApp();
  } else {
    showLogin(status.needs_setup);
  }
})();
