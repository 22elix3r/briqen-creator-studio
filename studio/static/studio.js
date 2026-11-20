const workspace = document.querySelector("#workspace");
const projectList = document.querySelector("#project-list");
const wallet = document.querySelector("#wallet");
const notice = document.querySelector("#notice");
const active = new Set(["queued", "running", "confirming"]);
const statuses = {
  queued: "Queued", running: "Working", confirming: "Checking generation",
  succeeded: "Ready", failed: "Failed", expired: "Expired",
};
const tabs = ["Script", "Storyboard", "Export"];
const requests = new Map();
const state = {
  projectId: new URL(location.href).searchParams.get("project"),
  projects: [], data: null, tab: "Script", painted: "", script: null,
  timeline: null, split: null, scenarios: new Map(), audit: null,
};
let noticeTimer;

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
  const response = await fetch(path, { method, headers, body: JSON.stringify(body) });
  if (response.status === 204) return null;
  const payload = await response.json();
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail : payload.detail?.map((item) => item.msg).join(". ");
    throw new ApiError(response.status, detail || "The request could not be completed");
  }
  return payload;
}

async function queue(path, body) {
  const action = JSON.stringify([path, body]);
  if (!requests.has(action)) requests.set(action, crypto.randomUUID());
  try {
    const result = await api(path, "POST", body, requests.get(action));
    requests.delete(action);
    return result;
  } catch (error) {
    if (error.status >= 400 && error.status < 500) requests.delete(action);
    throw error;
  }
}

function message(text, error = false) {
  clearTimeout(noticeTimer);
  notice.textContent = text;
  notice.classList.toggle("error", error);
  noticeTimer = setTimeout(() => { notice.textContent = ""; }, 4800);
}

function showCredits(credits) {
  wallet.textContent = `${credits.available} credits available${credits.held ? ` · ${credits.held} held` : ""}`;
}

async function loadList() {
  const response = await api("/api/projects");
  state.projects = response.projects;
  showCredits(response.credits);
  paintList();
}

function paintList() {
  projectList.innerHTML = state.projects.map((project) => `
    <button class="project-link" data-action="select" data-id="${h(project.id)}"
      aria-current="${project.id === state.projectId ? "page" : "false"}">${h(project.title)}</button>
  `).join("");
}

function selectProject(projectId, updateLocation = true) {
  state.projectId = projectId;
  state.data = null;
  state.script = null;
  state.timeline = null;
  state.split = null;
  state.audit = null;
  state.tab = "Script";
  state.painted = "";
  if (updateLocation) {
    const url = new URL(location.href);
    if (projectId) url.searchParams.set("project", projectId);
    else url.searchParams.delete("project");
    history.pushState({}, "", url);
  }
  paintList();
  paint();
  return refresh();
}

function revision() {
  return state.data.revisions.find(({ id }) => id === state.data.project.current_revision_id);
}

