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
  if (!(me.rank >= (VIEW_RANK[view] || Infinity))) {
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
  if (currentEngagement?.id !== e.id) { regOffset = 0; regSelected.clear(); }
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
  reg = await getJSON(`/api/engagements/${id}/findings?${q}`);
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
  renderActivity($("eng-activity"), await getJSON(`/api/activity?engagement_id=${id}&limit=100`), false);
}

function renderRegister() {
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
  if (scroll) $("fd").scrollIntoView({ behavior: "smooth" });
}

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
  document.body.dataset.rank = user.rank;
  $("user-name").textContent = user.name;
  $("user-role").textContent = user.role_label;
  $("user-chip").hidden = $("nav").hidden = false;
}

function showLogin(needsSetup) {
  me = null;
  delete document.body.dataset.rank;
  $("user-chip").hidden = $("nav").hidden = true;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== "view-login"; });
  $("results").hidden = true;
  $("login-form").hidden = needsSetup;
  $("setup-form").hidden = !needsSetup;
  (needsSetup ? $("setup-name") : $("login-email")).focus();
}

async function signedIn(res) {
  setUser((await res.json()).user);
  showError("");
  if (appStarted) route(); else { appStarted = true; await startApp(); }
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
  location.hash = "#/engagements";
  showLogin(false);
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

async function showUsers() {
  users = await getJSON("/api/users");
  const roleOptions = (sel) => roles.map((r) => `<option value="${r.id}"${r.id === sel ? " selected" : ""}>${esc(r.label)}</option>`).join("");
  $("nu-role").innerHTML = roleOptions("preparer");
  $("users").innerHTML = "<tr><th>Name</th><th>Email</th><th>Role</th><th>Last sign-in</th><th>Status</th><th></th></tr>" +
    users.map((u) => `<tr data-user="${u.id}">
      <td>${esc(u.name)}${u.id === me.id ? ' <span class="muted">(you)</span>' : ""}</td><td>${esc(u.email)}</td>
      <td><select class="map" data-role>${roleOptions(u.role)}</select></td>
      <td class="nowrap">${esc(when(u.last_login_at)) || '<span class="muted">Never</span>'}</td>
      <td>${u.active ? "Active" : '<span class="muted">Deactivated</span>'}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-reset>Reset password</button>
        <button type="button" class="small secondary${u.active ? " danger" : ""}" data-active="${!u.active}">${u.active ? "Deactivate" : "Reactivate"}</button>
      </div></td></tr>`).join("");
}

$("users").addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-role]");
  if (!sel) return;
  const id = sel.closest("[data-user]").dataset.user;
  guard(async () => {
    try { await sendJSON("PATCH", `/api/users/${id}`, { role: sel.value }); } finally { await showUsers(); }
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
    await sendJSON("POST", "/api/users", formObject($("user-form")));
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
  meta = await getJSON("/api/materiality/benchmarks");
  $("mat-benchmark").innerHTML = meta.benchmarks.map((b) => `<option value="${b.id}">${esc(b.label)}</option>`).join("");
  $("eng-framework").innerHTML = meta.frameworks.map((f) => `<option>${esc(f)}</option>`).join("");
  samplingOptions = await getJSON("/api/sampling/options");
  $("smp-method").innerHTML = samplingOptions.methods.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
  $("smp-population").innerHTML = samplingOptions.populations.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("");
  $("smp-confidence").innerHTML = samplingOptions.confidence_levels.map((c) => `<option value="${c}"${c === 95 ? " selected" : ""}>${c}%</option>`).join("");
  $("reg-status").innerHTML = '<option value="">All statuses</option>' +
    (await getJSON("/api/findings/options")).statuses.map((x) => `<option value="${x.id}">${esc(x.label)}</option>`).join("");
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
