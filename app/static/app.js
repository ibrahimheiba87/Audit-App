const $ = (id) => document.getElementById(id);
// Uploaded or typed data (names, accounts, files): never translated, and kept in its own
// direction so a Latin name reads correctly inside Arabic text.
const data = (v) => `<bdi translate="no">${esc(v)}</bdi>`;
const esc = (v) => v == null ? "" : String(v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// The browser's own number and date style, but always Western digits (0-9) and the
// Gregorian calendar, so figures and dates match the source files even where the
// browser is set to an Arabic region.
const LOCALE = (() => {
  const options = { numberingSystem: "latn", calendar: "gregory" };
  try { return new Intl.Locale(navigator.language || "en", options).toString(); }
  catch { return "en"; }
})();
const money = (n) => n == null ? "" : Number(n).toLocaleString(LOCALE,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// A date and time in the browser's style; in Arabic, i18n.js shows it the Arabic way.
const when = (iso) => {
  if (!iso) return "";
  const date = new Date(iso + "Z");
  return I18N.when(date, date.toLocaleString(LOCALE));
};
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

let mapperCount = 0;

function Mapper(root, onChange = () => {}) {
  let file = null;
  let loads = 0;  // only the latest preview is shown, if an older one answers later
  const uid = `mapper${++mapperCount}`;  // ids that tie each label to its select
  root.innerHTML = `
    <div class="mapper" hidden>
      <div class="grid" data-sheet-wrap hidden style="margin-top:14px">
        <div><label for="${uid}-sheet">Sheet</label><select class="map" id="${uid}-sheet" data-sheet></select></div>
      </div>
      <div data-mapping-wrap>
        <h2 style="margin-top:18px">Column mapping</h2>
        <p class="muted">Check which column in your file holds each field. Map either a signed Amount column, or Debit and Credit columns.</p>
        <div class="mapping" data-mapping></div>
      </div>
      <p class="muted" data-note></p>
      <div class="table-wrap"><table class="preview-table" data-preview translate="no"></table></div>
    </div>`;
  const q = (sel) => root.querySelector(sel);

  async function load(f, sheet = "") {
    file = f;
    const mine = ++loads;
    q(".mapper").hidden = true;
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    if (sheet) fd.append("sheet", sheet);
    const p = await (await postForm("/api/preview", fd)).json();
    if (mine !== loads) return;  // another file or sheet was picked meanwhile
    q("[data-sheet-wrap]").hidden = p.sheets.length < 2;
    q("[data-sheet]").innerHTML = p.sheets.map((s) =>
      `<option ${s === p.sheet ? "selected" : ""} translate="no">${esc(s)}</option>`).join("");
    const options = (selected) => '<option value="">(not in file)</option>' + p.columns.map((c) =>
      `<option value="${esc(c)}" ${c === selected ? "selected" : ""} translate="no">${esc(c)}</option>`).join("");
    q("[data-mapping]").innerHTML = p.fields.map((f) => `
      <div>
        <label for="${uid}-${f.id}">${esc(f.label)}</label>
        <select class="map" id="${uid}-${f.id}" data-field="${f.id}">${options(p.suggested[f.id])}</select>
      </div>`).join("");
    q("[data-note]").textContent =
      `First ${p.rows.length} of ${p.row_count.toLocaleString(LOCALE)} rows in the file:`;
    q("[data-preview]").innerHTML = "<tr>" + p.columns.map((c) => `<th>${esc(c)}</th>`).join("") + "</tr>" +
      p.rows.map((r) => "<tr>" + r.map((v) => `<td title="${esc(v)}">${esc(v)}</td>`).join("") + "</tr>").join("");
    q(".mapper").hidden = false;
  }

  q("[data-sheet]").addEventListener("change", () => {
    onChange();  // a different sheet is different data: earlier results no longer apply
    guard(() => load(file, q("[data-sheet]").value));
  });
  q("[data-mapping]").addEventListener("change", onChange);

  return {
    load: (f) => guard(() => load(f)),
    reset: () => { file = null; loads++; q(".mapper").hidden = true; },
    // Files with nothing to map (lists) still need the sheet picked.
    setMapping: (on) => { q("[data-mapping-wrap]").hidden = !on; },
    addTo(fd) {
      if (q(".mapper").hidden) return fd;
      if (q("[data-mapping-wrap]").hidden) {
        if (!q("[data-sheet-wrap]").hidden) fd.set("sheet", q("[data-sheet]").value);
        return fd;
      }
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
const VIEW_RANK = { engagements: 1, quick: 1, overview: 2, users: 4, activity: 4 };

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
  refreshBell();
  if (view === "engagements") guard(() => showEngagements(id ? Number(id) : null));
  if (view === "users") guard(showUsers);
  if (view === "activity") guard(showActivity);
  if (view === "overview") guard(showOverview);
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
    guard(loadMyTasks);
  }
}

async function renderClients(activeId) {
  const clients = await getJSON("/api/clients");
  // Archived engagements stay out of the list unless asked for, or open.
  const showArchived = $("show-archived").checked;
  const listed = (c) => c.engagements.filter((e) => showArchived || !e.archived || e.id === activeId);
  const archivedCount = clients.reduce((n, c) => n + c.engagements.filter((e) => e.archived).length, 0);
  $("show-archived").closest("label").hidden = !archivedCount;
  $("client-list").innerHTML = clients.length ? clients.map((c) => `
    <div class="client">
      <div class="client-name"><span>${data(c.name)}</span>
        <button type="button" data-new-eng="${c.id}" class="link min-2" data-client-name="${esc(c.name)}">+ engagement</button></div>
      ${c.industry ? `<div class="muted" style="font-size:13px">${data(c.industry)}</div>` : ""}
      <ul>${listed(c).map((e) => `<li><a href="#/engagements/${e.id}" class="${e.id === activeId ? "active" : ""}">
        ${data(e.name)} <span class="muted">· ${esc(e.period_end)}</span>${e.archived ? ' <span class="tag">Archived</span>'
          : e.locked ? ' <span class="tag">Locked</span>' : ""}</a></li>`).join("")}</ul>
    </div>`).join("") : `<p class="muted">No clients yet.</p>`;
  if (me.role !== "admin" && !clients.some((c) => c.engagements.length)) {
    $("client-list").insertAdjacentHTML("beforeend", `<p class="muted">You only see engagements
      whose team you're on. An admin adds you to an engagement's team.</p>`);
  }
  $("new-client").open = !clients.length;
}
$("show-archived").addEventListener("change", () => guard(() => renderClients(currentEngagement?.id)));

$("client-list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-new-eng]");
  if (!btn) return;
  newEngagementClient = Number(btn.dataset.newEng);
  $("eng-new-client").textContent = btn.dataset.clientName;
  $("eng-form").reset();
  $("eng-empty").hidden = $("eng-detail").hidden = $("my-tasks").hidden = true;
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

let engagementLoads = 0;  // so a slow load can't replace the engagement opened after it

async function openEngagement(id) {
  const load = ++engagementLoads;
  const e = await getJSON(`/api/engagements/${id}`);
  if (load !== engagementLoads) return;
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
    $("dash-cards").innerHTML = $("dash-steps").innerHTML = "";
    bank = null; bankChecked = null;
    strat = null; $("str-result").hidden = $("str-export").hidden = true; $("str-bands").value = "";
    resetWho(); whoTab = "users";
    lx = null; $("lx-result").hidden = $("lx-export").hidden = true; $("lx-form").reset();
    aged = null; $("ag-result").hidden = $("ag-export").hidden = true; $("ag-terms").value = "30";
    rr = null; renderRisks(); resetRiskForm(); $("rr-add").open = false; $("ra-imported").textContent = "";
    ct = null; renderControls(); resetControlForm(); $("ct-add").open = false; $("ct-calc-out").textContent = "";
    ap = null; apEditing = null; renderExpectations(); $("ap-form").reset(); $("ap-add").open = false;
    resetFixedAssets(); faTab = "categories";
    py = null; pyEditing = null; renderPriorPoints(); $("py-form").reset(); $("py-add").open = false;
    $("py-filter").value = ""; $("py-imported").textContent = "";
    sc = null; scEditing = null; resetStockListing(); renderStockCount(); $("sc-filter").value = "";
    $("sc-setup").open = false; $("sc-form").reset(); $("sc-add-form").reset(); $("sc-details").reset();
    cf = null; cfEditing = null; renderConfirmations(); $("cf-form").reset(); $("cf-pick-form").reset();
    $("cf-picked").textContent = ""; $("cf-filter").value = "";
    cp = null; renderCompletion(); $("cp-check").open = false; $("cp-event-form").reset(); $("cp-sign").reset();
    qr = null; renderQuality(); $("qr-check").open = false; $("qr-sign").reset();
    rp = null; renderReport(); $("rp-preview-box").open = false;
    rpt = null; rptFound = null; renderRelated(); $("rpt-form").reset(); $("rpt-add").open = false;
    ds = null; renderSummaries(); $("ds-form").reset(); $("ds-wait").hidden = dsReading === 0;
    wp = null; renderWorkplan(); $("wp-form").reset(); $("wp-time-form").reset(); $("wp-filter").value = "open";
    $("bank-file").innerHTML = $("bank-gl").innerHTML = $("bank-accounts").innerHTML = "";
    $("bank-recs").innerHTML = "";
    $("bank-balance").value = $("bank-book").value = "";
    $("bank-window").value = "5";
    $("bank-flip").checked = false;
    $("bank-result").hidden = true;
    $("doc-out").hidden = true;
    $("doc-form").reset();
    $("doc-wait").hidden = true;
    ac = null; renderAcceptance(); $("ac-check").open = false;
    xm = null; xmColumns = {}; $("xm-result").hidden = true; $("xm-keys").innerHTML = "";
    $("xm-name").value = ""; $("xm-threshold").value = "90";
    dc = null; renderDisclosures(); $("dc-ai-form").reset();
    showRisk(null);
    showLetter(null);
    aiMapHints = {};
    $("fs-ai-note").hidden = true;
  }
  const switched = currentEngagement?.id !== e.id;
  currentEngagement = e;
  $("eng-empty").hidden = true;
  $("my-tasks").hidden = true;
  $("eng-detail").hidden = false;
  $("eng-title").textContent = `${e.client.name} · ${e.name}`;
  $("eng-meta").textContent = `${e.period_start ? e.period_start + " to " : "Period ending "}${e.period_end} · ${e.framework}`;
  renderTeam(e.team);
  renderLock(e.lock);
  renderMateriality(e.materiality, e.materiality_carried);
  renderUploads(e.uploads);
  renderRuns(e.runs);
  renderTemplates(e, switched);
  updatePackLink();
  renderComparisonForm(e);
  renderComparisons(e.comparisons);
  renderMatchForm(e);
  renderMatches(e.cross_matches);
  renderStratifyForm(e);
  renderExplorerForm(e);
  renderWhoForm(e);
  renderFixedAssetForm(e);
  renderStockForm(e);
  renderAgingForm(e);
  renderConfirmationListing(e);
  renderSampleForm(e);
  renderSamples(e.samples);
  guard(loadBank);
  renderDocPanel();
  renderAiPanels();
  if (currentFinding && currentFinding.engagement_id !== e.id) closeFinding();
  guard(loadReview);  // on its own, so a slow or failing panel above doesn't hold it up
  guard(loadRequests);
  guard(loadAiDrafts);
  guard(loadRisks);
  guard(loadControls);
  guard(loadConfirmations);
  guard(loadExpectations);
  guard(loadStockCount);
  guard(loadPriorPoints);
  guard(loadWorkplan);
  guard(loadCompletion);
  guard(loadQuality);
  guard(loadReport);
  guard(loadRelated);
  guard(loadSummaries);
  guard(loadAcceptance);
  guard(loadDisclosures);
  await loadStatements();
  await loadRegister();
  scrollToPendingPanel();
}

// Engagement team: who (besides admins) can see the engagement. Admins change it.

function renderTeam(team) {
  $("eng-team").innerHTML = team.length
    ? `<span>Team:</span> ${team.map((m) => data(m.name) + (m.active ? "" : " <span>(deactivated)</span>")).join(", ")}`
    : "No one is on the team yet, so only admins can see this engagement.";
}

$("eng-team-edit").addEventListener("click", () => guard(async () => {
  const e = currentEngagement;
  const staff = (await getJSON("/api/users")).filter((u) => u.role !== "client");
  const on = new Set(e.team.map((m) => m.id));
  $("team-who").textContent = `${e.client.name} · ${e.name}`;
  $("team-list").innerHTML = staff.map((u) => u.role === "admin"
    ? `<label><input type="checkbox" disabled checked> ${data(u.name)} <span class="tag">Admin, sees every engagement</span></label>`
    : `<label><input type="checkbox" value="${u.id}"${on.has(u.id) ? " checked" : ""}> ${data(u.name)} <span class="tag">${esc(u.role_label)}${u.active ? "" : " · deactivated"}</span></label>`).join("")
    || '<p class="muted">Add people on the Users page first.</p>';
  $("team-error").hidden = true;
  $("team-form").dataset.engagement = e.id;
  $("team-dialog").showModal();
}));
$("team-cancel").addEventListener("click", () => $("team-dialog").close());
$("team-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const id = Number($("team-form").dataset.engagement);
  const ids = [...$("team-list").querySelectorAll("input:checked:not(:disabled)")].map((x) => Number(x.value));
  try {
    const r = await sendJSON("PUT", `/api/engagements/${id}/team`, { user_ids: ids });
    $("team-dialog").close();
    if (currentEngagement?.id === id) {
      currentEngagement.team = r.team;
      renderTeam(r.team);
      loadEngagementActivity();
      guard(loadWorkplan);  // the task owner choices follow the team
      refreshDashboard();
    }
  } catch (err) {
    $("team-error").textContent = err.message;
    $("team-error").hidden = false;
  }
});

// Roll forward: start next year's engagement from this one.

function nextYear(iso) {  // as the server does it: a month end stays a month end
  const [y, m, d] = iso.split("-").map(Number);
  const last = (yr) => new Date(Date.UTC(yr, m, 0)).getUTCDate();
  const day = d === last(y) ? last(y + 1) : Math.min(d, last(y + 1));
  return `${y + 1}-${String(m).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

$("eng-roll").addEventListener("click", () => {
  const e = currentEngagement;
  const bumped = e.name.replace(/(?<!\d)(19|20)\d\d(?!\d)/g, (y) => String(Number(y) + 1));
  $("roll-form").reset();
  $("roll-who").textContent = `${e.client.name} · ${e.name}`;
  $("roll-name").value = bumped !== e.name ? bumped : `${e.name.slice(0, 183)} (rolled forward)`;
  $("roll-period").innerHTML = `<span>New period end:</span> <bdi dir="ltr" translate="no">${esc(nextYear(e.period_end))}</bdi>`;
  $("roll-error").hidden = true;
  $("roll-form").dataset.engagement = e.id;
  $("roll-dialog").showModal();
});
$("roll-cancel").addEventListener("click", () => $("roll-dialog").close());
$("roll-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = $("roll-form");
  const body = { name: $("roll-name").value };
  for (const k of ["trial_balance", "mapping", "materiality", "requests", "controls", "workplan", "related_parties", "expectations", "prior_points", "quality", "team"]) body[k] = f[k].checked;
  $("roll-submit").disabled = true;
  try {
    const r = await sendJSON("POST", `/api/engagements/${f.dataset.engagement}/roll-forward`, body);
    $("roll-dialog").close();
    location.hash = `#/engagements/${r.id}`;
  } catch (err) {
    $("roll-error").textContent = err.message;
    $("roll-error").hidden = false;
  } finally {
    $("roll-submit").disabled = false;
  }
});

$("eng-delete").addEventListener("click", () => {
  const e = currentEngagement;
  if (!e || !confirm(`Delete ${e.name} for ${e.client.name}, with all its files and runs?`)) return;
  guard(async () => {
    await sendJSON("DELETE", `/api/engagements/${e.id}`);
    location.hash = "#/engagements";
  });
});

// Locking and archiving: a partner locks the file once the report is signed; only an admin
// unlocks it, with a reason. A locked engagement can be archived out of the list.

function renderLock(lock) {
  $("eng-lock").hidden = !!lock;
  $("eng-unlock").hidden = !lock;
  $("eng-archive").hidden = !lock;
  $("eng-archive").textContent = lock?.archived ? "Bring back from the archive" : "Archive";
  $("eng-delete").hidden = !!lock;
  $("eng-locked").hidden = !lock;
  if (!lock) { $("eng-locked").innerHTML = ""; return; }
  $("eng-locked").innerHTML = `<strong>${lock.archived ? "Archived and locked." : "Locked."}</strong>
    <span>This engagement's file can be read and downloaded but not changed.</span>
    <span class="muted">${data(lock.locked_by)} · <span>${esc(when(lock.locked_at))}</span></span>
    ${lock.note ? `<div>${data(lock.note)}</div>` : ""}`;
}

// After locking, unlocking or archiving: the engagement, the list and the log.
async function lockChanged(id) {
  if (currentEngagement?.id !== id) return;
  await openEngagement(id);
  await renderClients(id);
  await loadEngagementActivity();
}

$("eng-lock").addEventListener("click", () => {
  const e = currentEngagement;
  const note = prompt(`Lock ${e.name}? Nothing in its file can be changed afterwards; an admin can unlock it. Add a note if you like (for example, the date the report was signed).`, "");
  if (note === null) return;
  busy($("eng-lock"), async () => {
    await sendJSON("POST", `/api/engagements/${e.id}/lock`, { note });
    await lockChanged(e.id);
  });
});

$("eng-unlock").addEventListener("click", () => {
  const e = currentEngagement;
  const reason = prompt(`Why does ${e.name} need to be unlocked? The reason is kept in the activity log.`, "");
  if (reason === null) return;
  if (!reason.trim()) { showError("Give a reason for unlocking."); return; }
  busy($("eng-unlock"), async () => {
    await sendJSON("DELETE", `/api/engagements/${e.id}/lock`, { reason });
    await lockChanged(e.id);
  });
});

$("eng-archive").addEventListener("click", () => {
  const e = currentEngagement;
  const archived = !e.lock?.archived;
  busy($("eng-archive"), async () => {
    await sendJSON("POST", `/api/engagements/${e.id}/archive`, { archived });
    await lockChanged(e.id);
  });
});

// Acceptance and independence

let ac = null;      // the checklist, decision and declarations shown
let acLoads = 0;

async function loadAcceptance() {
  const id = currentEngagement.id;
  const load = ++acLoads;
  const a = await getJSON(`/api/engagements/${id}/acceptance`);
  if (load !== acLoads || currentEngagement?.id !== id) return;
  ac = { ...a, engagement_id: id };
  renderAcceptance();
}

const ANSWER_LABELS = { yes: "Yes", no: "No", na: "Not applicable" };

function renderAcceptance() {
  if (!ac) {
    $("ac-cards").innerHTML = $("ac-status").innerHTML = $("ac-questions").innerHTML = "";
    $("ac-members").innerHTML = $("ind-mine").textContent = $("ac-saved").textContent = "";
    $("ac-conclusion").value = $("ac-note").value = "";
    $("ac-export").removeAttribute("href");
    $("ac-pick").value = "accept";
    $("ind-form").reset();
    $("ind-threat").hidden = true;
    return;
  }
  const ind = ac.independence;
  $("ac-export").href = `/api/engagements/${ac.engagement_id}/acceptance/export`;
  const decision = ac.decision ? ac.decision_label : "Not decided";
  $("ac-cards").innerHTML = [
    [`${ac.answered}/${ac.questions.length}`, "Questions answered", false],
    [ac.flags.length, "Answers needing a second look", ac.flags.length > 0],
    [decision, "Decision", ac.decision === "decline"],
    [`${ind.declared}/${ind.expected}`, "Independence confirmed", ind.declared < ind.expected],
    [ind.threats, "Threats declared", ind.threats > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("ac-status").innerHTML = ac.decision
    ? `<p><strong>${esc(ac.decision_label)}</strong> <span class="muted">${data(ac.decided_by)} · <span>${esc(when(ac.decided_at))}</span></span></p>
       ${ac.decision_note ? `<p>${data(ac.decision_note)}</p>` : ""}
       ${ac.changed_since_decision ? '<p class="warn-box">The checklist changed after the decision was recorded. The partner may want to look at it again.</p>' : ""}`
    : "";
  $("ac-questions").innerHTML = ac.questions.map((q, i) => {
    const a = ac.answers[q.id];
    const flagged = ac.flags.includes(q.id);
    return `<div class="ac-q${flagged ? " flagged" : ""}" data-q="${esc(q.id)}">
      <p><bdi>${i + 1}.</bdi> <span>${esc(q.text)}</span></p>
      <fieldset class="checks-inline"><legend class="visually-hidden">Answer</legend>
        ${Object.entries(ANSWER_LABELS).map(([v, label]) => `<label><input type="radio" name="ac-${esc(q.id)}" value="${v}" ${a.answer === v ? "checked" : ""}><span>${label}</span></label>`).join("")}
      </fieldset>
      <input data-comment maxlength="5000" value="${esc(a.comment)}" placeholder="Comment" aria-label="Comment">
      ${flagged ? '<div class="tag flag-note">This answer needs a second look before accepting.</div>' : ""}
    </div>`;
  }).join("");
  $("ac-conclusion").value = ac.conclusion;
  $("ac-saved").innerHTML = ac.updated_by ? `<span>Last saved by</span> ${data(ac.updated_by)} · <span>${esc(when(ac.updated_at))}</span>` : "";
  $("ac-pick").value = ac.decision || "accept";
  $("ac-note").value = ac.decision_note;
  $("ac-members").innerHTML = ind.members.length
    ? `<thead><tr><th>Team member</th><th>Role</th><th>Declaration</th><th>Threats and safeguards</th><th>Declared</th></tr></thead><tbody>
      ${ind.members.map((m) => {
        const d = m.declaration;
        const status = !d ? '<span class="muted">Not yet declared</span>'
          : d.independent ? "<span>Independent</span>" : '<span class="level-significant">Threat declared</span>';
        return `<tr><td>${data(m.name)}${m.active ? "" : " <span>(deactivated)</span>"}${m.on_team ? "" : ' <span class="muted">(not on the team)</span>'}</td>
          <td>${esc(m.role_label)}</td><td>${status}</td>
          <td>${d && !d.independent ? `${data(d.threats)}${d.safeguards ? `<br><span class="muted">Safeguards:</span> ${data(d.safeguards)}` : ""}` : ""}</td>
          <td class="muted">${d ? esc(when(d.declared_at)) : ""}</td></tr>`;
      }).join("")}</tbody>`
    : '<tbody><tr><td class="muted">No one is on the team yet. An admin adds the team with "Change team".</td></tr></tbody>';
  const mine = ind.mine;
  $("ind-form").independent.value = mine && !mine.independent ? "no" : "yes";
  $("ind-threats").value = mine?.threats || "";
  $("ind-safeguards").value = mine?.safeguards || "";
  $("ind-threat").hidden = $("ind-form").independent.value !== "no";
  $("ind-mine").innerHTML = mine ? `<span>You declared on</span> <span>${esc(when(mine.declared_at))}</span>` : "";
  $("ind-save").textContent = mine ? "Update my declaration" : "Confirm my declaration";
}

async function acceptanceChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadAcceptance();
  refreshDashboard();
  await loadEngagementActivity();
}

$("ac-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!ac) return;
  const id = ac.engagement_id;
  const answers = {};
  for (const q of $("ac-questions").querySelectorAll("[data-q]")) {
    const picked = q.querySelector("input[type=radio]:checked");
    answers[q.dataset.q] = { answer: picked ? picked.value : null, comment: q.querySelector("[data-comment]").value };
  }
  busy($("ac-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/acceptance`, { answers, conclusion: $("ac-conclusion").value });
    await acceptanceChanged(id);
  });
});

$("ac-decide").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!ac) return;
  const id = ac.engagement_id;
  busy($("ac-decide-btn"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/acceptance/decision`,
      { decision: $("ac-pick").value, note: $("ac-note").value });
    await acceptanceChanged(id);
  });
});

$("ind-form").addEventListener("change", (ev) => {
  if (ev.target.name === "independent") $("ind-threat").hidden = ev.target.value !== "no";
});

$("ind-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!ac) return;
  const id = ac.engagement_id;
  const independent = $("ind-form").independent.value === "yes";
  busy($("ind-save"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/independence`, {
      independent, threats: independent ? "" : $("ind-threats").value,
      safeguards: independent ? "" : $("ind-safeguards").value });
    await acceptanceChanged(id);
  });
});

// Materiality

function benchmarkInfo(id) { return meta.benchmarks.find((b) => b.id === id); }

function renderMateriality(saved, carried) {
  const f = $("mat-form");
  const m = saved || carried;  // carried: last year's method, rolled forward without amounts
  f.benchmark.value = m?.benchmark || "pbt";
  f.benchmark_amount.value = m?.benchmark_amount ?? "";
  f.benchmark_pct.value = m?.benchmark_pct ?? benchmarkInfo(f.benchmark.value).default_pct;
  f.performance_pct.value = m?.performance_pct ?? meta.default_performance_pct;
  f.trivial_pct.value = m?.trivial_pct ?? meta.default_trivial_pct;
  f.rationale.value = m?.rationale || "";
  $("mat-saved").textContent = saved ? "Saved."
    : carried ? "Last year's method is filled in. Enter this year's benchmark amount and save."
      : "Not set yet; the materiality check is skipped until it is.";
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
    refreshDashboard();
    refreshFinishing(eng.id);
  });
});

// Uploads

const uploadMapper = Mapper($("up-mapper"));
// Lists (vendor or employee master files, fixed asset registers, inventory listings) have no
// amounts to map: their columns are picked where they're used.
const LIST_KINDS = ["list", "fixed_assets", "inventory"];
const uploadIsList = () => LIST_KINDS.includes($("up-kind").value);
function loadUploadMapper() {
  uploadMapper.setMapping(!uploadIsList());
  uploadMapper.load($("up-file").files[0]);
}
$("up-file").addEventListener("change", loadUploadMapper);
$("up-kind").addEventListener("change", loadUploadMapper);

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
      <td>${data(u.filename)}<div class="muted" style="font-size:12px" title="SHA-256">${esc(u.sha256.slice(0, 12))}…</div></td>
      <td>${esc(u.kind_label)}</td>
      <td class="num">${u.import.rows_used.toLocaleString(LOCALE)}</td>
      <td class="nowrap">${esc(when(u.uploaded_at))}</td>
      <td class="muted">${u.import.warnings.length ? u.import.warnings.map((w) => `<div>${esc(w)}</div>`).join("") : "OK"}</td>
      <td><div class="cell-actions">
        ${u.kind === "bank"
          ? `<button type="button" class="small" data-reconcile="${u.id}">Reconcile</button>`
          : u.kind === "list" ? `<button type="button" class="small" data-match="${u.id}">Match</button>`
          : u.kind === "fixed_assets" ? `<button type="button" class="small" data-goto="fa-panel" data-goto-upload="${u.id}">Look at it</button>`
          : u.kind === "inventory" ? `<button type="button" class="small" data-goto="sc-panel" data-goto-upload="${u.id}">Count</button>`
          : `<button type="button" class="small" data-run="${u.id}">Run audit</button>`}
        <button type="button" class="small secondary danger min-2" data-delete-upload="${u.id}">Delete</button>
      </div></td></tr>`).join("")
    : '<tr><td class="muted">No files uploaded yet.</td></tr>';
}

$("uploads").addEventListener("click", (e) => {
  const run = e.target.closest("[data-run]");
  const del = e.target.closest("[data-delete-upload]");
  const rec = e.target.closest("[data-reconcile]");
  const matchBtn = e.target.closest("[data-match]");
  const goto = e.target.closest("[data-goto]");
  if (goto) {
    const panel = goto.dataset.goto, select = panel === "fa-panel" ? "fa-upload" : "sc-upload";
    $(select).value = goto.dataset.gotoUpload;
    $(select).dispatchEvent(new Event("change"));
    if (panel === "sc-panel" && !sc?.count) $("sc-setup").open = true;
    $(panel).scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (matchBtn) {
    $("xm-left").value = matchBtn.dataset.match;
    $("xm-left").dispatchEvent(new Event("change"));
    $("xm-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (rec) {
    if ([...$("bank-file").options].some((o) => o.value === rec.dataset.reconcile)) {
      $("bank-file").value = rec.dataset.reconcile;
    }
    $("bank-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (run) {
    guard(async () => {
      run.disabled = true;
      run.textContent = "Running…";
      try {
        const r = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/runs`, {
          upload_id: Number(run.dataset.run),
          cutoff_days: Number($("run-cutoff").value || 0),
          round_threshold: Number($("run-round").value || 1000),
          weekend: $("run-weekend").value,
          keywords: $("run-keywords").value.split(/[,،\n]/).filter((w) => w.trim()),
          // The server keeps the name only if these are the template's settings unchanged.
          template_id: /^\d+$/.test($("run-template").value) ? Number($("run-template").value) : null,
        });
        await openEngagement(currentEngagement.id);
        showRun(r);
      } finally {  // so a failed run can be tried again
        run.disabled = false;
        run.textContent = "Run audit";
      }
    });
  } else if (del && confirm("Delete this file and the work made from it (audit runs, samples, comparisons and bank reconciliations)?")) {
    guard(async () => {
      await sendJSON("DELETE", `/api/uploads/${del.dataset.deleteUpload}`);
      await openEngagement(currentEngagement.id);
    });
  }
});

// Firm templates for the audit check settings, and the audit file download

let templates = [];
let checkDefaults = null;  // the app's standard settings, from /api/checks/options

function updatePackLink() {
  if (!currentEngagement) return;
  $("eng-pack").href = `/api/engagements/${currentEngagement.id}/pack${$("eng-pack-files").checked ? "?files=true" : ""}`;
}
$("eng-pack-files").addEventListener("change", updatePackLink);

function currentCheckSettings() {
  return {
    cutoff_days: Number($("run-cutoff").value || 0),
    round_threshold: Number($("run-round").value || 1000),
    weekend: $("run-weekend").value,
    keywords: $("run-keywords").value.split(/[,،\n]/).map((w) => w.trim()).filter(Boolean),
  };
}

function fillCheckSettings(s) {
  $("run-cutoff").value = s.cutoff_days;
  $("run-round").value = s.round_threshold;
  if ([...$("run-weekend").options].some((o) => o.value === s.weekend)) $("run-weekend").value = s.weekend;
  $("run-keywords").value = s.keywords.join(", ");
}

// The picker: the last run's settings, the firm's templates, and the app's standard ones. A
// newly opened engagement starts from its last run, else the firm default.
function renderTemplates(e, switched, pick = null) {
  const last = e.runs[0];
  const keep = pick ?? $("run-template").value;
  $("run-template").innerHTML = (last ? `<option value="last">Same as the last run (#${last.id})</option>` : "") +
    templates.map((t) => `<option value="${t.id}" translate="no">${esc(t.name)}${t.is_default ? " ★" : ""}</option>`).join("") +
    '<option value="app">The app\'s standard settings</option>';
  const has = (v) => [...$("run-template").options].some((o) => o.value === String(v));
  const firmDefault = templates.find((t) => t.is_default);
  let value = has(keep) ? keep : "app";
  if (switched || !has(keep)) value = last ? "last" : firmDefault ? String(firmDefault.id) : "app";
  if (pick !== null && has(pick)) value = String(pick);
  $("run-template").value = value;
  if (switched || pick !== null || value !== keep) applyTemplate();
  else renderTemplateButtons();
}

function selectedTemplate() {
  return templates.find((t) => String(t.id) === $("run-template").value) || null;
}

function renderTemplateButtons() {
  const t = selectedTemplate();
  const partner = me.rank >= 3;  // only partners and admins change the firm's templates
  $("tpl-save").hidden = !partner;
  $("tpl-default").hidden = !partner || !t || t.is_default;
  $("tpl-delete").hidden = !partner || !t;
}

function applyTemplate() {
  const v = $("run-template").value;
  const last = currentEngagement?.runs[0];
  if (v === "last" && last) fillCheckSettings({ ...checkDefaults, ...last.settings });
  else if (v === "app") fillCheckSettings(checkDefaults);
  else if (selectedTemplate()) fillCheckSettings(selectedTemplate());
  renderTemplateButtons();
}
$("run-template").addEventListener("change", applyTemplate);

async function reloadTemplates(pick) {
  templates = await getJSON("/api/test-templates");
  if (currentEngagement) renderTemplates(currentEngagement, false, pick);
}

$("tpl-save").addEventListener("click", () => {
  const t = selectedTemplate();
  const name = (prompt("Name for the template", t ? t.name : "") || "").trim();
  if (!name) return;
  const same = templates.find((x) => x.name.toLowerCase() === name.toLowerCase());
  if (same && !confirm(`Replace the template ${same.name} with these settings?`)) return;
  guard(async () => {
    const body = { name, ...currentCheckSettings(), is_default: same ? same.is_default : false };
    const saved = same ? await sendJSON("PUT", `/api/test-templates/${same.id}`, { ...body, name: same.name })
      : await sendJSON("POST", "/api/test-templates", body);
    await reloadTemplates(saved.id);
  });
});

$("tpl-default").addEventListener("click", () => {
  const t = selectedTemplate();
  if (!t) return;
  guard(async () => {
    await sendJSON("PUT", `/api/test-templates/${t.id}`, { ...t, is_default: true });
    await reloadTemplates(t.id);
  });
});

$("tpl-delete").addEventListener("click", () => {
  const t = selectedTemplate();
  if (!t || !confirm(`Delete the template ${t.name}? Runs already made with it keep its name.`)) return;
  guard(async () => {
    await sendJSON("DELETE", `/api/test-templates/${t.id}`);
    await reloadTemplates(null);
  });
});

// Runs