function prepareDrafts() {
  const current = revision();
  if (!state.script?.dirty && state.script?.base !== (current?.id ?? null)) {
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
  if (!projectId) return;
  const result = await api(`/api/projects/${projectId}`);
  if (state.projectId !== projectId) return;
  state.data = result;
  prepareDrafts();
  showCredits(result.credits);
  const freshStamp = JSON.stringify(result, (key, value) => key === "url" ? undefined : value);
  const focus = document.activeElement;
  const isEditing = workspace.contains(focus) && focus?.matches("input, textarea, select");
  if (!background || (freshStamp !== state.painted && !isEditing)) {
    state.painted = freshStamp;
    paint();
  }
}

function heading() {
  const { title, brief } = state.data.project;
  return `<div class="intro"><h1>${h(title)}</h1><p>${h(brief)}</p></div>
    <nav class="tabs" aria-label="Studio workflow" role="tablist">
      ${tabs.map((tab) => `<button class="tab" data-action="tab" data-value="${tab}"
        aria-selected="${state.tab === tab}" tabindex="${state.tab === tab ? 0 : -1}"
        role="tab">${tab}</button>`).join("")}
    </nav>`;
}

function newProject() {
  return `
    <div class="intro"><h1>Start a project</h1>
      <p>Turn a creative brief into a two-scene reel. Try the cold brew brief to see how a late video result is recovered.</p>
    </div>
    <form id="project-form" class="panel new-project">
      <label>Project name<input name="title" required maxlength="120" value="Northline Cold Brew"></label>
      <label>Creative brief<textarea name="brief" required rows="4" maxlength="6000">A short product reel for a cold brew concentrate. Lead with motion in the first two seconds and finish with a clean packshot and a clear call to action.</textarea></label>
      <button class="primary" type="submit">Create project</button>
    </form>`;
}

function jobPill(job) {
  return job ? `<span class="state ${h(job.state)}">${h(statuses[job.state])}</span>` : "";
}

function scriptView() {
  const { revisions, jobs, scenes } = state.data;
  const draft = state.script;
  const scriptJob = jobs.find(({ kind }) => kind === "script");
  const jobBusy = scriptJob && active.has(scriptJob.state);
  const current = revision();
  return `
    <div class="grid script-grid">
      <section class="panel">
        <div class="between"><h2>Script</h2>${jobPill(scriptJob)}</div>
        <label for="script-draft">Draft</label>
        <textarea id="script-draft" class="script-text" maxlength="6000" placeholder="Write from your brief or request an assisted draft.">${h(draft.content)}</textarea>
        <div class="actions">
          <button data-action="assist" ${jobBusy ? "disabled" : ""}>${current ? "Suggest revision" : "Draft with AI"}</button>
          <button class="primary" data-action="save-script">Save revision</button>
          ${current && !scenes.length ? '<button data-action="split">Split into scenes</button>' : ""}
          ${scenes.length ? '<button data-action="tab" data-value="Storyboard">Open storyboard</button>' : ""}
        </div>
        ${scriptJob?.error ? `<p class="error">${h(scriptJob.error)}</p>` : ""}
      </section>
      <section class="panel">
        <h2>Revision history</h2>
        ${revisions.length ? `<ol class="revisions">${revisions.map((item, index) => `
          <li><div>Revision ${revisions.length - index}<br><small class="note">${item.source === "provider" ? "Assisted draft" : "Editor revision"}${item.id === current?.id ? " · Current" : ""}</small></div>
          <button data-action="load-revision" data-id="${h(item.id)}">Load</button></li>`).join("")}</ol>`
          : '<p class="note">Saved revisions will appear here.</p>'}
      </section>
    </div>
    ${state.split ? splitForm() : ""}`;
}

function splitForm() {
  return `<form id="split-form" class="panel audit">
    <h2>Scene breakdown</h2>
    <div class="grid">${state.split.scenes.map((scene, index) => `
      <section>
        <label>Scene ${index + 1} title<input name="title-${index}" required maxlength="120" value="${h(scene.title)}"></label>
        <label>Visual prompt<textarea name="prompt-${index}" rows="4" required maxlength="6000">${h(scene.prompt)}</textarea></label>
        <label>Media type<select name="kind-${index}">
          <option value="image" ${scene.kind === "image" ? "selected" : ""}>Image</option>
          <option value="video" ${scene.kind === "video" ? "selected" : ""}>Video</option>
        </select></label>
      </section>`).join("")}</div>
    <div class="actions"><button class="primary" type="submit">Create scenes</button>
      <button type="button" data-action="cancel-split">Cancel</button></div>
  </form>`;
}

function preview(asset) {
  if (!asset) return '<span class="note">No media yet</span>';
  if (asset.kind === "image") return `<img src="${h(asset.url)}" alt="Generated scene preview">`;
  return `<video src="${h(asset.url)}" controls playsinline muted preload="metadata" aria-label="Generated scene preview"></video>`;
}

function sceneCard(scene) {
  const { accepted, candidate, job, id, title, prompt, kind } = scene;
  const inProgress = job && active.has(job.state);
  const newCandidate = candidate && candidate.id !== accepted?.id;
  const scenario = state.scenarios.get(id) || (candidate ? "normal" : "late");
  const credits = state.data.costs[kind];
  return `<article class="panel scene-card">
    <div class="between"><h2 class="scene-title">${h(title)}</h2><span class="note">${kind === "video" ? "Video" : "Image"}</span></div>
    <p>${h(prompt)}</p>
    <div class="scene-preview">${preview(candidate || accepted)}</div>
    <p class="note">${newCandidate ? "Candidate for review" : accepted ? '<span class="accepted">Accepted in cut</span>' : "Awaiting media"}${newCandidate && accepted ? " · Current cut keeps the previous asset" : ""}</p>
    ${kind === "video" ? `<label>Provider scenario
      <select data-scenario="${h(id)}" ${inProgress ? "disabled" : ""}>
        <option value="normal" ${scenario === "normal" ? "selected" : ""}>Normal</option>
        <option value="late" ${scenario === "late" ? "selected" : ""}>Late result after timeout</option>
        <option value="expire" ${scenario === "expire" ? "selected" : ""}>Expires before result</option>
      </select></label>` : ""}
    <div class="actions">
      <button data-action="generate" data-id="${h(id)}" ${inProgress ? "disabled" : ""}>
        ${candidate ? "Regenerate" : "Generate"} · ${credits} credits</button>
      ${newCandidate ? `<button class="primary" data-action="accept" data-id="${h(id)}">Accept media</button>` : ""}
      ${job?.state === "confirming" ? `<button class="primary" data-action="retry" data-id="${h(id)}">Retry status</button>` : ""}
    </div>
    ${job ? `<div class="actions audit">${jobPill(job)}<span class="note">${h(job.credit_amount)} credits ${h(job.credit_state)}</span>
      <button data-action="inspect" data-id="${h(job.id)}">Inspect job</button></div>` : ""}
    ${job?.error ? `<p class="error">${h(job.error)}</p>` : ""}
  </article>`;
}

function timelineView() {
  const { timeline, data } = state;
  return `<form class="panel" id="timeline-form">
    <h2>Cut order and timing</h2>
    <ol class="timeline">${timeline.scenes.map((edit, index) => {
      const item = data.scenes.find((scene) => scene.id === edit.scene_id);
      if (!item) return "";
      return `<li>
        <strong>${index + 1}. ${h(item.title)}</strong>
        <div class="timing">
          <label>Start (s)<input name="trim_ms" data-timing="${h(item.id)}" ${item.kind === "image" ? "disabled" : ""} type="number" min="0" max="15" step=".1" required value="${edit.trim_ms / 1000}"></label>
          <label>Length (s)<input name="duration_ms" data-timing="${h(item.id)}" type="number" min="1" max="15" step=".1" required value="${edit.duration_ms / 1000}"></label>
          <button type="button" data-action="move" data-id="${item.id}" data-value="-1" aria-label="Move ${h(item.title)} earlier" ${index === 0 ? "disabled" : ""}>↑</button>
          <button type="button" data-action="move" data-id="${item.id}" data-value="1" aria-label="Move ${h(item.title)} later" ${index === timeline.scenes.length - 1 ? "disabled" : ""}>↓</button>
        </div>
      </li>`;
    }).join("")}</ol>
    <div class="actions audit"><button class="primary" type="submit">Save timeline</button>
      <button type="button" data-action="reset-timeline">Reset edits</button></div>
  </form>`;
}

function storyboardView() {
  if (!state.data.scenes.length) return `<div class="empty"><p>Save a script and split it into scenes to start the storyboard.</p></div>`;
  return `<div class="stack"><div class="grid">${state.data.scenes.map(sceneCard).join("")}</div>${timelineView()}</div>`;
}

function exportView() {
  const jobs = state.data.jobs.filter(({ kind }) => kind === "export");
  const ready = state.data.scenes.length && state.data.scenes.every((scene) => scene.accepted);
  return `<div class="stack">
    <section class="panel">
      <h2>Draft export</h2>
      <p>${ready ? "Render the saved cut as a vertical MP4. This export keeps its scene order, timing, and accepted assets even if you edit again." : "Accept media for each scene before exporting."}</p>
      <button class="primary" data-action="export" ${ready ? "" : "disabled"}>Export MP4</button>
    </section>
    ${jobs.length ? `<div class="grid">${jobs.map((job) => `
      <article class="panel export-item">
        <div class="between"><h2>Draft render</h2>${jobPill(job)}</div>
        ${job.asset ? `${preview(job.asset)}
          <a class="button primary" href="${h(job.asset.url)}&amp;download=true">Download MP4</a>` : ""}
        ${job.error ? `<p class="error">${h(job.error)}</p>` : ""}
        <button data-action="inspect" data-id="${h(job.id)}">Inspect job</button>
      </article>`).join("")}</div>` : ""}
  </div>`;
}

function auditView() {
  if (!state.audit) return "";
  const job = state.audit;
  return `<section class="panel audit" id="job-inspector">
    <div class="between"><h2>Job inspector</h2><button class="icon" data-action="close-audit" aria-label="Close inspector">×</button></div>
    <div class="actions">${jobPill(job)} <code>${h(job.id)}</code></div>
    ${job.provider_run_id ? `<p class="note">Provider run <code>${h(job.provider_run_id)}</code></p>` : ""}
    ${job.credit ? `<p class="note">${job.credit.amount} credits · ${h(job.credit.state)}</p>` : ""}
    <ol>${job.events.map(({ event }) => `<li>${h(event.replaceAll("_", " "))}</li>`).join("")}</ol>
    ${job.manifest ? `<details><summary>Export manifest</summary><ol>${job.manifest.scenes.map(
      (scene) => `<li><code>${h(scene.asset_id)}</code> · ${scene.duration_ms / 1000}s</li>`
    ).join("")}</ol></details>` : ""}
    <button data-action="inspect" data-id="${h(job.id)}">Refresh job</button>
  </section>`;
}

function paint() {
  workspace.setAttribute("aria-busy", String(Boolean(state.projectId && !state.data)));
  if (!state.projectId) {
    workspace.innerHTML = newProject();
    return;
  }
  if (!state.data) {
    workspace.innerHTML = '<div class="empty"><p>Loading project…</p></div>';
    return;
  }
  const view = { Script: scriptView, Storyboard: storyboardView, Export: exportView }[state.tab];
  workspace.innerHTML = heading() + `<div role="tabpanel" aria-label="${state.tab}">${view()}</div>` + auditView();
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
  state.tab = tab;
  state.audit = null;
  paint();
  workspace.querySelector(`[role="tab"][data-value="${tab}"]`)?.focus({ preventScroll: true });
}

function findScene(id) {
  return state.data.scenes.find((scene) => scene.id === id);
}

async function performAction(button) {
  const { action, id, value } = button.dataset;
  const projectPath = `/api/projects/${state.projectId}`;
  switch (action) {
    case "new": return selectProject(null);
    case "select": return selectProject(id);
    case "tab": return setTab(value);
    case "assist": {
      if (state.script.dirty) throw new Error("Save your edits before requesting an assisted revision");
      await queue(`${projectPath}/script-assist`, { parent_id: revision()?.id ?? null });
      break;
    }
    case "save-script": {
      await api(`${projectPath}/revisions`, "POST", {
        parent_id: state.script.base, content: state.script.content,
      });
      state.script = null;
      message("Revision saved");
      break;
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
        scenario: scene.kind === "video"
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
      message("Media accepted in the cut");
      break;
    }
    case "move": {
      const entries = state.timeline.scenes;
      const from = entries.findIndex((item) => item.scene_id === id);
      const [entry] = entries.splice(from, 1);
      entries.splice(from + Number(value), 0, entry);
      state.timeline.dirty = true;
      return paint();
    }
    case "reset-timeline": state.timeline = null; break;
    case "export": {
      if (state.timeline.dirty) throw new Error("Save your timeline edits before exporting");
      await queue(`${projectPath}/exports`, { version: state.data.project.timeline_version });
      break;
    }
    case "inspect": {
      state.audit = await api(`/api/jobs/${id}`);
      paint();
      return document.querySelector("#job-inspector").scrollIntoView({ block: "nearest" });
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
    message(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button) act(button, () => performAction(button));
});

workspace.addEventListener("keydown", (event) => {
  if (!event.target.matches('[role="tab"]')) return;
  const current = tabs.indexOf(state.tab);
  const next = { ArrowLeft: (current + 2) % 3, ArrowRight: (current + 1) % 3, Home: 0, End: 2 };
  if (!(event.key in next)) return;
  event.preventDefault();
  setTab(tabs[next[event.key]]);
});

workspace.addEventListener("input", ({ target }) => {
  if (target.id === "script-draft") {
    state.script.content = target.value;
    state.script.dirty = true;
  }
  if (target.dataset.timing && target.value !== "") {
    const edit = state.timeline.scenes.find((item) => item.scene_id === target.dataset.timing);
    edit[target.name] = Math.round(Number(target.value) * 1000);
    state.timeline.dirty = true;
  }
  if (target.form?.id === "split-form") {
    const [field, index] = target.name.split("-");
    if (index !== undefined) state.split.scenes[index][field] = target.value;
  }
});

workspace.addEventListener("change", ({ target }) => {
  if (target.dataset.scenario) state.scenarios.set(target.dataset.scenario, target.value);
});

workspace.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const fields = new FormData(form);
  act(event.submitter, async () => {
    const projectPath = `/api/projects/${state.projectId}`;
    if (form.id === "project-form") {
      const project = await api("/api/projects", "POST", Object.fromEntries(fields));
      await selectProject(project.id);
      await loadList();
    } else if (form.id === "split-form") {
      await api(`${projectPath}/scenes`, "POST", {
        revision_id: state.split.revisionId, scenes: state.split.scenes,
      });
      state.split = null;
      state.tab = "Storyboard";
      await refresh();
    } else if (form.id === "timeline-form") {
      await api(`${projectPath}/timeline`, "PUT", {
        version: state.timeline.version, scenes: state.timeline.scenes,
      });
      state.timeline = null;
      await refresh();
      message("Timeline saved");
    }
  });
});

window.addEventListener("popstate", () => {
  selectProject(new URL(location.href).searchParams.get("project"), false)
    .catch((error) => message(error.message, true));
});

async function boot() {
  paint();
  try {
    await loadList();
    if (state.projectId && !state.projects.some(({ id }) => id === state.projectId)) {
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
      if (!document.hidden) await refresh({ background: true });
    } catch (error) {
      if (state.data) console.warn("Studio status polling:", error.message);
    } finally {
      setTimeout(poll, 950);
    }
  }
  setTimeout(poll, 950);
}

boot();
