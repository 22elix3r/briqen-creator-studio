const workspace = document.querySelector("#workspace");
const projectList = document.querySelector("#project-list");
const wallet = document.querySelector("#wallet");
const notice = document.querySelector("#notice");
const workspaceLabel = document.querySelector("#workspace-label");
const signout = document.querySelector("#signout");
const active = new Set(["queued", "running", "confirming"]);
const statuses = {
  queued: "Queued", running: "Working", confirming: "Checking generation",
  succeeded: "Ready", failed: "Failed", expired: "Expired",
};
const tabs = ["Script", "Storyboard", "Export"];
const requests = new Map();
const state = {
  projectId: new URL(location.href).searchParams.get("project"),
  projects: [], data: null, tab: viewFromLocation(), painted: "", script: null,
  timeline: null, split: null, scenarios: new Map(), audit: null, session: null,
  sessionLoaded: false, cursor: null, connectionError: false, sceneId: null,
  exportId: null, mediaMode: "candidate", briefOpen: false, prompts: new Map(),
};
let noticeTimer;
let refreshRequest = 0;
let appliedRefresh = 0;
let historyPosition = history.state?.studioPosition ?? 0;
history.replaceState({ studioPosition: historyPosition }, "", location.href);

function hasUnsavedEdits() {
  return Boolean(state.script?.dirty || state.timeline?.dirty);
}

function confirmDiscardEdits() {
  return !hasUnsavedEdits() || window.confirm("Discard unsaved changes and leave this project?");
}

function warnBeforeUnload(event) {
  event.preventDefault();
  event.returnValue = true;
}

function updateUnloadWarning() {
  if (hasUnsavedEdits()) window.addEventListener("beforeunload", warnBeforeUnload);
  else window.removeEventListener("beforeunload", warnBeforeUnload);
}

function projectUrl(projectId, tab) {
  const url = new URL(location.href);
  if (projectId) url.searchParams.set("project", projectId);
  else url.searchParams.delete("project");
  if (tab === "Script") url.searchParams.delete("view");
  else url.searchParams.set("view", tab.toLowerCase());
  return url;
}

function viewFromLocation() {
  const view = new URL(location.href).searchParams.get("view");
  return tabs.find((tab) => tab.toLowerCase() === view) || "Script";
}

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, method = "GET", body, key) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const response = await fetch(path, { method, headers, body: JSON.stringify(body), signal: AbortSignal.timeout(15000) });
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail : payload.detail?.map((item) => item.msg).join(". ");
    throw new ApiError(response.status, detail || "The request could not be completed");
  }
  return payload;
}

async function queue(path, body) {
  const action = JSON.stringify([path, body]);
  const fingerprint = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(action));
  const hash = Array.from(new Uint8Array(fingerprint), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const storageKey = `studio.pending.${hash}`;
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey)); } catch { saved = null; }
  const key = requests.get(hash) || (saved && Date.now() - saved.at < 24 * 3600000 ? saved.key : null)
    || crypto.randomUUID();
  requests.set(hash, key);
  try { sessionStorage.setItem(storageKey, JSON.stringify({ key, at: Date.now() })); } catch { /* use memory */ }
  function clear() {
    requests.delete(hash);
    try { sessionStorage.removeItem(storageKey); } catch { /* use memory */ }
  }
  try {
    const result = await api(path, "POST", body, key);
    clear();
    return result;
  } catch (error) {
    if (error.status >= 400 && error.status < 500 && error.status !== 401) clear();
    throw error;
  }
}

function canEdit() {
  return state.session?.role === "editor";
}

async function loadSession() {
  try {
    state.session = await api("/api/session");
  } catch (error) {
    if (error.status !== 401) throw error;
    state.session = null;
    state.data = null;
    state.projects = [];
    state.cursor = null;
    state.script = null;
    state.timeline = null;
    state.audit = null;
    state.split = null;
    state.scenarios.clear();
    wallet.textContent = "";
    wallet.hidden = true;
  }
  state.sessionLoaded = true;
  workspaceLabel.textContent = state.session
    ? `${state.session.workspace}${state.session.demo_provider ? " · Simulated provider" : ""}${canEdit() ? "" : " · View only"}`
    : "";
  signout.hidden = !state.session?.auth_required;
  document.querySelector(".api-link").hidden = !state.session?.api_docs;
  document.querySelector('[data-action="new"]').hidden = !canEdit();
  paintList();
  paint();
}

function message(text, error = false) {
  clearTimeout(noticeTimer);
  notice.textContent = text;
  notice.classList.toggle("error", error);
  noticeTimer = setTimeout(() => { notice.textContent = ""; }, 4800);
}

function showCredits(credits) {
  wallet.textContent = `${credits.available} credits available${credits.held ? ` · ${credits.held} held` : ""}`;
  wallet.hidden = false;
}

async function loadList(append = false) {
  const path = append && state.cursor ? `/api/projects?before=${encodeURIComponent(state.cursor)}` : "/api/projects";
  const response = await api(path);
  state.projects = append ? [...state.projects, ...response.projects] : response.projects;
  state.cursor = response.next_cursor;
  showCredits(response.credits);
  paintList();
}