function renderRuns(runs) {
  $("runs").innerHTML = runs.length ? "<tr><th>Run</th><th>When</th><th>File</th><th>Rows</th><th>High</th><th>Medium</th><th>Low</th><th></th></tr>" +
    runs.map((r) => `<tr>
      <td>#${r.id}</td><td class="nowrap">${esc(when(r.created_at))}</td><td>${data(r.filename)}</td>
      <td class="num">${r.rows.toLocaleString(LOCALE)}</td>
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

// Matching two files: rows they have in common, such as a vendor sharing an employee's bank
// account, or the same supplier twice in one file.

let xm = null;          // the match shown
let xmColumns = {};     // upload id -> its columns, as the server read them
let xmLoads = 0;        // only the latest column lists fill the pickers
let xmWanted = false;   // the form has been used, so other files' columns are worth reading
const XM_METHODS = { exact: "Same text", digits: "Same digits", name: "Similar names" };

function renderMatchForm(e) {
  const keep = [$("xm-left").value, $("xm-right").value];
  const opts = e.uploads.map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`).join("");
  // Lists come first: they're what's usually matched, and their columns are known already.
  const lists = e.uploads.filter((u) => u.kind === "list");
  for (const [i, id] of [[0, "xm-left"], [1, "xm-right"]]) {
    $(id).innerHTML = opts || '<option value="">Upload a file first</option>';
    if (e.uploads.some((u) => String(u.id) === keep[i])) $(id).value = keep[i];
    else if (lists.length) $(id).value = lists[Math.min(i, lists.length - 1)].id;
  }
  if (!$("xm-keys").children.length && e.uploads.length) addMatchKey();
  xmWanted = false;
  const listIds = new Set(lists.map((u) => String(u.id)));
  if (listIds.has($("xm-left").value) && listIds.has($("xm-right").value)) guard(refreshMatchColumns);
}

async function columnsOf(uploadId) {
  if (!uploadId) return [];
  if (!xmColumns[uploadId]) xmColumns[uploadId] = (await getJSON(`/api/uploads/${uploadId}/columns`)).columns;
  return xmColumns[uploadId];
}

// Fill each column picker from the chosen files, keeping what was picked where it still exists.
async function refreshMatchColumns() {
  const id = currentEngagement?.id;
  const load = ++xmLoads;
  const [left, right] = await Promise.all([columnsOf($("xm-left").value), columnsOf($("xm-right").value)]);
  if (load !== xmLoads || currentEngagement?.id !== id) return;
  const fill = (select, cols) => {
    const keep = select.value;
    select.innerHTML = cols.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    if (cols.includes(keep)) select.value = keep;
  };
  for (const row of $("xm-keys").querySelectorAll(".xm-key")) {
    fill(row.querySelector("[data-left]"), left);
    fill(row.querySelector("[data-right]"), right);
  }
}

function addMatchKey() {
  const n = $("xm-keys").children.length;
  if (n >= 5) return;
  $("xm-keys").insertAdjacentHTML("beforeend", `<div class="xm-key grid">
    <div><label>Column in the first file</label><select class="map" data-left translate="no"></select></div>
    <div><label>Column in the second file</label><select class="map" data-right translate="no"></select></div>
    <div><label>Match by</label><select class="map" data-method>${Object.entries(XM_METHODS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
    <div class="xm-remove"><button type="button" class="small secondary" data-remove-key>Remove</button></div>
  </div>`);
}

$("xm-add-key").addEventListener("click", () => { addMatchKey(); guard(refreshMatchColumns); });
$("xm-keys").addEventListener("click", (ev) => {
  if (!ev.target.closest("[data-remove-key]")) return;
  if ($("xm-keys").children.length > 1) ev.target.closest(".xm-key").remove();
});
$("xm-left").addEventListener("change", () => guard(refreshMatchColumns));
$("xm-right").addEventListener("change", () => guard(refreshMatchColumns));
// Reading a large ledger's columns takes a moment, so it waits until the form is used.
$("xm-form").addEventListener("focusin", () => {
  if (xmWanted) return;
  xmWanted = true;
  guard(refreshMatchColumns);
});

$("xm-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  const keys = [...$("xm-keys").querySelectorAll(".xm-key")].map((row) => ({
    left: row.querySelector("[data-left]").value, right: row.querySelector("[data-right]").value,
    method: row.querySelector("[data-method]").value }));
  busy($("xm-run"), async () => {
    const m = await sendJSON("POST", `/api/engagements/${id}/cross-matches`, {
      left_upload_id: Number($("xm-left").value), right_upload_id: Number($("xm-right").value),
      keys, threshold: Number($("xm-threshold").value || 90), title: $("xm-name").value });
    if (currentEngagement?.id !== id) return;
    await openEngagement(id);
    showMatch(m);
    refreshDashboard();
    await loadEngagementActivity();
  });
});

function matchTitle(m) {
  return m.title || (m.left_upload_id === m.right_upload_id ? `Duplicates in ${m.left_filename}`
    : `${m.left_filename} against ${m.right_filename}`);
}

function renderMatches(list) {
  $("xm-list").innerHTML = list.length
    ? "<tr><th>Match</th><th>When</th><th>Files</th><th>Pairs found</th><th></th></tr>" + list.map((m) => `<tr>
      <td>#${m.id} ${m.title ? data(m.title) : ""}</td><td class="nowrap">${esc(when(m.created_at))}</td>
      <td>${data(m.left_filename)}${m.left_upload_id === m.right_upload_id ? "" : ` · ${data(m.right_filename)}`}</td>
      <td class="num">${m.totals.pairs.toLocaleString(LOCALE)}</td>
      <td><button type="button" class="small secondary" data-view-match="${m.id}">View</button></td></tr>`).join("")
    : "";
  if (xm && !list.some((m) => m.id === xm.id)) { xm = null; $("xm-result").hidden = true; }
}

$("xm-list").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-view-match]");
  if (!b) return;
  const id = currentEngagement.id;
  guard(async () => {
    const m = await getJSON(`/api/cross-matches/${b.dataset.viewMatch}`);
    if (currentEngagement?.id === id && m.engagement_id === id) showMatch(m);
  });
});

function showMatch(m) {
  xm = m;
  const r = m.result, t = r.totals;
  $("xm-result").hidden = false;
  $("xm-heading").innerHTML = `<bdi>#${m.id}</bdi> ${m.title ? data(m.title)
    : m.left_upload_id === m.right_upload_id ? `<span>Duplicates in</span> ${data(m.left_filename)}`
    : `${data(m.left_filename)} <span>against</span> ${data(m.right_filename)}`}`;
  $("xm-export").href = `/api/cross-matches/${m.id}/export`;
  const rowCards = r.same_file ? [[`${t.left_matched}/${t.left_rows}`, "Rows with a duplicate", false]]
    : [[`${t.left_matched}/${t.left_rows}`, "Rows matched in the first file", false],
      [`${t.right_matched}/${t.right_rows}`, "Rows matched in the second file", false]];
  $("xm-cards").innerHTML = [[t.pairs, "Matching pairs", t.pairs > 0], [t.all_keys, "Matching on every column", t.all_keys > 0], ...rowCards]
    .map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(typeof v === "number" ? v.toLocaleString(LOCALE) : v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const notes = [];
  if (r.truncated) notes.push(`<span>Only the first ${r.hidden + r.matches.length} pairs were kept. Pick columns that tell the rows apart to see the rest.</span>`);
  if (r.hidden) notes.push(`<span>Showing the first ${r.matches.length}. The Excel download has the rest.</span>`);
  for (const k of r.keys) {
    if (k.common_value_count) notes.push(`${data(k.left)}: <span>Values in more than 50 rows were skipped, as they don't tell rows apart:</span> ${k.common_values.map((x) => `${data(x.value)} (<bdi>${x.rows}</bdi>)`).join(", ")}`);
    if (k.rows_not_compared) notes.push(`${data(k.left)}: <span>${k.rows_not_compared} rows had too many similar names to compare.</span>`);
  }
  $("xm-note").innerHTML = notes.map((n) => `<div>${n}</div>`).join("");
  const keyLabel = (k) => `${data(k.left)} = ${data(k.right)} <span class="tag">(<span>${esc(k.method_label)}</span>)</span>`;
  const side = (row, file, values, cols) => `<div class="tag"><span>Row</span> <bdi>${row}</bdi></div>` +
    cols.filter((c) => c in values).map((c) => `<div><bdi class="muted" translate="no">${esc(c)}:</bdi> ${data(values[c])}</div>`).join("");
  const leftCols = [...new Set([r.left_columns[0], ...r.keys.map((k) => k.left)])];
  const rightCols = [...new Set([r.right_columns[0], ...r.keys.map((k) => k.right)])];
  $("xm-table").innerHTML = r.matches.length
    ? "<tr><th>Matched on</th><th>First file</th><th>Second file</th></tr>" + r.matches.map((x) => `<tr>
      <td>${x.keys.map((k) => `<div>${keyLabel(r.keys[k.key])}${k.score < 100 ? ` <bdi>${k.score}%</bdi>` : ""}</div>`).join("")}</td>
      <td>${side(x.left_row, 1, x.left, leftCols)}</td><td>${side(x.right_row, 2, x.right, rightCols)}</td></tr>`).join("")
    : '<tr><td class="muted">No rows match. The files have nothing in common on these columns.</td></tr>';
  $("xm-result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("xm-delete").addEventListener("click", () => {
  if (!xm || !confirm(`Delete match #${xm.id}?`)) return;
  const id = currentEngagement.id;
  guard(async () => {
    await sendJSON("DELETE", `/api/cross-matches/${xm.id}`);
    xm = null;
    $("xm-result").hidden = true;
    await openEngagement(id);
    await loadEngagementActivity();
  });
});

// Disclosure checklist, with the AI's reading of the draft statements beside the answers

let dc = null;
let dcLoads = 0;
const dcReading = {};  // engagement id -> AI reviews under way
const DC_STATUSES = { yes: "Given", no: "Missing", na: "Not applicable" };

async function loadDisclosures() {
  const id = currentEngagement.id;
  const load = ++dcLoads;
  const d = await getJSON(`/api/engagements/${id}/disclosures`);
  if (load !== dcLoads || currentEngagement?.id !== id) return;
  showDisclosures(d, id);
}

// The answers on the page that differ from the last ones loaded: unsaved edits.
function disclosureEdits() {
  const edits = {};
  if (!dc) return edits;
  const before = Object.fromEntries(dc.items.map((x) => [x.id, x]));
  for (const el of $("dc-items").querySelectorAll(".dc-item")) {
    const x = before[el.dataset.item];
    if (!x) continue;
    const picked = el.querySelector("input[type=radio]:checked");
    const now = { status: picked ? picked.value : null,
      reference: el.querySelector("[data-reference]").value, comment: el.querySelector("[data-comment]").value };
    if (now.status !== (x.status || null) || now.reference !== (x.reference || "") || now.comment !== (x.comment || "")) edits[x.id] = now;
  }
  return edits;
}

// Show fresh answers from the server, keeping anything typed but not saved yet (unless it's
// for another engagement).
function showDisclosures(d, id) {
  const edits = dc && dc.engagement_id === id ? disclosureEdits() : {};
  dc = { ...d, engagement_id: id };
  renderDisclosures();
  for (const [item, a] of Object.entries(edits)) {
    const el = $("dc-items").querySelector(`.dc-item[data-item="${CSS.escape(item)}"]`);
    if (!el) continue;
    for (const r of el.querySelectorAll("input[type=radio]")) r.checked = r.value === a.status;
    el.querySelector("[data-reference]").value = a.reference;
    el.querySelector("[data-comment]").value = a.comment;
  }
}

function renderDisclosures() {
  if (!dc) {
    $("dc-cards").innerHTML = $("dc-items").innerHTML = $("dc-ai-note").innerHTML = $("dc-saved").textContent = "";
    $("dc-export").removeAttribute("href");
    $("dc-framework").hidden = true;
    return;
  }
  const n = dc.counts;
  // The items are full IFRS; IFRS for SMEs asks for less, and differently.
  $("dc-framework").hidden = !currentEngagement || currentEngagement.framework === "IFRS";
  $("dc-export").href = `/api/engagements/${dc.engagement_id}/disclosures/export`;
  $("dc-cards").innerHTML = [[`${n.answered}/${n.items}`, "Items answered", false], [n.given, "Given", false],
    [n.missing, "Missing", n.missing > 0], [n.na, "Not applicable", false],
    [dc.differs, "Read differently by the AI", dc.differs > 0]]
    .map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("dc-ai-note").innerHTML = dc.ai
    ? `<p class="muted"><span>The AI assistant read</span> ${data(dc.ai.filename)} (${data(dc.ai.model)}). <span>Asked by</span> ${data(dc.ai.by)}, <span>${esc(when(dc.ai.at.replace(/\+00:00$/, "")))}</span></p>
       ${dc.ai_pending ? `<div class="actions"><button type="button" class="small" id="dc-accept">Use the AI's answers for the ${dc.ai_pending} items not answered yet</button></div>` : ""}`
    : "";
  let standard = null;
  $("dc-items").innerHTML = dc.items.map((x) => {
    const head = x.standard !== standard ? `<h3 translate="no">${esc((standard = x.standard))}</h3>` : "";
    const ai = x.ai ? `<div class="tag dc-ai${x.differs ? " flag-note" : ""}"><span>AI:</span> <span>${esc(x.ai.status_label)}</span>${x.ai.where ? ` · ${data(x.ai.where)}` : ""}${x.ai.evidence ? ` · ${data(x.ai.evidence)}` : ""}</div>` : "";
    return `${head}<div class="ac-q dc-item${x.differs ? " flagged" : ""}${x.status === "no" ? " missing" : ""}" data-item="${esc(x.id)}" data-status="${esc(x.status || "")}" data-differs="${x.differs ? 1 : ""}">
      <p><span>${esc(x.text)}</span></p>
      <fieldset class="checks-inline"><legend class="visually-hidden">Answer</legend>
        ${Object.entries(DC_STATUSES).map(([v, label]) => `<label><input type="radio" name="dc-${esc(x.id)}" value="${v}" ${x.status === v ? "checked" : ""}><span>${label}</span></label>`).join("")}
      </fieldset>
      <div class="grid"><div><input data-reference maxlength="2000" value="${esc(x.reference)}" placeholder="Where (note or page)" aria-label="Where (note or page)"></div>
        <div><input data-comment maxlength="2000" value="${esc(x.comment)}" placeholder="Comment" aria-label="Comment"></div></div>
      ${ai}
    </div>`;
  }).join("");
  lockDisclosures();
  $("dc-saved").innerHTML = dc.updated_by ? `<span>Last saved by</span> ${data(dc.updated_by)} · <span>${esc(when(dc.updated_at))}</span>` : "";
  filterDisclosures();
}

function filterDisclosures() {
  const only = $("dc-open-only").checked;
  for (const el of $("dc-items").querySelectorAll(".dc-item")) {
    el.hidden = only && el.dataset.status && el.dataset.status !== "no" && !el.dataset.differs;
  }
}
$("dc-open-only").addEventListener("change", filterDisclosures);

async function disclosuresChanged(id) {
  if (currentEngagement?.id !== id) return;
  refreshDashboard();
  await loadEngagementActivity();
}

// The answers can't be changed while a save is on its way: what's typed meanwhile wasn't
// sent, and would be lost when the saved answers come back.
let dcSaving = 0;
function lockDisclosures() {
  for (const el of $("dc-items").querySelectorAll("input")) el.disabled = dcSaving > 0;
}
async function whileSavingDisclosures(work) {
  dcSaving += 1;
  lockDisclosures();
  try {
    await work();
  } finally {
    dcSaving -= 1;
    lockDisclosures();
  }
}

$("dc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!dc) return;
  const id = dc.engagement_id;
  // Only what was changed here, so answers someone else saved meanwhile aren't undone.
  const answers = disclosureEdits();
  busy($("dc-save"), () => whileSavingDisclosures(async () => {
    const d = await sendJSON("PUT", `/api/engagements/${id}/disclosures`, { answers });
    if (currentEngagement?.id !== id) return;
    dc = { ...d, engagement_id: id };
    renderDisclosures();
    await disclosuresChanged(id);
  }));
});

$("dc-ai-note").addEventListener("click", (ev) => {
  const b = ev.target.closest("#dc-accept");
  if (!b || !dc) return;
  const id = dc.engagement_id;
  busy(b, () => whileSavingDisclosures(async () => {
    const d = await sendJSON("POST", `/api/engagements/${id}/disclosures/accept-ai`);
    if (currentEngagement?.id !== id) return;
    showDisclosures(d, id);
    await disclosuresChanged(id);
  }));
});

$("dc-ai-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  const fd = new FormData();
  fd.append("file", $("dc-file").files[0]);
  fd.append("language", I18N.lang === "ar" ? "ar" : "en");
  guard(async () => {
    dcReading[id] = (dcReading[id] || 0) + 1;
    renderAiPanels();
    try {
      const d = await (await postForm(`/api/engagements/${id}/disclosures/ai-review`, fd)).json();
      if (currentEngagement?.id !== id) return;
      showDisclosures(d, id);
      $("dc-ai-form").reset();
    } finally {
      dcReading[id] -= 1;
      if (currentEngagement?.id === id) { renderAiPanels(); loadEngagementActivity(); }
    }
  });
});

// Prior-year comparison

let comparisonFormFor = null;

function renderComparisonForm(e) {
  if (comparisonFormFor !== e.id) {  // another engagement: don't carry its thresholds over
    comparisonFormFor = e.id;
    $("cmp-amount").value = "";
    $("cmp-pct").value = "10";
  }
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
      <td>${data(c.current_filename)}</td><td>${data(c.prior_filename)}</td>
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
    rows.map((r) => `<tr><td>${data(r.account)}</td><td>${data(r.name)}</td>
      <td class="num">${money(r.current)}</td><td class="num">${money(r.prior)}</td>
      <td class="num">${money(r.change)}</td>
      <td class="num">${r.change_pct == null ? "–" : (r.change_pct > 0 ? "+" : "") + r.change_pct + "%"}</td>
      <td>${r.reasons.map((x) => `<div>${esc(x)}</div>`).join("")}</td></tr>`).join("");
}
$("cmp-flagged-only").addEventListener("change", renderComparisonTable);

// Bank reconciliation

let bank = null;          // latest /bank-reconciliations/options answer
let bankChecked = null;   // accounts ticked by hand for the general ledger in bank.gl_upload_id
let bankLoads = 0;

async function loadBank(glId) {
  const id = currentEngagement.id;
  const load = ++bankLoads;
  const q = glId ? `?gl_upload_id=${glId}` : "";
  const o = await getJSON(`/api/engagements/${id}/bank-reconciliations/options${q}`);
  if (currentEngagement?.id !== id || load !== bankLoads) return;
  const keepBank = $("bank-file").value;
  if (!bank || bank.gl_upload_id !== o.gl_upload_id) bankChecked = null;
  bank = o;
  const opts = (list) => list.map((u) =>
    `<option value="${u.id}" translate="no">${esc(u.filename)}</option>`).join("");
  $("bank-file").innerHTML = opts(o.bank_uploads) || '<option value="">Upload a bank statement first</option>';
  if (o.bank_uploads.some((u) => String(u.id) === keepBank)) $("bank-file").value = keepBank;
  else if (o.bank_uploads.length) $("bank-file").value = o.bank_uploads[o.bank_uploads.length - 1].id;
  $("bank-gl").innerHTML = opts(o.gl_uploads) || '<option value="">Upload the general ledger first</option>';
  if (o.gl_upload_id) $("bank-gl").value = o.gl_upload_id;
  renderBankAccounts();
  renderBankRecs();
  bankBalanceHint();
}

function renderBankAccounts() {
  const ticked = bankChecked || new Set(bank.accounts.filter((a) => a.cash).map((a) => a.account));
  const box = (a) => `<label><input type="checkbox" value="${esc(a.account)}" ${ticked.has(a.account) ? "checked" : ""}>
    ${data(a.account)} <span class="muted">(${a.lines.toLocaleString(LOCALE)})</span></label>`;
  const cash = bank.accounts.filter((a) => a.cash);
  const other = bank.accounts.filter((a) => !a.cash);
  $("bank-accounts").innerHTML = !bank.accounts.length ? '<span class="muted">Upload the general ledger first.</span>'
    : (cash.length ? cash.map(box).join("") : '<span class="muted">No account looks like cash at bank. Tick it below.</span>') +
      (other.length ? `<details style="flex-basis:100%"${other.some((a) => ticked.has(a.account)) ? " open" : ""}>
        <summary>Other accounts</summary><div class="check-list">${other.map(box).join("")}</div></details>` : "");
  bankBookHint();
}

function bankTicked() {
  return [...$("bank-accounts").querySelectorAll("input:checked")].map((i) => i.value);
}

function bankBalanceHint() {
  const u = bank.bank_uploads.find((x) => String(x.id) === $("bank-file").value);
  const read = u?.import?.read_by_ai;
  $("bank-balance").placeholder = read && read.period_end === bank.period_end && read.balance_at_period_end != null
    && !(read.problems || []).length
    ? `${money(read.balance_at_period_end)} (from the statement)` : "";
}
$("bank-file").addEventListener("change", () => { if (bank) bankBalanceHint(); });

function bankBookHint() {
  const codes = new Set(bankTicked().map((a) => bank.accounts.find((x) => x.account === a)?.code));
  const tb = bank.tb_balances;
  const found = tb ? [...codes].filter((c) => c in tb) : [];
  $("bank-book").placeholder = found.length
    ? `${money(found.reduce((t, c) => t + tb[c], 0))} (from the trial balance)` : "Amount";
}

$("bank-accounts").addEventListener("change", () => {
  bankChecked = new Set(bankTicked());
  bankBookHint();
});
$("bank-gl").addEventListener("change", () => { if ($("bank-gl").value) guard(() => loadBank($("bank-gl").value)); });

$("bank-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const num = (el) => $(el).value === "" ? null : Number($(el).value);
  guard(async () => {
    $("bank-run").disabled = true;
    try {
      const r = await sendJSON("POST", `/api/engagements/${currentEngagement.id}/bank-reconciliations`, {
        bank_upload_id: Number($("bank-file").value),
        gl_upload_id: Number($("bank-gl").value),
        accounts: bankTicked(),
        window_days: Number($("bank-window").value || 0),
        bank_balance: num("bank-balance"),
        book_balance: num("bank-book"),
        flip_bank: $("bank-flip").checked,
      });
      await loadBank($("bank-gl").value);
      showBankRec(r);
      refreshDashboard();
    } finally {
      $("bank-run").disabled = false;
    }
  });
});

function renderBankRecs() {
  const list = bank.reconciliations;
  $("bank-recs").innerHTML = list.length ? "<tr><th>#</th><th>When</th><th>Bank statement</th><th>Matched</th><th>Outstanding</th><th>Not in the books</th><th>Difference</th><th></th></tr>" +
    list.map((r) => `<tr><td>${r.id}</td><td class="nowrap">${esc(when(r.created_at))}</td>
      <td>${data(r.bank_filename)}</td><td class="num">${r.summary.matched}</td>
      <td class="num">${r.summary.outstanding}</td><td class="num">${r.summary.bank_only}</td>
      <td class="num">${r.difference == null ? "–" : money(r.difference)}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-view-bank="${r.id}">View</button>
        <a class="button" href="/api/bank-reconciliations/${r.id}/report">Excel</a>
      </div></td></tr>`).join("") : "";
}

$("bank-recs").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-view-bank]");
  if (btn) guard(async () => showBankRec(await getJSON(`/api/bank-reconciliations/${btn.dataset.viewBank}`)));
});

const BANK_STATEMENT = [
  ["bank_balance", "Balance per bank statement"],
  ["deposits_in_transit", "Add: deposits in transit"],
  ["outstanding_payments", "Less: outstanding payments"],
  ["adjusted_bank", "Adjusted bank balance", "subtotal"],
  ["book_balance", "Balance per books"],
  ["receipts_not_in_books", "Add: receipts on the bank statement not in the books"],
  ["payments_not_in_books", "Less: payments on the bank statement not in the books"],
  ["adjusted_book", "Adjusted book balance", "subtotal"],
  ["difference", "Unexplained difference", "total"],
];

function showBankRec(r) {
  const res = r.result, s = res.summary, st = res.statement;
  $("bank-result").hidden = false;
  $("bank-title").textContent = `#${r.id}`;
  $("bank-files").innerHTML = `${data(r.bank_filename)} · ${data(res.settings.accounts.join(", "))} · ${data(res.settings.period_end)}`;
  $("bank-actions").innerHTML = `<a class="button" href="/api/bank-reconciliations/${r.id}/report">Download Excel</a>`;
  const diff = st.difference;
  $("bank-cards").innerHTML = [
    [s.matched, "Matched"],
    [s.outstanding, "Outstanding at the period end"],
    [s.cleared_after, "Cleared after the period end"],
    [s.stale, "Old and not cleared", s.stale > 0],
    [s.bank_only, "Not in the books"],
    [diff == null ? "–" : money(diff), "Unexplained difference", diff != null && Math.abs(diff) >= 0.01],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("bank-statement").className = "fs-table";
  $("bank-statement").innerHTML = BANK_STATEMENT.map(([key, label, cls]) => `<tr class="${cls || "line"}">
    <td>${esc(label)}</td><td class="num">${st[key] == null ? "–" : money(st[key])}</td></tr>`).join("") +
    (st.bank_balance == null || st.book_balance == null
      ? '<tr><td class="muted" colspan="2">Enter the bank and book balances to see the unexplained difference.</td></tr>' : "");
  $("bank-outstanding").innerHTML = res.outstanding.length
    ? "<tr><th>Type</th><th>Date</th><th>Entry</th><th>Description</th><th>Amount</th><th>Days outstanding</th><th>Cleared on</th><th>Cleared by</th></tr>" +
      res.outstanding.map((o) => `<tr><td>${esc(o.kind)}${o.stale ? ' <span class="sev high">Old and not cleared</span>' : ""}</td>
        <td class="nowrap">${data(o.date)}</td><td>${data(o.entry_id || "")}</td><td>${data(o.description || "")}</td>
        <td class="num">${money(o.amount)}</td><td class="num">${o.days_outstanding}</td>
        <td class="nowrap">${o.cleared ? data(o.cleared.date) : ""}</td><td>${o.cleared ? data(o.cleared.description || "") : ""}</td></tr>`).join("")
    : '<tr><td class="muted">None.</td></tr>';
  $("bank-only").innerHTML = res.bank_only.length
    ? "<tr><th>Type</th><th>Date</th><th>Description</th><th>Amount</th></tr>" +
      res.bank_only.map((b) => `<tr><td>${esc(b.kind)}${b.early ? ' <span class="sev low" title="An item outstanding at last year\'s end clears early in this period and is already in last year\'s books.">May be last year\'s item</span>' : ""}</td><td class="nowrap">${data(b.date)}</td>
        <td>${data(b.description || "")}</td><td class="num">${money(b.amount)}</td></tr>`).join("")
    : '<tr><td class="muted">None.</td></tr>';
  $("bank-matched").innerHTML = res.matched.length
    ? "<tr><th>Bank date</th><th>Bank description</th><th>Ledger date</th><th>Entry</th><th>Ledger description</th><th>Amount</th><th>Days apart</th></tr>" +
      res.matched.map((m) => `<tr><td class="nowrap">${data(m.bank_date)}</td><td>${data(m.bank_description || "")}</td>
        <td class="nowrap">${data(m.ledger_date)}</td><td>${data(m.entry_id || "")}</td><td>${data(m.ledger_description || "")}</td>
        <td class="num">${money(m.amount)}</td><td class="num">${m.days}</td></tr>`).join("")
    : '<tr><td class="muted">None.</td></tr>';
  $("bank-result").scrollIntoView({ behavior: "smooth" });
}

// Reading documents with AI

let docReading = 0;  // reads still waiting on the AI, even for another engagement

function renderDocPanel() {
  const off = !assistant.enabled;
  $("doc-off").hidden = !off;
  $("doc-form").querySelectorAll("select, input, button").forEach((el) => { el.disabled = off; });
  if (docReading) $("doc-run").disabled = true;
}

$("doc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  const fd = new FormData();
  fd.append("kind", $("doc-kind").value);
  fd.append("file", $("doc-file").files[0]);
  guard(async () => {
    docReading += 1;
    $("doc-run").disabled = true;
    $("doc-wait").hidden = false;
    try {
      const r = await (await postForm(`/api/engagements/${id}/documents/read`, fd)).json();
      if (currentEngagement?.id !== id) return;
      showDocument(r);
      if (r.kind === "bank") await openEngagement(id);  // the new file, ready to reconcile
    } finally {
      docReading -= 1;
      $("doc-run").disabled = docReading > 0 || !assistant.enabled;
      $("doc-wait").hidden = true;
      if (currentEngagement?.id === id) loadEngagementActivity();
    }
  });
});

function showDocument(r) {
  const card = (v, k) => `<div class="card"><div class="v">${v}</div><div class="k">${esc(k)}</div></div>`;
  $("doc-out").hidden = false;
  $("doc-label").innerHTML = `<span>Read by the AI assistant</span> (${data(r.model)}): ${data(r.kind === "bank" ? r.read.source : r.filename)}`;
  const problems = r.kind === "bank" ? r.read.problems : [];
  $("doc-problems").hidden = !problems.length;
  $("doc-problems").innerHTML = problems.map((p) => `<li>${esc(p)}</li>`).join("");
  $("doc-matches-title").hidden = r.kind !== "invoice";
  if (r.kind === "bank") {
    const x = r.read;
    $("doc-cards").innerHTML = [
      card(r.lines, "Lines read"),
      card(data(`${x.statement_from} – ${x.statement_to}`), "Dates"),
      card(x.closing_balance == null ? "–" : money(x.closing_balance), "Closing balance"),
      card(x.balance_at_period_end == null ? "–" : money(x.balance_at_period_end), "Balance at the period end"),
    ].join("");
    $("doc-checks").innerHTML = `<li>${problems.length ? "" : "✓ "}<span>Saved as the bank statement</span> ${data(r.upload.filename)}<span>. Reconcile it in the bank reconciliation panel.</span></li>`;
    $("doc-matches").innerHTML = "";
    return;
  }
  const inv = r.invoice;
  $("doc-cards").innerHTML = [
    card(data(inv.supplier || "–"), "Supplier"),
    card(data(inv.invoice_number || "–"), "Invoice number"),
    card(data(inv.invoice_date || "–"), "Invoice date"),
    card(inv.total == null ? "–" : `${money(inv.total)} ${esc(inv.currency || "")}`, "Total"),
    card(inv.tax == null ? "–" : money(inv.tax), "Tax"),
  ].join("");
  $("doc-checks").innerHTML = r.checks.map((c) => `<li class="${c.ok ? "" : "warn-high"}">${c.ok ? "✓" : "✗"} <span>${esc(c.text)}</span></li>`).join("");
  $("doc-matches").innerHTML = !r.ledger
    ? '<tr><td class="muted">Upload the general ledger to look the invoice up in it.</td></tr>'
    : r.matches.length
      ? "<tr><th>Date</th><th>Entry</th><th>Account</th><th>Description</th><th>Amount</th><th>Days from the invoice</th><th>Why</th></tr>" +
        r.matches.map((m) => `<tr><td class="nowrap">${data(m.date || "")}</td><td>${data(m.entry_id || "")}</td>
          <td>${data(m.account)}</td><td>${data(m.description)}</td><td class="num">${money(m.amount)}</td>
          <td class="num">${m.days_from_invoice ?? ""}</td><td>${m.reasons.map((x) => `<div>${esc(x)}</div>`).join("")}</td></tr>`).join("")
      : '<tr><td class="muted">No entry in the general ledger has this amount or invoice number.</td></tr>';
}

// AI risk assessment and management letter

const LEVELS = { significant: "Significant risk", higher: "Higher risk", lower: "Lower risk" };
const ASSERTIONS = {
  existence: "Existence", occurrence: "Occurrence", completeness: "Completeness", accuracy: "Accuracy",
  valuation: "Valuation", cut_off: "Cut-off", rights: "Rights and obligations",
  classification: "Classification", presentation: "Presentation and disclosure",
};
const PRIORITIES = { high: "High priority", medium: "Medium priority", low: "Low priority" };
const aiDrafting = {};  // requests still waiting on the AI, by "kind:engagement id"
let letterText = "";
const aiShown = { risk: 0, letter: 0 };  // the draft on show, so an older load can't replace it

function renderAiPanels() {
  const off = !assistant.enabled;
  document.querySelectorAll(".ai-off").forEach((el) => { el.hidden = !off; });
  const id = currentEngagement?.id;
  for (const [kind, run] of [["risk", "ra"], ["letter", "ml"]]) {
    const waiting = (aiDrafting[`${kind}:${id}`] || 0) > 0;
    $(`${run}-run`).disabled = off || waiting;
    $(`${run}-wait`).hidden = !waiting;
  }
  $("ml-statuses").querySelectorAll("input").forEach((el) => { el.disabled = off; });
  $("dc-file").disabled = off;
  $("dc-ai-run").disabled = off || (dcReading[id] || 0) > 0;
  $("dc-wait").hidden = !(dcReading[id] || 0);
}

function draftLabel(d) {
  return `<span>Drafted by the AI assistant</span> (${data(d.model)}). <span>Asked by</span> ${data(d.created_by)}, ${data(when(d.created_at))}`;
}

function showRisk(d) {
  aiShown.risk = d?.id || 0;
  $("ra-out").hidden = !d;
  $("ra-export").hidden = !d;
  if (!d) return;
  $("ra-export").href = `/api/ai-drafts/${d.id}/export`;
  $("ra-import").hidden = !d.content.risks.length;
  $("ra-label").innerHTML = draftLabel(d);
  $("ra-summary").innerHTML = data(d.content.summary);
  $("ra-risks").innerHTML = "<tr><th>Level</th><th>Area</th><th>Risk</th><th>Assertions</th><th>Why</th><th>Planned procedures</th></tr>" +
    d.content.risks.map((r) => `<tr>
      <td class="nowrap level-${esc(r.level)}">${esc(LEVELS[r.level])}</td>
      <td>${esc(r.area_label)}</td><td>${data(r.title)}</td>
      <td>${r.assertions.map((a) => `<div>${esc(ASSERTIONS[a])}</div>`).join("")}</td>
      <td>${data(r.why)}</td>
      <td><ul>${r.procedures.map((p) => `<li>${data(p)}</li>`).join("")}</ul></td></tr>`).join("");
}

