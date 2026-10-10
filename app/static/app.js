const $ = (id) => document.getElementById(id);
const esc = (v) => v == null ? "" : String(v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => n == null ? "" : Number(n).toLocaleString(undefined,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const MAX_ROWS = 1000;
let result = null;
let checkNames = {};

// --- Requests -------------------------------------------------------------

function currentMapping() {
  const mapping = {};
  document.querySelectorAll("select.map[data-field]").forEach((s) => {
    if (s.value) mapping[s.dataset.field] = s.value;
  });
  return mapping;
}

function formData(extra = {}) {
  const fd = new FormData($("form"));
  if (!$("mapping-panel").hidden) fd.set("mapping", JSON.stringify(currentMapping()));
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  return fd;
}

async function post(url, fd) {
  const res = await fetch(url, { method: "POST", body: fd });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res;
}

function showError(msg) {
  $("error").textContent = msg || "";
  $("error").hidden = !msg;
}

// --- Column mapping -------------------------------------------------------

async function loadPreview(sheet = "") {
  showError("");
  const file = $("file").files[0];
  $("mapping-panel").hidden = true;
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  if (sheet) fd.append("sheet", sheet);
  try {
    renderPreview(await (await post("/api/preview", fd)).json());
  } catch (err) {
    showError(err.message);
  }
}

function renderPreview(p) {
  $("sheet-wrap").hidden = p.sheets.length < 2;
  $("sheet").innerHTML = p.sheets.map((s) =>
    `<option ${s === p.sheet ? "selected" : ""}>${esc(s)}</option>`).join("");

  const options = (selected) => '<option value="">(not in file)</option>' + p.columns.map((c) =>
    `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`).join("");
  $("mapping").innerHTML = p.fields.map((f) => `
    <div>
      <label for="map-${f.id}">${esc(f.label)}</label>
      <select class="map" id="map-${f.id}" data-field="${f.id}">${options(p.suggested[f.id])}</select>
    </div>`).join("");

  $("preview-note").textContent =
    `First ${p.rows.length} of ${p.row_count.toLocaleString()} rows in the file:`;
  $("preview").innerHTML = "<tr>" + p.columns.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr>" +
    p.rows.map((r) => "<tr>" + r.map((v) => `<td title="${esc(v)}">${esc(v)}</td>`).join("") + "</tr>").join("");
  $("mapping-panel").hidden = false;
}

$("file").addEventListener("change", () => {
  $("dl-xlsx").disabled = $("dl-csv").disabled = true;
  loadPreview();
});
$("sheet").addEventListener("change", () => loadPreview($("sheet").value));
$("mapping").addEventListener("change", () => {
  $("dl-xlsx").disabled = $("dl-csv").disabled = true;
});

// --- Running and downloading ----------------------------------------------

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError("");
  $("run").disabled = true;
  $("run").textContent = "Running…";
  try {
    result = await (await post("/api/audit", formData())).json();
    render();
  } catch (err) {
    showError(err.message);
  } finally {
    $("run").disabled = false;
    $("run").textContent = "Run audit";
  }
});

async function download(format) {
  showError("");
  try {
    const res = await post("/api/report", formData({ format }));
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1]
      || `audit-report.${format}`;
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError(err.message);
  }
}
$("dl-xlsx").addEventListener("click", () => download("xlsx"));
$("dl-csv").addEventListener("click", () => download("csv"));

// --- Results --------------------------------------------------------------

function render() {
  const s = result.summary;
  const imp = result.import;
  checkNames = Object.fromEntries(result.checks.map((c) => [c.id, c.name]));
  $("results").hidden = false;
  $("dl-xlsx").disabled = $("dl-csv").disabled = false;

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
  $("cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("import-warnings").innerHTML = (imp?.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");

  $("checks").innerHTML = "<tr><th>Check</th><th>Status</th><th>Flagged</th><th>Notes</th></tr>" +
    result.checks.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.status)}</td>
      <td class="num">${c.status === "ran" ? c.flagged : "–"}</td><td class="muted">${esc(c.note)}</td></tr>`).join("");

  $("f-check").innerHTML = '<option value="">All checks</option>' +
    result.checks.filter((c) => c.flagged).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
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