function paintList() {
  projectList.innerHTML = state.projects.map((project) => `
    <button class="project-link" data-action="select" data-id="${h(project.id)}"
      aria-current="${project.id === state.projectId ? "page" : "false"}">
      <span class="project-glyph" aria-hidden="true"></span><span>${h(project.title)}</span></button>
  `).join("") + (state.cursor ? '<button class="quiet" data-action="more">Load more projects</button>' : "");
}

function selectProject(projectId, updateLocation = true) {
  state.projectId = projectId;
  state.data = null;
  state.script = null;
  state.timeline = null;
  state.split = null;
  state.audit = null;
  state.tab = updateLocation ? "Script" : viewFromLocation();
  state.sceneId = null;
  state.exportId = null;
  state.mediaMode = "candidate";
  state.briefOpen = false;
  state.prompts.clear();
  state.painted = "";
  if (updateLocation) {
    historyPosition += 1;
    history.pushState({ studioPosition: historyPosition }, "", projectUrl(projectId, state.tab));
  }
  paintList();
  paint();
  return refresh();
}

function revision() {
  return state.data.revisions.find(({ id }) => id === state.data.project.current_revision_id);
}

function prepareDrafts({ keepScript }) {
  const current = revision();
  if (!keepScript && !state.script?.dirty && state.script?.base !== (current?.id ?? null)) {
    state.script = { base: current?.id ?? null, content: current?.content ?? "", dirty: false };
  }
  if (!state.script) state.script = { base: null, content: "", dirty: false };
  const version = state.data.project.timeline_version;
  if (!state.timeline?.dirty && state.timeline?.version !== version) {
    state.timeline = {
      version, dirty: false,
      scenes: state.data.scenes.map(({ id, duration_ms, trim_ms }) => ({
        scene_id: id, duration_ms, trim_ms,
      })),
    };
  }
}

async function refresh({ background = false } = {}) {
  const projectId = state.projectId;
  if (!projectId || !state.session) return;
  const request = ++refreshRequest;
  const result = await api(`/api/projects/${projectId}`);
  if (state.projectId !== projectId || !state.session || request <= appliedRefresh) return;
  appliedRefresh = request;
  state.data = result;
  const focus = document.activeElement;
  const isEditing = workspace.contains(focus) && focus?.matches("input, textarea, select");
  // Keep the buffer and its base together while the focused editor stays on screen.
  prepareDrafts({ keepScript: background && isEditing && focus.id === "script-draft" });
  showCredits(result.credits);
  const freshStamp = JSON.stringify(result, (key, value) => key === "url" ? undefined : value);
  if (!background || (freshStamp !== state.painted && !isEditing)) {
    state.painted = freshStamp;
    paint();
  } else {
    updateScriptControls();
  }
}

function heading() {
  const { title, brief } = state.data.project;
  return `<div class="project-head">
      <h1>${h(title)}</h1>
      <details class="brief"${state.briefOpen ? " open" : ""}>
        <summary>Creative brief</summary><p>${h(brief)}</p>
      </details>
    </div>
    <nav class="tabs" aria-label="Studio workflow" role="tablist">
      ${tabs.map((tab) => `<button class="tab" id="tab-${tab.toLowerCase()}" data-action="tab" data-value="${tab}"
        aria-selected="${state.tab === tab}" aria-controls="studio-stage" tabindex="${state.tab === tab ? 0 : -1}" role="tab">${tab}</button>`).join("")}
    </nav>`;
}

function signinView() {
  return `<section class="entry">
    <h1>Sign in to Studio</h1>
    <form id="signin-form" class="entry-form">
      <label>Workspace access key<input name="access_key" type="password" required maxlength="128"
        autocomplete="current-password" spellcheck="false"></label>
      <button class="primary" type="submit">Sign in</button>
    </form>
  </section>`;
}

function newProject() {
  if (!canEdit()) return '<div class="empty"><p>Select a project from the workspace.</p></div>';
  const demo = state.session.demo_provider;
  return `<section class="entry">
    <h1>Start a project</h1>
    <form id="project-form" class="entry-form">
      <label>Project name<input name="title" required maxlength="120" placeholder="Campaign name"
        value="${demo ? "Northline Cold Brew" : ""}"></label>
      <label>Creative brief<textarea name="brief" required rows="5" maxlength="6000"
        placeholder="What are you making, and who is it for?">${demo ? "A short product reel for a cold brew concentrate. Lead with motion in the first two seconds and finish with a clean packshot and a clear call to action." : ""}</textarea></label>
      <button class="primary" type="submit">Create project <span aria-hidden="true">→</span></button>
    </form>
  </section>`;
}

function jobPill(job) {
  return job ? `<span class="state ${h(job.state)}">${h(statuses[job.state])}</span>` : "";
}

function scriptNeedsRebase() {
  const draft = state.script;
  return draft.dirty && (draft.conflict || draft.base !== state.data.project.current_revision_id);
}

function updateScriptControls() {
  const status = workspace.querySelector("#script-save-state");
  if (!status) return;
  const draft = state.script;
  const conflict = scriptNeedsRebase();
  const label = conflict ? "Revision conflict" : draft.dirty ? "Unsaved changes" : draft.base ? "Saved" : "";
  if (status.textContent !== label) status.textContent = label;
  workspace.querySelector("#script-conflict").hidden = !conflict;
  workspace.querySelector('[data-action="save-script"]').disabled = !canEdit() || conflict || draft.pending;
  workspace.querySelector('[data-action="rebase-script"]').disabled = !canEdit() || draft.pending;
  const mainAction = draft.dirty ? (conflict ? "rebase-script" : "save-script")
    : !draft.base ? "assist" : state.data.scenes.length ? "tab" : "split";
  for (const button of workspace.querySelectorAll("[data-script-action]")) {
    button.classList.toggle("primary", button.dataset.action === mainAction);
  }
}