function showLetter(d) {
  aiShown.letter = d?.id || 0;
  $("ml-out").hidden = !d;
  $("ml-export").hidden = $("ml-copy").hidden = !d;
  if (!d) return;
  const c = d.content;
  $("ml-export").href = `/api/ai-drafts/${d.id}/export`;
  const s = d.sent || {};
  $("ml-label").innerHTML = draftLabel(d) + (s.findings_total > s.findings
    ? `<div class="warn-box">${esc(`Written from ${s.findings} of ${s.findings_total} findings, the most severe first. The rest were not sent.`)}</div>` : "");
  const findings = (ids) => ids.map((i) => `<a href="#" data-open-finding="${i}" translate="no">#${i}</a>`).join(", ");
  $("ml-letter").innerHTML = `<p>${data(c.intro)}</p>` + c.points.map((p, i) => `<div class="point">
      <h3>${i + 1}. ${data(p.title)}</h3>
      <div class="tag"><span class="sev ${esc(p.priority)}">${esc(PRIORITIES[p.priority])}</span>${p.finding_ids.length ? ` <span>Findings</span> ${findings(p.finding_ids)}` : ""}</div>
      <dl><dt>Observation</dt><dd>${data(p.observation)}</dd>
      <dt>Implication</dt><dd>${data(p.implication)}</dd>
      <dt>Recommendation</dt><dd>${data(p.recommendation)}</dd></dl></div>`).join("") +
    `<p>${data(c.closing)}</p>`;
  // Plain text to paste into the letter; the AI's own words, in the language it wrote in.
  letterText = [c.intro, ...c.points.map((p, i) => [`${i + 1}. ${p.title}`, p.observation,
    p.implication, p.recommendation].filter(Boolean).join("\n\n")), c.closing].filter(Boolean).join("\n\n");
}

async function loadAiDrafts() {
  const id = currentEngagement.id;
  const r = await getJSON(`/api/engagements/${id}/ai-drafts`);
  if (currentEngagement?.id !== id) return;
  // A draft made while this was loading is newer than what it found.
  if ((r.risk.latest?.id || 0) >= aiShown.risk) showRisk(r.risk.latest);
  if ((r.letter.latest?.id || 0) >= aiShown.letter) showLetter(r.letter.latest);
}

async function draftWithAi(kind, url, body) {
  const id = currentEngagement.id;
  const key = `${kind}:${id}`;
  aiDrafting[key] = (aiDrafting[key] || 0) + 1;
  renderAiPanels();
  try {
    const d = await sendJSON("POST", `/api/engagements/${id}/${url}`, body);
    if (currentEngagement?.id === id) (kind === "risk" ? showRisk : showLetter)(d);
  } finally {
    aiDrafting[key] -= 1;
    renderAiPanels();
    if (currentEngagement?.id === id) loadEngagementActivity();
  }
}

$("ra-run").addEventListener("click", () => guard(() => {
  const q = Object.fromEntries(new URLSearchParams(fsQuery()));
  return draftWithAi("risk", "ai/risk-assessment", {
    upload_id: q.upload_id ? Number(q.upload_id) : null,
    prior_upload_id: q.prior_upload_id ? Number(q.prior_upload_id) : null,
    language: I18N.lang,
  });
}));

$("ml-run").addEventListener("click", () => guard(() => {
  const statuses = [...$("ml-statuses").querySelectorAll("input:checked")].map((el) => el.value);
  if (!statuses.length) throw new Error("Tick at least one status.");
  return draftWithAi("letter", "ai/management-letter", { statuses, language: I18N.lang });
}));

$("ml-copy").addEventListener("click", () => guard(async () => {
  await navigator.clipboard.writeText(letterText);
  $("ml-copy").textContent = "Copied";
  setTimeout(() => { $("ml-copy").textContent = "Copy letter"; }, 2000);
}));

$("ml-letter").addEventListener("click", (ev) => {
  const a = ev.target.closest("[data-open-finding]");
  if (!a) return;
  ev.preventDefault();
  guard(async () => openFinding(await getJSON(`/api/findings/${a.dataset.openFinding}`)));
});

// Risk register and audit program

let rr = null;         // the register on show: {engagement_id, summary, risks}
let rrOptions = null;  // areas, levels and assertions, loaded once
let rrEditing = null;  // the risk being changed in the form, or null when adding one
let rrLoads = 0;       // only the latest answer is shown, if an older one arrives after it

async function loadRiskOptions() {
  if (rrOptions) return;
  const o = await getJSON("/api/risk-register/options");
  $("rr-area").innerHTML = o.areas.map((a) => `<option value="${esc(a.id)}">${esc(a.label)}</option>`).join("");
  $("rr-level").innerHTML = o.levels.map((l) => `<option value="${esc(l.id)}">${esc(l.label)}</option>`).join("");
  $("rr-assertions").innerHTML = "<legend>Assertions</legend>" + o.assertions.map((a) =>
    `<label><input type="checkbox" name="assertions" value="${esc(a.id)}"><span>${esc(a.label)}</span></label>`).join("");
  rrOptions = o;
}

async function loadRisks() {
  const id = currentEngagement.id;
  const load = ++rrLoads;
  await loadRiskOptions();
  const r = await getJSON(`/api/engagements/${id}/risks`);
  if (load !== rrLoads || currentEngagement?.id !== id) return;
  rr = { ...r, engagement_id: id };
  renderRisks();
}

function resetRiskForm() {
  rrEditing = null;
  $("rr-form").reset();
  $("rr-form-title").textContent = "Add a risk";
  $("rr-save").textContent = "Add risk";
  $("rr-cancel").hidden = true;
  $("rr-procs-wrap").hidden = false;
}

function renderRisks() {
  if (ctOptions) fillControlRisks();
  if (!rr) { $("rr-cards").innerHTML = $("rr-list").innerHTML = ""; return; }
  const s = rr.summary;
  $("rr-export").href = `/api/engagements/${rr.engagement_id}/risks/export`;
  $("rr-cards").innerHTML = [[s.risks, "Risks"], [s.by_level.significant, "Significant risks"],
    [`${s.done}/${s.procedures}`, "Procedures done"], [s.without_procedures, "Risks with no procedures"]]
    .map(([v, k]) => `<div class="card${k === "Risks with no procedures" && v ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  if (!rr.risks.length) {
    $("rr-list").innerHTML = '<p class="muted">No risks yet. Add one above, or add the risks from an AI risk assessment.</p>';
    return;
  }
  $("rr-list").innerHTML = rr.risks.map((r, i) => {
    const procs = r.procedures.map((p) => {
      const removable = p.status !== "done" || me.rank >= 2;
      return `<tr data-proc="${p.id}">
        <td class="nowrap"><label class="check-label"><input type="checkbox" data-done ${p.status === "done" ? "checked" : ""}><span>Done</span></label></td>
        <td>${data(p.text)}</td>
        <td><input data-ref maxlength="200" value="${esc(p.reference)}" aria-label="Workpaper reference"></td>
        <td><textarea data-conclusion rows="2" maxlength="5000" aria-label="Conclusion">${esc(p.conclusion)}</textarea></td>
        <td class="muted">${p.done_by ? `${data(p.done_by)}<br>${data(when(p.done_at))}` : ""}</td>
        <td class="nowrap"><button type="button" class="small" data-proc-save>Save</button>
          ${removable ? '<button type="button" class="small secondary danger" data-proc-delete>Remove</button>' : ""}</td></tr>`;
    }).join("");
    return `<div class="risk-item" data-risk="${r.id}">
      <div class="row-between"><h3><bdi>R${i + 1}.</bdi> ${data(r.title)}</h3><span class="nowrap level-${esc(r.level)}">${esc(r.level_label)}</span></div>
      <div class="tag"><span>${esc(r.area_label)}</span>${r.assertion_labels.map((a) => ` · <span>${esc(a)}</span>`).join("")}${r.source ? " · <span>From the AI risk assessment</span>" : ""}</div>
      ${r.description ? `<p>${data(r.description)}</p>` : ""}
      ${r.response ? `<p><strong>Overall response</strong><br>${data(r.response)}</p>` : ""}
      ${procs ? `<div class="table-wrap"><table class="proc-table"><tr><th>Status</th><th>Procedure</th><th>Workpaper</th><th>Conclusion</th><th>Done by</th><th></th></tr>${procs}</table></div>`
        : '<p class="warn-box">No procedures planned for this risk yet.</p>'}
      <form class="proc-add" data-proc-add>
        <input data-proc-text maxlength="2000" required placeholder="Add a procedure" aria-label="New procedure">
        <button type="submit" class="small">Add procedure</button>
      </form>
      <div class="actions"><button type="button" class="small secondary" data-risk-edit>Edit risk</button>
        ${me.rank >= 2 ? '<button type="button" class="small secondary danger" data-risk-delete>Remove risk</button>' : ""}</div>
    </div>`;
  }).join("");
}

// After a change: reload the register, and the activity log and sign-offs it can affect.
async function risksChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadRisks();
  guard(loadControls);  // a control may name a risk that was changed or removed
  refreshDashboard();
  await loadEngagementActivity();
}

$("rr-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("rr-save"), async () => {
    const id = currentEngagement.id;
    const f = formObject($("rr-form"));
    const body = { area: f.area, level: f.level, title: f.title,
      assertions: [...$("rr-assertions").querySelectorAll("input:checked")].map((el) => el.value),
      description: f.description || "", response: f.response || "" };
    if (rrEditing) {
      await sendJSON("PATCH", `/api/risks/${rrEditing}`, body);
    } else {
      body.procedures = (f.procedures || "").split("\n").map((t) => t.trim()).filter(Boolean);
      await sendJSON("POST", `/api/engagements/${id}/risks`, body);
    }
    if (currentEngagement?.id !== id) return;
    resetRiskForm();
    $("rr-add").open = false;
    await risksChanged(id);
  });
});

$("rr-cancel").addEventListener("click", () => { resetRiskForm(); $("rr-add").open = false; });

$("rr-list").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-proc-add]");
  if (!form) return;
  ev.preventDefault();
  const riskId = form.closest("[data-risk]").dataset.risk;
  busy(form.querySelector("button"), async () => {
    const id = currentEngagement.id;
    await sendJSON("POST", `/api/risks/${riskId}/procedures`, { text: form.querySelector("[data-proc-text]").value.trim() });
    await risksChanged(id);
  });
});

$("rr-list").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-proc-save], button[data-proc-delete], button[data-risk-edit], button[data-risk-delete]");
  if (!btn) return;
  const id = currentEngagement.id;
  const riskId = Number(btn.closest("[data-risk]").dataset.risk);
  const risk = rr.risks.find((r) => r.id === riskId);
  const row = btn.closest("[data-proc]");
  if (btn.hasAttribute("data-proc-save")) {
    busy(btn, async () => {
      await sendJSON("PATCH", `/api/procedures/${row.dataset.proc}`, {
        status: row.querySelector("[data-done]").checked ? "done" : "planned",
        reference: row.querySelector("[data-ref]").value.trim(),
        conclusion: row.querySelector("[data-conclusion]").value.trim(),
      });
      await risksChanged(id);
    });
  } else if (btn.hasAttribute("data-proc-delete")) {
    if (!confirm("Remove this procedure?")) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/procedures/${row.dataset.proc}`);
      await risksChanged(id);
    });
  } else if (btn.hasAttribute("data-risk-delete")) {
    if (!confirm(`Remove the risk "${risk.title}" and its procedures?`)) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/risks/${riskId}`);
      if (rrEditing === riskId) resetRiskForm();
      await risksChanged(id);
    });
  } else if (risk) {  // edit: load the risk into the form above
    resetRiskForm();
    rrEditing = riskId;
    const form = $("rr-form");
    form.area.value = risk.area;
    form.level.value = risk.level;
    form.title.value = risk.title;
    form.description.value = risk.description;
    form.response.value = risk.response;
    $("rr-assertions").querySelectorAll("input").forEach((el) => { el.checked = risk.assertions.includes(el.value); });
    $("rr-form-title").textContent = "Change the risk";
    $("rr-save").textContent = "Save changes";
    $("rr-cancel").hidden = false;
    $("rr-procs-wrap").hidden = true;
    $("rr-add").open = true;
    $("rr-add").scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

$("ra-import").addEventListener("click", () => busy($("ra-import"), async () => {
  const id = currentEngagement.id;
  const out = await sendJSON("POST", `/api/engagements/${id}/risks/from-draft/${aiShown.risk}`);
  if (currentEngagement?.id !== id) return;
  $("ra-imported").textContent = out.skipped
    ? `Added ${out.added} risks. ${out.skipped} were already in the register.`
    : `Added ${out.added} risks to the register.`;
  await risksChanged(id);
}));

// Internal controls and tests of controls

let ct = null;          // the register on show: {engagement_id, summary, controls}
let ctOptions = null;   // areas, types, natures, frequencies, designs and methods, loaded once
let ctEditing = null;   // the control being changed in the form, or null when adding one
let ctLoads = 0;
const CT_RESULT_PILL = { effective: "good", not_effective: "bad", not_tested: "" };
const CT_DESIGN_PILL = { effective: "good", deficient: "bad", not_assessed: "" };
const optionList = (list, pick) => list.map((o) =>
  `<option value="${esc(o.id)}"${o.id === pick ? " selected" : ""}>${esc(o.label)}</option>`).join("");
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const intOrNull = (v) => v === "" || v == null ? null : Math.round(Number(v));
const numOrNull = (v) => v === "" || v == null ? null : Number(v);

async function loadControlOptions() {
  if (ctOptions) return;
  const o = await getJSON("/api/controls/options");
  $("ct-area").innerHTML = optionList(o.areas);
  $("ct-kind").innerHTML = optionList(o.kinds);
  $("ct-nature").innerHTML = optionList(o.natures);
  $("ct-frequency").innerHTML = optionList(o.frequencies, "monthly");
  ctOptions = o;
}

// The risks a control can address: this engagement's risk register.
function fillControlRisks(pick) {
  const keep = pick !== undefined ? String(pick ?? "") : $("ct-risk").value;
  const risks = rr && rr.engagement_id === currentEngagement?.id ? rr.risks : [];
  $("ct-risk").innerHTML = '<option value="">None</option>' + risks.map((r, i) =>
    `<option value="${r.id}" translate="no">R${i + 1}. ${esc(r.title)}</option>`).join("");
  $("ct-risk").value = risks.some((r) => String(r.id) === keep) ? keep : "";
}

async function loadControls() {
  const id = currentEngagement.id;
  const load = ++ctLoads;
  await loadControlOptions();
  const c = await getJSON(`/api/engagements/${id}/controls`);
  if (load !== ctLoads || currentEngagement?.id !== id) return;
  ct = { ...c, engagement_id: id };
  renderControls();
}

function resetControlForm() {
  ctEditing = null;
  $("ct-form").reset();
  $("ct-form-title").textContent = "Add a control";
  $("ct-save").textContent = "Add control";
  $("ct-cancel").hidden = true;
  fillControlRisks("");
}

function controlHtml(c, i) {
  const o = ctOptions;
  const statistical = c.method === "statistical";
  const freq = o.frequencies.find((f) => f.id === c.frequency);
  const suggested = c.nature === "automated" ? 1 : freq.sample_size;
  const methods = o.methods.filter((m) => c.nature !== "automated" || m.id === "frequency");
  const plan = c.method ? `<p><span>Sample size:</span> <bdi>${esc(c.sample_size)}</bdi> · <span>${esc(c.method_label)}</span>${statistical
      ? ` · <span>Confidence:</span> <bdi dir="ltr">${esc(c.confidence)}%</bdi> · <span>Tolerable rate:</span> <bdi dir="ltr">${esc(c.tolerable_rate)}%</bdi> · <span>Expected rate:</span> <bdi dir="ltr">${esc(c.expected_rate)}%</bdi>` : ""}${c.population
      ? ` · <span>Population:</span> <bdi>${esc(c.population)}</bdi>` : ""}</p>
    ${c.selected.length ? `<p class="ct-picked"><span>Items to test, by their number in the population:</span> <bdi dir="ltr" translate="no">${esc(c.selected.join(", "))}</bdi>
      <span class="muted"><span>Seed:</span> <bdi translate="no">${esc(c.seed)}</bdi></span></p>` : ""}` : "";
  const result = c.result !== "not_tested" ? `<p><span class="pill ${CT_RESULT_PILL[c.result]}">${esc(c.result_label)}</span>
      <span>Items tested:</span> <bdi>${esc(c.tested)}</bdi> · <span>Deviations:</span> <bdi>${esc(c.deviations)}</bdi>${c.upper_limit != null
      ? ` · <span>Upper deviation limit:</span> <bdi dir="ltr">${esc(c.upper_limit)}%</bdi>` : ""}
      <span class="muted">${data(c.tested_by)} · <span>${esc(when(c.tested_at))}</span></span></p>
    ${c.deviation_note ? `<p><strong>Deviations found</strong><br>${data(c.deviation_note)}</p>` : ""}
    ${c.test_conclusion ? `<p><strong>Conclusion</strong><br>${data(c.test_conclusion)}</p>` : ""}` : "";
  return `<div class="risk-item ct-item" data-control="${c.id}">
    <div class="row-between"><h3><bdi>C${i + 1}.</bdi> ${data(c.title)}</h3>
      <span class="nowrap"><span class="pill ${CT_DESIGN_PILL[c.design]}">${esc(c.design_label)}</span> <span class="pill ${CT_RESULT_PILL[c.result]}">${esc(c.result_label)}</span></span></div>
    <div class="tag">${c.key ? "<span>Key control</span> · " : ""}<span>${esc(c.area_label)}</span>${c.process ? ` · ${data(c.process)}` : ""}
      · <span>${esc(c.kind_label)}</span> · <span>${esc(c.nature_label)}</span> · <span>${esc(c.frequency_label)}</span>${c.owner ? ` · ${data(c.owner)}` : ""}${c.source ? " · <span>Carried from last year</span>" : ""}</div>
    ${c.description ? `<p>${data(c.description)}</p>` : ""}
    ${c.risk_title ? `<p><span class="muted">Risk addressed:</span> ${data(c.risk_title)}</p>` : ""}
    ${c.design_note || c.walkthrough ? `<p><span class="muted">Design:</span> ${data(c.design_note)}${c.walkthrough ? ` <span class="muted">(${data(c.walkthrough)})</span>` : ""}</p>` : ""}
    ${result}
    <details data-box="${c.id}:design"><summary>Design and implementation</summary>
      <form data-design>
        <div class="grid">
          <div><label>Assessment</label><select name="design" class="map">${optionList(o.designs, c.design)}</select></div>
          <div><label>Walkthrough workpaper</label><input name="walkthrough" maxlength="200" value="${esc(c.walkthrough)}"></div>
        </div>
        <label>How the design was assessed</label>
        <textarea name="design_note" rows="2" maxlength="5000">${esc(c.design_note)}</textarea>
        <div class="actions"><button type="submit" class="small">Save the design assessment</button></div>
      </form>
    </details>
    <details data-box="${c.id}:test"><summary>Test of operating effectiveness</summary>
      ${plan}
      <form data-plan>
        <div class="grid">
          <div><label>Sample size from</label><select name="method" class="map">${optionList(methods, c.method || "frequency")}</select></div>
          <div data-stat ${statistical ? "" : "hidden"}><label>Confidence</label><select name="confidence" class="map">${o.confidences.map((v) =>
            `<option value="${v}"${(c.confidence || 95) === v ? " selected" : ""}>${v}%</option>`).join("")}</select></div>
          <div data-stat ${statistical ? "" : "hidden"}><label>Tolerable deviation rate %</label><input name="tolerable_rate" type="number" step="any" min="0" max="50" value="${esc(c.tolerable_rate ?? 5)}"></div>
          <div data-stat ${statistical ? "" : "hidden"}><label>Expected deviation rate %</label><input name="expected_rate" type="number" step="any" min="0" max="50" value="${esc(c.expected_rate ?? 0)}"></div>
          <div><label>Population <span class="muted">(times it ran in the period)</span></label><input name="population" type="number" min="1" step="1" value="${esc(c.population ?? "")}"></div>
          <div><label>Sample size <span class="muted">(blank for the suggested size)</span></label><input name="sample_size" type="number" min="1" max="2000" step="1" data-suggested="${suggested}" placeholder="${statistical ? "" : suggested}"></div>
        </div>
        <div class="actions"><button type="submit" class="small">${c.method ? "Plan the test again" : "Plan the test"}</button>
          ${c.result !== "not_tested" ? '<span class="muted">Planning again clears the result.</span>' : ""}</div>
      </form>
      ${c.method ? `<form data-result>
        <div class="grid">
          <div><label>Items tested</label><input name="tested" type="number" min="1" max="2000" step="1" required value="${esc(c.tested ?? c.sample_size)}"></div>
          <div><label>Deviations found</label><input name="deviations" type="number" min="0" max="2000" step="1" required value="${esc(c.deviations ?? 0)}"></div>
          <div><label>Workpaper</label><input name="workpaper" maxlength="200" value="${esc(c.test_workpaper)}"></div>
        </div>
        <label>Deviations <span class="muted">(what went wrong, and why)</span></label>
        <textarea name="deviation_note" rows="2" maxlength="5000">${esc(c.deviation_note)}</textarea>
        <label>Conclusion</label>
        <textarea name="conclusion" rows="2" maxlength="5000">${esc(c.test_conclusion)}</textarea>
        <div class="actions"><button type="submit" class="small">Record the result</button></div>
      </form>` : ""}
    </details>
    <div class="actions"><button type="button" class="small secondary" data-control-edit>Edit control</button>
      ${me.rank >= 2 ? '<button type="button" class="small secondary danger" data-control-delete>Remove control</button>' : ""}</div>
  </div>`;
}

function renderControls() {
  if (!ct) { $("ct-cards").innerHTML = $("ct-list").innerHTML = ""; return; }
  fillControlRisks();
  const s = ct.summary;
  $("ct-export").href = `/api/engagements/${ct.engagement_id}/controls/export`;
  $("ct-cards").innerHTML = [
    [s.controls, "Controls", false], [s.key, "Key controls", false],
    [s.design.effective, "Designed and implemented", false],
    [s.tested, "Tested", false], [s.effective, "Operating effectively", false],
    [s.deficient, "Deficiencies", s.deficient > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  // Keep open whatever was open, so saving one part doesn't fold the control away.
  const open = new Set([...$("ct-list").querySelectorAll("details[open]")].map((d) => d.dataset.box));
  $("ct-list").innerHTML = ct.controls.length ? ct.controls.map(controlHtml).join("")
    : '<p class="muted">No controls yet. Add the controls the client relies on in each area that matters to the audit.</p>';
  for (const d of $("ct-list").querySelectorAll("details[data-box]")) d.open = open.has(d.dataset.box);
}

async function controlsChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadControls();
  refreshDashboard();
  await loadEngagementActivity();
}

$("ct-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("ct-save"), async () => {
    const id = currentEngagement.id;
    const f = formObject($("ct-form"));
    const body = { area: f.area, title: f.title, process: f.process || "", description: f.description || "",
      risk_id: f.risk_id ? Number(f.risk_id) : null, kind: f.kind, nature: f.nature,
      frequency: f.frequency, key: $("ct-key").checked, owner: f.owner || "" };
    if (ctEditing) await sendJSON("PATCH", `/api/controls/${ctEditing}`, body);
    else await sendJSON("POST", `/api/engagements/${id}/controls`, body);
    if (currentEngagement?.id !== id) return;
    resetControlForm();
    $("ct-add").open = false;
    await controlsChanged(id);
  });
});

$("ct-cancel").addEventListener("click", () => { resetControlForm(); $("ct-add").open = false; });

$("ct-calc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy(ev.submitter || $("ct-calc-form").querySelector("button"), async () => {
    $("ct-calc-out").textContent = "";
    const q = new URLSearchParams({ confidence: $("ct-calc-conf").value,
      tolerable: $("ct-calc-tol").value || "0", expected: $("ct-calc-exp").value || "0" });
    const r = await getJSON(`/api/controls/sample-size?${q}`);
    $("ct-calc-out").textContent = `Test ${r.sample_size} items. If no more than ${r.allowed_deviations} deviations are found, the deviation rate is below the tolerable rate at this confidence.`;
  });
});

$("ct-list").addEventListener("change", (ev) => {
  if (ev.target.name !== "method") return;
  const form = ev.target.closest("form");
  const statistical = ev.target.value === "statistical";
  form.querySelectorAll("[data-stat]").forEach((el) => { el.hidden = !statistical; });
  const size = form.elements.sample_size;
  size.placeholder = statistical ? "" : size.dataset.suggested;
});

$("ct-list").addEventListener("submit", (ev) => {
  const form = ev.target.closest("form");
  if (!form) return;
  ev.preventDefault();
  const id = currentEngagement.id;
  const controlId = form.closest("[data-control]").dataset.control;
  const f = formObject(form);
  busy(form.querySelector("button[type=submit]"), async () => {
    if (form.hasAttribute("data-design")) {
      await sendJSON("PATCH", `/api/controls/${controlId}`,
        { design: f.design, walkthrough: f.walkthrough || "", design_note: f.design_note || "" });
    } else if (form.hasAttribute("data-plan")) {
      const statistical = f.method === "statistical";
      await sendJSON("POST", `/api/controls/${controlId}/plan`, {
        method: f.method, population: intOrNull(f.population), sample_size: intOrNull(f.sample_size),
        ...(statistical ? { confidence: Number(f.confidence), tolerable_rate: numOrNull(f.tolerable_rate),
          expected_rate: numOrNull(f.expected_rate) ?? 0 } : {}) });
    } else if (form.hasAttribute("data-result")) {
      await sendJSON("POST", `/api/controls/${controlId}/result`, {
        tested: intOrNull(f.tested), deviations: intOrNull(f.deviations), workpaper: f.workpaper || "",
        deviation_note: f.deviation_note || "", conclusion: f.conclusion || "" });
    }
    await controlsChanged(id);
  });
});

$("ct-list").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-control-edit], button[data-control-delete]");
  if (!btn) return;
  const id = currentEngagement.id;
  const controlId = Number(btn.closest("[data-control]").dataset.control);
  const c = ct.controls.find((x) => x.id === controlId);
  if (!c) return;
  if (btn.hasAttribute("data-control-delete")) {
    if (!confirm(`Remove the control "${c.title}" and its test?`)) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/controls/${controlId}`);
      if (ctEditing === controlId) resetControlForm();
      await controlsChanged(id);
    });
    return;
  }
  resetControlForm();
  ctEditing = controlId;
  const form = $("ct-form");
  for (const k of ["area", "title", "process", "description", "kind", "nature", "frequency", "owner"]) form[k].value = c[k];
  fillControlRisks(c.risk_id ?? "");
  $("ct-key").checked = c.key;
  $("ct-form-title").textContent = "Change the control";
  $("ct-save").textContent = "Save changes";
  $("ct-cancel").hidden = false;
  $("ct-add").open = true;
  $("ct-add").scrollIntoView({ behavior: "smooth", block: "start" });
});

// External confirmations

let cf = null;          // the list on show: {engagement_id, summary, confirmations, ...}
let cfOptions = null;   // kinds, forms and statuses, loaded once
let cfEditing = null;   // the request whose details are open for changing
let cfLoads = 0;
const CF_PILL = { agreed: "good", explained: "good", alternative_done: "good", negative_no_reply: "good",
  to_send: "", waiting: "", overdue: "bad", difference: "bad", alternative_needed: "warn" };

async function loadConfirmationOptions() {
  if (cfOptions) return;
  const o = await getJSON("/api/confirmations/options");
  $("cf-kind").innerHTML = optionList(o.kinds);
  $("cf-form-type").innerHTML = optionList(o.forms, "positive");
  cfOptions = o;
}

async function loadConfirmations() {
  const id = currentEngagement.id;
  const load = ++cfLoads;
  await loadConfirmationOptions();
  const c = await getJSON(`/api/engagements/${id}/confirmations`);
  if (load !== cfLoads || currentEngagement?.id !== id) return;
  cf = { ...c, engagement_id: id };
  renderConfirmations();
}

function renderConfirmationListing(e) {
  const keep = $("cf-listing").value;
  const files = e.uploads.filter((u) => u.kind === "ar" || u.kind === "ap");
  $("cf-listing").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a receivables or payables listing first</option>';
  if (files.some((u) => String(u.id) === keep)) $("cf-listing").value = keep;
}

function confirmationEditRow(c) {
  const o = cfOptions;
  return `<tr class="cf-edit" data-cf-edit="${c.id}"><td colspan="9"><form data-cf-form>
    <div class="grid">
      <div><label>Status</label><select name="status" class="map">${optionList(o.statuses, c.status)}</select></div>
      <div><label>Sent on</label><input name="sent_on" type="date" value="${esc(c.sent_on || "")}"></div>
      <div><label>Reminder sent on</label><input name="reminder_on" type="date" value="${esc(c.reminder_on || "")}"></div>
      <div><label>Reply received on</label><input name="received_on" type="date" value="${esc(c.received_on || "")}"></div>
      <div><label>Balance per books</label><input name="balance" type="number" step="any" value="${esc(c.balance ?? "")}"></div>
      <div><label>Balance the reply confirms</label><input name="confirmed" type="number" step="any" value="${esc(c.confirmed ?? "")}"></div>
      <div><label>Name</label><input name="party" maxlength="300" required value="${esc(c.party)}"></div>
      <div><label>Account or customer code</label><input name="reference" maxlength="200" value="${esc(c.reference)}"></div>
      <div><label>Workpaper</label><input name="workpaper" maxlength="200" value="${esc(c.workpaper)}"></div>
    </div>
    <label>Contact</label><textarea name="contact" rows="2" maxlength="5000">${esc(c.contact)}</textarea>
    <label>Explanation of the difference</label><textarea name="difference_note" rows="2" maxlength="5000">${esc(c.difference_note)}</textarea>
    <label>Alternative procedures <span class="muted">(when there's no reply)</span></label><textarea name="alternative" rows="2" maxlength="5000">${esc(c.alternative)}</textarea>
    <label>Conclusion</label><textarea name="conclusion" rows="2" maxlength="5000">${esc(c.conclusion)}</textarea>
    <div class="actions"><button type="submit" class="small">Save</button>
      <button type="button" class="small secondary" data-cf-close>Close</button>
      ${c.status === "to_send" || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-cf-delete>Remove request</button>' : ""}</div>
  </form></td></tr>`;
}

function renderConfirmations() {
  if (!cf) { $("cf-cards").innerHTML = $("cf-coverage").innerHTML = $("cf-table").innerHTML = ""; return; }
  const s = cf.summary;
  $("cf-export").href = `/api/engagements/${cf.engagement_id}/confirmations/export`;
  $("cf-cards").innerHTML = [
    [s.requests, "Requests", false], [s.sent, "Sent", false], [s.replied, "Replied", false],
    [s.response_rate == null ? "–" : `${s.response_rate}%`, "Response rate", false],
    [s.open, "Still open", false], [s.outcomes.difference, "Differences to explain", s.outcomes.difference > 0],
    [s.outcomes.overdue, "Overdue replies", s.outcomes.overdue > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("cf-coverage").innerHTML = s.by_kind.length ? `<thead><tr><th>Sent to</th><th class="num">Requests</th><th class="num">Replied</th>
      <th class="num">Balance asked about</th><th class="num">Balance covered</th><th class="num">Covered %</th></tr></thead><tbody>
    ${s.by_kind.map((k) => `<tr><td>${esc(k.label)}</td><td class="num">${k.requests}</td><td class="num">${k.replied}</td>
      <td class="num">${money(k.balance)}</td><td class="num">${money(k.covered)}</td>
      <td class="num"><bdi dir="ltr">${k.covered_pct == null ? "" : `${k.covered_pct}%`}</bdi></td></tr>`).join("")}</tbody>` : "";
  const filter = $("cf-filter").value;
  const rows = cf.confirmations.map((c, i) => ({ c, ref: `K${i + 1}` }))
    .filter(({ c }) => !filter || (filter === "open") === c.open);
  if (!cf.confirmations.length) {
    $("cf-table").innerHTML = '<tbody><tr><td class="muted">No requests yet. Add one above, or pick them from a receivables or payables listing.</td></tr></tbody>';
    return;
  }
  $("cf-table").innerHTML = `<thead><tr><th>Ref</th><th>Sent to</th><th class="num">Balance per books</th><th>Status</th>
      <th>Sent</th><th>Reply</th><th class="num">Confirmed</th><th class="num">Difference</th><th></th></tr></thead><tbody>
    ${rows.map(({ c, ref }) => `<tr data-cf="${c.id}">
      <td><bdi>${ref}</bdi></td>
      <td>${data(c.party)}<div class="tag"><span>${esc(c.kind_label)}</span>${c.reference ? ` · ${data(c.reference)}` : ""} · <span>${esc(c.form_label)}</span></div></td>
      <td class="num">${money(c.balance)}</td>
      <td><span class="pill ${CF_PILL[c.outcome]}">${esc(c.outcome_label)}</span></td>
      <td class="nowrap">${esc(c.sent_on || "")}${c.reminder_on ? `<div class="tag"><span>Reminder:</span> ${esc(c.reminder_on)}</div>` : ""}</td>
      <td class="nowrap">${esc(c.received_on || "")}</td>
      <td class="num">${money(c.confirmed)}</td>
      <td class="num${c.outcome === "difference" ? " bad" : ""}">${money(c.difference)}</td>
      <td class="nowrap">${c.status === "to_send" ? '<button type="button" class="small" data-cf-sent>Sent today</button>'
        : c.status === "sent" ? '<button type="button" class="small secondary" data-cf-remind>Reminder sent today</button>' : ""}
        <button type="button" class="small secondary" data-cf-open>${cfEditing === c.id ? "Close" : "Update"}</button></td></tr>
      ${cfEditing === c.id ? confirmationEditRow(c) : ""}`).join("") || '<tr><td colspan="9" class="muted">Nothing to show with this filter.</td></tr>'}</tbody>`;
}

async function confirmationsChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadConfirmations();
  refreshDashboard();
  await loadEngagementActivity();
}

$("cf-filter").addEventListener("change", renderConfirmations);

$("cf-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("cf-save"), async () => {
    const id = currentEngagement.id;
    const f = formObject($("cf-form"));
    await sendJSON("POST", `/api/engagements/${id}/confirmations`, { kind: f.kind, party: f.party,
      reference: f.reference || "", form: f.form, balance: numOrNull(f.balance), contact: f.contact || "" });
    if (currentEngagement?.id !== id) return;
    $("cf-form").reset();
    await confirmationsChanged(id);
  });
});

$("cf-pick-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("cf-pick-btn"), async () => {
    const id = currentEngagement.id;
    $("cf-picked").textContent = "";
    const out = await sendJSON("POST", `/api/engagements/${id}/confirmations/from-listing`, {
      upload_id: Number($("cf-listing").value), min_balance: numOrNull($("cf-min").value),
      largest: intOrNull($("cf-largest").value) });
    if (currentEngagement?.id !== id) return;
    $("cf-picked").textContent = out.skipped
      ? `Added ${out.added} requests. ${out.skipped} were already on the list.`
      : `Added ${out.added} requests.`;
    await confirmationsChanged(id);
  });
});

$("cf-table").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-cf-sent], button[data-cf-remind], button[data-cf-open], button[data-cf-close], button[data-cf-delete]");
  if (!btn) return;
  const id = currentEngagement.id;
  const confId = Number(btn.closest("[data-cf], [data-cf-edit]").dataset.cf || btn.closest("[data-cf-edit]").dataset.cfEdit);
  const c = cf.confirmations.find((x) => x.id === confId);
  if (!c) return;
  if (btn.hasAttribute("data-cf-open") || btn.hasAttribute("data-cf-close")) {
    cfEditing = cfEditing === confId ? null : confId;
    renderConfirmations();
  } else if (btn.hasAttribute("data-cf-sent")) {
    busy(btn, async () => {
      await sendJSON("PATCH", `/api/confirmations/${confId}`, { status: "sent", sent_on: today() });
      await confirmationsChanged(id);
    });
  } else if (btn.hasAttribute("data-cf-remind")) {
    busy(btn, async () => {
      await sendJSON("PATCH", `/api/confirmations/${confId}`, { reminder_on: today() });
      await confirmationsChanged(id);
    });
  } else if (btn.hasAttribute("data-cf-delete")) {
    if (!confirm(`Remove the confirmation request to ${c.party}?`)) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/confirmations/${confId}`);
      cfEditing = null;
      await confirmationsChanged(id);
    });
  }
});

$("cf-table").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-cf-form]");
  if (!form) return;
  ev.preventDefault();
  const id = currentEngagement.id;
  const confId = Number(form.closest("[data-cf-edit]").dataset.cfEdit);
  const f = formObject(form);
  busy(form.querySelector("button[type=submit]"), async () => {
    await sendJSON("PATCH", `/api/confirmations/${confId}`, {
      status: f.status, sent_on: f.sent_on || null, reminder_on: f.reminder_on || null,
      received_on: f.received_on || null, balance: numOrNull(f.balance), confirmed: numOrNull(f.confirmed),
      party: f.party, reference: f.reference || "", workpaper: f.workpaper || "", contact: f.contact || "",
      difference_note: f.difference_note || "", alternative: f.alternative || "", conclusion: f.conclusion || "" });
    cfEditing = null;
    await confirmationsChanged(id);
  });
});

// Analytical procedures and proofs in total

let ap = null;          // the list on show: {engagement_id, expectations, summary, problem, ...}
let apOptions = null;   // types, methods, lines and the presets of each proof, loaded once
let apEditing = null;   // the expectation whose details are open for changing
let apLoads = 0;
const AP_PILL = { within: "good", explained: "good", misstatement: "bad", to_explain: "bad",
  no_actual: "warn", no_expectation: "warn", no_threshold: "warn" };
// The inputs each way of building an expectation uses.
const AP_FIELDS = {
  prior_change: [["base", "Starting figure (blank for last year's balance)", "number"], ["change_pct", "Change expected %", "number"]],
  rate: [["base_label", "What the base is", "text"], ["base", "Base", "number"], ["rate_pct", "Rate %", "number"], ["months", "Months", "number"]],
  units: [["units_label", "What is counted", "text"], ["units", "Units", "number"], ["price_label", "What the price is", "text"], ["price", "Price per unit", "number"], ["periods", "Times (for example 12 months)", "number"]],
  amount: [["amount", "Expected amount", "number"]],
};

async function loadExpectationOptions() {
  if (apOptions) return;
  const o = await getJSON("/api/expectations/options");
  $("ap-kind").innerHTML = optionList(o.kinds, "analytic");
  $("ap-line").innerHTML = optionList(o.lines, "revenue");
  $("ap-method").innerHTML = optionList(o.methods, "prior_change");
  apOptions = o;
}

async function loadExpectations() {
  const id = currentEngagement.id;
  const load = ++apLoads;
  await loadExpectationOptions();
  const x = await getJSON(`/api/engagements/${id}/expectations`);
  if (load !== apLoads || currentEngagement?.id !== id) return;
  ap = { ...x, engagement_id: id };
  renderExpectations();
}

// How the expected figure is reached, as a sum (so it reads the same in either language).
function apHow(x) {
  const i = x.inputs, n = (v) => `<bdi dir="ltr">${esc(Number(v).toLocaleString(LOCALE, { maximumFractionDigits: 4 }))}</bdi>`;
  if (x.expected == null) return "";
  if (x.method === "prior_change") return `<bdi dir="ltr">${money(x.base_used)} × (1 + ${esc(i.change_pct)}%)</bdi>`;
  if (x.method === "rate") return `<bdi dir="ltr">${money(i.base)} × ${esc(i.rate_pct)}% × ${esc(i.months || 12)}/12</bdi>`;
  if (x.method === "units") return `${n(i.units)} × <bdi dir="ltr">${money(i.price)}</bdi>${(i.periods || 1) !== 1 ? ` × ${n(i.periods)}` : ""}`;
  return "";
}

function apInputs(method, inputs) {
  return AP_FIELDS[method].map(([k, label, type]) => {
    const v = inputs[k] ?? "";
    // A proof's own name for its base or units, if it has one, labels the figure.
    const shown = k === "base" && inputs.base_label ? inputs.base_label : k === "units" && inputs.units_label
      ? inputs.units_label : k === "price" && inputs.price_label ? inputs.price_label : label;
    return `<div><label>${esc(shown)}</label>${type === "number"
      ? `<input name="in_${k}" type="number" step="any" value="${esc(v)}">`
      : `<input name="in_${k}" maxlength="200" value="${esc(v)}">`}</div>`;
  }).join("");
}

function expectationEditRow(x) {
  const o = apOptions;
  return `<tr class="cf-edit" data-ap-edit="${x.id}"><td colspan="9"><form data-ap-form>
    <div class="grid">
      <div><label>Title <span class="muted">(optional)</span></label><input name="title" maxlength="200" value="${esc(x.own_title)}"></div>
      <div><label>Statement line</label><select name="line" class="map"><option value="">None</option>${optionList(o.lines, x.line)}</select></div>
      <div><label>Built from</label><select name="method" class="map" data-ap-method>${optionList(o.methods, x.method)}</select></div>
      <div><label>Threshold <span class="muted">(blank for performance materiality)</span></label><input name="threshold" type="number" step="any" min="0" value="${esc(x.threshold_set ? x.threshold : "")}"></div>
    </div>
    <div class="grid" data-ap-inputs>${apInputs(x.method, x.inputs)}</div>
    <label>Where the inputs come from <span class="muted">(and how they were checked)</span></label><textarea name="basis" rows="2" maxlength="5000">${esc(x.basis)}</textarea>
    <div class="grid">
      <div><label>Actual figure <span class="muted">(blank for the line's adjusted balance)</span></label><input name="actual_override" type="number" step="any" value="${esc(x.actual_override ?? "")}"></div>
      <div><label>Where the actual figure comes from</label><input name="actual_note" maxlength="5000" value="${esc(x.actual_note)}"></div>
    </div>
    <label>Explanation of the difference</label><textarea name="explanation" rows="2" maxlength="5000">${esc(x.explanation)}</textarea>
    <label class="check-label" style="margin-top:8px"><input type="checkbox" name="misstatement" ${x.misstatement ? "checked" : ""}> <span>Treat the difference as a misstatement</span></label>
    <label>Conclusion</label><textarea name="conclusion" rows="2" maxlength="5000">${esc(x.conclusion)}</textarea>
    <div class="actions"><button type="submit" class="small">Save</button>
      <button type="button" class="small secondary" data-ap-close>Close</button>
      ${me.rank >= 2 ? (x.reviewed_at ? '<button type="button" class="small secondary" data-ap-unreview>Take back the sign-off</button>'
        : '<button type="button" class="small" data-ap-review>Sign off as reviewed</button>') : ""}
      ${!x.reviewed_at || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-ap-delete>Remove</button>' : ""}</div>
  </form></td></tr>`;
}

function renderExpectations() {
  if (!ap) { $("ap-cards").innerHTML = $("ap-table").innerHTML = ""; $("ap-problem").hidden = true; return; }
  const s = ap.summary, out = s.outcomes;
  $("ap-export").href = `/api/engagements/${ap.engagement_id}/expectations/export`;
  $("ap-problem").hidden = !(ap.problem && ap.expectations.length);
  $("ap-problem").textContent = ap.problem || "";
  $("ap-cards").innerHTML = [
    [s.expectations, "Expectations and proofs", false], [out.within + out.explained, "Within the threshold or explained", false],
    [out.to_explain, "Differences to explain", out.to_explain > 0], [money(s.misstatements), "Misstatements found", out.misstatement > 0],
    [s.not_reviewed, "Not reviewed", false],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  if (!ap.expectations.length) {
    $("ap-table").innerHTML = '<tbody><tr><td class="muted">None yet. Add an expectation for a statement line, or a proof of depreciation, payroll or interest.</td></tr></tbody>';
    return;
  }
  $("ap-table").innerHTML = `<thead><tr><th>Ref</th><th>Expectation</th><th class="num">Expected</th><th class="num">Actual</th>
      <th class="num">Difference</th><th class="num">Threshold</th><th>Outcome</th><th>Reviewed</th><th></th></tr></thead><tbody>
    ${ap.expectations.map((x, i) => `<tr data-ap="${x.id}">
      <td><bdi>AP${i + 1}</bdi></td>
      <td>${x.title === x.kind_label || x.title === x.line_label ? esc(x.title) : data(x.title)}<div class="tag">${[x.kind_label, x.line_label].filter((t) => t && t !== x.title).map((t) => `<span>${esc(t)}</span>`).join(" · ")}</div></td>
      <td class="num">${money(x.expected)}<div class="tag">${apHow(x)}</div></td>
      <td class="num">${money(x.actual)}${x.actual_override != null ? '<div class="tag">Entered by hand</div>' : ""}</td>
      <td class="num${x.outcome === "to_explain" || x.outcome === "misstatement" ? " bad" : ""}">${money(x.difference)}${x.difference_pct != null ? `<div class="tag"><bdi dir="ltr">${esc(x.difference_pct)}%</bdi></div>` : ""}</td>
      <td class="num">${money(x.threshold)}</td>
      <td><span class="pill ${AP_PILL[x.outcome]}">${esc(x.outcome_label)}</span></td>
      <td>${x.reviewed_at ? `${data(x.reviewed_by)}${x.review_stale ? ' <span class="pill warn">Changed since</span>' : ""}` : ""}</td>
      <td><button type="button" class="small secondary" data-ap-open>${apEditing === x.id ? "Close" : "Update"}</button></td></tr>
      ${apEditing === x.id ? expectationEditRow(x) : ""}`).join("")}</tbody>`;
}

async function expectationsChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadExpectations();
  refreshDashboard();
  await loadEngagementActivity();
}

// Picking a proof starts it on its usual line and method.
$("ap-kind").addEventListener("change", () => {
  const p = apOptions?.presets[$("ap-kind").value];
  if (!p) return;
  $("ap-line").value = p.line;
  $("ap-method").value = p.method;
});

$("ap-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("ap-save"), async () => {
    const id = currentEngagement.id;
    const out = await sendJSON("POST", `/api/engagements/${id}/expectations`, { kind: $("ap-kind").value,
      line: $("ap-line").value || null, method: $("ap-method").value, title: $("ap-title").value.trim() });
    if (currentEngagement?.id !== id) return;
    $("ap-form").reset();
    $("ap-kind").value = "analytic"; $("ap-line").value = "revenue"; $("ap-method").value = "prior_change";
    apEditing = out.expectations[out.expectations.length - 1]?.id ?? null;  // straight on to its inputs
    await expectationsChanged(id);
  });
});

$("ap-table").addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-ap-method]");
  if (!sel) return;
  const x = ap.expectations.find((e) => e.id === Number(sel.closest("[data-ap-edit]").dataset.apEdit));
  sel.closest("form").querySelector("[data-ap-inputs]").innerHTML = apInputs(sel.value, x ? x.inputs : {});
});

$("ap-table").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-ap-open], button[data-ap-close], button[data-ap-delete], button[data-ap-review], button[data-ap-unreview]");
  if (!btn) return;
  const id = currentEngagement.id;
  const row = btn.closest("[data-ap], [data-ap-edit]");
  const xid = Number(row.dataset.ap || row.dataset.apEdit);
  const x = ap.expectations.find((e) => e.id === xid);
  if (!x) return;
  if (btn.hasAttribute("data-ap-open") || btn.hasAttribute("data-ap-close")) {
    apEditing = apEditing === xid ? null : xid;
    renderExpectations();
  } else if (btn.hasAttribute("data-ap-delete")) {
    if (!confirm(`Remove the expectation ${x.title}?`)) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/expectations/${xid}`);
      apEditing = null;
      await expectationsChanged(id);
    });
  } else {
    const reviewed = btn.hasAttribute("data-ap-review");
    busy(btn, async () => {
      await sendJSON("PATCH", `/api/expectations/${xid}`, { reviewed });
      await expectationsChanged(id);
    });
  }
});

$("ap-table").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-ap-form]");
  if (!form) return;
  ev.preventDefault();
  const id = currentEngagement.id;
  const xid = Number(form.closest("[data-ap-edit]").dataset.apEdit);
  const f = formObject(form);
  const inputs = {};
  for (const [k, , type] of AP_FIELDS[f.method]) {
    const v = f[`in_${k}`] ?? "";
    inputs[k] = type === "number" ? numOrNull(v) : v;
  }
  busy(form.querySelector("button[type=submit]"), async () => {
    await sendJSON("PATCH", `/api/expectations/${xid}`, {
      title: f.title || "", line: f.line || null, method: f.method, inputs, basis: f.basis || "",
      threshold: numOrNull(f.threshold), actual_override: numOrNull(f.actual_override),
      actual_note: f.actual_note || "", explanation: f.explanation || "",
      misstatement: form.querySelector("[name=misstatement]").checked, conclusion: f.conclusion || "" });
    apEditing = null;
    await expectationsChanged(id);
  });
});

// Registers and listings whose columns the auditor picks (fixed assets, inventory)

// A picker for each field, holding the column used (or none).
function columnPickers(fields, fileColumns, picked) {
  return fields.map((f) => `<div><label>${esc(f.label)}</label><select class="map" data-field="${esc(f.id)}">
    <option value="">(not in file)</option>${fileColumns.map((c) =>
      `<option value="${esc(c)}" ${picked[f.id] === c ? "selected" : ""} translate="no">${esc(c)}</option>`).join("")}</select></div>`).join("");
}

// The columns picked, or {} before any picker is shown (the server then guesses them).
function columnsPicked(root) {
  const out = {};
  root.querySelectorAll("[data-field]").forEach((sel) => { if (sel.value) out[sel.dataset.field] = sel.value; });
  return out;
}

const date = (d) => d ? `<bdi dir="ltr">${esc(d)}</bdi>` : "";
const qty = (n) => n == null ? "" : `<bdi dir="ltr">${esc(Number(n).toLocaleString(LOCALE, { maximumFractionDigits: 4 }))}</bdi>`;

// Fixed asset register

let fa = null;         // the analysis on show
let faTab = "categories";

function renderFixedAssetForm(e) {
  const keep = $("fa-upload").value;
  const files = e.uploads.filter((u) => u.kind === "fixed_assets");
  $("fa-upload").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)}</option>`)
    .join("") || '<option value="">Upload a fixed asset register first</option>';
  if (files.some((u) => String(u.id) === keep)) $("fa-upload").value = keep;
  if (fa && String(fa.upload_id) !== $("fa-upload").value) resetFixedAssets();
}

function resetFixedAssets() {
  fa = null;
  $("fa-columns").innerHTML = "";
  $("fa-columns-note").hidden = $("fa-problem").hidden = true;
  $("fa-result").hidden = $("fa-export").hidden = true;
}

$("fa-upload").addEventListener("change", resetFixedAssets);

$("fa-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("fa-run"), async () => {
    const id = Number($("fa-upload").value);
    if (!id) throw new Error("Upload a fixed asset register first.");
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/fixed-assets`, { columns: columnsPicked($("fa-columns")) });
    if (currentEngagement?.id !== engagementId) return;
    fa = out;
    $("fa-columns").innerHTML = columnPickers(out.fields, out.file_columns, out.columns);
    $("fa-columns-note").hidden = false;
    $("fa-problem").hidden = !out.problem;
    $("fa-problem").textContent = out.problem || "";
    $("fa-export").hidden = !!out.problem;
    renderFixedAssets();
  });
});

$("fa-export").addEventListener("click", () => {
  busy($("fa-export"), async () => {
    const res = await request(`/api/uploads/${fa.upload_id}/fixed-assets/export`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ columns: fa.columns }) });
    await saveFile(res, "fixed-assets.xlsx");
  });
});

$("fa-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-fa]");
  if (!tab) return;
  faTab = tab.dataset.fa;
  renderFixedAssets();
});

function assetRows(items, extra = false) {
  return `<tr><th>Row</th><th>Asset</th><th>Category</th><th>Acquired</th><th>Disposed</th><th class="num">Cost</th>
    <th class="num">Accumulated depreciation</th><th class="num">Net book value</th><th class="num">Depreciation this year</th>
    ${extra ? '<th class="num">Recomputed</th><th class="num">Difference</th>' : ""}<th>Points to look at</th></tr>` +
    items.map((x) => `<tr><td class="num">${x.row}</td><td>${data(x.asset)}<div class="tag">${data(x.description)}</div></td>
      <td>${x.category === "(none)" ? esc(x.category) : data(x.category)}</td><td class="nowrap">${date(x.acquired)}</td><td class="nowrap">${date(x.disposed)}</td>
      <td class="num">${money(x.cost)}</td><td class="num">${money(x.accumulated)}</td><td class="num">${money(x.nbv)}</td>
      <td class="num">${money(x.charge)}</td>
      ${extra ? `<td class="num">${money(x.expected)}</td><td class="num bad">${money(x.difference)}</td>` : ""}
      <td>${x.issues.map((i) => `<div>${esc(i)}</div>`).join("")}</td></tr>`).join("");
}

function renderFixedAssets() {
  $("fa-result").hidden = !fa || !!fa.problem;
  if (!fa || fa.problem) return;
  $("fa-tabs").querySelectorAll("[data-fa]").forEach((b) => b.setAttribute("aria-selected", b.dataset.fa === faTab));
  const t = fa.totals, r = fa.recomputed;
  const tieOff = fa.tie.some((x) => x.difference);
  $("fa-cards").innerHTML = [
    [t.assets.toLocaleString(LOCALE), "Assets held at the period end", false], [money(t.cost), "Cost", false],
    [money(t.nbv), "Net book value", false], [money(t.charge), "Depreciation this year", false],
    [r.differences.toLocaleString(LOCALE), "Depreciation differences", r.differences > 0],
    [fa.issues.count.toLocaleString(LOCALE), "Assets with points to look at", fa.issues.count > 0],
    ...(fa.tie.length ? [[tieOff ? "Differs" : "Agrees", "Register and trial balance", tieOff]] : []),
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  let note = [], html = "";
  if (faTab === "categories") {
    html = `<tr><th>Category</th><th class="num">Assets</th><th class="num">Cost</th><th class="num">Accumulated depreciation</th>
      <th class="num">Net book value</th><th class="num">Depreciation this year</th></tr>` +
      fa.categories.map((c) => `<tr><td>${c.category === "(none)" ? esc(c.category) : data(c.category)}</td><td class="num">${c.assets}</td>
        <td class="num">${money(c.cost)}</td><td class="num">${money(c.accumulated)}</td><td class="num">${money(c.nbv)}</td>
        <td class="num">${money(c.charge)}</td></tr>`).join("") +
      `<tr class="total"><td>Total</td><td class="num">${t.assets}</td><td class="num">${money(t.cost)}</td><td class="num">${money(t.accumulated)}</td>
        <td class="num">${money(t.nbv)}</td><td class="num">${money(t.charge)}</td></tr>`;
    note = ["Assets disposed of in the year are left out of the totals; their charge for the year is kept."];
  } else if (faTab === "tie") {
    if (!fa.tie.length) note = ["Upload this year's trial balance and map it to tie the register to it."];
    else html = '<tr><th>Line</th><th class="num">Register</th><th class="num">Trial balance</th><th class="num">Difference</th></tr>' +
      fa.tie.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${money(x.register)}</td>
        <td class="num">${x.trial_balance == null ? esc("Not mapped") : money(x.trial_balance)}</td>
        <td class="num${x.difference ? " bad" : ""}">${money(x.difference)}</td></tr>`).join("");
  } else if (faTab === "recomputed") {
    note = [`Recomputed on the straight line for ${r.checked} assets, for the part of the year each was held.`,
      "Differences over 1% are listed."];
    if (r.items.length) html = assetRows(r.items, true);
  } else {
    const part = fa[faTab];
    if (faTab === "additions") note = [`${part.count} additions costing ${money(part.cost)}.`];
    if (faTab === "disposals") note = [`${part.count} disposals costing ${money(part.cost)}.`];
    if (faTab === "issues" && !part.count) note = ["Nothing odd found."];
    if (part.items.length) html = assetRows(part.items);
    if (part.items.length < part.count) note.push(`Showing the first ${part.items.length}.`, "The Excel download has them all.");
  }
  $("fa-note").innerHTML = [].concat(note).map((n) => `<span>${esc(n)}</span>`).join(" ");
  $("fa-table").innerHTML = html;
}

// Inventory count

let sc = null;         // {engagement_id, count, items, summary}
let scListing = null;  // the listing as last looked at, before picking
let scEditing = null;  // the item whose note and explanation are open
let scLoads = 0;
const SC_PILL = { to_count: "warn", agreed: "good", explained: "good", difference: "bad", not_listed: "bad" };

async function loadStockCount() {
  const id = currentEngagement.id;
  const load = ++scLoads;
  const out = await getJSON(`/api/engagements/${id}/stock-count`);
  if (load !== scLoads || currentEngagement?.id !== id) return;
  sc = { ...out, engagement_id: id };
  renderStockCount();
}

function renderStockForm(e) {
  const keep = $("sc-upload").value;
  const files = e.uploads.filter((u) => u.kind === "inventory");
  $("sc-upload").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)}</option>`)
    .join("") || '<option value="">Upload an inventory listing first</option>';
  if (files.some((u) => String(u.id) === keep)) $("sc-upload").value = keep;
  if (scListing && String(scListing.upload_id) !== $("sc-upload").value) resetStockListing();
}

function resetStockListing() {
  scListing = null;
  $("sc-columns").innerHTML = "";
  $("sc-columns-note").hidden = $("sc-problem").hidden = $("sc-listing").hidden = true;
}

$("sc-upload").addEventListener("change", resetStockListing);

$("sc-look").addEventListener("click", () => {
  busy($("sc-look"), async () => {
    const id = Number($("sc-upload").value);
    if (!id) throw new Error("Upload an inventory listing first.");
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/inventory`, { upload_id: id, columns: columnsPicked($("sc-columns")) });
    if (currentEngagement?.id !== engagementId) return;
    scListing = out;
    renderStockListing();
  });
});

function renderStockListing() {
  const l = scListing;
  $("sc-columns").innerHTML = columnPickers(l.fields, l.file_columns, l.columns);
  $("sc-columns-note").hidden = false;
  $("sc-problem").hidden = !l.problem;
  $("sc-problem").textContent = l.problem || "";
  $("sc-listing").hidden = !!l.problem;
  if (l.problem) return;
  $("sc-listing-cards").innerHTML = [[l.lines.toLocaleString(LOCALE), "Lines in the listing"], [money(l.value), "Value of the listing"],
    [l.checks.lines.toLocaleString(LOCALE), "Lines worth a look"]]
    .map(([v, k]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("sc-checks").innerHTML = l.checks.items.map((c) =>
    `<li><span>${esc(c.label)}</span>: <bdi dir="ltr">${esc(c.rows.join(", "))}</bdi></li>`).join("");
}

$("sc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("sc-pick"), async () => {
    const id = Number($("sc-upload").value);
    if (!id) throw new Error("Upload an inventory listing first.");
    if (sc?.count && !confirm("Pick the items again? The items picked before go.")) return;
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/engagements/${engagementId}/stock-count`, {
      upload_id: id, columns: columnsPicked($("sc-columns")), largest: Number($("sc-largest").value || 0),
      random: Number($("sc-random").value || 0), seed: numOrNull($("sc-seed").value) });
    if (currentEngagement?.id !== engagementId) return;
    sc = { ...out, engagement_id: engagementId };
    $("sc-setup").open = false;
    renderStockCount();
    await stockChanged(engagementId, false);
  });
});

function stockItemEditRow(x) {
  const floor = x.direction === "to_book";
  return `<tr class="cf-edit" data-sc-edit="${x.id}"><td colspan="9"><form data-sc-form>
    ${floor ? `<div class="grid">
      <div><label>Book quantity <span class="muted">(blank if it isn't in the books)</span></label><input name="book_qty" type="number" step="any" value="${esc(x.book_qty ?? "")}"></div>
      <div><label>Unit cost</label><input name="unit_cost" type="number" step="any" min="0" value="${esc(x.unit_cost ?? "")}"></div></div>` : ""}
    <label>Note <span class="muted">(tag number, condition, damage)</span></label><textarea name="note" rows="2" maxlength="5000">${esc(x.note)}</textarea>
    <label>Explanation of the difference</label><textarea name="explanation" rows="2" maxlength="5000">${esc(x.explanation)}</textarea>
    <div class="actions"><button type="submit" class="small">Save</button>
      <button type="button" class="small secondary" data-sc-close>Close</button>
      ${floor ? '<button type="button" class="small secondary danger" data-sc-delete>Remove</button>' : ""}</div>
  </form></td></tr>`;
}

function renderStockCount() {
  const c = sc?.count;
  $("sc-count").hidden = $("sc-export").hidden = !c;
  $("sc-setup").querySelector("summary").textContent = c ? "Pick the items again" : "Pick the items to count";
  if (!c) { $("sc-table").innerHTML = ""; return; }
  const s = sc.summary, st = s.statuses;
  $("sc-export").href = `/api/engagements/${sc.engagement_id}/stock-count/export`;
  $("sc-cards").innerHTML = [
    [`${s.items - st.to_count} / ${s.items}`, "Items counted", false],
    [st.difference + st.not_listed, "Differences to explain", st.difference + st.not_listed > 0],
    [money(s.found), "Differences found", s.found !== 0],
    [money(s.projected), "Projected over the listing", s.outcome === "over"],
    [money(s.performance_materiality), "Performance materiality", false],
    [s.coverage_pct == null ? "" : `${s.coverage_pct}%`, "Value covered", false],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("") +
    `<div class="card"><div class="v"><span class="pill ${s.outcome === "over" ? "bad" : s.outcome === "within" ? "good" : "warn"}">${esc(s.outcome_label)}</span></div><div class="k">${esc("Outcome")}</div></div>`;
  $("sc-about").innerHTML = [`<span>${esc(`Picked from ${c.filename}`)}</span>`.replace(esc(c.filename), data(c.filename)),
    `<span>${esc(`${c.population_lines} lines worth ${money(c.population_value)}.`)}</span>`,
    `<span>${esc(`Random seed ${c.selection.seed}.`)}</span>`,
    ...(c.upload_id == null ? [`<span>${esc("The listing was deleted, so items picked on the floor can't be looked up.")}</span>`] : [])].join(" ");
  if (document.activeElement?.closest("#sc-details") == null) {
    $("sc-date").value = c.count_date || "";
    $("sc-location").value = c.location;
    $("sc-observations").value = c.observations;
    $("sc-conclusion").value = c.conclusion;
  }
  $("sc-review").hidden = !(me.rank >= 2) || !!c.reviewed_at;
  $("sc-unreview").hidden = !(me.rank >= 2) || !c.reviewed_at;
  $("sc-restart").hidden = !(me.rank >= 2);
  $("sc-reviewed").innerHTML = c.reviewed_at ? `<span>${esc("Reviewed by")}</span> ${data(c.reviewed_by)} · ${esc(when(c.reviewed_at))}` : "";
  const show = $("sc-filter").value;
  const items = sc.items.map((x, i) => ({ ...x, ref: i + 1 })).filter((x) => !show || (show === "open" ? x.open : x.direction === show));
  if (!items.length) { $("sc-table").innerHTML = '<tbody><tr><td class="muted">Nothing to show.</td></tr></tbody>'; return; }
  $("sc-table").innerHTML = `<thead><tr><th>Ref</th><th>Item</th><th>Location</th><th class="num">Unit cost</th><th class="num">Book quantity</th>
      <th class="num">Counted</th><th class="num">Difference</th><th>Status</th><th></th></tr></thead><tbody>
    ${items.map((x) => `<tr data-sc="${x.id}">
      <td><bdi>SC${x.ref}</bdi><div class="tag">${esc(x.reason_label)}</div></td>
      <td>${data(x.item)}<div class="tag">${data(x.description)}</div>${x.note ? `<div class="tag">${data(x.note)}</div>` : ""}</td>
      <td>${data(x.location)}</td>
      <td class="num">${money(x.unit_cost)}</td>
      <td class="num">${x.book_qty == null ? esc("Not in the listing") : qty(x.book_qty)}${x.source_row ? `<div class="tag"><span>${esc("Row")}</span> ${x.source_row}</div>` : ""}</td>
      <td class="num"><input type="number" step="any" class="qty-input" data-sc-qty="${x.id}" value="${esc(x.counted_qty ?? "")}" aria-label="${esc("Counted quantity")}"></td>
      <td class="num${x.open && x.counted_qty != null ? " bad" : ""}">${x.difference_qty ? qty(x.difference_qty) : ""}${x.difference ? `<div class="tag">${money(x.difference)}</div>` : ""}</td>
      <td><span class="pill ${SC_PILL[x.status]}">${esc(x.status_label)}</span>${x.explanation ? `<div class="tag">${data(x.explanation)}</div>` : ""}</td>
      <td><button type="button" class="small secondary" data-sc-open>${scEditing === x.id ? "Close" : "Update"}</button></td></tr>
      ${scEditing === x.id ? stockItemEditRow(x) : ""}`).join("")}</tbody>`;
}

async function stockChanged(id, reload = true) {
  if (currentEngagement?.id !== id) return;
  if (reload) await loadStockCount();
  refreshDashboard();
  await loadEngagementActivity();
}

// Saving one count, then putting the cursor back where it was, so counts can be typed in a run.
$("sc-table").addEventListener("change", (ev) => {
  const input = ev.target.closest("[data-sc-qty]");
  if (!input) return;
  const id = currentEngagement.id;
  guard(async () => {
    const out = await sendJSON("PATCH", `/api/stock-count-items/${input.dataset.scQty}`, { counted_qty: numOrNull(input.value) });
    if (currentEngagement?.id !== id) return;
    // Where the cursor went (the next count, or a row's button), by its row.
    const at = document.activeElement?.closest?.("#sc-table [data-sc]");
    const focused = at && (document.activeElement.matches("[data-sc-qty]") ? "[data-sc-qty]" : "[data-sc-open]");
    sc = { ...out, engagement_id: id };
    renderStockCount();
    if (focused) document.querySelector(`#sc-table [data-sc="${at.dataset.sc}"] ${focused}`)?.focus();
    await stockChanged(id, false);
  });
});

// Enter saves a count and goes on to the next one, as in a spreadsheet.
$("sc-table").addEventListener("keydown", (ev) => {
  const input = ev.target.closest("[data-sc-qty]");
  if (!input || ev.key !== "Enter") return;
  ev.preventDefault();
  const boxes = [...$("sc-table").querySelectorAll("[data-sc-qty]")];
  const next = boxes[boxes.indexOf(input) + 1];
  if (next) next.focus(); else input.blur();
});

$("sc-table").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-sc-open], button[data-sc-close], button[data-sc-delete]");
  if (!btn) return;
  const id = currentEngagement.id;
  const row = btn.closest("[data-sc], [data-sc-edit]");
  const xid = Number(row.dataset.sc || row.dataset.scEdit);
  const x = sc.items.find((i) => i.id === xid);
  if (!x) return;
  if (btn.hasAttribute("data-sc-delete")) {
    if (!confirm(`Remove ${x.item || x.description} from the count?`)) return;
    busy(btn, async () => {
      await sendJSON("DELETE", `/api/stock-count-items/${xid}`);
      scEditing = null;
      await stockChanged(id);
    });
  } else {
    scEditing = scEditing === xid ? null : xid;
    renderStockCount();
  }
});