function scriptView() {
  const { revisions, jobs, scenes } = state.data;
  const draft = state.script;
  const job = jobs.find(({ kind }) => kind === "script");
  const current = revision();
  const cost = state.data.costs.script;
  return `
    <div class="script-layout">
      <section class="editor-panel">
        <div class="section-heading"><h2>Script draft</h2>${jobPill(job)}</div>
        <label class="visually-hidden" for="script-draft">Draft</label>
        <textarea id="script-draft" class="script-text" maxlength="6000"
          placeholder="Write from your brief or request an assisted draft.">
${h(draft.content)}</textarea>
        <div class="editor-tools">
          <div class="actions">
            <button data-script-action data-action="assist" ${active.has(job?.state) ? "disabled" : ""}>${current ? "Suggest revision" : "Draft with AI"}${cost ? ` · ${cost} ${cost === 1 ? "credit" : "credits"}` : ""}</button>
            <button data-script-action data-action="save-script">Save revision</button>
            ${current && !scenes.length ? `<button data-script-action data-action="split">Split into scenes <span aria-hidden="true">→</span></button>` : ""}
            ${scenes.length ? `<button data-script-action data-action="tab" data-value="Storyboard">Open storyboard <span aria-hidden="true">→</span></button>` : ""}
          </div>
          <span class="note" id="script-save-state" role="status"></span>
        </div>
        <div class="actions error-message" id="script-conflict" hidden>
          <p>A newer revision is available. Rebase to keep your text and save it as a new revision.</p>
          <button data-script-action data-action="rebase-script">Rebase draft</button>
        </div>
        ${job?.error ? `<p class="error-message">${h(job.error)}</p>` : ""}
      </section>
      <aside class="revisions-panel">
        <div class="section-heading"><h2>Revision history</h2></div>
        ${revisions.length ? `<ol class="revisions">${revisions.map((item, index) => `
          <li><div><strong>Revision ${revisions.length - index}</strong><small class="note">${item.source === "provider" ? "AI draft" : "Editor revision"}${item.id === current?.id ? " · Current" : ""}</small></div>
          <button class="quiet" data-action="load-revision" data-id="${h(item.id)}"
            aria-label="Load revision ${revisions.length - index}">Load</button></li>`).join("")}</ol>`
          : '<p class="note">No saved revisions yet.</p>'}
      </aside>
    </div>
    ${state.split ? splitForm() : ""}`;
}

function splitForm() {
  return `<form id="split-form" class="split-form">
    <div class="section-heading"><h2>Scene breakdown</h2></div>
    <div class="split-grid">${state.split.scenes.map((scene, index) => `
      <section>
        <label>Scene ${index + 1} title<input name="title-${index}" required maxlength="120" value="${h(scene.title)}"></label>
        <label>Visual prompt<textarea name="prompt-${index}" rows="4" required maxlength="6000">${h(scene.prompt)}</textarea></label>
        <label>Media type<select name="kind-${index}">
          <option value="image" ${scene.kind === "image" ? "selected" : ""}>Image</option>
          <option value="video" ${scene.kind === "video" ? "selected" : ""}>Video</option>
        </select></label>
      </section>`).join("")}</div>
    <div class="actions"><button class="primary" type="submit">Create scenes <span aria-hidden="true">→</span></button>
      <button class="quiet" type="button" data-action="cancel-split">Cancel</button></div>
  </form>`;
}

function durationText(milliseconds) {
  return `${(milliseconds / 1000).toLocaleString("en", { maximumFractionDigits: 1 })}s`;
}