$("sc-table").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-sc-form]");
  if (!form) return;
  ev.preventDefault();
  const id = currentEngagement.id;
  const xid = Number(form.closest("[data-sc-edit]").dataset.scEdit);
  const x = sc.items.find((i) => i.id === xid);
  const f = formObject(form);
  busy(form.querySelector("button[type=submit]"), async () => {
    await sendJSON("PATCH", `/api/stock-count-items/${xid}`, { note: f.note || "", explanation: f.explanation || "",
      ...(x?.direction === "to_book" ? { book_qty: numOrNull(f.book_qty), unit_cost: numOrNull(f.unit_cost) } : {}) });
    scEditing = null;
    await stockChanged(id);
  });
});

$("sc-filter").addEventListener("change", renderStockCount);

$("sc-details").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  busy($("sc-save"), async () => {
    await sendJSON("PATCH", `/api/engagements/${id}/stock-count`, { count_date: $("sc-date").value || null,
      location: $("sc-location").value.trim(), observations: $("sc-observations").value.trim(),
      conclusion: $("sc-conclusion").value.trim() });
    document.activeElement?.blur();
    await stockChanged(id);
  });
});

for (const [button, reviewed] of [["sc-review", true], ["sc-unreview", false]]) {
  $(button).addEventListener("click", () => {
    const id = currentEngagement.id;
    busy($(button), async () => {
      await sendJSON("PATCH", `/api/engagements/${id}/stock-count`, { reviewed });
      await stockChanged(id);
    });
  });
}

$("sc-restart").addEventListener("click", () => {
  if (!confirm("Start the count over? Every item and count recorded goes.")) return;
  const id = currentEngagement.id;
  busy($("sc-restart"), async () => {
    await sendJSON("DELETE", `/api/engagements/${id}/stock-count`);
    scEditing = null;
    $("sc-setup").open = true;
    await stockChanged(id);
  });
});

$("sc-add-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  const f = formObject($("sc-add-form"));
  busy($("sc-add-btn"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/stock-count/items`, { item: f.item || "",
      description: f.description || "", location: f.location || "", counted_qty: numOrNull(f.counted_qty) });
    $("sc-add-form").reset();
    await stockChanged(id);
  });
});

// Last year's points

let py = null;         // {engagement_id, points, summary, sources}
let pyEditing = null;  // the point whose follow-up is open
let pyLoads = 0;
const PY_PILL = { to_follow_up: "warn", resolved: "good", partly: "bad", not_resolved: "bad", not_applicable: "good" };
const PY_STATUSES = [["to_follow_up", "To follow up"], ["resolved", "Resolved"], ["partly", "Partly resolved"],
  ["not_resolved", "Not resolved"], ["not_applicable", "No longer applies"]];
const pyUnresolved = (p) => p.status === "partly" || p.status === "not_resolved";

async function loadPriorPoints() {
  const id = currentEngagement.id;
  const load = ++pyLoads;
  const out = await getJSON(`/api/engagements/${id}/prior-points`);
  if (load !== pyLoads || currentEngagement?.id !== id) return;
  py = { ...out, engagement_id: id };
  renderPriorPoints();
}

function priorPointEditRow(p) {
  const status = PY_STATUSES.map(([v, k]) => `<option value="${v}"${v === p.status ? " selected" : ""}>${esc(k)}</option>`).join("");
  const priority = Object.entries(SEVERITY_LABEL).map(([v, k]) => `<option value="${v}"${v === p.priority ? " selected" : ""}>${esc(k)}</option>`).join("");
  const signed = !!p.reviewed_at;
  return `<tr class="cf-edit" data-py-edit="${p.id}"><td colspan="7"><form data-py-form>
    <div class="grid">
      <div><label>This year</label><select name="status" class="map">${status}</select></div>
      <div><label>Priority</label><select name="priority" class="map">${priority}</select></div>
    </div>
    <label>How it was followed up <span class="muted">(what you saw or asked, and when)</span></label><textarea name="follow_up" rows="2" maxlength="5000">${esc(p.follow_up)}</textarea>
    <label>Management's response</label><textarea name="management_response" rows="2" maxlength="5000">${esc(p.management_response)}</textarea>
    <details><summary>Change the point itself</summary>
      <label>Point</label><input name="title" maxlength="300" value="${esc(p.title)}" required>
      <label>What was found</label><textarea name="detail" rows="2" maxlength="5000">${esc(p.detail)}</textarea>
      <label>Recommendation</label><textarea name="recommendation" rows="2" maxlength="5000">${esc(p.recommendation)}</textarea>
    </details>
    <div class="actions"><button type="submit" class="small">Save</button>
      <button type="button" class="small secondary" data-py-close>Close</button>
      ${pyUnresolved(p) && !p.finding_id ? '<button type="button" class="small" data-py-finding>Make it one of this year\'s findings</button>' : ""}
      ${me.rank >= 2 && p.status !== "to_follow_up" ? `<button type="button" class="small${signed ? " secondary" : ""}" data-py-review="${signed ? "0" : "1"}">${signed ? "Take back the sign-off" : "Sign off as reviewed"}</button>` : ""}
      ${!signed || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-py-delete>Remove</button>' : ""}</div>
  </form></td></tr>`;
}

function renderPriorPoints() {
  if (!py) {
    $("py-cards").innerHTML = $("py-table").innerHTML = "";
    $("py-import").hidden = true;
    return;
  }
  const s = py.summary, st = s.statuses;
  $("py-export").href = `/api/engagements/${py.engagement_id}/prior-points/export`;
  $("py-cards").innerHTML = [
    [s.points, "Points from last year", false],
    [st.to_follow_up, "To follow up", st.to_follow_up > 0],
    [st.resolved + st.not_applicable, "Resolved", false],
    [st.partly + st.not_resolved, "Not resolved", st.partly + st.not_resolved > 0],
    [s.unresolved_without_finding, "Not yet in this year's findings", s.unresolved_without_finding > 0],
    [s.not_reviewed, "Not signed off", s.not_reviewed > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const keep = $("py-source").value;
  $("py-import").hidden = !py.sources.length;
  $("py-source").innerHTML = py.sources.map((x) =>
    `<option value="${x.id}" translate="no">${esc(x.name)} (${esc(x.period_end)})</option>`).join("");
  if (py.sources.some((x) => String(x.id) === keep)) $("py-source").value = keep;
  const show = $("py-filter").value;
  const points = py.points.map((p, i) => ({ ...p, ref: i + 1 })).filter((p) => !show
    || (show === "open" ? p.open : show === "unresolved" ? pyUnresolved(p) : p.status !== "to_follow_up" && !p.reviewed_at));
  if (!points.length) {
    $("py-table").innerHTML = `<tbody><tr><td class="muted">${esc(py.points.length ? "Nothing to show."
      : py.sources.length ? "No points yet. Bring them in from last year's engagement, or add them by hand."
        : "No points yet. They come in when last year's engagement is rolled forward, or add them by hand.")}</td></tr></tbody>`;
    return;
  }
  $("py-table").innerHTML = `<thead><tr><th>Ref</th><th>Point</th><th>Priority</th><th>Management's response</th>
      <th>How it was followed up</th><th>This year</th><th></th></tr></thead><tbody>
    ${points.map((p) => `<tr data-py="${p.id}">
      <td><bdi>PY${p.ref}</bdi></td>
      <td><strong>${data(p.title)}</strong>${p.detail ? `<div class="tag">${data(p.detail)}</div>` : ""}
        ${p.recommendation ? `<div class="tag"><span>${esc("Recommendation:")}</span> ${data(p.recommendation)}</div>` : ""}
        <div class="tag"><span>${esc(p.origin_label)}</span>${p.first_raised ? ` · <span>${esc("First raised")}</span> ${data(p.first_raised)}` : ""}${p.repeated ? ` <span class="pill bad">${esc("Raised before")}</span>` : ""}</div></td>
      <td><span class="sev ${esc(p.priority)}">${esc(SEVERITY_LABEL[p.priority])}</span></td>
      <td>${data(p.management_response)}</td>
      <td>${data(p.follow_up)}${p.checked_at ? `<div class="tag">${data(p.checked_by)} · <span>${esc(when(p.checked_at))}</span></div>` : ""}</td>
      <td><span class="pill ${PY_PILL[p.status]}">${esc(p.status_label)}</span>
        ${p.finding_id ? `<div class="tag"><a href="#" data-open-finding="${p.finding_id}"><span>${esc("This year's finding")}</span> <bdi translate="no">#${p.finding_id}</bdi></a></div>` : ""}
        ${p.reviewed_at ? `<div class="tag"><span>${esc("Reviewed by")}</span> ${data(p.reviewed_by)}</div>` : ""}</td>
      <td><button type="button" class="small secondary" data-py-open>${pyEditing === p.id ? "Close" : "Update"}</button></td></tr>
      ${pyEditing === p.id ? priorPointEditRow(p) : ""}`).join("")}</tbody>`;
}

async function priorPointsChanged(id, out) {
  if (currentEngagement?.id !== id) return;
  if (out) { py = { ...out, engagement_id: id }; renderPriorPoints(); } else await loadPriorPoints();
  refreshDashboard();
  await loadEngagementActivity();
}

$("py-filter").addEventListener("change", renderPriorPoints);

$("py-import").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  busy($("py-import-btn"), async () => {
    const out = await sendJSON("POST", `/api/engagements/${id}/prior-points/import`, { from_engagement_id: Number($("py-source").value) });
    if (currentEngagement?.id !== id) return;
    $("py-imported").textContent = out.added === 1 ? "Brought in 1 point." : `Brought in ${out.added} points.`;
    await priorPointsChanged(id, out);
  });
});

$("py-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  busy($("py-save"), async () => {
    const out = await sendJSON("POST", `/api/engagements/${id}/prior-points`, formObject($("py-form")));
    $("py-form").reset();
    $("py-add").open = false;
    await priorPointsChanged(id, out);
  });
});

$("py-table").addEventListener("click", (ev) => {
  const link = ev.target.closest("[data-open-finding]");
  if (link) {
    ev.preventDefault();
    guard(async () => openFinding(await getJSON(`/api/findings/${link.dataset.openFinding}`)));
    return;
  }
  const btn = ev.target.closest("button[data-py-open], button[data-py-close], button[data-py-finding], button[data-py-review], button[data-py-delete]");
  if (!btn) return;
  const id = currentEngagement.id;
  const row = btn.closest("[data-py], [data-py-edit]");
  const pid = Number(row.dataset.py || row.dataset.pyEdit);
  const p = py.points.find((x) => x.id === pid);
  if (!p) return;
  if (btn.hasAttribute("data-py-delete")) {
    if (!confirm(`Remove the point ${p.title}?`)) return;
    busy(btn, async () => {
      const out = await sendJSON("DELETE", `/api/prior-points/${pid}`);
      pyEditing = null;
      await priorPointsChanged(id, out);
    });
  } else if (btn.hasAttribute("data-py-finding")) {
    busy(btn, async () => {
      const out = await sendJSON("POST", `/api/prior-points/${pid}/finding`);
      pyEditing = null;
      await priorPointsChanged(id, out);
      if (currentEngagement?.id !== id) return;
      await loadRegister();
      openFinding(await getJSON(`/api/findings/${out.finding_id}`));
    });
  } else if (btn.hasAttribute("data-py-review")) {
    busy(btn, async () => {
      const out = await sendJSON("PATCH", `/api/prior-points/${pid}`, { reviewed: btn.dataset.pyReview === "1" });
      await priorPointsChanged(id, out);
    });
  } else {
    pyEditing = pyEditing === pid ? null : pid;
    renderPriorPoints();
  }
});

$("py-table").addEventListener("submit", (ev) => {
  const form = ev.target.closest("[data-py-form]");
  if (!form) return;
  ev.preventDefault();
  const id = currentEngagement.id;
  const pid = Number(form.closest("[data-py-edit]").dataset.pyEdit);
  const p = py.points.find((x) => x.id === pid);
  const f = formObject(form);
  // Only what changed, so saving an unchanged point doesn't take its sign-off away.
  const body = Object.fromEntries(Object.entries(f).filter(([k, v]) => p && v !== (p[k] ?? "")));
  busy(form.querySelector("button[type=submit]"), async () => {
    const out = Object.keys(body).length ? await sendJSON("PATCH", `/api/prior-points/${pid}`, body) : null;
    pyEditing = null;
    if (out) await priorPointsChanged(id, out); else renderPriorPoints();
  });
});

// Tasks and time

let wp = null;       // the work plan shown
let wpLoads = 0;
const WP_PILL = { open: "", in_progress: "warn", done: "good" };
const hours = (n) => n == null ? "" : `<bdi dir="ltr">${Number(n).toLocaleString(LOCALE, { maximumFractionDigits: 2 })}</bdi>`;

async function loadWorkplan() {
  const id = currentEngagement.id;
  const load = ++wpLoads;
  const w = await getJSON(`/api/engagements/${id}/workplan`);
  if (load !== wpLoads || currentEngagement?.id !== id) return;
  wp = { ...w, engagement_id: id };
  renderWorkplan();
}

function renderWorkplan() {
  if (!wp) {
    for (const t of ["wp-cards", "wp-tasks", "wp-budget", "wp-people", "wp-entries", "wp-budget-fields"]) $(t).innerHTML = "";
    $("wp-mine").textContent = "";
    $("wp-export").removeAttribute("href");
    delete $("wp-budget-fields").dataset.engagement;
    delete $("wp-budget-fields").dataset.dirty;
    return;
  }
  const s = wp.summary;
  $("wp-export").href = `/api/engagements/${wp.engagement_id}/workplan/export`;
  const used = s.budget ? `${Math.round(s.actual / s.budget * 100)}%` : "–";
  $("wp-cards").innerHTML = [
    [s.open, "Tasks not done", false], [s.overdue, "Overdue", s.overdue > 0],
    [s.unassigned, "No owner", false],
    [s.budget == null ? "–" : s.budget, "Hours budgeted", false],
    [s.actual, "Hours worked", s.over_areas.length > 0],
    [used, "Budget used", s.budget != null && s.actual > s.budget],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v"><bdi dir="ltr">${esc(v)}</bdi></div><div class="k">${esc(k)}</div></div>`).join("");
  const keepArea = $("wp-area").value, keepTimeArea = $("wp-time-area").value;
  const keepTask = $("wp-time-task").value;
  $("wp-area").innerHTML = $("wp-time-area").innerHTML = optionList(wp.areas, "other");
  if (keepArea) $("wp-area").value = keepArea;
  if (keepTimeArea) $("wp-time-area").value = keepTimeArea;
  const owners = [{ id: "", label: "No owner" }, ...wp.people.map((p) => ({ id: String(p.id), label: p.name }))];
  const keepOwner = $("wp-owner").value;
  $("wp-owner").innerHTML = owners.map((o) => `<option value="${esc(o.id)}">${o.id ? data(o.label) : esc(o.label)}</option>`).join("");
  $("wp-owner").value = owners.some((o) => o.id === keepOwner) ? keepOwner : "";
  $("wp-time-task").innerHTML = '<option value="">None</option>' + wp.tasks.filter((t) => t.status !== "done")
    .map((t) => `<option value="${t.id}">${data(t.title)}</option>`).join("");
  if ([...$("wp-time-task").options].some((o) => o.value === keepTask)) $("wp-time-task").value = keepTask;
  if (!$("wp-time-day").value) $("wp-time-day").value = today();
  renderWorkplanTasks();
  $("wp-budget").innerHTML = wp.budget.length
    ? `<thead><tr><th>Area</th><th class="num">Budget</th><th class="num">Worked</th><th class="num">Remaining</th><th class="num">Used</th></tr></thead><tbody>
      ${wp.budget.map((r) => `<tr${r.over ? ' class="flagged"' : ""}><td>${esc(r.label)}</td><td class="num">${hours(r.budget)}</td>
        <td class="num">${hours(r.actual)}</td><td class="num">${r.over ? '<span class="pill bad">' : ""}${hours(r.remaining)}${r.over ? "</span>" : ""}</td>
        <td class="num">${r.used_pct == null ? "" : `<bdi dir="ltr">${r.used_pct}%</bdi>`}</td></tr>`).join("")}
      <tr class="total"><td>Total</td><td class="num">${hours(wp.budget_total)}</td><td class="num">${hours(wp.actual_total)}</td><td></td><td></td></tr></tbody>`
    : '<tbody><tr><td class="muted">No budget or hours yet.</td></tr></tbody>';
  const budget = Object.fromEntries(wp.budget.map((r) => [r.area, r.budget]));
  // Keep numbers typed but not yet saved while something else on the panel changes.
  const fields = $("wp-budget-fields");
  if (fields.dataset.engagement !== String(wp.engagement_id) || !fields.dataset.dirty) {
    fields.dataset.engagement = wp.engagement_id;
    fields.innerHTML = wp.areas.map((a) => `<div><label for="wp-b-${esc(a.id)}">${esc(a.label)}</label>
    <input id="wp-b-${esc(a.id)}" data-area="${esc(a.id)}" type="number" min="0" step="0.5" value="${budget[a.id] ?? ""}"></div>`).join("");
  }
  $("wp-people").innerHTML = wp.by_person.length
    ? `<thead><tr><th>Who</th><th class="num">Hours</th></tr></thead><tbody>${wp.by_person.map((p) =>
      `<tr><td>${data(p.name)}</td><td class="num">${hours(p.hours)}</td></tr>`).join("")}</tbody>` : "";
  $("wp-mine").innerHTML = wp.my_hours ? `<span>You have logged</span> ${hours(wp.my_hours)} <span>hours here.</span>` : "";
  $("wp-entries").innerHTML = wp.entries.length
    ? `<thead><tr><th>Day</th><th>Who</th><th>Area</th><th class="num">Hours</th><th>What</th><th></th></tr></thead><tbody>
      ${wp.entries.map((x) => `<tr data-time="${x.id}"><td class="nowrap"><bdi dir="ltr">${esc(x.worked_on)}</bdi></td><td>${data(x.user_name)}</td>
        <td>${esc(x.area_label)}</td><td class="num">${hours(x.hours)}</td><td>${data(x.note)}</td>
        <td>${x.user_id === me.id || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-time-delete>Remove</button>' : ""}</td></tr>`).join("")}
      ${wp.entries_total > wp.entries.length ? `<tr><td colspan="6" class="muted"><span>Showing the latest</span> <bdi dir="ltr">${wp.entries.length}</bdi>. <span>The Excel download has them all.</span></td></tr>` : ""}</tbody>`
    : '<tbody><tr><td class="muted">No hours logged yet.</td></tr></tbody>';
}

function renderWorkplanTasks() {
  const filter = $("wp-filter").value;
  const tasks = wp.tasks.filter((t) => filter === "" || (filter === "open" ? t.status !== "done" : t.assignee_id === me.id && t.status !== "done"));
  if (!tasks.length) {
    $("wp-tasks").innerHTML = `<tbody><tr><td class="muted">${esc(wp.tasks.length ? "Nothing to show here." : "No tasks yet.")}</td></tr></tbody>`;
    return;
  }
  const owners = [{ id: "", name: "No owner" }, ...wp.people];
  $("wp-tasks").innerHTML = `<thead><tr><th>Task</th><th>Area</th><th>Owner</th><th>Due</th><th>Status</th><th></th></tr></thead><tbody>
    ${tasks.map((t) => `<tr data-task="${t.id}"${t.overdue ? ' class="flagged"' : ""}>
      <td>${data(t.title)}${t.note ? `<div class="tag">${data(t.note)}</div>` : ""}${t.done_by ? `<div class="tag"><span>Done by</span> ${data(t.done_by)}</div>` : ""}</td>
      <td>${esc(t.area_label)}</td>
      <td><select data-task-owner class="map" aria-label="Owner">${owners.map((o) => `<option value="${o.id}"${String(o.id) === String(t.assignee_id ?? "") ? " selected" : ""}>${o.id ? data(o.name) : esc(o.name)}</option>`).join("")}
        ${t.assignee_id && !wp.people.some((p) => p.id === t.assignee_id) ? `<option value="${t.assignee_id}" selected>${data(t.assignee_name)}</option>` : ""}</select></td>
      <td><input type="date" data-task-due value="${esc(t.due_on || "")}" aria-label="Due">${t.overdue ? '<div><span class="pill bad">Overdue</span></div>' : ""}</td>
      <td><select data-task-status class="map" aria-label="Status">${optionList(wp.statuses, t.status)}</select></td>
      <td>${t.created_by_id === me.id || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-task-delete>Remove</button>' : ""}</td></tr>`).join("")}</tbody>`;
}

// Reload rather than show the save's reply, so replies arriving out of order can't
// bring back an older state.
async function workplanChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadWorkplan();
  refreshDashboard();
  await loadEngagementActivity();
}

$("wp-filter").addEventListener("change", () => { if (wp) renderWorkplanTasks(); });
$("wp-budget-fields").addEventListener("input", () => { $("wp-budget-fields").dataset.dirty = "1"; });

$("wp-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!wp) return;
  const id = wp.engagement_id;
  const body = { title: $("wp-title").value, area: $("wp-area").value, note: $("wp-note").value,
    assignee_id: intOrNull($("wp-owner").value), due_on: $("wp-due").value || null };
  busy($("wp-save"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/tasks`, body);
    if (currentEngagement?.id !== id) return;
    $("wp-title").value = $("wp-note").value = $("wp-due").value = "";
    await workplanChanged(id);
  });
});

$("wp-tasks").addEventListener("change", (ev) => {
  const row = ev.target.closest("[data-task]");
  if (!row || !wp) return;
  const id = wp.engagement_id;
  let body = null;
  if (ev.target.matches("[data-task-owner]")) body = { assignee_id: intOrNull(ev.target.value) };
  if (ev.target.matches("[data-task-due]")) body = { due_on: ev.target.value || null };
  if (ev.target.matches("[data-task-status]")) body = { status: ev.target.value };
  if (!body) return;
  guard(async () => {
    try {
      await sendJSON("PATCH", `/api/tasks/${row.dataset.task}`, body);
      await workplanChanged(id);
    } catch (err) {
      if (wp?.engagement_id === id) renderWorkplanTasks();  // put the control back as it was
      throw err;
    }
  });
});

$("wp-tasks").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-task]");
  if (!row || !wp || !ev.target.matches("[data-task-delete]")) return;
  if (!confirm("Remove this task?")) return;
  const id = wp.engagement_id;
  guard(async () => {
    await sendJSON("DELETE", `/api/tasks/${row.dataset.task}`);
    await workplanChanged(id);
  });
});

$("wp-budget-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!wp) return;
  const id = wp.engagement_id;
  const hoursBy = {};
  for (const input of $("wp-budget-fields").querySelectorAll("[data-area]")) hoursBy[input.dataset.area] = Number(input.value || 0);
  busy($("wp-budget-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/budget`, { hours: hoursBy });
    if (currentEngagement?.id !== id) return;
    delete $("wp-budget-fields").dataset.dirty;
    await workplanChanged(id);
  });
});

$("wp-time-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!wp) return;
  const id = wp.engagement_id;
  const body = { worked_on: $("wp-time-day").value, area: $("wp-time-area").value,
    hours: Number($("wp-time-hours").value), note: $("wp-time-note").value,
    task_id: intOrNull($("wp-time-task").value) };
  busy($("wp-time-save"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/time`, body);
    if (currentEngagement?.id !== id) return;
    $("wp-time-hours").value = $("wp-time-note").value = "";
    await workplanChanged(id);
  });
});

$("wp-entries").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-time]");
  if (!row || !wp || !ev.target.matches("[data-time-delete]")) return;
  if (!confirm("Remove these hours?")) return;
  const id = wp.engagement_id;
  guard(async () => {
    await sendJSON("DELETE", `/api/time-entries/${row.dataset.time}`);
    await workplanChanged(id);
  });
});

// The signed-in user's tasks on every engagement, shown when no engagement is open.
async function loadMyTasks() {
  const m = await getJSON("/api/my-tasks");
  if (currentEngagement || !$("eng-new").hidden) return;
  $("my-tasks").hidden = !m.tasks.length;
  if (!m.tasks.length) return;
  $("my-tasks-sum").innerHTML = `<bdi dir="ltr">${m.tasks.length}</bdi> <span>${m.tasks.length === 1 ? "task to do" : "tasks to do"}</span>`
    + (m.overdue ? ` · <span class="pill bad"><bdi dir="ltr">${m.overdue}</bdi> <span>overdue</span></span>` : "");
  $("my-tasks-table").innerHTML = `<thead><tr><th>Task</th><th>Engagement</th><th>Area</th><th>Due</th><th>Status</th></tr></thead><tbody>
    ${m.tasks.map((t) => `<tr${t.overdue ? ' class="flagged"' : ""}><td><a href="#/engagements/${t.engagement_id}">${data(t.title)}</a></td>
      <td>${data(t.client_name)} · ${data(t.engagement_name)}</td><td>${esc(t.area_label)}</td>
      <td class="nowrap">${t.due_on ? `<bdi dir="ltr">${esc(t.due_on)}</bdi>` : ""}${t.overdue ? ' <span class="pill bad">Overdue</span>' : ""}</td>
      <td><span class="pill ${WP_PILL[t.status]}">${esc(t.status_label)}</span></td></tr>`).join("")}</tbody>`;
}

// Completion procedures

let cp = null;
let cpLoads = 0;
const CP_PILL = { pending: "warn", adjusted: "good", disclosed: "good", not_needed: "" };

async function loadCompletion() {
  const id = currentEngagement.id;
  const load = ++cpLoads;
  const c = await getJSON(`/api/engagements/${id}/completion`);
  if (load !== cpLoads || currentEngagement?.id !== id) return;
  cp = { ...c, engagement_id: id };
  renderCompletion();
}

function renderCompletion() {
  if (!cp) {
    for (const t of ["cp-cards", "cp-status", "cp-questions", "cp-signs", "cp-events"]) $(t).innerHTML = "";
    $("cp-saved").textContent = "";
    $("cp-export").removeAttribute("href");
    $("cp-letter").removeAttribute("href");
    $("cp-form").reset();
    $("cp-gc").innerHTML = "";
    delete $("cp-form").dataset.dirty;
    delete $("cp-form").dataset.engagement;
    return;
  }
  const id = cp.engagement_id;
  $("cp-export").href = `/api/engagements/${id}/completion/export`;
  $("cp-letter").href = `/api/engagements/${id}/completion/representation-letter?language=${I18N.lang === "ar" ? "ar" : "en"}`;
  $("cp-cards").innerHTML = [
    [`${cp.answered}/${cp.questions.length}`, "Questions answered", false],
    [cp.flags.length, "Answers needing a second look", cp.flags.length > 0],
    [cp.events.length, "Subsequent events", false],
    [cp.pending_events, "Events to decide", cp.pending_events > 0],
    [cp.going_concern_label || "Not concluded", "Going concern", cp.going_concern && cp.going_concern !== "appropriate"],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("cp-status").innerHTML = cp.signed_by
    ? `<p><strong>Signed off</strong> <span class="muted">${data(cp.signed_by)} · <span>${esc(when(cp.signed_at))}</span></span></p>
       ${cp.signed_note ? `<p>${data(cp.signed_note)}</p>` : ""}
       ${cp.changed_since_signoff ? '<p class="warn-box">The completion work changed after the sign-off. The partner may want to look at it again.</p>' : ""}`
    : cp.blockers.length ? `<ul class="warn-list">${cp.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : "";
  $("cp-saved").innerHTML = cp.updated_by ? `<span>Last saved by</span> ${data(cp.updated_by)} · <span>${esc(when(cp.updated_at))}</span>` : "";
  renderCompletionEvents();
  // Worked out by the server, not typed here, so always shown fresh.
  const signs = cp.signs;
  $("cp-signs").innerHTML = !signs.statements ? '<p class="muted">Upload the trial balance to see warning signs from the draft statements here.</p>'
    : signs.warnings.length ? `<ul class="warn-list">${signs.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
    : '<p class="muted">The draft statements show no going concern warning signs.</p>';
  // Keep answers typed but not yet saved while an event or the sign-off changes.
  const form = $("cp-form");
  if (form.dataset.engagement === String(id) && form.dataset.dirty) return;
  form.dataset.engagement = id;
  let group = null;
  $("cp-questions").innerHTML = cp.questions.map((q, i) => {
    const a = cp.answers[q.id];
    const flagged = cp.flags.includes(q.id);
    const head = q.group !== group ? `<h3>${esc(cp.groups.find((g) => g.id === q.group).label)}</h3>` : "";
    group = q.group;
    return `${head}<div class="ac-q${flagged ? " flagged" : ""}" data-q="${esc(q.id)}">
      <p><bdi>${i + 1}.</bdi> <span>${esc(q.text)}</span></p>
      <fieldset class="checks-inline"><legend class="visually-hidden">Answer</legend>
        ${Object.entries(ANSWER_LABELS).map(([v, label]) => `<label><input type="radio" name="cp-${esc(q.id)}" value="${v}" ${a.answer === v ? "checked" : ""}><span>${label}</span></label>`).join("")}
      </fieldset>
      <input data-comment maxlength="5000" value="${esc(a.comment)}" placeholder="Comment" aria-label="Comment">
      ${flagged ? '<div class="tag flag-note">This answer needs a second look before signing off.</div>' : ""}
    </div>`;
  }).join("");
  $("cp-gc").innerHTML = '<option value="">Not concluded</option>' + optionList(cp.going_concern_choices, cp.going_concern);
  $("cp-gc-note").value = cp.going_concern_note;
  $("cp-reps-asked").value = cp.reps_requested_on || "";
  $("cp-reps-dated").value = cp.reps_dated || "";
  $("cp-conclusion").value = cp.conclusion;
}

function renderCompletionEvents() {
  if (!$("cp-ev-kind").options.length) {
    $("cp-ev-kind").innerHTML = optionList(cp.event_kinds, "non_adjusting");
    $("cp-ev-treatment").innerHTML = optionList(cp.treatments, "pending");
  }
  $("cp-events").innerHTML = cp.events.length
    ? `<thead><tr><th>Date</th><th>Event</th><th>Type</th><th class="num">Amount</th><th>Treatment</th><th></th></tr></thead><tbody>
      ${cp.events.map((x) => `<tr data-event="${x.id}"><td class="nowrap">${x.occurred_on ? `<bdi dir="ltr">${esc(x.occurred_on)}</bdi>` : ""}</td>
        <td>${data(x.description)}</td>
        <td><select data-ev-kind class="map" aria-label="Type">${optionList(cp.event_kinds, x.kind)}</select></td>
        <td class="num">${money(x.amount)}</td>
        <td><select data-ev-treatment class="map" aria-label="Treatment">${optionList(cp.treatments, x.treatment)}</select>
          ${x.treatment === "pending" ? ' <span class="pill warn">To decide</span>' : ""}</td>
        <td>${me.rank >= 2 ? '<button type="button" class="small secondary danger" data-ev-delete>Remove</button>' : ""}</td></tr>`).join("")}</tbody>`
    : '<tbody><tr><td class="muted">No subsequent events recorded.</td></tr></tbody>';
}

// Reload rather than show the save's reply, so replies arriving out of order can't
// bring back an older state.
async function completionChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadCompletion();
  refreshDashboard();
  if (rp?.engagement_id === id) guard(loadReport);  // the suggestion follows the conclusion
  await loadEngagementActivity();
}

$("cp-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!cp) return;
  const id = cp.engagement_id;
  const answers = {};
  for (const q of $("cp-questions").querySelectorAll("[data-q]")) {
    const picked = q.querySelector("input[type=radio]:checked");
    answers[q.dataset.q] = { answer: picked ? picked.value : null, comment: q.querySelector("[data-comment]").value };
  }
  const body = { answers, conclusion: $("cp-conclusion").value, going_concern: $("cp-gc").value || null,
    going_concern_note: $("cp-gc-note").value, reps_requested_on: $("cp-reps-asked").value || null,
    reps_dated: $("cp-reps-dated").value || null };
  const rev = $("cp-form").dataset.rev;
  busy($("cp-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/completion`, body);
    if (currentEngagement?.id !== id) return;
    if ($("cp-form").dataset.rev === rev) delete $("cp-form").dataset.dirty;
    await completionChanged(id);
  });
});

$("cp-form").addEventListener("input", () => formEdited($("cp-form")));

$("cp-event-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!cp) return;
  const id = cp.engagement_id;
  const body = { description: $("cp-ev-desc").value, occurred_on: $("cp-ev-date").value || null,
    kind: $("cp-ev-kind").value, treatment: $("cp-ev-treatment").value, amount: numOrNull($("cp-ev-amount").value) };
  busy($("cp-ev-save"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/completion/events`, body);
    if (currentEngagement?.id !== id) return;
    $("cp-ev-desc").value = $("cp-ev-date").value = $("cp-ev-amount").value = "";
    await completionChanged(id);
  });
});

$("cp-events").addEventListener("change", (ev) => {
  const row = ev.target.closest("[data-event]");
  if (!row || !cp) return;
  const id = cp.engagement_id;
  const body = ev.target.matches("[data-ev-kind]") ? { kind: ev.target.value }
    : ev.target.matches("[data-ev-treatment]") ? { treatment: ev.target.value } : null;
  if (!body) return;
  guard(async () => {
    try {
      await sendJSON("PATCH", `/api/subsequent-events/${row.dataset.event}`, body);
      await completionChanged(id);
    } catch (err) {
      if (cp?.engagement_id === id) renderCompletionEvents();
      throw err;
    }
  });
});

$("cp-events").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-event]");
  if (!row || !cp || !ev.target.matches("[data-ev-delete]")) return;
  if (!confirm("Remove this subsequent event?")) return;
  const id = cp.engagement_id;
  guard(async () => {
    await sendJSON("DELETE", `/api/subsequent-events/${row.dataset.event}`);
    await completionChanged(id);
  });
});

$("cp-sign").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!cp) return;
  const id = cp.engagement_id;
  busy($("cp-sign-btn"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/completion/signoff`, { note: $("cp-sign-note").value });
    await completionChanged(id);
  });
});

$("cp-letter").addEventListener("click", () => {  // in the language the page is in now
  if (cp) $("cp-letter").href = `/api/engagements/${cp.engagement_id}/completion/representation-letter?language=${I18N.lang === "ar" ? "ar" : "en"}`;
});

// Engagement quality review

let qr = null;
let qrLoads = 0;

async function loadQuality() {
  const id = currentEngagement.id;
  const load = ++qrLoads;
  const q = await getJSON(`/api/engagements/${id}/quality-review`);
  if (load !== qrLoads || currentEngagement?.id !== id) return;
  qr = { ...q, engagement_id: id };
  renderQuality();
}

function renderQuality() {
  if (!qr) {
    for (const t of ["qr-cards", "qr-status", "qr-questions", "qr-who"]) $(t).innerHTML = "";
    $("qr-saved").textContent = "";
    $("qr-export").removeAttribute("href");
    $("qr-setup").reset(); $("qr-form").reset();
    $("qr-reason").innerHTML = $("qr-reviewer").innerHTML = "";
    delete $("qr-form").dataset.dirty; delete $("qr-form").dataset.engagement;
    delete $("qr-setup").dataset.dirty; delete $("qr-setup").dataset.engagement;
    return;
  }
  const id = qr.engagement_id;
  const fills = qr.required && (qr.is_reviewer || me.role === "admin");
  $("qr-export").href = `/api/engagements/${id}/quality-review/export`;
  $("qr-cards").innerHTML = !qr.required ? "" : [
    [`${qr.answered}/${qr.questions.length}`, "Questions answered", false],
    [qr.flags.length, "Answers needing a second look", qr.flags.length > 0],
    [qr.signed_by && !qr.changed_since_signoff ? "Signed off" : "Not signed off", "Sign-off", !qr.signed_by || qr.changed_since_signoff],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("qr-status").innerHTML = !qr.required ? '<p class="muted">No quality review is needed for this engagement.</p>'
    : qr.signed_by
      ? `<p><strong>Signed off</strong> <span class="muted">${data(qr.signed_by)} · <span>${esc(when(qr.signed_at))}</span></span></p>
       ${qr.signed_note ? `<p>${data(qr.signed_note)}</p>` : ""}
       ${qr.changed_since_signoff ? '<p class="warn-box">The quality review changed after the sign-off. The reviewer may want to look at it again.</p>' : ""}`
      : qr.blockers.length ? `<ul class="warn-list">${qr.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : "";
  if (qr.reviewer_on_team) $("qr-status").insertAdjacentHTML("beforeend", '<p class="warn-box">The quality reviewer is on the engagement team. The reviewer should be someone outside the team.</p>');
  $("qr-who").innerHTML = !qr.required ? "" : qr.reviewer_name
    ? `<span>Quality reviewer:</span> ${data(qr.reviewer_name)}${qr.reason_label ? ` · <span>${esc(qr.reason_label)}</span>` : ""}${fills ? "" : ' · <span>Only the quality reviewer fills in the checklist.</span>'}`
    : "";
  $("qr-check").hidden = !qr.required;
  $("qr-sign").hidden = !qr.required || !qr.is_reviewer && !qr.signed_by;
  $("qr-sign-btn").hidden = !qr.is_reviewer || Boolean(qr.signed_by) && !qr.changed_since_signoff;
  $("qr-unsign").hidden = !qr.signed_by || !(qr.is_reviewer || me.role === "admin");
  $("qr-saved").innerHTML = qr.updated_by ? `<span>Last saved by</span> ${data(qr.updated_by)} · <span>${esc(when(qr.updated_at))}</span>` : "";
  const setup = $("qr-setup");
  if (!(setup.dataset.engagement === String(id) && setup.dataset.dirty)) {
    setup.dataset.engagement = id;
    $("qr-required").checked = qr.required;
    $("qr-reason").innerHTML = '<option value="">Pick a reason</option>' + optionList(qr.reasons, qr.reason);
    $("qr-reviewer").innerHTML = '<option value="">Not named yet</option>' + qr.reviewers.map((u) =>
      `<option value="${u.id}" translate="no" ${u.id === qr.reviewer_id ? "selected" : ""}>${esc(u.name)}</option>`).join("");
    $("qr-reason-note").value = qr.reason_note;
  }
  const form = $("qr-form");
  for (const el of form.elements) el.disabled = !fills;
  if (form.dataset.engagement === String(id) && form.dataset.dirty) return;
  form.dataset.engagement = id;
  $("qr-questions").innerHTML = qr.questions.map((q, i) => {
    const a = qr.answers[q.id];
    const flagged = qr.flags.includes(q.id);
    return `<div class="ac-q${flagged ? " flagged" : ""}" data-q="${esc(q.id)}">
      <p><bdi>${i + 1}.</bdi> <span>${esc(q.text)}</span></p>
      <fieldset class="checks-inline"><legend class="visually-hidden">Answer</legend>
        ${Object.entries(ANSWER_LABELS).map(([v, label]) => `<label><input type="radio" name="qr-${esc(q.id)}" value="${v}" ${a.answer === v ? "checked" : ""} ${fills ? "" : "disabled"}><span>${label}</span></label>`).join("")}
      </fieldset>
      <input data-comment maxlength="5000" value="${esc(a.comment)}" placeholder="Comment" aria-label="Comment" ${fills ? "" : "disabled"}>
      ${flagged ? '<div class="tag flag-note">This answer needs a second look before signing off.</div>' : ""}
    </div>`;
  }).join("");
  $("qr-conclusion").value = qr.conclusion;
}

async function qualityChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadQuality();
  refreshDashboard();
  if (rp?.engagement_id === id) guard(loadReport);  // the report warns while it isn't signed
  await loadEngagementActivity();
}

$("qr-setup").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!qr) return;
  const id = qr.engagement_id;
  const reviewer = $("qr-reviewer").value;
  const body = { required: $("qr-required").checked, reason: $("qr-reason").value || null,
    reason_note: $("qr-reason-note").value, reviewer_id: reviewer ? Number(reviewer) : null };
  if (qr.signed_by && body.reviewer_id !== qr.reviewer_id && !confirm("A new reviewer signs off afresh. Change the reviewer?")) return;
  const rev = $("qr-setup").dataset.rev;
  busy($("qr-setup-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/quality-review/setup`, body);
    if (currentEngagement?.id !== id) return;
    if ($("qr-setup").dataset.rev === rev) delete $("qr-setup").dataset.dirty;
    await qualityChanged(id);
  });
});
$("qr-setup").addEventListener("input", () => formEdited($("qr-setup")));

$("qr-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!qr) return;
  const id = qr.engagement_id;
  const answers = {};
  for (const q of $("qr-questions").querySelectorAll("[data-q]")) {
    const picked = q.querySelector("input[type=radio]:checked");
    answers[q.dataset.q] = { answer: picked ? picked.value : null, comment: q.querySelector("[data-comment]").value };
  }
  const rev = $("qr-form").dataset.rev;
  busy($("qr-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/quality-review`, { answers, conclusion: $("qr-conclusion").value });
    if (currentEngagement?.id !== id) return;
    if ($("qr-form").dataset.rev === rev) delete $("qr-form").dataset.dirty;
    await qualityChanged(id);
  });
});
$("qr-form").addEventListener("input", () => formEdited($("qr-form")));

$("qr-sign").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!qr) return;
  const id = qr.engagement_id;
  busy($("qr-sign-btn"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/quality-review/signoff`, { note: $("qr-sign-note").value });
    $("qr-sign-note").value = "";
    await qualityChanged(id);
  });
});

$("qr-unsign").addEventListener("click", () => {
  if (!qr || !confirm("Take back the quality review sign-off?")) return;
  const id = qr.engagement_id;
  busy($("qr-unsign"), async () => {
    await sendJSON("DELETE", `/api/engagements/${id}/quality-review/signoff`);
    await qualityChanged(id);
  });
});

// The going concern signs and the suggested opinion follow the figures: reload both panels
// (keeping unsaved edits) after the mapping, adjusting entries or materiality change.
function refreshFinishing(id) {
  if (currentEngagement?.id !== id) return;
  if (cp?.engagement_id === id) guard(loadCompletion);
  if (rp?.engagement_id === id) guard(loadReport);
  if (ap?.engagement_id === id && !apEditing) guard(loadExpectations);  // the actuals move too
  if (sc?.engagement_id === id && !scEditing) guard(loadStockCount);  // against materiality
}

// Auditor's report draft

let rp = null;
let rpLoads = 0;

async function loadReport() {
  const id = currentEngagement.id;
  const load = ++rpLoads;
  const r = await getJSON(`/api/engagements/${id}/report`);
  if (load !== rpLoads || currentEngagement?.id !== id) return;
  rp = { ...r, engagement_id: id };
  renderReport();
}

function kamRow(k = { title: "", why: "", how: "" }) {
  return `<div class="rp-kam" data-kam>
    <label>Matter</label><input data-kam-title maxlength="300" value="${esc(k.title)}" aria-label="Matter">
    <label>Why it mattered</label><textarea data-kam-why rows="2" maxlength="5000" aria-label="Why it mattered">${esc(k.why)}</textarea>
    <label>How the audit dealt with it</label><textarea data-kam-how rows="2" maxlength="5000" aria-label="How the audit dealt with it">${esc(k.how)}</textarea>
    <div class="actions"><button type="button" class="small secondary danger" data-kam-remove>Remove</button></div></div>`;
}

function renderReport() {
  if (!rp) {
    for (const t of ["rp-suggestion", "rp-warnings", "rp-kams", "rp-preview"]) $(t).innerHTML = "";
    $("rp-form").reset();
    delete $("rp-form").dataset.dirty;
    delete $("rp-form").dataset.engagement;
    $("rp-saved").textContent = "";
    $("rp-word-en").removeAttribute("href");
    $("rp-word-ar").removeAttribute("href");
    return;
  }
  const id = rp.engagement_id;
  $("rp-word-en").href = `/api/engagements/${id}/report/download?language=en`;
  $("rp-word-ar").href = `/api/engagements/${id}/report/download?language=ar`;
  const s = rp.suggestion;
  $("rp-suggestion").innerHTML = `<p><span>The file points to:</span> <strong>${esc(s.label)}</strong></p>
    ${s.reasons.length ? `<ul>${s.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${s.going_concern_section ? '<p class="muted">The report includes a section on the material uncertainty about going concern.</p>' : ""}`;
  $("rp-warnings").hidden = !rp.warnings.length;
  $("rp-warnings").innerHTML = rp.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  $("rp-saved").innerHTML = rp.updated_by ? `<span>Last saved by</span> ${data(rp.updated_by)} · <span>${esc(when(rp.updated_at))}</span>` : "";
  if ($("rp-preview-box").open) guard(loadReportPreview);
  // Keep edits not yet saved when the completion work changes the suggestion.
  const form = $("rp-form");
  if (form.dataset.engagement === String(id) && form.dataset.dirty) return;
  form.dataset.engagement = id;
  $("rp-opinion").innerHTML = optionList(rp.opinions, rp.opinion);
  $("rp-entity").value = rp.entity_name;
  $("rp-addressee").value = rp.addressee;
  $("rp-framework").value = rp.framework_name;
  $("rp-firm").value = rp.firm_name;
  $("rp-partner").value = rp.partner_name;
  $("rp-city").value = rp.city;
  $("rp-date").value = rp.report_date || "";
  $("rp-basis").value = rp.basis;
  $("rp-emphasis").value = rp.emphasis.join("\n");
  $("rp-other").value = rp.other_matter;
  $("rp-kams").innerHTML = rp.key_matters.map(kamRow).join("");
}

async function loadReportPreview() {
  if (!rp) return;
  const id = rp.engagement_id;
  const lang = I18N.lang === "ar" ? "ar" : "en";
  const p = await getJSON(`/api/engagements/${id}/report/preview?language=${lang}`);
  if (rp?.engagement_id !== id || (I18N.lang === "ar" ? "ar" : "en") !== lang) return;
  const tag = { title: "h3", heading: "h4", subheading: "h5" };
  $("rp-preview").dir = lang === "ar" ? "rtl" : "ltr";
  $("rp-preview").innerHTML = p.paragraphs.map((x) => `<${tag[x.style] || "p"} class="rp-${esc(x.style)}" translate="no">${esc(x.text).replace(/\n/g, "<br>")}</${tag[x.style] || "p"}>`).join("");
}

document.addEventListener("languagechange", () => { if (rp && $("rp-preview-box").open) guard(loadReportPreview); });
$("rp-preview-box").addEventListener("toggle", () => { if ($("rp-preview-box").open) guard(loadReportPreview); });
// Edits not yet saved: `dirty` keeps them through a refresh, and `rev` counts them, so a save
// only clears `dirty` when nothing was typed while it was on its way.
function formEdited(form) {
  form.dataset.dirty = "1";
  form.dataset.rev = String(Number(form.dataset.rev || 0) + 1);
}
$("rp-form").addEventListener("input", () => formEdited($("rp-form")));
$("rp-kam-add").addEventListener("click", () => {
  $("rp-kams").insertAdjacentHTML("beforeend", kamRow());
  formEdited($("rp-form"));
});
$("rp-kams").addEventListener("click", (ev) => {
  if (!ev.target.matches("[data-kam-remove]")) return;
  ev.target.closest("[data-kam]").remove();
  formEdited($("rp-form"));
});

$("rp-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!rp) return;
  const id = rp.engagement_id;
  const body = {
    opinion: $("rp-opinion").value, entity_name: $("rp-entity").value, addressee: $("rp-addressee").value,
    framework_name: $("rp-framework").value, firm_name: $("rp-firm").value, partner_name: $("rp-partner").value,
    city: $("rp-city").value, report_date: $("rp-date").value || null, basis: $("rp-basis").value,
    emphasis: $("rp-emphasis").value.split("\n").map((x) => x.trim()).filter(Boolean),
    other_matter: $("rp-other").value,
    key_matters: [...$("rp-kams").querySelectorAll("[data-kam]")].map((k) => ({
      title: k.querySelector("[data-kam-title]").value.trim(), why: k.querySelector("[data-kam-why]").value,
      how: k.querySelector("[data-kam-how]").value })).filter((k) => k.title || k.why.trim() || k.how.trim()),
  };
  const rev = $("rp-form").dataset.rev;
  busy($("rp-save"), async () => {
    await sendJSON("PUT", `/api/engagements/${id}/report`, body);
    if (currentEngagement?.id !== id) return;
    if ($("rp-form").dataset.rev === rev) delete $("rp-form").dataset.dirty;  // else keep newer edits
    await loadReport();
    refreshDashboard();
    await loadEngagementActivity();
  });
});

// Related parties

// Text someone is typing in a list that is about to be redrawn (another row's save reloads
// it), to put back afterwards with the focus where it was.
function keepListDrafts(container, selector, key) {
  const drafts = new Map();
  let focused = null;
  container.querySelectorAll(selector).forEach((el) => {
    if (el.value !== el.defaultValue) drafts.set(key(el), el.value);
    if (el === document.activeElement) focused = key(el);
  });
  return () => container.querySelectorAll(selector).forEach((el) => {
    const k = key(el);
    if (drafts.has(k)) el.value = drafts.get(k);
    if (k === focused) el.focus();
  });
}

let rpt = null;       // the register on show: {engagement_id, parties, summary, ...}
let rptLoads = 0;
let rptFound = null;  // what the last scan found, for the engagement it ran on

async function loadRelated() {
  const id = currentEngagement.id;
  const load = ++rptLoads;
  const r = await getJSON(`/api/engagements/${id}/related-parties`);
  if (load !== rptLoads || currentEngagement?.id !== id) return;
  rpt = { ...r, engagement_id: id };
  renderRelated();
}

function renderRelated() {
  if (!rpt) {
    for (const t of ["rpt-cards", "rpt-table", "rpt-found", "rpt-ledger"]) $(t).innerHTML = "";
    $("rpt-export").removeAttribute("href");
    return;
  }
  const restore = keepListDrafts($("rpt-table"), "[data-rpt-conclusion]", (el) => el.closest("[data-party]").dataset.party);
  const s = rpt.summary;
  $("rpt-export").href = `/api/engagements/${rpt.engagement_id}/related-parties/export`;
  $("rpt-cards").innerHTML = [
    [s.parties, "Related parties", false], [s.with_matches, "With transactions found", false],
    [s.to_review, "Transactions to review", s.to_review > 0],
    [s.missing_disclosure, "Not disclosed", s.missing_disclosure > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v"><bdi dir="ltr">${esc(v)}</bdi></div><div class="k">${esc(k)}</div></div>`).join("");
  if (!$("rpt-rel").options.length) $("rpt-rel").innerHTML = optionList(rpt.relationships, "other");
  const keepLedger = $("rpt-ledger").value;
  $("rpt-ledger").innerHTML = rpt.ledgers.map((u) => `<option value="${u.id}">${data(u.filename)}</option>`).join("");
  if (rpt.ledgers.some((u) => String(u.id) === keepLedger)) $("rpt-ledger").value = keepLedger;
  else if (rpt.ledgers.length) $("rpt-ledger").value = String(rpt.ledgers[rpt.ledgers.length - 1].id);
  $("rpt-scan").disabled = !rpt.ledgers.length || !rpt.parties.length;
  $("rpt-table").innerHTML = rpt.parties.length
    ? `<thead><tr><th>Name</th><th>Relationship</th><th class="num">Found</th><th>Conclusion on the transactions</th><th>Reviewed</th><th>Disclosure</th><th></th></tr></thead><tbody>
      ${rpt.parties.map((p) => `<tr data-party="${p.id}"${p.disclosed === "missing" || (p.matches && !p.reviewed_at) ? ' class="flagged"' : ""}>
        <td>${data(p.name)}${p.aliases.length ? `<div class="tag"><span>Also:</span> ${p.aliases.map(data).join(", ")}</div>` : ""}${p.note ? `<div class="tag">${data(p.note)}</div>` : ""}${p.searchable ? "" : '<div class="tag">Too short to search for</div>'}</td>
        <td>${esc(p.relationship_label)}</td>
        <td class="num">${p.scanned_at ? `<bdi dir="ltr">${p.matches}</bdi>` : '<span class="muted">Not scanned</span>'}</td>
        <td><textarea data-rpt-conclusion rows="2" maxlength="5000" aria-label="Conclusion">${esc(p.conclusion)}</textarea></td>
        <td><label><input type="checkbox" data-rpt-reviewed ${p.reviewed_at ? "checked" : ""}> <span>Reviewed</span></label>${p.reviewed_by ? `<div class="tag">${data(p.reviewed_by)}</div>` : ""}</td>
        <td><select data-rpt-disclosed class="map" aria-label="Disclosure">${optionList(rpt.disclosed, p.disclosed)}</select></td>
        <td>${me.rank >= 2 || (!p.matches && !p.reviewed_at) ? '<button type="button" class="small secondary danger" data-rpt-delete>Remove</button>' : ""}</td></tr>`).join("")}</tbody>`
    : '<tbody><tr><td class="muted">No related parties yet.</td></tr></tbody>';
  restore();
  renderRelatedFound();
}

function renderRelatedFound() {
  const f = rptFound?.engagement_id === rpt?.engagement_id ? rptFound : null;
  if (!f) { $("rpt-found").innerHTML = ""; return; }
  const hits = f.parties.filter((p) => p.matches);
  $("rpt-found").innerHTML = `<h3>Found in <bdi translate="no">${esc(f.filename)}</bdi></h3>` + (hits.length
    ? hits.map((p) => `<details><summary>${data(p.name)} · <bdi dir="ltr">${p.matches}</bdi> <span>${p.matches === 1 ? "entry" : "entries"}</span>${p.flagged ? ` · <span class="pill warn"><bdi dir="ltr">${p.flagged}</bdi> <span>flagged</span></span>` : ""}</summary>
      <div class="table-wrap"><table><thead><tr><th>Date</th><th>Entry</th><th>Account</th><th>Description</th><th class="num">Amount</th><th></th></tr></thead><tbody>
      ${p.rows.map((r) => `<tr><td class="nowrap">${r.date ? `<bdi dir="ltr">${esc(r.date)}</bdi>` : ""}</td><td>${data(r.entry_id)}</td><td>${data(r.account)}</td>
        <td>${data(r.description)}</td><td class="num">${money(r.amount)}</td><td>${r.flags.map((x) => `<span class="pill warn">${esc(x)}</span>`).join(" ")}</td></tr>`).join("")}
      ${p.rows.length < p.matches ? `<tr><td colspan="6" class="muted"><span>Showing the largest</span> <bdi dir="ltr">${p.rows.length}</bdi>. <span>The Excel download has them all.</span></td></tr>` : ""}</tbody></table></div></details>`).join("")
    : '<p class="muted">None of the names appear in the ledger.</p>');
}

async function relatedChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadRelated();
  refreshDashboard();
  await loadEngagementActivity();
}

$("rpt-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!rpt) return;
  const id = rpt.engagement_id;
  const body = { name: $("rpt-name").value, relationship: $("rpt-rel").value, note: $("rpt-note").value,
    aliases: $("rpt-aliases").value.split("\n").map((x) => x.trim()).filter(Boolean) };
  busy($("rpt-save"), async () => {
    await sendJSON("POST", `/api/engagements/${id}/related-parties`, body);
    if (currentEngagement?.id !== id) return;
    $("rpt-name").value = $("rpt-aliases").value = $("rpt-note").value = "";
    await relatedChanged(id);
  });
});

$("rpt-table").addEventListener("change", (ev) => {
  const row = ev.target.closest("[data-party]");
  if (!row || !rpt) return;
  const id = rpt.engagement_id;
  let body = null;
  if (ev.target.matches("[data-rpt-conclusion]")) body = { conclusion: ev.target.value };
  if (ev.target.matches("[data-rpt-disclosed]")) body = { disclosed: ev.target.value };
  if (ev.target.matches("[data-rpt-reviewed]")) {
    body = { reviewed: ev.target.checked };
    if (ev.target.checked) body.conclusion = row.querySelector("[data-rpt-conclusion]").value;
  }
  if (!body) return;
  guard(async () => {
    try {
      await sendJSON("PATCH", `/api/related-parties/${row.dataset.party}`, body);
      await relatedChanged(id);
    } catch (err) {
      if (rpt?.engagement_id === id && ev.target.matches("[data-rpt-reviewed]")) ev.target.checked = !ev.target.checked;
      throw err;
    }
  });
});

$("rpt-table").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-party]");
  if (!row || !rpt || !ev.target.matches("[data-rpt-delete]")) return;
  if (!confirm("Remove this related party?")) return;
  const id = rpt.engagement_id;
  guard(async () => {
    await sendJSON("DELETE", `/api/related-parties/${row.dataset.party}`);
    await relatedChanged(id);
  });
});

$("rpt-scan-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  if (!rpt) return;
  const id = rpt.engagement_id;
  busy($("rpt-scan"), async () => {
    const r = await sendJSON("POST", `/api/engagements/${id}/related-parties/scan`, { upload_id: intOrNull($("rpt-ledger").value) });
    if (currentEngagement?.id !== id) return;
    rptFound = { ...r.scan, engagement_id: id };
    await relatedChanged(id);
  });
});

// Contracts and minutes, summarised by the AI

let ds = null;
let dsLoads = 0;
let dsReading = 0;  // summaries still waiting on the AI, even for another engagement

async function loadSummaries() {
  const id = currentEngagement.id;
  const load = ++dsLoads;
  const r = await getJSON(`/api/engagements/${id}/document-summaries`);
  if (load !== dsLoads || currentEngagement?.id !== id) return;
  ds = { ...r, engagement_id: id };
  renderSummaries();
}

function renderSummaries() {
  const off = !(ds ? ds.enabled : assistant.enabled);
  $("ds-off").hidden = !off;
  $("ds-form").querySelectorAll("select, input, button").forEach((el) => { el.disabled = off; });
  if (dsReading) $("ds-run").disabled = true;
  if (!ds) {
    $("ds-list").innerHTML = "";
    $("ds-export").removeAttribute("href");
    return;
  }
  $("ds-export").href = `/api/engagements/${ds.engagement_id}/document-summaries/export`;
  if (!$("ds-kind").options.length) $("ds-kind").innerHTML = optionList(ds.kinds, "contract");
  const restore = keepListDrafts($("ds-list"), "[data-ds-note]", (el) => el.closest("[data-ds]").dataset.ds);
  const list = (title, items) => items.length ? `<h4>${esc(title)}</h4><ul>${items.join("")}</ul>` : "";
  $("ds-list").innerHTML = ds.summaries.length ? ds.summaries.map((s) => {
    const c = s.content;
    return `<article class="ds-item${s.reviewed_at ? "" : " flagged"}" data-ds="${s.id}">
      <h3>${data(c.title || s.filename)}</h3>
      <p class="muted"><span>${esc(s.kind_label)}</span> · ${data(s.filename)}${c.document_date ? ` · <bdi dir="ltr">${esc(c.document_date)}</bdi>` : ""} · <span>Summarised by</span> ${data(s.created_by)} · <span>${esc(when(s.created_at))}</span></p>
      <p>${data(c.summary)}</p>
      ${s.after_period_end.length ? `<div class="warn-box"><p>Dated after the period end: look at these in the subsequent events review.</p><ul>${s.after_period_end.map((d) => `<li><bdi dir="ltr">${esc(d.date)}</bdi> ${d.own ? "<span>The date of the document itself</span>" : data(d.what)}</li>`).join("")}</ul></div>` : ""}
      ${s.related.length ? `<h4>Parties</h4><ul>${s.related.map((p) => `<li>${data(p.name)} ${p.party ? `<span class="pill warn"><span>In the related parties register:</span> ${data(p.party)}</span>` : `<button type="button" class="small secondary" data-ds-add-party="${esc(p.name)}">Add to related parties</button>`}</li>`).join("")}</ul>` : ""}
      ${list("Points for the audit", (c.audit_points || []).map((p) => `<li><span class="pill">${esc(ds.areas[p.area] || p.area)}</span> ${data(p.point)}</li>`))}
      <details><summary>Key terms, amounts, dates and decisions</summary>
        ${list("Key terms", (c.key_terms || []).map((t) => `<li><strong>${data(t.term)}</strong> ${data(t.detail)}</li>`))}
        ${list("Amounts", (c.amounts || []).map((a) => `<li>${data(a.what)}: <bdi dir="ltr">${a.amount == null ? "" : money(a.amount)} ${esc(a.currency)}</bdi></li>`))}
        ${list("Dates", (c.dates || []).map((d) => `<li><bdi dir="ltr">${esc(d.date)}</bdi> ${data(d.what)}</li>`))}
        ${list("Decisions", (c.decisions || []).map((d) => `<li>${data(d)}</li>`))}
      </details>
      <div class="grid">
        <div><label for="ds-note-${s.id}">Your note</label><input id="ds-note-${s.id}" data-ds-note maxlength="5000" value="${esc(s.note)}"></div>
        <div class="actions" style="align-self:end">
          <label><input type="checkbox" data-ds-reviewed ${s.reviewed_at ? "checked" : ""}> <span>Checked against the document</span></label>
          ${s.reviewed_by ? `<span class="muted">${data(s.reviewed_by)}</span>` : ""}
          ${s.created_by_id === me.id || me.rank >= 2 ? '<button type="button" class="small secondary danger" data-ds-delete>Remove</button>' : ""}
        </div>
      </div>
      <p class="muted"><span>Written by the AI assistant</span> (${data(s.model)}).</p>
    </article>`;
  }).join("") : '<p class="muted">No documents summarised yet.</p>';
  restore();
}

async function summariesChanged(id) {
  if (currentEngagement?.id !== id) return;
  await loadSummaries();
  refreshDashboard();
  await loadEngagementActivity();
}

$("ds-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const id = currentEngagement.id;
  const fd = new FormData();
  fd.append("kind", $("ds-kind").value);
  fd.append("language", I18N.lang === "ar" ? "ar" : "en");
  fd.append("file", $("ds-file").files[0]);
  guard(async () => {
    dsReading += 1;
    $("ds-run").disabled = true;
    $("ds-wait").hidden = false;
    try {
      await postForm(`/api/engagements/${id}/document-summaries`, fd);
      if (currentEngagement?.id === id) $("ds-file").value = "";
      await summariesChanged(id);
    } finally {
      dsReading -= 1;
      $("ds-run").disabled = dsReading > 0 || !(ds ? ds.enabled : assistant.enabled);
      $("ds-wait").hidden = dsReading === 0;
    }
  });
});

$("ds-list").addEventListener("change", (ev) => {
  const item = ev.target.closest("[data-ds]");
  if (!item || !ds) return;
  const id = ds.engagement_id;
  const body = ev.target.matches("[data-ds-note]") ? { note: ev.target.value }
    : ev.target.matches("[data-ds-reviewed]") ? { reviewed: ev.target.checked } : null;
  if (!body) return;
  guard(async () => {
    await sendJSON("PATCH", `/api/document-summaries/${item.dataset.ds}`, body);
    await summariesChanged(id);
  });
});

$("ds-list").addEventListener("click", (ev) => {
  const item = ev.target.closest("[data-ds]");
  if (!item || !ds) return;
  const id = ds.engagement_id;
  if (ev.target.matches("[data-ds-delete]")) {
    if (!confirm("Remove this summary?")) return;
    guard(async () => {
      await sendJSON("DELETE", `/api/document-summaries/${item.dataset.ds}`);
      await summariesChanged(id);
    });
  }
  const name = ev.target.closest("[data-ds-add-party]")?.dataset.dsAddParty;
  if (name) {
    guard(async () => {
      await sendJSON("POST", `/api/engagements/${id}/related-parties`, { name, source: `summary:${item.dataset.ds}` });
      await Promise.all([relatedChanged(id), summariesChanged(id)]);
    });
  }
});

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
  refreshDashboard();  // also after a failure, so the overview shows the same files
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
  if (fs.has_prior && Math.abs(st.difference.prior) >= 0.005) cards.push([signed(st.difference.prior), "Prior-year balance sheet out by"]);
  $("fs-cards").innerHTML = cards.map(([v, k]) => `<div class="card"><div class="v">${esc(v)}</div><div class="k">${k}</div></div>`).join("");
  renderMapping();
  renderEntries();
  renderLeadSheets();
  renderStatementTables();
  renderUncorrected();
  renderRatios();
}