function timecode(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function sceneNumber(index) {
  return String(index + 1).padStart(2, "0");
}

function isCandidate(scene) {
  return scene.candidate && scene.candidate.id !== scene.accepted?.id;
}

function sceneStatus(scene) {
  if (active.has(scene.job?.state)) return jobPill(scene.job);
  if (isCandidate(scene)) return '<span class="state review">Needs review</span>';
  if (scene.job?.error) return jobPill(scene.job);
  if (scene.accepted) return '<span class="state succeeded">Approved</span>';
  return '<span class="state idle">Awaiting media</span>';
}

function cutEntries() {
  return state.timeline.scenes.map((edit) => ({ edit, scene: findScene(edit.scene_id) }));
}

function sceneThumbnail(scene) {
  const asset = scene.accepted || scene.candidate;
  if (asset?.kind === "image") return `<img src="${h(asset.content_url)}" alt="">`;
  if (asset?.kind === "video") return `<video src="${h(asset.content_url)}#t=0.1" muted playsinline
    preload="metadata" aria-hidden="true" tabindex="-1"></video>`;
  return `<span class="frame-mark" aria-hidden="true"></span>`;
}

function sceneRail(entries, selected) {
  return `<nav class="scene-rail" aria-label="Scenes">
    <h2>Scenes</h2>
    <ol>${entries.map(({ edit, scene }, index) => `<li>
      <button class="scene-button" type="button" data-action="scene" data-id="${h(scene.id)}"
        aria-pressed="${selected === scene.id}">
        <span class="scene-print">${sceneThumbnail(scene)}<span class="scene-no">${sceneNumber(index)}</span></span>
        <span class="scene-row-text"><strong>${h(scene.title)}</strong>${sceneStatus(scene)}</span>
        <span class="scene-length">${durationText(edit.duration_ms)}</span>
      </button></li>`).join("")}</ol>
  </nav>`;
}

function preview(asset, name) {
  if (asset.kind === "image") return `<img src="${h(asset.content_url)}" alt="${h(name)}">`;
  return `<video src="${h(asset.content_url)}" data-asset="${h(asset.id)}" controls playsinline muted
    preload="metadata" aria-label="${h(name)}"></video>`;
}

function monitor({ asset, title, label, mode, toggles = "", placeholder }) {
  return `<section class="monitor" aria-label="${h(title)} preview">
    <div class="monitor-header"><span class="timecode">${h(label)}</span>${toggles}</div>
    <div class="monitor-frame">
      ${asset ? preview(asset, title) : `<div class="monitor-empty"><span class="frame-mark" aria-hidden="true"></span>
        <strong>${h(placeholder)}</strong></div>`}
    </div>
    <div class="monitor-footer"><span>${h(mode)}</span>${asset?.duration_ms ? `<span>${durationText(asset.duration_ms)} source</span>` : ""}</div>
  </section>`;
}

function sceneMonitor(scene, index) {
  const comparison = Boolean(isCandidate(scene) && scene.accepted);
  const showingApproved = comparison && state.mediaMode === "approved";
  const asset = showingApproved ? scene.accepted : scene.candidate || scene.accepted;
  const toggles = comparison ? `<div class="view-toggle" aria-label="Preview source">
    <button data-action="media-view" data-value="candidate" aria-pressed="${!showingApproved}">Candidate</button>
    <button data-action="media-view" data-value="approved" aria-pressed="${showingApproved}">Approved</button>
  </div>` : "";
  return monitor({
    asset, title: scene.title,
    label: `Scene ${sceneNumber(index)}`,
    mode: asset ? (asset.id === scene.accepted?.id ? "Approved in cut" : "Candidate preview") : "Preview",
    toggles, placeholder: `No ${scene.kind} yet`,
  });
}

function sceneInspector(scene, edit) {
  const { accepted, candidate, job, id, title, prompt, kind } = scene;
  const inProgress = active.has(job?.state);
  const newCandidate = isCandidate(scene);
  const hasTimingIssue = newCandidate && kind === "video" && candidate.duration_ms < edit.duration_ms + edit.trim_ms;
  const scenario = state.scenarios.get(id) || (candidate ? "normal" : "late");
  const generation = candidate ? "Regenerate" : "Generate";
  const credits = state.data.costs[kind];

  return `<article class="scene-details">
    <div class="scene-heading"><div><p class="eyebrow">${kind === "video" ? "Video" : "Image"}</p>
      <h2>${h(title)}</h2></div>${sceneStatus(scene)}</div>
    <div class="scene-feedback" aria-live="polite">
      ${job?.state === "confirming" ? '<p>Studio is checking the original request.</p>' : ""}
      ${newCandidate && accepted ? '<p>Your approved clip stays in the cut until you accept this candidate.</p>' : ""}
      ${hasTimingIssue ? `<p class="error-message">This clip is ${durationText(candidate.duration_ms)}. Shorten the saved timing before accepting it.</p>` : ""}
      ${job?.error ? `<p class="error-message">${h(job.error)}</p>` : ""}
    </div>
    <div class="scene-actions">
      ${job?.state === "confirming" ? `<button class="primary" data-action="retry" data-id="${h(id)}">Retry status</button>` : ""}
      ${newCandidate ? `<button class="${job?.state === "confirming" ? "" : "primary"}" data-action="accept" data-id="${h(id)}">Accept candidate <span aria-hidden="true">→</span></button>` : ""}
      <button class="${newCandidate || inProgress || accepted ? "quiet secondary-generation" : "primary"}" data-action="generate" data-id="${h(id)}" ${inProgress ? "disabled" : ""}>
        ${generation} · ${credits} credits</button>
    </div>
    ${job ? `<div class="job-line"><span class="note">${h(job.credit_amount)} credits ${h(job.credit_state)}</span>
      <button class="quiet" data-action="inspect" data-id="${h(job.id)}">Inspect job</button></div>` : ""}
    <details class="prompt"${(state.prompts.get(id) ?? Boolean(candidate || accepted)) ? " open" : ""}><summary>Visual prompt</summary><p>${h(prompt)}</p></details>
    ${kind === "video" && state.session.demo_provider ? `<label class="scenario">Provider scenario
      <select data-scenario="${h(id)}" ${inProgress ? "disabled" : ""}>
        <option value="normal" ${scenario === "normal" ? "selected" : ""}>Normal</option>
        <option value="late" ${scenario === "late" ? "selected" : ""}>Late result after timeout</option>
        <option value="expire" ${scenario === "expire" ? "selected" : ""}>Expires before result</option>
      </select></label>` : ""}
  </article>`;
}

function timelineView(entries, scene, edit, index) {
  const total = entries.reduce((sum, entry) => sum + entry.edit.duration_ms, 0);
  return `<form id="timeline-form" class="cut-panel">
    <div class="cut-heading"><h2>Cut order and timing</h2>
      <div class="actions cut-actions"><span class="note" id="cut-save-state">${state.timeline.dirty ? "Unsaved edits" : "Saved cut"}</span>
        <button class="quiet" id="cut-reset" type="button" data-action="reset-timeline" ${state.timeline.dirty ? "" : "hidden"}>Reset edits</button>
        <button class="primary" id="cut-save" type="submit" ${state.timeline.dirty ? "" : "disabled"}>Save timeline</button>
      </div></div>
    <div class="time-ruler"><span>00:00</span><span data-cut-mid>${timecode(total / 2)}</span><span data-cut-total>${timecode(total)}</span></div>
    <ol class="cut-track" aria-label="Cut timeline">${entries.map(({ edit: entry, scene: item }, position) => `
      <li data-clip-length="${Math.round(entry.duration_ms / 1000)}">
        <button type="button" class="clip" data-action="scene" data-id="${h(item.id)}" aria-pressed="${item.id === scene.id}">
          <span class="scene-no">${sceneNumber(position)}</span><strong>${h(item.title)}</strong>
          <span class="clip-duration">${durationText(entry.duration_ms)}</span>
        </button></li>`).join("")}</ol>
    <div class="timing">
      <strong class="timing-title">${h(scene.title)}</strong>
      <label>Start (s)<input name="trim_ms" data-timing="${h(scene.id)}" ${scene.kind === "image" ? "disabled" : ""} type="number"
        min="0" max="15" step=".1" required value="${edit.trim_ms / 1000}"></label>
      <label>Length (s)<input name="duration_ms" data-timing="${h(scene.id)}" type="number"
        min="1" max="15" step=".1" required value="${edit.duration_ms / 1000}"></label>
      <div class="actions move-actions">
        <button type="button" data-action="move" data-id="${h(scene.id)}" data-value="-1"
          aria-label="Move ${h(scene.title)} earlier" ${index === 0 ? "disabled" : ""}><span aria-hidden="true">←</span> Earlier</button>
        <button type="button" data-action="move" data-id="${h(scene.id)}" data-value="1"
          aria-label="Move ${h(scene.title)} later" ${index === entries.length - 1 ? "disabled" : ""}>Later <span aria-hidden="true">→</span></button>
      </div>
    </div>
  </form>`;
}

function storyboardView() {
  if (!state.data.scenes.length) {
    return `<div class="empty"><h2>No scenes yet</h2><p>Save a script and split it into scenes to start the storyboard.</p>
      <button data-action="tab" data-value="Script">Back to script</button></div>`;
  }
  const entries = cutEntries();
  const index = Math.max(0, entries.findIndex(({ scene }) => scene.id === state.sceneId));
  const { scene, edit } = entries[index];
  state.sceneId = scene.id;
  return `<div class="editing-deck">
      <div class="proof-stage">
        ${sceneMonitor(scene, index)}
        ${sceneRail(entries, scene.id)}
      </div>
      ${sceneInspector(scene, edit)}
    </div>
    ${timelineView(entries, scene, edit, index)}`;
}

function exportView() {
  const { jobs: allJobs, scenes, project } = state.data;
  const jobs = allJobs.filter(({ kind }) => kind === "export");
  const isCurrent = (item) => item.timeline_version === project.timeline_version;
  const job = jobs.find(({ id }) => id === state.exportId) || jobs.find(isCurrent) || jobs[0];
  const earlierCut = job && !isCurrent(job);
  const approved = scenes.filter((scene) => scene.accepted).length;
  const ready = scenes.length > 0 && approved === scenes.length;
  const pending = jobs.some(({ state }) => active.has(state));
  const selectedNumber = job ? jobs.length - jobs.indexOf(job) : 0;

  return `<div class="export-layout">
    ${monitor({
      asset: job?.asset, title: "Draft export", label: job ? `Export ${sceneNumber(selectedNumber - 1)}` : "Export preview",
      mode: earlierCut ? "Earlier cut · Vertical MP4" : "Vertical MP4 · 540 × 960",
      placeholder: pending ? "Rendering the cut" : "No preview yet",
    })}
    <section class="exports-panel">
      <div class="section-heading"><h2>Draft export</h2>${jobPill(job)}</div>
      <p class="note">${!scenes.length ? "Create scenes to start a cut." :
        ready ? `All ${approved} scenes approved` : `${approved} of ${scenes.length} scenes approved`}</p>
      ${earlierCut ? '<p class="note export-context">This export is from an earlier cut.</p>' : ""}
      ${job?.error ? `<p class="error-message">${h(job.error)}</p>` : ""}
      <div class="export-buttons">
        ${job?.asset && !earlierCut ? `<a class="button primary" href="${h(job.asset.content_url)}?download=true">Download MP4 <span aria-hidden="true">↓</span></a>` : ""}
        <button class="${job?.asset && !earlierCut ? "" : "primary"}" data-action="export" ${ready && !pending && !state.timeline.dirty ? "" : "disabled"}>${earlierCut ? "Export updated cut" : "Export MP4"}</button>
        ${job?.asset && earlierCut ? `<a class="button" href="${h(job.asset.content_url)}?download=true">Download earlier MP4 <span aria-hidden="true">↓</span></a>` : ""}
        ${state.timeline.dirty ? '<button class="quiet" data-action="tab" data-value="Storyboard">Save your timeline on the storyboard</button>' : ""}
      </div>
      ${job ? `<button class="quiet inspect-export" data-action="inspect" data-id="${h(job.id)}">Inspect job</button>` : ""}
      ${jobs.length ? `<section class="export-history"><h3>Export history</h3><ol>${jobs.map((item, index) => `
        <li><button class="export-row" data-action="show-export" data-id="${h(item.id)}" aria-pressed="${item.id === job.id}">
          <span><strong>Export ${sceneNumber(jobs.length - index - 1)}</strong>
            <small class="note">${isCurrent(item) ? "Current cut" : "Earlier cut"}</small></span>${jobPill(item)}</button></li>`).join("")}</ol></section>` : ""}
    </section>
  </div>`;
}

function auditView() {
  if (!state.audit) return "";
  const job = state.audit;
  return `<section class="audit-panel" id="job-inspector" tabindex="-1">
    <div class="section-heading"><h2>Job inspector</h2><button class="icon" data-action="close-audit" aria-label="Close inspector">×</button></div>
    <div class="audit-meta">${jobPill(job)} <code>${h(job.id)}</code></div>
    ${job.provider_run_id ? `<p class="note">Provider run <code>${h(job.provider_run_id)}</code></p>` : ""}
    ${job.credit ? `<p class="note">${job.credit.amount} credits · ${h(job.credit.state)}</p>` : ""}
    <ol class="event-list">${job.events.map(({ event }) => `<li>${h(event.replaceAll("_", " "))}</li>`).join("")}</ol>
    ${job.manifest ? `<details class="manifest"><summary>Export manifest</summary><ol>${job.manifest.scenes.map(
      (scene) => `<li><code>${h(scene.asset_id)}</code> · ${durationText(scene.duration_ms)}</li>`
    ).join("")}</ol></details>` : ""}
    <button data-action="inspect" data-id="${h(job.id)}">Refresh job</button>
  </section>`;
}

function paint() {
  updateUnloadWarning();
  workspace.setAttribute("aria-busy", String(!state.sessionLoaded || Boolean(state.session && state.projectId && !state.data)));
  if (!state.sessionLoaded) {
    workspace.innerHTML = '<div class="empty"><p>Connecting to Studio…</p></div>';
    return;
  }
  if (!state.session) {
    workspace.innerHTML = signinView();
    return;
  }
  if (!state.projectId) {
    workspace.innerHTML = newProject();
    return;
  }
  if (!state.data) {
    workspace.innerHTML = '<div class="empty"><p>Loading project…</p></div>';
    return;
  }
  const currentVideo = workspace.querySelector("video[data-asset]");
  const wasPlaying = currentVideo && !currentVideo.paused;
  const view = { Script: scriptView, Storyboard: storyboardView, Export: exportView }[state.tab];
  workspace.innerHTML = heading() + `<div id="studio-stage" role="tabpanel" aria-labelledby="tab-${state.tab.toLowerCase()}">${view()}</div>` + auditView();
  updateScriptControls();
  const nextVideo = workspace.querySelector("video[data-asset]");
  if (currentVideo && nextVideo?.dataset.asset === currentVideo.dataset.asset) {
    nextVideo.replaceWith(currentVideo);
    if (wasPlaying) currentVideo.play().catch(() => {});
  }
  if (!canEdit()) {
    const allowed = new Set(["tab", "inspect", "close-audit", "load-revision", "scene", "media-view", "show-export"]);
    for (const element of workspace.querySelectorAll("input, textarea, select, button")) {
      if (!allowed.has(element.dataset.action)) element.disabled = true;
    }
  }
}

function openSplit() {
  const current = revision();
  if (!current) return;
  if (state.script.dirty) throw new Error("Save your script before creating scenes");
  const paragraphs = current.content.split(/\n\s*\n/);
  state.split = {
    revisionId: current.id,
    scenes: [
      { title: "Product still", prompt: paragraphs[0], kind: "image", duration_ms: 4000 },
      { title: "Pour video", prompt: paragraphs.slice(1).join("\n\n") || state.data.project.brief,
        kind: "video", duration_ms: 6000 },
    ],
  };
  paint();
  document.querySelector("#split-form").scrollIntoView({ block: "nearest" });
}

function setTab(tab) {
  changeView(tab);
  paint();
  workspace.querySelector(`[role="tab"][data-value="${tab}"]`)?.focus({ preventScroll: true });
}

function changeView(tab) {
  state.tab = tab;
  state.audit = null;
  history.replaceState(history.state, "", projectUrl(state.projectId, tab));
}

function findScene(id) {
  return state.data.scenes.find((scene) => scene.id === id);
}

async function saveScript(projectPath, draft) {
  const { base, content } = draft;
  let saved;
  try {
    saved = await api(`${projectPath}/revisions`, "POST", { parent_id: base, content });
  } catch (error) {
    if (error.status !== 409) throw error;
    if (state.script === draft) {
      draft.conflict = true;
      draft.dirty = true;
    }
    return;
  }
  if (state.script !== draft) return;
  // A poll that started before the save may still return the previous revision.
  appliedRefresh = refreshRequest;
  if (state.data.project.current_revision_id === base) {
    state.data.project.current_revision_id = saved.id;
    state.data.revisions.unshift(saved);
  }
  draft.base = saved.id;
  draft.dirty = draft.content !== content;
  if (!draft.dirty) draft.content = saved.content;
  updateUnloadWarning();
  await refresh({ background: draft.dirty });
  message("Revision saved");
}

async function performAction(button) {
  const { action, id, value } = button.dataset;
  const projectPath = `/api/projects/${state.projectId}`;
  switch (action) {
    case "signout": {
      if (!confirmDiscardEdits()) return;
      await api("/api/session", "DELETE");
      requests.clear();
      return loadSession();
    }
    case "more": return loadList(true);
    case "new":
    case "select": {
      const target = id ?? null;
      if (target !== state.projectId && confirmDiscardEdits()) return selectProject(target);
      return;
    }
    case "tab": return setTab(value);
    case "scene": {
      if (state.sceneId === id) return;
      state.sceneId = id;
      state.mediaMode = "candidate";
      state.audit = null;
      const area = button.closest(".cut-track") ? ".cut-track" : ".scene-rail";
      paint();
      return workspace.querySelector(`${area} button[aria-pressed="true"]`)?.focus({ preventScroll: true });
    }
    case "media-view": {
      state.mediaMode = value;
      paint();
      return workspace.querySelector('.view-toggle [aria-pressed="true"]')?.focus({ preventScroll: true });
    }
    case "show-export": {
      state.exportId = id;
      state.audit = null;
      paint();
      return workspace.querySelector('.export-row[aria-pressed="true"]')?.focus({ preventScroll: true });
    }
    case "assist": {
      if (state.script.dirty) throw new Error("Save your edits before requesting an assisted revision");
      await queue(`${projectPath}/script-assist`, {
        parent_id: revision()?.id ?? null, quoted_credits: state.data.costs.script,
      });
      return refresh({ background: true });
    }
    case "save-script":
    case "rebase-script": {
      const draft = state.script;
      const rebase = action === "rebase-script";
      if (draft.pending || rebase !== scriptNeedsRebase()) return;
      draft.pending = true;
      updateScriptControls();
      try {
        if (!rebase) return await saveScript(projectPath, draft);
        await refresh({ background: true });
        if (state.script !== draft) return;
        draft.base = state.data.project.current_revision_id;
        draft.conflict = false;
      } finally {
        draft.pending = false;
        if (state.script === draft) {
          updateUnloadWarning();
          updateScriptControls();
        }
      }
      if (document.activeElement === document.body) {
        workspace.querySelector('[data-action="save-script"]')?.focus({ preventScroll: true });
      }
      return;
    }
    case "load-revision": {
      const item = state.data.revisions.find(({ id: revisionId }) => revisionId === id);
      state.script = { base: revision()?.id ?? null, content: item.content, dirty: true };
      return paint();
    }
    case "split": return openSplit();
    case "cancel-split": state.split = null; return paint();
    case "generate": {
      const scene = findScene(id);
      const cost = state.data.costs[scene.kind];
      if (scene.candidate && !window.confirm(`Generate another variant for ${cost} credits?`)) return;
      await queue(`/api/scenes/${id}/generations`, {
        intent: scene.candidate ? "regenerate" : "generate", quoted_credits: cost,
        scenario: scene.kind === "video" && state.session.demo_provider
          ? state.scenarios.get(id) || (scene.candidate ? "normal" : "late") : "normal",
      });
      state.scenarios.delete(id);
      break;
    }
    case "retry": {
      const scene = findScene(id);
      await api(`/api/scenes/${id}/generations/${scene.job.id}/retry`, "POST");
      message("Checking the original request. No additional credits held.");
      break;
    }
    case "accept": {
      await api(`/api/scenes/${id}/accepted/${findScene(id).candidate.id}`, "PUT");
      state.mediaMode = "candidate";
      message("Media accepted in the cut");
      break;
    }
    case "move": {
      const entries = state.timeline.scenes;
      const from = entries.findIndex((item) => item.scene_id === id);
      const [entry] = entries.splice(from, 1);
      entries.splice(from + Number(value), 0, entry);
      state.timeline.dirty = true;
      paint();
      return workspace.querySelector('.clip[aria-pressed="true"]')?.focus({ preventScroll: true });
    }
    case "reset-timeline": state.timeline = null; break;
    case "export": {
      if (state.timeline.dirty) throw new Error("Save your timeline edits before exporting");
      const result = await queue(`${projectPath}/exports`, { version: state.data.project.timeline_version });
      state.exportId = result.id;
      break;
    }
    case "inspect": {
      state.audit = await api(`/api/jobs/${id}`);
      paint();
      const inspector = document.querySelector("#job-inspector");
      inspector.scrollIntoView({ block: "nearest" });
      return inspector.focus({ preventScroll: true });
    }
    case "close-audit": state.audit = null; return paint();
    default: return;
  }
  await refresh();
}

async function act(button, operation) {
  if (button) button.disabled = true;
  try {
    await operation();
  } catch (error) {
    if (error.status === 401 && state.session) await loadSession();
    message(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const scriptWrite = ["save-script", "rebase-script"].includes(button.dataset.action);
  act(scriptWrite ? null : button, () => performAction(button));
});

workspace.addEventListener("keydown", (event) => {
  if (!event.target.matches('[role="tab"]')) return;
  const current = tabs.indexOf(state.tab);
  const next = { ArrowLeft: (current + tabs.length - 1) % tabs.length, ArrowRight: (current + 1) % tabs.length,
    Home: 0, End: tabs.length - 1 };
  if (!(event.key in next)) return;
  event.preventDefault();
  setTab(tabs[next[event.key]]);
});

workspace.addEventListener("input", ({ target }) => {
  if (target.id === "script-draft") {
    state.script.content = target.value;
    state.script.dirty = true;
    updateUnloadWarning();
    updateScriptControls();
  }
  if (target.dataset.timing && target.validity.valid) {
    const edit = state.timeline.scenes.find((item) => item.scene_id === target.dataset.timing);
    edit[target.name] = Math.round(Number(target.value) * 1000);
    state.timeline.dirty = true;
    updateUnloadWarning();
    document.querySelector("#cut-save-state").textContent = "Unsaved edits";
    document.querySelector("#cut-save").disabled = false;
    document.querySelector("#cut-reset").hidden = false;
    if (target.name === "duration_ms") {
      const clip = workspace.querySelector('.clip[aria-pressed="true"]');
      clip.parentElement.dataset.clipLength = Math.round(edit.duration_ms / 1000);
      clip.querySelector(".clip-duration").textContent = durationText(edit.duration_ms);
      workspace.querySelector('.scene-button[aria-pressed="true"] .scene-length').textContent = durationText(edit.duration_ms);
      const total = state.timeline.scenes.reduce((sum, scene) => sum + scene.duration_ms, 0);
      workspace.querySelector("[data-cut-mid]").textContent = timecode(total / 2);
      workspace.querySelector("[data-cut-total]").textContent = timecode(total);
    }
  }
  if (target.form?.id === "split-form") {
    const [field, index] = target.name.split("-");
    if (index !== undefined) state.split.scenes[index][field] = target.value;
  }
});

workspace.addEventListener("toggle", ({ target }) => {
  if (!target.isConnected) return;
  if (target.matches(".brief")) state.briefOpen = target.open;
  if (target.matches(".prompt")) state.prompts.set(state.sceneId, target.open);
}, true);

workspace.addEventListener("change", ({ target }) => {
  if (target.dataset.scenario) state.scenarios.set(target.dataset.scenario, target.value);
});

workspace.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const fields = new FormData(form);
  act(event.submitter, async () => {
    const projectPath = `/api/projects/${state.projectId}`;
    if (form.id === "signin-form") {
      await api("/api/session", "POST", Object.fromEntries(fields));
      await loadSession();
      await loadList();
      await refresh();
    } else if (form.id === "project-form") {
      const project = await api("/api/projects", "POST", Object.fromEntries(fields));
      await selectProject(project.id);
      await loadList();
    } else if (form.id === "split-form") {
      await api(`${projectPath}/scenes`, "POST", {
        revision_id: state.split.revisionId, scenes: state.split.scenes,
      });
      state.split = null;
      changeView("Storyboard");
      await refresh();
    } else if (form.id === "timeline-form") {
      const timeline = state.timeline;
      const { version } = timeline;
      const scenes = structuredClone(timeline.scenes);
      await api(`${projectPath}/timeline`, "PUT", { version, scenes });
      if (state.timeline !== timeline) return;
      timeline.version = version + 1;
      timeline.dirty = JSON.stringify(timeline.scenes) !== JSON.stringify(scenes);
      updateUnloadWarning();
      await refresh();
      message("Timeline saved");
    }
  });
});

window.addEventListener("popstate", (event) => {
  const projectId = new URL(location.href).searchParams.get("project");
  const position = event.state?.studioPosition;
  if (projectId !== state.projectId && !confirmDiscardEdits()) {
    // Back and Forward have already changed the active entry when popstate runs.
    if (Number.isInteger(position) && position !== historyPosition) {
      history.go(historyPosition - position);
    } else {
      history.pushState({ studioPosition: historyPosition }, "", projectUrl(state.projectId, state.tab));
    }
    return;
  }
  if (Number.isInteger(position)) historyPosition = position;
  if (projectId !== state.projectId) {
    selectProject(projectId, false).catch((error) => message(error.message, true));
  } else if (state.tab !== viewFromLocation()) {
    state.tab = viewFromLocation();
    state.audit = null;
    paint();
  }
});

async function boot() {
  paint();
  try {
    await loadSession();
    if (state.session) await loadList();
    if (state.session && state.projectId && !state.projects.some(({ id }) => id === state.projectId)) {
      const id = state.projectId;
      try { await refresh(); } catch (error) {
        if (error.status !== 404 && error.status !== 422) throw error;
        await selectProject(null);
        message(`Project ${id.slice(0, 8)} could not be found`, true);
      }
    } else {
      await refresh();
    }
  } catch (error) {
    message(error.message, true);
  }
  async function poll() {
    try {
      if (!document.hidden && state.session && state.projectId) {
        await refresh({ background: true });
        state.connectionError = false;
      }
    } catch (error) {
      if (error.status === 401) await loadSession();
      else if (!state.connectionError) {
        state.connectionError = true;
        message("Studio is reconnecting. Your last saved project is still visible.", true);
      }
    } finally {
      const running = state.data?.jobs.some((job) => active.has(job.state));
      setTimeout(poll, running ? 1400 : 7000);
    }
  }
  setTimeout(poll, 850);
}

boot();