["fs-tb", "fs-prior"].forEach((id) => $(id).addEventListener("change", () => {
  $("fs-ai-note").hidden = true;
  guard(loadStatements);
}));

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
  $("fs-ai-map").hidden = !assistant.enabled || !fs.unmapped.length;
  const lineLabel = Object.fromEntries(fs.lines.map((l) => [l.id, l.label]));
  const mapped = fs.accounts.filter((a) => a.line).length;
  $("fs-map-note").textContent = `${mapped} of ${fs.accounts.length} accounts mapped.` +
    (fs.unmapped.length ? " Unmapped accounts are left out of the statements, so the balance sheet won't balance until they are mapped." : "");
  const onlyUnmapped = $("fs-unmapped-only").checked;
  const rows = fs.accounts.filter((a) => !onlyUnmapped || !a.line || a.account in fsMapEdits);
  $("fs-map").innerHTML = "<tr><th>Account</th><th>Name</th><th class=\"num\">Adjusted balance</th><th>Financial statement line</th></tr>" +
    rows.map((a) => {
      const value = a.account in fsMapEdits ? fsMapEdits[a.account] : a.line || "";
      const ai = aiMapHints[a.account];
      const hint = ai && a.account in fsMapEdits
        ? `<div class="tag">AI (${esc(CONFIDENCE[ai.confidence])}): ${data(ai.reason)}</div>`
        : !a.line && a.suggested && !(a.account in fsMapEdits)
          ? `<div class="tag">Suggested: ${esc(lineLabel[a.suggested])}</div>` : "";
      return `<tr data-account="${esc(a.account)}"${a.account in fsMapEdits ? ' class="changed"' : ""}>
        <td>${data(a.account)}${a.in_tb ? "" : ` <span class="tag">(${a.prior != null ? "prior year only" : a.proposed_only ? "proposed entries only" : "from entries"})</span>`}</td><td>${data(a.name)}</td>
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
  if (aiMapHints[account]) {  // their own choice now, not the AI's
    delete aiMapHints[account];
    ev.target.parentElement.querySelector("div.tag")?.remove();
  }
  row.classList.toggle("changed", account in fsMapEdits);
  $("fs-save-map").disabled = !canSaveMapping() || $("fs-save-map").busy > 0;
});
$("fs-unmapped-only").addEventListener("change", renderMapping);
$("fs-accept").addEventListener("click", () => {
  fs.accounts.filter((a) => !a.line && a.suggested && !(a.account in fsMapEdits))
    .forEach((a) => { fsMapEdits[a.account] = a.suggested; });
  renderMapping();
});
let aiMapHints = {};  // account -> {reason, confidence} for the AI's unsaved suggestions
const CONFIDENCE = { high: "high confidence", medium: "medium confidence", low: "low confidence" };

$("fs-ai-map").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  const id = fsEngagementId;
  const query = fsQuery();
  const q = Object.fromEntries(new URLSearchParams(query));
  try {
    const r = await sendJSON("POST", `/api/engagements/${id}/mapping/suggest`, {
      upload_id: q.upload_id ? Number(q.upload_id) : null,
      prior_upload_id: q.prior_upload_id ? Number(q.prior_upload_id) : null,
      language: I18N.lang,
    });
    // Drop the answer if they've opened other files, or are saving, while the AI was answering.
    if (currentEngagement.id !== id || fsQuery() !== query || $("fs-save-map").busy > 0) return;
    let used = 0;
    const accounts = new Map(fs.accounts.map((a) => [a.account, a]));
    for (const s of r.suggestions) {
      const a = accounts.get(s.account);
      if (!a || a.line || s.account in fsMapEdits) continue;  // mapped meanwhile, or their own choice
      fsMapEdits[s.account] = s.line;
      aiMapHints[s.account] = s;
      used++;
    }
    $("fs-ai-note").hidden = false;
    $("fs-ai-note").textContent = r.total > r.sent
      ? `The AI suggested a line for ${used} of the first ${r.sent} of ${r.total} unmapped accounts. Check them, save, then ask again for the rest.`
      : `The AI suggested a line for ${used} of ${r.sent} unmapped accounts. Check them, then save the mapping.`;
    renderMapping();
  } finally {
    loadEngagementActivity();
  }
}));

const canSaveMapping = () => Object.keys(fsMapEdits).length > 0;
$("fs-save-map").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  const id = fsEngagementId;
  if (id !== currentEngagement.id) return;
  const mappings = Object.fromEntries(Object.entries(fsMapEdits).map(([k, v]) => [k, v || null]));
  await sendJSON("PUT", `/api/engagements/${id}/mapping`, { mappings });
  if (currentEngagement.id !== id) return;  // the user has opened another engagement
  fsMapEdits = {};
  aiMapHints = {};
  $("fs-ai-note").hidden = true;
  await loadStatements();
  refreshFinishing(id);
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
        <td>${data(e.description)}</td>
        <td class="nowrap">${e.lines.map((l) => `${l.debit ? "<span>Dr</span>" : "&nbsp;&nbsp;&nbsp;&nbsp;<span>Cr</span>"} ${data(l.account)} ${money(l.debit || l.credit)}${l.memo ? ` <span class="tag">${data(l.memo)}</span>` : ""}`).join("<br>")}</td>
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
    refreshFinishing(id);
    await loadEngagementActivity();
  });
});
$("aje-list").addEventListener("click", (ev) => {
  const book = ev.target.closest("[data-book]"), del = ev.target.closest("[data-delete-entry]");
  if (book) guard(async () => {
    await sendJSON("PATCH", `/api/adjusting-entries/${book.dataset.book}`, { kind: "adjusting" });
    await loadStatements();
    refreshFinishing(currentEngagement?.id);
    await loadEngagementActivity();
  });
  if (del) {
    const e = fs.entries.find((x) => String(x.id) === del.dataset.deleteEntry);
    if (!confirm(`Delete ${e.ref}: ${e.description}?`)) return;
    guard(async () => {
      await sendJSON("DELETE", `/api/adjusting-entries/${e.id}`);
      await loadStatements();
      refreshFinishing(currentEngagement?.id);
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
    s.accounts.map((a) => `<tr><td>${data(a.account)}</td><td>${data(a.name)}</td>${nums(a)}</tr>`).join("") +
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
  const priorDiff = fs.has_prior ? fs.statements.difference.prior : 0;
  const curOut = Math.abs(diff) >= 0.005, priorOut = Math.abs(priorDiff) >= 0.005;
  $("fs-diff").hidden = !curOut && !priorOut;
  const tbOut = Math.abs(fs.totals.adjusted) >= 0.005;
  const priorTbOut = fs.has_prior && Math.abs(fs.totals.prior) >= 0.005;
  const n = fs.unmapped.length;
  const mapHint = n ? `${n} account${n === 1 ? " isn't" : "s aren't"} mapped yet; map ${n === 1 ? "it" : "them"} on the Account mapping tab.` : "";
  // Each year in its own sentence, so each is translated on its own.
  const parts = [];
  if (curOut) parts.push(`The balance sheet is out by ${signed(diff)}. ` + [
    mapHint,
    tbOut ? `The adjusted trial balance itself doesn't balance (it totals ${drcr(fs.totals.adjusted)}).` : "",
  ].filter(Boolean).join(" "));
  if (priorOut) parts.push(`The prior-year balance sheet is out by ${signed(priorDiff)}.` + [
    !curOut && mapHint ? ` ${mapHint}` : "",
    priorTbOut ? ` The prior-year trial balance itself doesn't balance (it totals ${drcr(fs.totals.prior)}).` : "",
  ].join(""));
  $("fs-diff").innerHTML = parts.map((t) => `<span>${esc(t)}</span>`).join(" ");
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
      u.entries.map((e) => `<tr><td>${esc(e.ref)}</td><td>${data(e.description)}</td><td class="num">${signed(e.profit)}</td><td class="num">${signed(e.net_assets)}</td></tr>`).join("")
    : '<tr><td class="muted">No proposed entries. Post one with type "Proposed" on the Adjusting entries tab.</td></tr>';
}

// Sampling

let samplingOptions = null;

function renderSampleForm(e) {
  $("smp-upload").innerHTML = e.uploads.filter((u) => !["prior_tb", ...LIST_KINDS].includes(u.kind))
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a file first</option>';
  const pm = e.materiality?.performance_materiality;
  $("smp-tm").placeholder = pm ? `${money(pm)} (performance materiality)` : "Amount";
  // Keep the open sample (and any results typed but not saved) unless it has gone.
  if (!currentSample || !e.samples.some((x) => x.id === currentSample.id)) {
    $("smp-result").hidden = true;
    currentSample = null;
  }
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
  $("samples").innerHTML = list.length ? "<tr><th>#</th><th>When</th><th>File</th><th>Method</th><th>Items</th><th>Value</th><th>Seed</th><th>Results</th><th></th></tr>" +
    list.map((s) => `<tr><td>${s.id}</td><td class="nowrap">${esc(when(s.created_at))}</td>
      <td>${data(s.filename)}</td><td>${esc(s.method_label)}</td>
      <td class="num">${s.sample_size} of ${s.population.lines}</td>
      <td class="num">${money(s.sample_value)}</td><td class="num">${s.seed}</td>
      <td>${s.evaluation ? `<span class="pill ev-${s.evaluation.status}">${esc(s.evaluation.label)}</span>` : '<span class="muted">Not recorded</span>'}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-view-smp="${s.id}">View</button>
        <a class="button" href="/api/samples/${s.id}/export">Excel</a>
      </div></td></tr>`).join("") : "";
}

$("samples").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-view-smp]");
  if (btn) guard(async () => showSample(await getJSON(`/api/samples/${btn.dataset.viewSmp}`)));
});

let currentSample = null;

function showSample(s, scroll = true) {
  currentSample = s;
  const r = s.result;
  const d = r.details;
  $("smp-result").hidden = false;
  $("smp-title").textContent = `Sample #${s.id}: ${r.method_label} from ${s.filename}`;
  $("smp-actions").innerHTML = `<a class="button" href="/api/samples/${s.id}/export">Download workpaper</a>`;
  const cards = [
    ["Items selected", r.sample_size], ["Value selected", money(r.sample_value)],
    ["Population lines", r.population.lines.toLocaleString(LOCALE)], ["Population value", money(r.population.value)],
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
  const ev = s.evaluation;
  const tested = Object.fromEntries((ev?.items || []).map((i) => [i.row, i]));
  $("smp-table").innerHTML = `<tr><th>Row</th><th>Entry</th><th>Date</th><th>Account</th><th>Description</th><th>Amount</th>${hits ? "<th>Hits</th>" : ""}<th>Why selected</th><th>Audited amount</th><th>Misstatement</th><th>Notes</th></tr>` +
    r.items.map((i) => {
      const t = tested[i.row] || {};
      return `<tr data-row="${i.row}" data-book="${Math.abs(i.amount)}"><td class="num">${i.row}</td><td>${esc(i.entry_id)}</td>
      <td class="nowrap">${esc(i.date)}</td><td>${data(i.account)}</td><td>${data(i.description)}</td>
      <td class="num">${money(i.amount)}</td>${hits ? `<td class="num">${i.hits}</td>` : ""}
      <td>${esc(i.reason)}</td>
      <td><input type="number" min="0" step="any" class="ev-audited" aria-label="Audited amount" value="${t.audited_amount ?? ""}" style="width:9em"></td>
      <td class="num${t.misstatement ? " bad" : ""}">${t.misstatement == null ? "" : money(t.misstatement)}</td>
      <td><input class="ev-notes" maxlength="1000" aria-label="Notes" value="${esc(t.notes || "")}" style="min-width:12em"></td></tr>`;
    }).join("");
  renderEvaluation(s);
  if (scroll) $("smp-result").scrollIntoView({ behavior: "smooth" });
}

function renderEvaluation(s) {
  const ev = s.evaluation;
  $("smp-ev-tm").value = ev?.tolerable_entered ?? "";
  $("smp-ev-tm").placeholder = s.default_tolerable ? `${money(s.default_tolerable)} (from the sample or performance materiality)` : "Amount";
  if (!ev) {
    $("smp-ev-cards").innerHTML = "";
    $("smp-ev-conclusion").textContent = "No results recorded yet.";
    $("smp-ev-conclusion").className = "muted";
    return;
  }
  const cards = [
    ["Items tested", `${ev.tested} of ${ev.tested + ev.untested}`],
    ["Items with a misstatement", ev.exceptions],
    ["Known misstatement", money(ev.known_misstatement)],
    ];
  if (ev.basis === "ratio") cards.push(["Projected overstatement", money(ev.projected_overstatement)],
    ["Projected understatement", money(ev.projected_understatement)]);
  else cards.push(["Projected misstatement", money(ev.projected_misstatement)]);
  if (ev.basis === "mus") cards.push(["Upper limit (overstatement)", money(ev.upper_limit)],
    ["Upper limit (understatement)", money(ev.upper_limit_understatement)]);
  if (ev.tolerable_misstatement) cards.push(["Tolerable misstatement", money(ev.tolerable_misstatement)]);
  $("smp-ev-cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const how = ev.basis === "mus"
    ? `Overstatements and understatements each get an upper limit at ${ev.confidence}% confidence: basic precision ${money(ev.basic_precision)} plus the projected errors and their allowance. The conclusion uses the larger.`
    : "Overstatements and understatements are each projected by applying them, as a share of the value tested, to the whole population, so they can't cancel out. This is a non-statistical estimate.";
  $("smp-ev-conclusion").innerHTML = `<strong>${esc(ev.conclusion.label)}</strong><br><span class="muted">${esc(how)}</span><br><span class="muted">${esc(`Recorded by ${ev.updated_by}, ${when(ev.updated_at)}`)}</span>`;
  $("smp-ev-conclusion").className = ev.conclusion.status === "exceeds" ? "warn-box" : "";
}

$("smp-ev-all").addEventListener("click", () => {
  $("smp-table").querySelectorAll("tr[data-row]").forEach((tr) => {
    const input = tr.querySelector(".ev-audited");
    if (input.value === "") input.value = tr.dataset.book;
  });
});

$("smp-ev-save").addEventListener("click", (ev) => busy(ev.currentTarget, async () => {
  const items = [...$("smp-table").querySelectorAll("tr[data-row]")].map((tr) => {
    const audited = tr.querySelector(".ev-audited").value;
    return { row: Number(tr.dataset.row), audited_amount: audited === "" ? null : Number(audited),
             notes: tr.querySelector(".ev-notes").value.trim() };
  });
  const tm = $("smp-ev-tm").value;
  const engagementId = currentEngagement.id, sampleId = currentSample.id;
  const s = await sendJSON("PUT", `/api/samples/${sampleId}/evaluation`,
    { items, tolerable_misstatement: tm === "" ? null : Number(tm) });
  if (currentEngagement?.id !== engagementId) return;  // they moved on to another engagement
  await openEngagement(engagementId);  // refreshes the samples list
  // Redraw only if this sample is still the one open, so edits to another aren't lost.
  if (currentEngagement?.id === engagementId && currentSample?.id === sampleId) showSample(s, false);
}));

// Stratification and summaries

let strat = null;
let stratTab = "strata";

function renderStratifyForm(e) {
  const keep = $("str-upload").value;
  const files = e.uploads.filter((u) => !["prior_tb", ...LIST_KINDS].includes(u.kind));
  $("str-upload").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a file first</option>';
  if (files.some((u) => String(u.id) === keep)) $("str-upload").value = keep;
  if (strat && !files.some((u) => u.id === strat.upload_id)) {
    strat = null; $("str-result").hidden = $("str-export").hidden = true;
  }
}

$("str-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const button = ev.submitter || ev.currentTarget.querySelector("button[type=submit]");
  busy(button, async () => {
    const text = $("str-bands").value.trim();
    const bands = text ? text.split(",").map((b) => b.trim()).filter(Boolean).map(Number) : null;
    if (bands && bands.some((b) => !Number.isFinite(b))) throw new Error("Bands must be amounts separated by commas.");
    const id = Number($("str-upload").value);
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/stratify`, { bands });
    if (currentEngagement?.id !== engagementId) return;  // they moved on to another engagement
    strat = out;
    $("str-export").href = `/api/uploads/${id}/stratify/export?bands=${encodeURIComponent(strat.bands.join(","))}`;
    $("str-export").hidden = false;
    renderStrat();
  });
});

$("str-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-str]");
  if (!tab) return;
  stratTab = tab.dataset.str;
  renderStrat();
});

function bandLabel(b) {
  return b.to == null ? `${money(b.from)} and above` : `${money(b.from)} to under ${money(b.to)}`;
}

function renderStrat() {
  $("str-result").hidden = !strat;
  if (!strat) return;
  $("str-tabs").querySelectorAll("[data-str]").forEach((b) => b.setAttribute("aria-selected", b.dataset.str === stratTab));
  const t = strat.totals;
  const cards = [["Lines", t.lines.toLocaleString(LOCALE)], ["Debits", money(t.debits)], ["Credits", money(t.credits)]];
  if (strat.summaries.account) cards.push(["Accounts", strat.summaries.account.count]);
  if (strat.summaries.user) cards.push(["Users", strat.summaries.user.count]);
  $("str-cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  let note = "", html = "";
  if (stratTab === "strata") {
    note = "Each line falls in one band by the size of its amount, debit or credit.";
    html = "<tr><th>Band</th><th>Lines</th><th>% of lines</th><th>Debits</th><th>Credits</th><th>Value</th><th>% of value</th></tr>" +
      strat.strata.map((b) => `<tr><td class="nowrap">${esc(bandLabel(b))}</td><td class="num">${b.lines.toLocaleString(LOCALE)}</td>
        <td class="num">${b.lines_pct}%</td><td class="num">${money(b.debits)}</td><td class="num">${money(b.credits)}</td>
        <td class="num">${money(b.value)}</td><td class="num">${b.value_pct}%</td></tr>`).join("");
  } else if (stratTab === "fields") {
    note = "Statistics for each column the app read from the file.";
    html = "<tr><th>Field</th><th>Filled</th><th>Blank</th><th>Distinct</th><th>Details</th></tr>" +
      strat.fields.map((f) => {
        let detail = "";
        if (f.kind === "number") detail = `Total ${money(f.total)} · min ${money(f.min)} · max ${money(f.max)} · average ${money(f.average)}`;
        else if (f.kind === "date") detail = `${f.earliest} to ${f.latest}`;
        else if (f.kind === "text") detail = `<span>Most common:</span> ${data(f.most_common)} <span>(${f.most_common_lines} lines)</span>`;
        return `<tr><td>${esc(f.label)}</td><td class="num">${f.filled}</td><td class="num${f.blank && !["debit", "credit"].includes(f.field) ? " bad" : ""}">${f.blank}</td>
          <td class="num">${f.distinct}</td><td>${f.kind === "text" ? detail : esc(detail)}</td></tr>`;
      }).join("");
  } else {
    const sum = strat.summaries[stratTab];
    const first = { account: "Account", user: "User", month: "Month" }[stratTab];
    if (!sum) {
      note = { account: "This file has no account column.", user: "This file has no user column.", month: "This file has no date column." }[stratTab];
    } else {
      note = sum.truncated ? `Showing the first ${sum.rows.length} of ${sum.count}. The Excel download has them all.` : "";
      html = `<tr><th>${first}</th><th>Lines</th><th>Debits</th><th>Credits</th><th>Net</th><th>Largest line</th></tr>` +
        sum.rows.map((r) => `<tr><td>${data(r.key)}</td><td class="num">${r.lines.toLocaleString(LOCALE)}</td>
          <td class="num">${money(r.debits)}</td><td class="num">${money(r.credits)}</td>
          <td class="num">${money(r.net)}</td><td class="num">${money(r.largest)}</td></tr>`).join("");
    }
  }
  $("str-note").textContent = note;
  $("str-table").innerHTML = html;
}

// Ledger explorer

// Who posted what

let who = null;      // the look on show
let whoTab = "users";

function renderWhoForm(e) {
  const keep = $("who-upload").value;
  const files = e.uploads.filter((u) => u.kind === "gl");
  $("who-upload").innerHTML = files
    .map((u) => `<option value="${u.id}" translate="no">${esc(u.filename)}</option>`)
    .join("") || '<option value="">Upload a general ledger first</option>';
  if (files.some((u) => String(u.id) === keep)) $("who-upload").value = keep;
  if (who && !files.some((u) => u.id === who.upload_id)) resetWho();
}

function resetWho() {
  who = null;
  $("who-result").hidden = $("who-export").hidden = true;
}

$("who-upload").addEventListener("change", resetWho);

$("who-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("who-run"), async () => {
    const id = Number($("who-upload").value);
    if (!id) throw new Error("Upload a general ledger first.");
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/posters`);
    if (currentEngagement?.id !== engagementId) return;
    who = out;
    renderWho();
    await loadEngagementActivity();
  });
});

function renderWho() {
  const w = who;
  $("who-result").hidden = $("who-export").hidden = false;
  $("who-export").href = `/api/uploads/${w.upload_id}/posters/export`;
  const flagged = Object.fromEntries(w.checks.map((c) => [c.check, c]));
  $("who-cards").innerHTML = [
    [w.entries.toLocaleString(LOCALE), "Entries", false],
    [w.users.toLocaleString(LOCALE), "People who posted", false],
    ...w.checks.filter((c) => c.check !== "no_approver" || w.has_approver)
      .map((c) => [c.count.toLocaleString(LOCALE), c.label, c.count > 0]),
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const notes = [];
  if (!w.has_approver) notes.push("The file has no approver column, so approvals can't be checked. Map an Approved by column if the system records one.");
  if (!w.has_entries) notes.push("The file has no entry numbers, so each line counts as its own entry.");
  notes.push(w.mapped ? `${w.lines_known_pct}% of lines have a statement line, from the account mapping or the account's name.`
    : `${w.lines_known_pct}% of lines have a statement line, guessed from the account's name. Map the trial balance for a better result.`);
  if (w.occasional_applies) notes.push(`People with ${w.occasional_limit} entries or fewer count as posting only now and then.`);
  $("who-note").innerHTML = notes.map((n) => `<span>${esc(n)}</span>`).join(" ");
  const tabs = [["users", "By user"], ...w.checks.filter((c) => c.check !== "no_approver" || w.has_approver).map((c) => [c.check, c.label])];
  if (!tabs.some(([k]) => k === whoTab)) whoTab = "users";
  $("who-tabs").innerHTML = tabs.map(([k, label]) =>
    `<button type="button" role="tab" data-who="${k}" aria-selected="${k === whoTab}">${esc(label)}${k !== "users" && flagged[k].count ? ` <span class="nav-count" translate="no">${flagged[k].count}</span>` : ""}</button>`).join("");
  if (whoTab === "users") {
    $("who-table").innerHTML = `<thead><tr><th>User</th><th class="num">Entries</th><th class="num">Share</th><th class="num">Debits</th>
        <th class="num">Largest entry</th><th>First and last</th><th class="num">Weekend</th><th class="num">Own approval</th>
        <th class="num">Cash and revenue</th><th>Main statement lines</th></tr></thead><tbody>
      ${w.by_user.map((u) => `<tr><td>${u.user === "(blank)" ? esc("No user") : data(u.user)}</td>
        <td class="num">${u.entries.toLocaleString(LOCALE)}</td><td class="num"><bdi dir="ltr">${esc(u.share_pct)}%</bdi></td>
        <td class="num">${money(u.debits)}</td><td class="num">${money(u.largest)}</td>
        <td class="nowrap">${u.first ? `<bdi dir="ltr" translate="no">${esc(u.first)} – ${esc(u.last)}</bdi>` : ""}</td>
        <td class="num">${u.weekend || ""}</td><td class="num${u.self_approved ? " bad" : ""}">${u.self_approved || ""}</td>
        <td class="num${u.cash_revenue ? " bad" : ""}">${u.cash_revenue || ""}</td>
        <td>${u.lines_touched.map((l) => `<div class="tag">${esc(l)}</div>`).join("")}</td></tr>`).join("")}</tbody>`;
    return;
  }
  const c = flagged[whoTab];
  if (!c.items.length) { $("who-table").innerHTML = '<tbody><tr><td class="muted">Nothing to show.</td></tr></tbody>'; return; }
  $("who-table").innerHTML = `<thead><tr><th>Entry</th><th>Date</th><th>User</th><th>Approved by</th><th>Description</th>
      <th>Accounts</th><th class="num">Debits</th></tr></thead><tbody>
    ${c.items.map((i) => `<tr><td>${i.entry_id ? data(i.entry_id) : `<span>${esc("Row")}</span> ${i.row}`}</td>
      <td class="nowrap">${i.date ? `<bdi dir="ltr" translate="no">${esc(i.date)}</bdi>` : ""}</td>
      <td>${data(i.user)}</td><td>${data(i.approver)}</td><td>${data(i.description)}</td>
      <td>${data(i.accounts)}</td><td class="num">${money(i.debits)}</td></tr>`).join("")}</tbody>` +
    (c.count > c.items.length ? `<tfoot><tr><td colspan="7" class="muted">${esc(`Showing the first ${c.items.length}.`)}</td></tr></tfoot>` : "");
}

$("who-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-who]");
  if (!tab) return;
  whoTab = tab.dataset.who;
  renderWho();
});

let lx = null;  // the look on show
let lxTab = "pivot";
const OTHER_COLUMN = "(other)";  // the explorer pivot adds up columns beyond its limit here
const LX_GROUPS = { account: "Account", user: "User", month: "Month", entry_id: "Entry" };

function renderExplorerForm(e) {
  const keep = $("lx-upload").value;
  const files = e.uploads.filter((u) => !["prior_tb", ...LIST_KINDS].includes(u.kind));
  $("lx-upload").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a file first</option>';
  if (files.some((u) => String(u.id) === keep)) $("lx-upload").value = keep;
  if (lx && !files.some((u) => u.id === lx.upload_id)) {
    lx = null; $("lx-result").hidden = $("lx-export").hidden = true;
  }
}

function explorerFilters() {
  const f = {};
  for (const el of $("lx-form").elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") { if (el.checked) f[el.name] = true; continue; }
    const v = el.value.trim();
    if (v === "") continue;
    f[el.name] = el.type === "number" ? Number(v) : v;
  }
  return f;
}

$("lx-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const button = ev.submitter || ev.currentTarget.querySelector("button[type=submit]");
  busy(button, async () => {
    const id = Number($("lx-upload").value);
    if (!id) throw new Error("Upload a file first.");
    const filters = explorerFilters();
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/explore`, filters);
    if (currentEngagement?.id !== engagementId) return;  // they moved on to another engagement
    lx = out;
    $("lx-export").href = `/api/uploads/${id}/explore/export?${new URLSearchParams(filters)}`;
    $("lx-export").hidden = false;
    renderExplorer();
  });
});

$("lx-clear").addEventListener("click", () => {
  const file = $("lx-upload").value;
  $("lx-form").reset();
  $("lx-upload").value = file;
});

$("lx-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-lx]");
  if (!tab) return;
  lxTab = tab.dataset.lx;
  renderExplorer();
});

function renderExplorer() {
  $("lx-result").hidden = !lx;
  if (!lx) return;
  $("lx-tabs").querySelectorAll("[data-lx]").forEach((b) => b.setAttribute("aria-selected", b.dataset.lx === lxTab));
  const cards = [["Lines matched", `${lx.matched.toLocaleString(LOCALE)} / ${lx.lines_in_file.toLocaleString(LOCALE)}`],
    ["Debits", money(lx.debits)], ["Credits", money(lx.credits)], ["Net", money(lx.net)]];
  $("lx-cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  let note = [], html = "";
  if (!lx.matched) {
    note = "No lines match these filters.";
  } else if (lxTab === "pivot") {
    const p = lx.pivot;
    const notes = [];
    if (p.rows.length < p.rows_total) notes.push(`Showing the first ${p.rows.length} of ${p.rows_total} rows.`);
    const shown = p.columns.filter((c) => c !== OTHER_COLUMN).length;
    if (shown < p.columns_total) notes.push(`Showing the largest ${shown} of ${p.columns_total} columns.`, '"(other)" adds up the rest.');
    if (p.rows.length < p.rows_total) notes.push("The Excel download has all the rows.");
    note = notes;
    html = `<tr><th>${esc(LX_GROUPS[p.rows_by])}</th><th>Lines</th><th>Debits</th><th>Credits</th>` +
      p.columns.map((c) => `<th class="num">${c === OTHER_COLUMN ? esc(c) : data(c)}</th>`).join("") + "<th>Net</th></tr>" +
      p.rows.map((r) => `<tr><td>${data(r.key)}</td><td class="num">${r.lines.toLocaleString(LOCALE)}</td>
        <td class="num">${money(r.debits)}</td><td class="num">${money(r.credits)}</td>` +
        r.cells.map((v) => `<td class="num">${v ? money(v) : ""}</td>`).join("") +
        `<td class="num">${money(r.net)}</td></tr>`).join("");
  } else {
    if (lx.lines.length < lx.matched) note = [`Showing the first ${lx.lines.length} of ${lx.matched} lines.`, "The Excel download has them all."];
    const has = (c) => lx.has.includes(c);
    const cols = [["row", "Row"], ["date", "Date"], ["account", "Account"], ["entry_id", "Entry"],
      ["description", "Description"], ["user", "User"]].filter(([c]) => c === "row" || has(c));
    html = "<tr>" + cols.map(([, label]) => `<th>${label}</th>`).join("") + "<th>Amount</th></tr>" +
      lx.lines.map((l) => "<tr>" + cols.map(([c]) => c === "row"
        ? `<td class="num">${l.row}</td>` : c === "date" ? `<td class="nowrap">${esc(l.date || "")}</td>` : `<td>${data(l[c])}</td>`).join("") +
        `<td class="num">${money(l.amount)}</td></tr>`).join("");
  }
  // One sentence to a span, so each is translated on its own.
  $("lx-note").innerHTML = [].concat(note || []).map((n) => `<span>${esc(n)}</span>`).join(" ");
  $("lx-table").innerHTML = html;
}

// Receivables and payables aging

let aged = null;  // the aging on show
let agingTab = "parties";
const AGING_SIDES = ["ar", "ap"];

function renderAgingForm(e) {
  const keep = $("ag-upload").value;
  const files = e.uploads.filter((u) => AGING_SIDES.includes(u.kind));
  $("ag-upload").innerHTML = files
    .map((u) => `<option value="${u.id}">${esc(u.filename)} (${esc(u.kind_label)})</option>`)
    .join("") || '<option value="">Upload a receivables or payables listing first</option>';
  if (files.some((u) => String(u.id) === keep)) $("ag-upload").value = keep;
  if (aged && !files.some((u) => u.id === aged.upload_id)) {
    aged = null; $("ag-result").hidden = $("ag-export").hidden = true;
  }
}

$("ag-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const button = ev.submitter || ev.currentTarget.querySelector("button[type=submit]");
  busy(button, async () => {
    const id = Number($("ag-upload").value);
    if (!id) throw new Error("Upload a receivables or payables listing first.");
    const terms = Number($("ag-terms").value);
    const engagementId = currentEngagement.id;
    const out = await sendJSON("POST", `/api/uploads/${id}/aging`, { terms_days: terms });
    if (currentEngagement?.id !== engagementId) return;  // they moved on to another engagement
    aged = out;
    $("ag-export").href = `/api/uploads/${id}/aging/export?terms_days=${out.terms_days}&as_of=${out.as_of}`;
    $("ag-export").hidden = false;
    renderAging();
  });
});

$("ag-tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[data-ag]");
  if (!tab) return;
  agingTab = tab.dataset.ag;
  renderAging();
});

const AGING_BUCKETS = { current: "Not yet due", d1_30: "1 to 30 days overdue", d31_60: "31 to 60 days overdue",
  d61_90: "61 to 90 days overdue", d91_180: "91 to 180 days overdue", over_180: "Over 180 days overdue", no_date: "No date" };
const AGING_SHORT = { current: "Not due", d1_30: "1-30", d31_60: "31-60", d61_90: "61-90", d91_180: "91-180", over_180: "180+", no_date: "No date" };

function renderAging() {
  $("ag-result").hidden = !aged;
  if (!aged) return;
  $("ag-tabs").querySelectorAll("[data-ag]").forEach((b) => b.setAttribute("aria-selected", b.dataset.ag === agingTab));
  const t = aged.totals;
  $("ag-note").textContent = aged.basis === "due"
    ? `${aged.side_label} aged at ${aged.as_of} from the due dates in the listing.`
    : `${aged.side_label} aged at ${aged.as_of}: the listing has no due dates, so each item is due ${aged.terms_days} days after its date.`;
  const cards = [[money(t.balance), "Listing total"], [t.parties.toLocaleString(LOCALE), aged.side === "ar" ? "Customers" : "Vendors"],
    [money(t.overdue), "Overdue"], [money(t.long_overdue), "Over 90 days overdue", t.long_overdue_items],
    [money(t.credit_balances), "Credit balances", t.credit_items], [money(t.after_period_end), "Dated after the period end", t.after_items]];
  $("ag-cards").innerHTML = cards.map(([v, k, flag]) =>
    `<div class="card${flag ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const lg = aged.ledger;
  $("ag-ledger").innerHTML = lg
    ? `<p class="${Math.abs(lg.difference) >= 0.005 ? "warn-box" : "muted"}"><span>Trial balance, ${esc(lg.label)}:</span> ${money(lg.balance)}. <span>Difference (listing less trial balance):</span> ${money(lg.difference)}</p>`
    : '<p class="muted">Map the trial balance in the lead sheets panel to compare the listing with the ledger.</p>';
  $("ag-buckets").innerHTML = "<tr><th>Age</th><th>Items</th><th>Balance</th><th>% of total</th></tr>" +
    aged.buckets.filter((b) => b.items || b.id !== "no_date").map((b) => `<tr><td>${esc(b.label)}</td><td class="num">${b.items.toLocaleString(LOCALE)}</td>
      <td class="num">${money(b.balance)}</td><td class="num">${b.pct}%</td></tr>`).join("") +
    `<tr class="total"><th>Total</th><th class="num">${t.items.toLocaleString(LOCALE)}</th><th class="num">${money(t.balance)}</th><th></th></tr>`;
  const party = aged.side === "ar" ? "Customer" : "Vendor";
  let html = "", more = 0;
  if (agingTab === "parties") {
    const keys = Object.keys(AGING_BUCKETS).filter((k) => k !== "no_date" || t.no_date);
    more = aged.parties_truncated;
    html = `<tr><th>${party}</th><th>Name</th><th>Items</th><th>Balance</th>${keys.map((k) => `<th class="nowrap" title="${esc(AGING_BUCKETS[k])}">${esc(AGING_SHORT[k])}</th>`).join("")}<th>Oldest (days overdue)</th></tr>` +
      aged.parties.map((p) => `<tr><td>${data(p.party)}</td><td>${data(p.name)}</td><td class="num">${p.items}</td>
        <td class="num${p.balance < 0 ? " bad" : ""}">${money(p.balance)}</td>
        ${keys.map((k) => `<td class="num${p.buckets[k] && ["d91_180", "over_180"].includes(k) ? " bad" : ""}">${p.buckets[k] ? money(p.buckets[k]) : ""}</td>`).join("")}
        <td class="num">${p.oldest_days ?? ""}</td></tr>`).join("");
  } else {
    more = aged.exceptions_truncated;
    html = aged.exceptions.length
      ? `<tr><th>Row</th><th>${party}</th><th>Invoice</th><th>Date</th><th>Due</th><th>Days overdue</th><th>Balance</th><th>Why</th></tr>` +
        aged.exceptions.map((x) => `<tr><td class="num">${x.row}</td><td>${data(x.party)} ${x.name ? `<span class="muted">${data(x.name)}</span>` : ""}</td>
          <td>${data(x.invoice)}</td><td class="nowrap">${esc(x.date || "")}</td><td class="nowrap">${esc(x.due || "")}</td>
          <td class="num">${x.days ?? ""}</td><td class="num">${money(x.balance)}</td>
          <td>${x.why.map((w) => `<div>${esc(w)}</div>`).join("")}</td></tr>`).join("")
      : '<tr><td class="muted">Nothing to look at: no item is more than 90 days overdue, has a credit balance or is dated after the period end.</td></tr>';
  }
  $("ag-more").textContent = more ? `Showing the largest ${agingTab === "parties" ? aged.parties.length : aged.exceptions.length}. The Excel download has all ${more + (agingTab === "parties" ? aged.parties.length : aged.exceptions.length)}.` : "";
  $("ag-table").innerHTML = html;
}

// Ratios: this year against last, with the big moves flagged

const RATIO_GROUPS = ["liquidity", "profitability", "efficiency", "leverage"];

// A ratio as text; `change` is the move on last year (percentage points for a % ratio).
function ratioValue(v, unit, change = false) {
  if (v == null) return "–";
  const digits = unit === "days" ? 0 : unit === "%" ? 1 : 2;
  const text = Math.abs(v).toLocaleString(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const prefix = v < 0 ? "−" : change && v > 0 ? "+" : "";
  const suffix = unit === "x" ? "×" : unit === "%" && !change ? "%" : "";
  return `<span dir="ltr">${prefix}${text}${suffix}</span>`;  // keeps the sign in place in Arabic
}

function renderRatios() {
  const r = fs.ratios;
  $("ratio-warnings").innerHTML = r.warnings.map((w) =>
    `<li class="${w.severity === "high" ? "warn-high" : ""}">${esc(w.text)}</li>`).join("");
  $("ratio-warnings").hidden = !r.warnings.length;
  $("ratio-note").textContent = r.has_prior
    ? (r.flagged ? `${r.flagged} of ${r.ratios.length} ratios moved a lot on last year.` : "No ratio moved a lot on last year.")
    : "Upload last year's trial balance as \"Prior-year trial balance\" to compare with last year.";
  const head = `<tr><th>Ratio</th><th class="num">This year</th>${r.has_prior ? '<th class="num">Last year</th><th class="num">Change</th><th>Follow up</th>' : ""}</tr>`;
  $("ratio-table").innerHTML = head + RATIO_GROUPS.map((g) => {
    const rows = r.ratios.filter((x) => x.group === g);
    if (!rows.length) return "";
    return `<tr class="group-row"><th colspan="${r.has_prior ? 5 : 2}">${esc(rows[0].group_label)}</th></tr>` + rows.map((x) => `<tr${x.flag ? ' class="flagged"' : ""}>
      <td>${esc(x.label)}<div class="tag">${esc(x.basis)}</div></td>
      <td class="num">${ratioValue(x.current, x.unit)}</td>
      ${r.has_prior ? `<td class="num">${ratioValue(x.prior, x.unit)}</td>
      <td class="num">${ratioValue(x.change, x.unit, true)}</td>
      <td>${x.flag ? `<span class="sev medium">${esc(x.flag)}</span>` : ""}</td>` : ""}</tr>`).join("");
  }).join("");
}

// Overview: where the engagement stands and what to do next

let dashTimer = null;
let dashLoads = 0;  // only the latest answer is shown, if an older one arrives after it
// Many panels reload together after a change, so wait for them to settle before asking.
function refreshDashboard() {
  clearTimeout(dashTimer);
  dashTimer = setTimeout(() => { if (currentEngagement) guard(loadDashboard); }, 300);
}

async function loadDashboard() {
  const id = currentEngagement.id;
  const load = ++dashLoads;
  // The trial balances picked in the statements panel (fsQuery skips any not of this engagement).
  const q = fsQuery();
  const d = await getJSON(`/api/engagements/${id}/dashboard?${q}`);
  if (currentEngagement?.id !== id || load !== dashLoads) return;
  const sevs = d.findings.open_by_severity;
  const cards = [
    [d.materiality ? money(d.materiality.overall_materiality) : "Not set", "Overall materiality"],
    [d.uploads, "Files uploaded"],
    [d.findings.open, "Open findings"],
    [sevs.high || 0, "Open high-severity findings"],
    [`${d.review.reviewed}/${d.review.total}`, "Sections reviewed"],
    [d.requests.outstanding, "Requests waiting for the client"],
    [d.requests.overdue, "Overdue requests"],
  ];
  if (d.statements && !d.statements.error) cards.push([d.statements.ratio_flags, "Ratios to explain"]);
  if (d.bank) {
    cards.push(!d.bank.reconciled ? ["Not done", "Bank reconciliation"]
      : d.bank.difference == null ? ["No balances", "Bank reconciliation"]
        : [money(d.bank.difference), "Bank difference"]);
  }
  if (d.risks?.risks) cards.push([`${d.risks.done}/${d.risks.procedures}`, "Audit procedures done"]);
  if (d.controls?.controls) cards.push([d.controls.deficient, "Control deficiencies"]);
  if (d.confirmations?.requests) cards.push([`${d.confirmations.replied}/${d.confirmations.sent}`, "Confirmation replies"]);
  if (d.workplan?.open) cards.push([d.workplan.overdue, "Tasks overdue"]);
  if (d.workplan?.budget) cards.push([`${Math.round(d.workplan.actual / d.workplan.budget * 100)}%`, "Hours budget used"]);
  if (d.days_since_period_end) cards.push([d.days_since_period_end, "Days since period end"]);
  $("dash-cards").innerHTML = cards.map(([v, k]) =>
    `<div class="card"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("dash-steps").innerHTML = d.next_steps.length ? d.next_steps.map((s) => `<li>
      <span class="sev ${esc(s.priority)}">${esc(SEVERITY_LABEL[s.priority])}</span>
      <button type="button" class="link" data-goto="${esc(s.panel)}" data-tab-to="${esc(s.tab || "")}">${esc(s.text)}</button></li>`).join("")
    : '<li class="muted">No next steps right now.</li>';
}

const SEVERITY_LABEL = { high: "High", medium: "Medium", low: "Low" };

$("dash-steps").addEventListener("click", (ev) => {
  const go = ev.target.closest("[data-goto]");
  if (!go) return;
  if (go.dataset.tabTo) $("fs-tabs").querySelector(`[data-tab="${go.dataset.tabTo}"]`)?.click();
  $(go.dataset.goto)?.scrollIntoView({ behavior: "smooth", block: "start" });
});

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
  refreshDashboard();
}

function signoffCell(s, kind) {
  const so = s[kind];
  const canSign = kind === "prepared" || me.rank >= (s.id === "completion" ? 3 : 2);
  const prepOk = s.prepared && !s.prepared.stale;
  const mine = so && so.user_id === me.id;
  let html = so ? `${data(so.user_name)}<div class="tag">${esc(when(so.at))}</div>${so.stale ? '<div class="stale">Changed since</div>' : ""}` : '<span class="muted">–</span>';
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
        <span>${data(n.author)} · ${esc(when(n.created_at))}</span>${n.cleared_by ? `<span>Cleared by ${esc(n.cleared_by)} · ${esc(when(n.cleared_at))}</span>` : ""}</div>
      <div class="text">${data(n.text)}</div>
      ${n.replies.map((r) => `<div class="reply">${data(r.text)}<div class="tag">${data(r.author)} · ${esc(when(r.at))}</div></div>`).join("")}
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

// `locked`: the engagement is locked, so nothing can be sent, changed or removed.
function requestCard(r, team, locked = false) {
  const open = r.status !== "accepted" && !locked;
  const files = r.files.map((f) => {
    const canRemove = open && (f.uploaded_by_id === me.id || (team && me.rank >= 2));
    return `<li><button type="button" class="small secondary file" data-rq-file="${f.id}">${data(f.filename)}</button>
      <span class="tag">${fileSize(f.size)} · ${esc(f.uploaded_by)}${team && f.from_client ? " (client)" : ""} · ${esc(when(f.uploaded_at))}</span>
      ${canRemove ? `<button type="button" class="link danger-link" data-rq-remove="${f.id}">Remove</button>` : ""}</li>`;
  }).join("");
  // One line each: replies keep the message's own line breaks (white-space: pre-wrap).
  const messages = r.messages.map((m) => `<div class="reply${m.from_client ? " client" : ""}">${
    m.status_label ? `<strong class="rq-status-${m.status_to}">${esc(m.status_label)}</strong>${m.text ? ": " : ""}` : ""}${data(m.text)}<div class="tag">${
    team && m.from_client ? esc(m.author) + " (client)" : data(m.author)} · ${esc(when(m.at))}</div></div>`).join("");
  const actions = [];
  if (open) actions.push(`<label class="button small upload">${r.files.length ? "Add a file" : "Upload a file"}<input type="file" multiple data-rq-upload class="visually-hidden"></label>`);
  if (team && !locked && r.status === "submitted") actions.push('<button type="button" class="small" data-rq-accept>Accept files</button>');
  if (team && open && (!r.files.length || me.rank >= 2)) actions.push('<button type="button" class="small secondary danger" data-rq-delete>Delete request</button>');
  // Reopening accepted files is for reviewers; the server refuses preparers.
  const returnable = team && !locked && (r.status === "submitted" || (r.status === "accepted" && me.rank >= 2));
  return `<div class="rq ${r.status}" data-rq="${r.id}">
    <div class="head"><strong>${r.number}. ${data(r.title)}</strong>
      <span class="st rq-${r.status}">${esc(r.status_label)}</span>
      ${r.due_date ? `<span class="${r.overdue ? "overdue" : "tag"}">Due ${esc(r.due_date)}${r.overdue ? " · overdue" : ""}</span>` : ""}
      ${team ? `<span class="tag">Asked by ${esc(r.created_by)} · ${esc(when(r.created_at))}</span>` : ""}</div>
    ${r.details ? `<div class="text">${data(r.details)}</div>` : ""}
    ${files ? `<ul class="rq-files">${files}</ul>` : `<p class="tag">${team ? "No files yet." : "Nothing sent yet."}</p>`}
    ${messages}
    ${actions.length ? `<div class="cell-actions">${actions.join("")}</div>` : ""}
    ${locked ? "" : `<form data-rq-message><textarea name="text" rows="1" maxlength="5000" aria-label="Message" placeholder="${team ? "Message to the client" : "Message to your auditors"}"></textarea>
      <button type="submit" class="small secondary">Send message</button>
      ${returnable ? '<button type="button" class="small secondary" data-rq-return title="Send this request back to the client with the message">Send back with message</button>' : ""}</form>`}
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
  refreshDashboard();
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
  $("rq-list").innerHTML = list.length ? list.map((r) => requestCard(r, true, !!currentEngagement?.lock)).join("")
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
      <h2>${data(e.name)} <span class="muted">· period ending ${esc(e.period_end)}</span></h2>
      <p class="tag" style="margin-top:-6px">${owed ? `${owed} still to send` : "Nothing left to send"}${e.summary.overdue ? ` · <span class="overdue">${e.summary.overdue} overdue</span>` : ""} · ${e.summary.accepted} of ${e.summary.total} accepted</p>
      ${e.locked ? '<p class="warn-box">Your auditors have closed this audit file, so nothing more can be sent for it.</p>' : ""}
      ${list.length ? list.map((r) => requestCard(r, false, e.locked)).join("") : '<p class="muted">Nothing here right now.</p>'}
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
        <td class="finding-cell"><button type="button" class="link finding-open" aria-label="Open finding ${f.id}: ${esc(f.check_name)}">${esc(f.check_name)}</button>${f.detail && f.detail !== f.check_name ? `<div class="muted">${findingDetail(f)}</div>` : ""}</td>
        <td>${esc(f.entry_id)}</td><td class="nowrap">${esc(f.date)}</td>
        <td>${data(f.account)}</td><td class="num">${money(f.amount)}</td>
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

// A check's detail is the app's own text and is translated; a finding added by hand is the
// auditor's own words and never is.
const findingDetail = (f) => f.source === "manual" ? data(f.detail) : esc(f.detail);

function openFinding(f, scroll = true) {
  currentFinding = f;
  $("fd").hidden = false;
  $("fd-title").innerHTML = `Finding #${f.id} ${statusPill(f)}`;
  // Built from pieces, so the labels are translated but the ledger's own data never is.
  const where = [f.entry_id && `<span>Entry</span> ${data(f.entry_id)}`, f.date && esc(f.date),
    f.account && data(f.account), f.amount != null && money(f.amount),
    f.row && `<span>row ${f.row} of</span> ${data(f.filename)}`].filter(Boolean);
  $("fd-meta").innerHTML = [`<span>${esc(f.check_name)}</span>`, ...where].join(" · ");
  $("fd-detail").innerHTML = [f.description && data(f.description), f.detail && `<span>${findingDetail(f)}</span>`]
    .filter(Boolean).join(": ");
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
        <td>${data(a.filename)}</td><td class="num">${fileSize(a.size)}</td>
        <td class="nowrap">${esc(when(a.uploaded_at))}${a.author ? " · " + data(a.author) : ""}</td>
        <td class="muted" title="${esc(a.sha256)}">${esc(a.sha256.slice(0, 12))}…</td>
        <td><div class="cell-actions"><a class="button" href="/api/evidence/${a.id}">Download</a>
          ${f.status === "cleared" ? "" : `<button type="button" class="small secondary danger min-2" data-remove-ev="${a.id}">Remove</button>`}</div></td></tr>`).join("")
    : '<tr><td class="muted">No evidence attached.</td></tr>';
  $("fd-trail").innerHTML = f.trail.length ? [...f.trail].reverse().map((n) => `<li>
      <div class="who">${esc(when(n.created_at))}${n.author ? " · " + data(n.author) : ""}</div>
      ${n.status_to ? `<div>Status: ${esc(n.status_from)} → <strong>${esc(n.status_to)}</strong></div>` : ""}
      ${n.text ? `<div>${data(n.text)}</div>` : ""}</li>`).join("")
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
      const r = await sendJSON("POST", `/api/findings/${id}/assistant`, { task: btn.dataset.ai, language: I18N.lang });
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
  // The heading is saved with the note, so it's written in the language on screen.
  const heading = I18N.tr(`${a.label} drafted with the AI assistant (${a.model}):`).replace(/[\u2068\u2069]/g, "");
  return `${heading}\n\n${a.text}`;
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

// The language picked on the sign-in page is saved once someone signs in; otherwise a
// signed-in user's saved choice wins, so the app opens in it on every device.
// The pick survives the reload a different user's sign-in causes.
let languagePicked = false;
try { languagePicked = sessionStorage.getItem("audit-lang-picked") === "1"; } catch {}
function pickLanguage(picked) {
  languagePicked = picked;
  try { if (picked) sessionStorage.setItem("audit-lang-picked", "1"); else sessionStorage.removeItem("audit-lang-picked"); } catch {}
}
// One save at a time, in order, so quick switches can't leave the older choice saved. A
// failed save (signed out meanwhile) is kept as a pick for the next sign-in, until a later
// save of the latest choice gets through.
let languageSaves = Promise.resolve();
let latestLanguage = null;
function saveLanguage(language) {
  latestLanguage = language;
  languageSaves = languageSaves.then(() => sendJSON("PUT", "/api/auth/language", { language }).then(
    () => { if (language === latestLanguage) pickLanguage(false); },
    () => pickLanguage(true)));
}

$("lang-toggle").addEventListener("click", () => {
  const next = I18N.lang === "ar" ? "en" : "ar";
  I18N.set(next);
  if (me) saveLanguage(next); else pickLanguage(true);
});

function setUser(user) {
  me = user;
  const picked = languagePicked;
  pickLanguage(false);
  if (picked) saveLanguage(I18N.lang);
  else if (user.language) I18N.set(user.language);
  lastUser = userKey(user);
  document.body.dataset.rank = user.rank;
  $("user-name").textContent = user.name;
  $("user-role").textContent = user.role_label;
  $("user-chip").hidden = false;
  $("nav").hidden = user.role === "client";
  if (user.role === "admin") refreshSignupCount();
  startBell();
}

function showLogin(needsSetup) {
  me = null;
  // Nothing of the last user's may reach the next one: unsent messages, the portal.
  rqDrafts = {}; rvDrafts = {}; rq = null; portal = null;
  $("pt-list").innerHTML = $("rq-list").innerHTML = "";
  delete document.body.dataset.rank;
  clearInterval(bellTimer); bellItems = []; closeBell();
  $("user-chip").hidden = $("nav").hidden = $("bell").hidden = true;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== "view-login"; });
  $("results").hidden = true;
  $("login-form").hidden = needsSetup;
  $("setup-form").hidden = !needsSetup;
  $("signup-form").hidden = $("signup-done").hidden = true;
  $("signup-open-row").hidden = needsSetup;
  (needsSetup ? $("setup-name") : $("login-email")).focus();
}

// Asking for an account: it waits for an admin to approve it before it can sign in.
function showSignup(form) {
  showError("");
  $("login-form").hidden = form !== "login";
  $("signup-form").hidden = form !== "signup";
  $("signup-done").hidden = form !== "done";
  if (form === "signup") $("signup-name").focus();
  if (form === "login") $("login-email").focus();
}
$("signup-open").addEventListener("click", () => {
  $("signup-email").value = $("login-email").value;
  showSignup("signup");
});
$("signup-back").addEventListener("click", () => showSignup("login"));
$("signup-done-back").addEventListener("click", () => showSignup("login"));
$("signup-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const button = ev.submitter || $("signup-form").querySelector("button[type=submit]");
  busy(button, async () => {
    const body = formObject($("signup-form"));
    await sendJSON("POST", "/api/auth/signup", body);
    $("login-email").value = body.email;
    $("signup-form").reset();
    showSignup("done");
  });
});

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
let signups = [];  // account requests waiting for an admin

function showSignupCount(n) {
  $("signup-count").textContent = n || "";
  $("signup-count").hidden = !n;
}
async function refreshSignupCount() {
  try { showSignupCount((await getJSON("/api/signups")).length); } catch {}
}

// The bell: what needs the signed-in user across their engagements, looked at every
// two minutes. Opening it marks what is there as seen.
let bellItems = [];
let bellTimer = null;
let pendingPanel = null;  // where to scroll once the engagement a bell item names is open

async function refreshBell() {
  if (!me || me.role === "client") return;
  try {
    const out = await getJSON("/api/notifications");
    bellItems = out.items;
    $("bell-count").textContent = out.new || "";
    $("bell-count").hidden = !out.new;
    if (!$("bell-menu").hidden) renderBell();
  } catch {}
}

function startBell() {
  clearInterval(bellTimer);
  $("bell").hidden = me.role === "client";
  if (me.role === "client") return;
  refreshBell();
  bellTimer = setInterval(refreshBell, 120000);
}

function renderBell() {
  $("bell-list").innerHTML = bellItems.length ? bellItems.map((x, i) => {
    const what = x.kind === "task" ? `${data(x.title)} · <span>${esc("Due")}</span> <bdi dir="ltr" translate="no">${esc(x.due_on)}</bdi>`
      : x.kind === "request" ? data(x.title)
        : x.kind === "signup" ? `<span>${esc(x.count === 1 ? "1 waiting" : `${x.count} waiting`)}</span>`
          : `${data(x.by)}: ${data(x.note)}`;
    return `<li><button type="button" class="bell-item${x.new ? " new" : ""}" data-bell="${i}">
      <span class="bell-text">${x.urgent ? `<span class="pill bad">${esc("Overdue")}</span> ` : ""}<span>${esc(x.text)}</span></span>
      <span class="tag">${what}</span>
      ${x.engagement ? `<span class="tag">${data(x.engagement)}</span>` : ""}
      ${x.kind === "task" ? "" : `<span class="tag">${esc(when(x.at))}</span>`}</button></li>`;
  }).join("") : `<li class="muted">${esc("Nothing needs you right now.")}</li>`;
}

function closeBell() {
  $("bell-menu").hidden = true;
  $("bell-btn").setAttribute("aria-expanded", "false");
}

$("bell-btn").addEventListener("click", () => {
  if (!$("bell-menu").hidden) { closeBell(); return; }
  renderBell();
  $("bell-menu").hidden = false;
  $("bell-btn").setAttribute("aria-expanded", "true");
  if (!$("bell-count").hidden) {
    $("bell-count").hidden = true;
    sendJSON("POST", "/api/notifications/seen").catch(() => {});
  }
});
$("bell-refresh").addEventListener("click", () => guard(refreshBell));
document.addEventListener("click", (ev) => { if (!ev.target.closest("#bell")) closeBell(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("bell-menu").hidden) { closeBell(); $("bell-btn").focus(); } });

$("bell-list").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-bell]");
  if (!btn) return;
  const x = bellItems[Number(btn.dataset.bell)];
  closeBell();
  if (x.kind === "signup") { location.hash = "#/users"; return; }
  const hash = `#/engagements/${x.engagement_id}`;
  if (location.hash === hash && currentEngagement?.id === x.engagement_id) {
    $(x.panel)?.scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    pendingPanel = x.panel;
    location.hash = hash;
  }
});

// After an engagement opens: the panel a bell item pointed at.
function scrollToPendingPanel() {
  const panel = pendingPanel;
  pendingPanel = null;
  if (panel) $(panel)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

let clients = [];  // for linking client users

async function showUsers() {
  [users, clients, signups] = await Promise.all([getJSON("/api/users"), getJSON("/api/clients"),
    getJSON("/api/signups")]);
  const staff = roles.filter((r) => r.id !== "client");
  const roleOptions = (sel, list) => list.map((r) => `<option value="${r.id}"${r.id === sel ? " selected" : ""}>${esc(r.label)}</option>`).join("");
  showSignupCount(signups.length);
  $("signups-panel").hidden = !signups.length;
  $("signups").innerHTML = signups.length
    ? "<tr><th>Name</th><th>Email</th><th>Asked</th><th>Role</th><th></th></tr>" +
      signups.map((r) => `<tr data-signup="${r.id}"><td>${data(r.name)}</td><td>${data(r.email)}</td>
        <td class="nowrap">${esc(when(r.created_at))}</td>
        <td><select class="map" data-signup-role aria-label="Role">${roleOptions("preparer", staff)}
          <optgroup label="Client user of">${clients.map((c) => `<option translate="no" value="client:${c.id}">${esc(c.name)}</option>`).join("")}</optgroup></select></td>
        <td><div class="cell-actions">
          <button type="button" class="small" data-approve>Approve</button>
          <button type="button" class="small secondary danger" data-reject>Turn down</button>
        </div></td></tr>`).join("")
    : "";
  const clientOptions = (sel) => clients.map((c) => `<option value="${c.id}"${c.id === sel ? " selected" : ""} translate="no">${esc(c.name)}</option>`).join("");
  const keep = $("nu-role").value;
  $("nu-role").innerHTML = roleOptions(keep || "preparer", [...staff, ...roles.filter((r) => r.id === "client")]);
  $("nu-client").innerHTML = '<option value="">Pick a client</option>' + clientOptions(Number($("nu-client").value));
  updateNewUserRole();
  $("users").innerHTML = "<tr><th>Name</th><th>Email</th><th>Role</th><th>Last sign-in</th><th>Status</th><th></th></tr>" +
    users.map((u) => `<tr data-user="${u.id}">
      <td>${data(u.name)}${u.id === me.id ? ' <span class="muted">(you)</span>' : ""}</td><td>${data(u.email)}</td>
      <td><select class="map" data-role aria-label="Role">${roleOptions(u.role, staff)}${
        u.role === "client" && !u.client_id ? '<option value="" selected>Client (no client)</option>' : ""}
        <optgroup label="Client user of">${clients.map((c) => `<option translate="no" value="client:${c.id}"${
          u.role === "client" && c.id === u.client_id ? " selected" : ""}>${esc(c.name)}</option>`).join("")}</optgroup></select></td>
      <td class="nowrap">${esc(when(u.last_login_at)) || '<span class="muted">Never</span>'}</td>
      <td>${u.active ? "Active" : '<span class="muted">Deactivated</span>'}</td>
      <td><div class="cell-actions">
        <button type="button" class="small secondary" data-reset>Reset password</button>
        <button type="button" class="small secondary${u.active ? " danger" : ""}" data-active="${!u.active}">${u.active ? "Deactivate" : "Reactivate"}</button>
      </div></td></tr>`).join("");
}

$("signups").addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-signup]");
  const button = ev.target.closest("[data-approve], [data-reject]");
  if (!row || !button) return;
  const r = signups.find((x) => String(x.id) === row.dataset.signup);
  if (button.matches("[data-reject]")) {
    if (!confirm(`Turn down ${r.name}'s request? They won't be able to sign in.`)) return;
    busy(button, async () => {
      try { await sendJSON("DELETE", `/api/signups/${r.id}`); } finally { await showUsers(); }
    }, () => false);
    return;
  }
  const role = row.querySelector("[data-signup-role]").value;
  const body = role.startsWith("client:") ? { role: "client", client_id: Number(role.slice(7)) } : { role };
  busy(button, async () => {
    try { await sendJSON("POST", `/api/signups/${r.id}/approve`, body); } finally { await showUsers(); }
  }, () => false);
});

function updateNewUserRole() {
  const client = $("nu-role").value === "client";
  $("nu-client-wrap").hidden = !client;
  $("nu-client").required = client;
}
$("nu-role").addEventListener("change", updateNewUserRole);

$("users").addEventListener("change", (ev) => {
  const sel = ev.target.closest("[data-role]");
  if (!sel || !sel.value) return;
  const id = sel.closest("[data-user]").dataset.user;
  const user = users.find((u) => String(u.id) === id);
  const toClient = sel.value.startsWith("client:");
  const body = toClient ? { role: "client", client_id: Number(sel.value.slice(7)) } : { role: sel.value };
  // Moving between the audit team and a client is a different app: confirm, as it signs them out.
  if (toClient !== (user.role === "client") && !confirm(toClient
    ? `Make ${user.name} a client user? They will only see their client's document requests, and be signed out.`
    : `Move ${user.name} onto the audit team? They will see every engagement, and be signed out.`)) {
    showUsers();
    return;
  }
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
        <td class="nowrap">${a.user_name ? data(a.user_name) : '<span class="muted">Not signed in</span>'}${a.user_role ? ` <span class="muted">${esc(a.user_role)}</span>` : ""}</td>
        ${withEngagement ? `<td>${a.engagement_id ? `<a href="#/engagements/${a.engagement_id}">${data(a.engagement_label)}</a>` : ""}</td>` : ""}
        <td>${esc(a.summary)}</td><td class="details-cell">${esc(activityDetails(a.details))}${a.ip ? ` <span title="IP address">${esc(a.ip)}</span>` : ""}</td></tr>`).join("")
    : '<tr><td class="muted">Nothing yet.</td></tr>';
}

async function showActivity() {
  if (!users.length) users = await getJSON("/api/users");
  const sel = $("act-user").value;
  $("act-user").innerHTML = '<option value="">Everyone</option>' +
    users.map((u) => `<option value="${u.id}" translate="no">${esc(u.name)}</option>`).join("");
  $("act-user").value = sel;
  const q = new URLSearchParams({ limit: 500 });
  if ($("act-user").value) q.set("user_id", $("act-user").value);
  if ($("act-action").value) q.set("action", $("act-action").value);
  renderActivity($("act-table"), await getJSON(`/api/activity?${q}`), true);
}
$("act-user").addEventListener("change", () => guard(showActivity));
$("act-action").addEventListener("change", () => guard(showActivity));

// --- Engagement overview -------------------------------------------------------
// Every engagement the user is on, one row each, for partners and reviewers.

let ov = null;
const OV_PILL = { planning: "warn", fieldwork: "warn", completion: "good", locked: "good" };

async function showOverview() {
  const archived = $("ov-archived").checked;
  ov = await getJSON(`/api/overview?archived=${archived}`);
  $("ov-export").href = `/api/overview/export?archived=${archived}`;
  renderOverview();
}

function renderOverview() {
  if (!ov) return;
  const t = ov.totals;
  $("ov-about").textContent = ov.all
    ? "Every engagement in the firm, with where it stands and what is holding it up. The files that need someone most come first."
    : "Every engagement you are on, with where it stands and what is holding it up. The files that need someone most come first.";
  $("ov-cards").innerHTML = [
    [t.open, "Engagements not yet locked", false],
    [t.needs_attention, "Needing attention", t.needs_attention > 0],
    [t.findings_high, "High-severity findings open", t.findings_high > 0],
    [t.requests_overdue, "Client requests overdue", t.requests_overdue > 0],
    [t.tasks_overdue, "Tasks overdue", t.tasks_overdue > 0],
    [t.open_notes, "Review notes open", t.open_notes > 0],
  ].map(([v, k, bad]) => `<div class="card${bad ? " bad" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const show = $("ov-stage").value, find = $("ov-search").value.trim().toLowerCase();
  const rows = ov.rows.filter((r) => (!show || (show === "attention" ? r.attention > 0 : r.stage === show))
    && (!find || `${r.client} ${r.name}`.toLowerCase().includes(find)));
  if (!rows.length) {
    $("ov-table").innerHTML = `<tbody><tr><td class="muted">${esc(ov.rows.length ? "Nothing to show." : "No engagements yet.")}</td></tr></tbody>`;
    return;
  }
  const count = (n, bad = true) => `<td class="num${n && bad ? " bad" : ""}">${n ? esc(n) : ""}</td>`;
  $("ov-table").innerHTML = `<thead><tr><th>Engagement</th><th>Period end</th><th>Stage</th><th>Partners</th>
      <th class="num">Open findings</th><th class="num">High</th><th class="num">Sections reviewed</th><th class="num">Review notes</th>
      <th class="num">Requests overdue</th><th class="num">Tasks overdue</th><th class="num">Hours</th><th>Holding it up</th></tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td><a href="#/engagements/${r.id}">${data(`${r.client} · ${r.name}`)}</a>${r.archived ? `<div class="tag">${esc("Archived")}</div>` : ""}</td>
      <td class="nowrap"><bdi dir="ltr" translate="no">${esc(r.period_end)}</bdi>${r.days_since_period_end && r.stage !== "locked" ? `<div class="tag">${esc(`${r.days_since_period_end} days ago`)}</div>` : ""}</td>
      <td><span class="pill ${OV_PILL[r.stage]}">${esc(r.stage_label)}</span>${r.completion_signed && r.stage !== "locked" ? `<div class="tag">${esc("Completion signed off")}</div>` : ""}</td>
      <td>${r.partners.map(data).join(", ")}</td>
      ${count(r.findings_open, false)}${count(r.findings_high)}
      <td class="num">${r.review_total ? `<bdi dir="ltr">${r.review_done}/${r.review_total}</bdi>` : ""}</td>
      ${count(r.open_notes, false)}${count(r.requests_overdue)}${count(r.tasks_overdue)}
      <td class="num">${r.hours_budget ? `<bdi dir="ltr">${esc(r.hours_actual)}/${esc(r.hours_budget)}</bdi>` : r.hours_actual ? esc(r.hours_actual) : ""}</td>
      <td>${r.waiting.map((w) => `<div class="tag">${esc(w)}</div>`).join("")}</td></tr>`).join("")}</tbody>`;
}

$("ov-archived").addEventListener("change", () => guard(showOverview));
$("ov-stage").addEventListener("change", renderOverview);
$("ov-search").addEventListener("input", renderOverview);

// --- Quick audit view ---------------------------------------------------------

const quickMapper = Mapper($("quick-mapper"), () => { $("dl-xlsx").disabled = $("dl-csv").disabled = true; });

function quickForm(extra = {}) {
  const fd = quickMapper.addTo(new FormData($("form")));
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  return fd;
}

$("file").addEventListener("change", () => quickMapper.load($("file").files[0]));
// Any change to the file or a setting makes the shown results out of date: the downloads
// would be built from the new values, so run again first.
$("form").addEventListener("input", () => { $("dl-xlsx").disabled = $("dl-csv").disabled = true; });

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
    ["Rows read", imp ? imp.rows_read.toLocaleString(LOCALE) : s.rows.toLocaleString(LOCALE)],
    ["Rows analysed", s.rows.toLocaleString(LOCALE)],
    ["Total debits", money(s.total_debits)],
    ["Total credits", money(s.total_credits)],
    ["Rows with findings", s.flagged_rows.toLocaleString(LOCALE)],
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
  renderRisk();
  renderFindings();
  renderBenford();
}

const RISK_ROWS = 50;

function renderRisk() {
  const risk = result.risk;  // runs saved before risk scoring have none
  $("risk-panel").hidden = !risk;
  if (!risk) return;
  const unit = risk.basis === "entry" ? "entries" : "lines";
  const parts = risk.scored
    ? [`Each of the ${risk.scored} flagged ${unit} scores up to 100: points for every check it fails, ` +
       `plus up to 20 for its size against ${risk.size_basis}. ` +
       `High ${risk.by_level.high}, medium ${risk.by_level.medium}, low ${risk.by_level.low}.`]
    : ["Nothing was flagged, so there is nothing to rank."];
  if (risk.scored > RISK_ROWS) {
    parts.push(risk.entries.length < risk.scored
      ? `Showing the top ${RISK_ROWS}; the Excel report has the top ${risk.entries.length}.`
      : `Showing the top ${RISK_ROWS}; download the Excel report for the rest.`);
  }
  $("risk-note").innerHTML = parts.map((t) => `<span>${esc(t)}</span>`).join(" ");
  $("risk").innerHTML = risk.scored ? "<tr><th>Rank</th><th>Score</th><th>Entry</th><th>Date</th><th>Account</th>" +
    "<th>Description</th><th>Amount</th><th>Why</th></tr>" +
    risk.entries.slice(0, RISK_ROWS).map((e) => `<tr><td class="num">${e.rank}</td>
      <td class="nowrap"><span class="sev ${esc(e.level)}">${e.score}</span></td>
      <td>${data(e.entry_id ?? "")}<div class="muted" style="font-size:12px">${esc(e.lines === 1 ? `Row ${e.row}` : `Row ${e.row}, ${e.lines} lines`)}</div></td>
      <td class="nowrap">${esc(e.date)}</td>
      <td>${e.account != null ? data(e.account) : e.accounts ? esc(`${e.accounts} accounts`) : ""}</td><td>${data(e.description)}</td>
      <td class="num">${money(e.amount)}</td>
      <td>${e.reasons.map((c) => esc(checkNames[c] || c)).join("<br>")}</td></tr>`).join("") : "";
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
      <td class="nowrap">${esc(f.date)}</td><td>${data(f.account)}</td><td>${data(f.description)}</td>
      <td class="num">${money(f.amount)}</td><td>${esc(f.detail)}</td></tr>`).join("");
}
$("f-sev").addEventListener("change", renderFindings);
$("f-check").addEventListener("change", renderFindings);

function renderBenford() {
  const b = result.benford;
  $("benford-note").textContent = b.n < 100
    ? `Only ${b.n} amounts of 10 or more, so this is shown for reference only.`
    : `${b.conformity}, MAD ${b.mad.toFixed(4)} across ${b.n.toLocaleString(LOCALE)} amounts. ` +
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
  const checkOptions = await getJSON("/api/checks/options");
  let weekend = "sat-sun";
  try { weekend = localStorage.getItem("audit-weekend") || weekend; } catch {}
  document.querySelectorAll(".weekend-choice").forEach((sel) => {
    sel.innerHTML = checkOptions.weekends.map((w) => `<option value="${w.id}">${esc(w.label)}</option>`).join("");
    if (checkOptions.weekends.some((w) => w.id === weekend)) sel.value = weekend;
    sel.addEventListener("change", () => {
      document.querySelectorAll(".weekend-choice").forEach((other) => { other.value = sel.value; });
      try { localStorage.setItem("audit-weekend", sel.value); } catch {}
    });
  });
  document.querySelectorAll(".keyword-list").forEach((input) => { input.value = checkOptions.keywords.join(", "); });
  checkDefaults = { cutoff_days: 5, round_threshold: 1000, weekend: $("run-weekend").value, keywords: checkOptions.keywords };
  try { templates = await getJSON("/api/test-templates"); } catch { templates = []; }
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
    $("setup-code-row").hidden = !status.setup_code_required;
    $("setup-code").required = status.setup_code_required;
    showLogin(status.needs_setup);
  }
})();
