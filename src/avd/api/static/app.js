"use strict";

// Explicit route templates are checked against OpenAPI by test_ui.py.
const API_ROUTES = Object.freeze({
  projects: "/projects",
  source: "/projects/{project_id}/source",
  uploads: "/uploads",
  characters: "/characters",
  sheet: "/projects/{project_id}/character-sheet",
  voices: "/voices",
  characterConsent: "/characters/{asset_id}/consent",
  voiceConsent: "/voices/{asset_id}/consent",
  assets: "/projects/{project_id}/assets",
  chat: "/projects/{project_id}/chat",
  status: "/projects/{project_id}/status",
  versions: "/projects/{project_id}/nodes/{node_id}/versions",
  revert: "/projects/{project_id}/nodes/{node_id}/revert",
  jobs: "/projects/{project_id}/jobs",
  approve: "/projects/{project_id}/approve",
  budget: "/projects/{project_id}/approve-budget",
  tools: "/projects/{project_id}/tools/{tool}",
  events: "/projects/{project_id}/events",
  media: "/media/{path}"
});
const $ = (selector) => document.querySelector(selector);
const state = {
  projectId: "", project: null, projects: [], characters: [], voices: [], jobs: [],
  stream: null, refreshPromise: null, refreshAgain: false, refreshingProject: "",
  lastRefresh: 0, busy: new Set(), signatures: new Map(), noticeTimer: null
};
const STAGES = [
  ["structure", "结构", "analysis"], ["script", "脚本", "script"],
  ["frames", "首帧", "shot_frame"], ["final", "成片", "final"]
];
const KIND_LABELS = {
  analysis: "样片分析", script: "脚本", shot_frame: "首帧", shot_video: "镜头视频",
  voiceover: "配音", lipsync: "口型合成", subtitles: "字幕视频", final: "最终成片",
  analyze_video: "分析样片", write_script: "编写脚本", image_gen: "生成首帧", image_to_video: "生成视频", tts: "生成配音"
};
const STATUS_LABELS = {
  clean: "已就绪", dirty: "待生成", queued: "排队中", running: "生成中",
  failed: "失败", succeeded: "已完成", cancelled: "已取消"
};

function el(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function route(name, params = {}) {
  const values = {project_id: state.projectId, ...params};
  return API_ROUTES[name].replace(/\{(\w+)\}/g, (_, key) => {
    if (!values[key]) throw new Error("请先选择项目或素材。");
    return encodeURIComponent(values[key]);
  });
}
function mediaURL(path) {
  const parts = String(path).split("/");
  if (parts.some((part) => !part || part === "." || part === "..") || /[\\:\x00]/.test(path)) {
    throw new Error("输出文件路径无效。");
  }
  return API_ROUTES.media.replace("{path}", parts.map(encodeURIComponent).join("/"));
}
function showError(error) {
  $("#error").textContent = error instanceof Error ? error.message : String(error);
  $("#error").hidden = false;
}
function notify(text) {
  clearTimeout(state.noticeTimer);
  $("#notice").textContent = text;
  $("#notice").hidden = false;
  state.noticeTimer = setTimeout(() => { $("#notice").hidden = true; }, 7000);
}
async function request(name, {method = "GET", body, params = {}} = {}) {
  const options = {method, headers: {Accept: "application/json"}};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(route(name, params), options); }
  catch { throw new Error("无法连接服务器。请检查服务是否正在运行，再点击刷新。"); }
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : null; }
  catch { data = {detail: raw.slice(0, 500)}; }
  if (!response.ok) {
    let detail = data?.detail || `HTTP ${response.status}`;
    if (Array.isArray(detail)) detail = detail.map((item) => `${item.loc?.join(".") || "输入"}: ${item.msg}`).join("\n");
    else if (typeof detail !== "string") detail = JSON.stringify(detail);
    const hints = {403: "请先确认素材授权。", 409: "请检查阶段审批、预算授权或正在进行的任务。", 413: "文件超过服务配置的大小限制。", 415: "请使用支持的媒体格式。", 422: "请检查表单与项目状态。"};
    throw new Error(`${hints[response.status] || "操作未完成。"}\n${detail}`);
  }
  return data;
}
async function runAction(button, action) {
  if (button && state.busy.has(button)) return;
  const caption = button?.textContent;
  if (button) { state.busy.add(button); button.disabled = true; button.textContent = "处理中…"; }
  $("#error").hidden = true;
  try { await action(); }
  catch (error) { showError(error); }
  finally {
    if (button) { state.busy.delete(button); button.textContent = caption; button.disabled = false; }
    updateControls();
  }
}
function bindForm(id, action) {
  const form = $(id);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (form.reportValidity()) void runAction(form.querySelector('button[type="submit"]'), () => action(form));
  });
}
function stableRender(key, value, render) {
  const signature = JSON.stringify(value);
  if (state.signatures.has(key) && state.signatures.get(key) === signature) return;
  state.signatures.set(key, signature);
  render();
}
function fillSelect(select, items, selected, placeholder) {
  select.replaceChildren(new Option(placeholder, ""));
  items.forEach((item) => select.add(new Option(item.label || item.name || item.title, item.id)));
  if (items.some((item) => item.id === selected)) select.value = selected;
}
async function loadProjects() {
  state.projects = await request("projects");
  fillSelect($("#project-select"), state.projects, state.projectId, "选择或新建项目");
  $("#new-project").open = state.projects.length === 0;
}
async function loadAssets(preferred = {}) {
  const [characters, voices] = await Promise.all([request("characters"), request("voices")]);
  state.characters = characters;
  state.voices = voices;
  const selectedCharacter = preferred.character || $("#character-select").value || state.project?.character_id;
  const selectedVoice = preferred.voice || $("#voice-select").value || state.project?.voice_id;
  const label = (asset) => ({...asset, label: `${asset.name} · ${asset.consent_confirmed ? "已授权" : "待授权"}`});
  fillSelect($("#character-select"), characters.map(label), selectedCharacter, "选择角色");
  fillSelect($("#voice-select"), voices.map(label), selectedVoice, "选择声音");
  const unconfirmed = [
    ...characters.filter((item) => !item.consent_confirmed).map((item) => ({id: `character:${item.id}`, label: `角色 · ${item.name}`})),
    ...voices.filter((item) => !item.consent_confirmed).map((item) => ({id: `voice:${item.id}`, label: `声音 · ${item.name}`}))
  ];
  const previousConsent = $("#consent-select").value;
  fillSelect($("#consent-select"), unconfirmed, preferred.consent || previousConsent, "选择待授权素材");
  if (previousConsent !== $("#consent-select").value) $("#consent-form").elements.confirmed.checked = false;
  updateControls();
}
async function selectProject(id) {
  state.stream?.close();
  state.stream = null;
  state.projectId = id;
  state.project = null;
  state.jobs = [];
  state.signatures.clear();
  $("#project-select").value = id;
  $("#chat-message").value = "";
  $("#script-form").reset();
  $("#budget-form").reset();
  $("#review-actor").value = "";
  $("#error").hidden = true;
  try { localStorage.setItem("avd.project", id); } catch { /* Storage can be disabled. */ }
  renderProject(); renderJobs(); renderChat([]);
  if (!id) { setConnection("选择项目后开启实时更新"); return; }
  setConnection("正在同步项目…");
  await refreshProject(id);
  if (state.projectId !== id) return;
  $("#character-select").value = state.project?.character_id || "";
  $("#voice-select").value = state.project?.voice_id || "";
  updateControls();
  openEvents(id);
}
function setConnection(text, connected = false) {
  $("#connection").textContent = text;
  $("#connection").classList.toggle("live", connected);
}
function openEvents(projectId) {
  if (typeof EventSource === "undefined") { setConnection("定时同步中"); return; }
  const stream = new EventSource(route("events", {project_id: projectId}));
  state.stream = stream;
  stream.addEventListener("open", () => { if (state.projectId === projectId) setConnection("实时同步已连接", true); });
  stream.addEventListener("job_finished", () => {
    if (state.projectId === projectId) void refreshProject(projectId).catch(showError);
  });
  stream.addEventListener("error", () => { if (state.projectId === projectId) setConnection("实时连接重试中 · 定时同步"); });
}
async function refreshProject(projectId = state.projectId) {
  if (!projectId) return;
  if (state.refreshPromise && state.refreshingProject === projectId) {
    state.refreshAgain = true;
    return state.refreshPromise;
  }
  state.refreshingProject = projectId;
  const task = (async () => {
    const params = {project_id: projectId};
    const [project, jobs, history] = await Promise.all([
      request("status", {params}), request("jobs", {params}), request("chat", {params})
    ]);
    if (state.projectId !== projectId) return;
    state.project = project; state.jobs = jobs; state.lastRefresh = Date.now();
    renderProject(); renderJobs(); renderChat(history);
  })();
  state.refreshPromise = task;
  try { await task; }
  finally {
    if (state.refreshPromise === task) {
      state.refreshPromise = null;
      if (state.refreshAgain) {
        state.refreshAgain = false;
        if (state.projectId === projectId) void refreshProject(projectId).catch(showError);
      }
    }
  }
}
function stageReady(stage) {
  if (!state.project) return false;
  const index = STAGES.findIndex(([key]) => key === stage);
  const kinds = STAGES.slice(0, index + 1).map(([, , kind]) => kind);
  const nodes = state.project.nodes.filter((node) => kinds.includes(node.kind));
  const pending = state.jobs.some((job) => ["queued", "running"].includes(job.status) &&
    (job.job_type === "analyze_video" || (stage !== "structure" && job.job_type === "write_script")));
  return !pending && kinds.every((kind) => nodes.some((node) => node.kind === kind)) &&
    nodes.every((node) => node.status === "clean" && node.current_version_id);
}
function updateControls() {
  document.querySelectorAll("[data-project]").forEach((button) => { button.disabled = !state.project || state.busy.has(button); });
  const reviews = state.project?.reviews || {};
  const disable = (id, condition) => { const button = $(id); button.disabled = condition || state.busy.has(button); };
  disable("#analyze", !state.project?.source_video_path);
  disable("#frames", !reviews.script?.approved);
  disable("#rebuild", !reviews.script?.approved);
  disable("#script-form button", !reviews.structure?.approved || !state.project?.character_id || !state.project?.voice_id);
  document.querySelectorAll("[data-stage]").forEach((button) => {
    button.disabled = !stageReady(button.dataset.stage) || reviews[button.dataset.stage]?.approved || !$("#review-actor").value.trim() || state.busy.has(button);
  });
  const character = state.characters.find((item) => item.id === $("#character-select").value);
  const voice = state.voices.find((item) => item.id === $("#voice-select").value);
  disable("#assets-form button", !state.project || !character?.consent_confirmed || !voice?.consent_confirmed);
  disable("#consent-form button", !$("#consent-select").value);
}
function renderCheckpoints() {
  const container = $("#checkpoints");
  container.replaceChildren();
  STAGES.forEach(([stage, title], index) => {
    const review = state.project?.reviews?.[stage];
    const card = el("div", null, `checkpoint${review?.approved ? " approved" : ""}`);
    card.append(el("div", index === 0 ? "STRUCTURE REVIEW" : `CHECKPOINT 0${index}`, "step-label"), el("h3", title));
    card.append(el("p", review?.approved ? `已由 ${review.approved_by} 确认` : stageReady(stage) ? "请查看当前内容，再确认通过。" : "等待内容完成与上游就绪。"));
    const button = el("button", review?.approved ? "已确认 ✓" : `确认${title}`);
    button.type = "button"; button.dataset.stage = stage;
    button.addEventListener("click", () => void runAction(button, async () => {
      const actor = $("#review-actor").value.trim();
      if (!actor) throw new Error("请先填写审核人姓名。");
      const projectId = state.projectId;
      await request("approve", {method: "POST", params: {project_id: projectId}, body: {stage, approved_by: actor}});
      notify(`${title}已确认。`);
      await refreshProject(projectId);
    }));
    card.append(button); container.append(card);
  });
}
function descriptionList(entries) {
  const list = el("dl");
  entries.forEach(([name, value]) => {
    if (value !== undefined && value !== null && value !== "") list.append(el("dt", name), el("dd", value));
  });
  return list;
}
function renderAnalysis() {
  const container = $("#analysis-content");
  container.replaceChildren();
  const template = state.project?.analysis_result;
  $("#structure-status").textContent = template ? "可查看" : "等待分析";
  if (!template) { container.append(el("p", "上传并分析样片后，在这里查看结构和节奏。", "empty")); return; }
  container.append(descriptionList([
    ["总时长", `${template.total_duration_s} 秒`], ["开场", template.hook],
    ["节奏", template.pacing_notes], ["音乐", template.music_style], ["声音", template.voice_style]
  ]));
  (template.shots || []).forEach((shot, index) => {
    const item = el("div", null, "review-shot");
    item.append(el("h3", `${String(index + 1).padStart(2, "0")} · ${shot.duration_s} 秒`), descriptionList([
      ["景别", shot.shot_size], ["运镜", shot.camera_move], ["画面", shot.visual_description], ["台词目的", shot.spoken_line_purpose]
    ]));
    container.append(item);
  });
}
function renderScript() {
  const container = $("#script-content");
  container.replaceChildren();
  const script = state.project?.nodes.find((node) => node.kind === "script");
  const storyboard = script?.recipe;
  $("#script-status").textContent = script ? `v${script.current_version?.number || "—"}` : "等待编写";
  if (!storyboard?.shots) { container.append(el("p", "确认结构、关联素材并填写创作要求后，开始编写。", "empty")); return; }
  container.append(el("h3", storyboard.title));
  storyboard.shots.forEach((shot) => {
    const item = el("div", null, "review-shot");
    item.append(el("h3", `镜头 ${shot.index} · ${shot.duration_s} 秒`), el("p", shot.line || "（无台词）", "spoken-line"), descriptionList([
      ["画面", shot.visual_prompt], ["运镜", shot.camera], ["情绪", shot.emotion]
    ]));
    container.append(item);
  });
}
function outputPaths(value) {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(outputPaths);
  if (value && typeof value === "object") return Object.values(value).flatMap(outputPaths);
  return [];
}
function renderMedia(container, outputs) {
  container.replaceChildren();
  const supported = /\.(png|jpe?g|webp|gif|mp4|mov|webm|mkv|avi|wav|mp3|flac|m4a|ogg|aac|srt|vtt)$/i;
  const paths = [...new Set(outputPaths(outputs))].filter((path) => supported.test(path));
  if (!paths.length) { container.append(el("p", "尚无可预览输出", "empty")); return; }
  paths.forEach((path) => {
    let url;
    try { url = mediaURL(path); }
    catch (error) { container.append(el("p", error.message, "job-error")); return; }
    let media;
    if (/\.(png|jpe?g|webp|gif)$/i.test(path)) {
      media = el("img"); media.alt = "镜头输出预览"; media.loading = "lazy";
    } else if (/\.(mp4|mov|webm|mkv|avi)$/i.test(path)) {
      media = el("video"); media.controls = true; media.preload = "metadata"; media.playsInline = true;
    } else if (/\.(wav|mp3|flac|m4a|ogg|aac)$/i.test(path)) {
      media = el("audio"); media.controls = true; media.preload = "none";
    }
    if (media) {
      media.src = url;
      media.addEventListener("error", () => {
        if (!container.querySelector(".media-error")) container.append(el("p", "预览暂不可用，可打开文件查看或检查任务输出。", "hint media-error"));
      }, {once: true});
      container.append(media);
    }
    const link = el("a", path.split("/").pop());
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    container.append(link);
  });
}
function renderNode(node) {
  const projectId = state.projectId;
  const container = el("div", null, "node-card");
  const heading = el("div", null, "node-heading");
  heading.append(el("h3", KIND_LABELS[node.kind] || node.kind), el("span", STATUS_LABELS[node.status] || node.status, `badge ${node.status}`));
  const preview = el("div", null, "media-preview");
  renderMedia(preview, node.output_paths);
  const row = el("div", null, "version-row");
  const select = el("select");
  select.setAttribute("aria-label", `${KIND_LABELS[node.kind] || node.kind}版本`);
  select.add(new Option(node.current_version ? `v${node.current_version.number} · 当前` : "暂无版本", ""));
  select.disabled = true;
  const revert = el("button", "回退");
  revert.type = "button"; revert.disabled = true;
  const note = el("p", "", "version-note");
  row.append(select, revert); container.append(heading, preview, row, note);
  if (!node.current_version_id) return container;
  const params = {project_id: projectId, node_id: node.id};
  request("versions", {params}).then((versions) => {
    if (!container.isConnected || state.projectId !== projectId) return;
    select.replaceChildren();
    versions.slice().reverse().forEach((version) => select.add(new Option(
      `v${version.number}${version.id === node.current_version_id ? " · 当前" : ""}`, String(version.number)
    )));
    select.value = String(node.current_version.number);
    select.disabled = false;
    const changed = () => {
      const version = versions.find((item) => item.number === Number(select.value));
      const pending = state.jobs.some((job) => job.node_id === node.id && ["queued", "running"].includes(job.status));
      revert.disabled = !version || version.id === node.current_version_id || pending || state.busy.has(revert);
      renderMedia(preview, version?.output_paths || {});
      note.textContent = version?.note || "";
    };
    select.addEventListener("change", changed);
    revert.addEventListener("click", () => void runAction(revert, async () => {
      await request("revert", {method: "POST", params, body: {version_number: Number(select.value)}});
      notify("已恢复所选版本；受影响的后续内容需要增量生成并重新审核。");
      await refreshProject(projectId);
    }));
    changed();
  }).catch((error) => { if (container.isConnected) { note.textContent = error.message; note.classList.add("job-error"); } });
  return container;
}
function renderShots() {
  const container = $("#shot-grid");
  container.replaceChildren();
  const nodes = state.project?.nodes || [];
  const indexes = [...new Set(nodes.map((node) => node.shot_index).filter((index) => index !== null))].sort((a, b) => a - b);
  $("#shot-count").textContent = `${indexes.length} 个镜头`;
  if (!indexes.length) container.append(el("div", "每个镜头的画面、视频和声音将在这里呈现。\n你可以独立查看版本，或在聊天中提出修改。", "empty empty-large"));
  const order = ["shot_frame", "shot_video", "voiceover", "lipsync", "subtitles"];
  indexes.forEach((index) => {
    const card = el("article", null, "shot-card");
    card.append(el("h3", `镜头 ${String(index).padStart(2, "0")}`));
    nodes.filter((node) => node.shot_index === index).sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)).forEach((node) => card.append(renderNode(node)));
    container.append(card);
  });
  const final = nodes.find((node) => node.kind === "final");
  $("#final-output").replaceChildren(final ? renderNode(final) : el("p", "首帧确认后，继续生成视频、配音、口型和字幕。", "empty"));
}
function renderProject() {
  const project = state.project;
  $("#project-title").textContent = project?.title || "开始你的下一部短片";
  $("#project-subtitle").textContent = project ? "先看结构，再定脚本；首帧满意后，让镜头动起来。" : "新建项目，上传参考视频，再与导演一起完善作品。";
  const amount = (value) => Number(value || 0).toFixed(2);
  $("#budget-spent").textContent = project ? `$${amount(project.budget.spent)} / ${project.budget.limit === null ? "不限" : `$${amount(project.budget.limit)}`}` : "—";
  $("#budget-remaining").textContent = project ? `USD · ${project.budget.remaining === null ? "未设上限" : `剩余 $${amount(project.budget.remaining)}`}` : "USD";
  const character = state.characters.find((item) => item.id === project?.character_id);
  const voice = state.voices.find((item) => item.id === project?.voice_id);
  $("#attached-assets").textContent = character && voice ? `已关联：${character.name} / ${voice.name}` : "选择并关联已授权的角色与声音。";
  stableRender("source", project?.source_video_path, () => {
    $("#source-preview").replaceChildren();
    if (project?.source_video_path) renderMedia($("#source-preview"), project.source_video_path);
  });
  stableRender("analysis", project?.analysis_result, renderAnalysis);
  stableRender("script", project?.nodes.filter((node) => node.kind === "script"), renderScript);
  stableRender("shots", [project?.nodes, state.jobs.filter((job) => ["queued", "running"].includes(job.status)).map((job) => job.node_id)], renderShots);
  stableRender("reviews", [project?.reviews, project?.nodes.map((node) => [node.id, node.status, node.current_version_id]), state.jobs.map((job) => [job.job_type, job.status])], renderCheckpoints);
  updateControls();
}
function renderJobs() {
  stableRender("jobs", state.jobs, () => {
    const container = $("#jobs");
    container.replaceChildren();
    const pending = state.jobs.filter((job) => ["queued", "running"].includes(job.status));
    $("#job-count").textContent = state.jobs.length ? `${pending.length} 进行中 · ${state.jobs.length} 个任务` : "暂无任务";
    if (!state.jobs.length) container.append(el("p", "生成任务会保存在项目中。", "empty"));
    state.jobs.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).forEach((job) => {
      const row = el("div", null, "job");
      const title = el("div", KIND_LABELS[job.job_type] || job.job_type);
      title.append(el("small", ` · ${job.id.slice(0, 8)} · 尝试 ${job.attempts}/${job.max_attempts}`));
      row.append(title, el("span", STATUS_LABELS[job.status] || job.status, `badge ${job.status}`));
      if (job.error) row.append(el("p", job.error, "job-error"));
      if (job.job_type === "character_sheet" && job.result?.outputs?.image) {
        const preview = el("div", null, "media-preview");
        renderMedia(preview, job.result.outputs);
        const select = el("button", "查看并授权此角色");
        select.addEventListener("click", () => void runAction(select, async () => {
          await loadAssets({character: job.result.character_id, consent: `character:${job.result.character_id}`});
          $("#consent-panel").open = true;
        }));
        row.append(preview, select);
      }
      container.append(row);
    });
  });
}
function messageText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.filter((item) => item.type === "text").map((item) => item.text || "").join("\n");
  return content ? JSON.stringify(content, null, 2) : "";
}
function renderChat(history) {
  stableRender("chat", history, () => {
    const container = $("#chat-history");
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
    container.replaceChildren();
    if (!history.length) container.append(el("p", "告诉导演你想创作的内容。也可以先上传样片，使用左侧与中间的操作按钮。", "empty"));
    history.forEach((message) => {
      const text = messageText(message.content);
      if (message.role === "tool") {
        const detail = el("details", null, "chat-tool");
        detail.append(el("summary", `执行记录 · ${message.name || "工具"}`), el("pre", text));
        container.append(detail);
      } else if (["user", "assistant"].includes(message.role) && text) {
        const bubble = el("div", null, `chat-message ${message.role}`);
        bubble.append(el("span", message.role === "user" ? "你" : "导演", "author"), el("div", text));
        container.append(bubble);
      }
    });
    if (nearBottom) container.scrollTop = container.scrollHeight;
  });
}
async function upload(file) {
  const body = new FormData(); body.append("file", file);
  return (await request("uploads", {method: "POST", body})).path;
}
async function callTool(tool, args = {}) {
  const projectId = state.projectId;
  const data = await request("tools", {method: "POST", params: {project_id: projectId, tool}, body: {args}});
  let result;
  try { result = JSON.parse(data.result); } catch { result = null; }
  notify(result?.job_id ? `任务已加入队列：${result.job_id.slice(0, 8)}。` : "操作已提交，请查看任务进度与检查点。");
  await refreshProject(projectId);
}

bindForm("#project-form", async (form) => {
  const values = new FormData(form);
  const project = await request("projects", {method: "POST", body: {title: values.get("title"), budget_limit: Number(values.get("budget_limit"))}});
  await loadProjects(); await selectProject(project.id);
  form.reset(); $("#new-project").open = false;
  notify("项目已创建，上传参考视频开始创作。");
});
bindForm("#source-form", async (form) => {
  const projectId = state.projectId;
  await request("source", {method: "POST", params: {project_id: projectId}, body: new FormData(form)});
  form.reset(); notify("样片已上传，可以开始分析。");
  await refreshProject(projectId);
});
bindForm("#character-form", async (form) => {
  const values = new FormData(form);
  const reference_images = {};
  for (const view of ["front", "side", "half", "full"]) {
    const file = values.get(view);
    if (file?.size) reference_images[view] = await upload(file);
  }
  const character = await request("characters", {method: "POST", body: {name: values.get("name"), appearance_prompt: values.get("appearance_prompt"), reference_images}});
  form.reset();
  await loadAssets({character: character.id, consent: `character:${character.id}`});
  $("#consent-panel").open = true;
  notify("角色已保存。请在素材授权表单中明确确认授权后再使用。");
});
bindForm("#voice-form", async (form) => {
  const values = new FormData(form);
  const sample_paths = [];
  for (const file of values.getAll("samples")) if (file?.size) sample_paths.push(await upload(file));
  const voice = await request("voices", {method: "POST", body: {name: values.get("name"), engine: values.get("engine"), engine_voice_id: values.get("engine_voice_id") || null, sample_paths}});
  form.reset(); await loadAssets({voice: voice.id, consent: `voice:${voice.id}`});
  $("#consent-panel").open = true;
  notify("声音已保存。请在素材授权表单中明确确认授权后再使用。");
});
bindForm("#sheet-form", async (form) => {
  const values = new FormData(form);
  const result = await request("sheet", {method: "POST", params: {project_id: state.projectId}, body: {name: values.get("name"), appearance_prompt: values.get("appearance_prompt")}});
  notify(`角色表任务已排队：${result.job_id}。完成后查看图片并确认授权。`);
  await refreshProject();
});
bindForm("#consent-form", async (form) => {
  const values = new FormData(form);
  if (!form.elements.confirmed.checked) throw new Error("请先勾选并确认素材授权声明。");
  const [kind, asset_id] = $("#consent-select").value.split(":");
  await request(kind === "character" ? "characterConsent" : "voiceConsent", {method: "POST", params: {asset_id}, body: {confirmed_by: values.get("confirmed_by")}});
  form.reset(); await loadAssets();
  notify("素材授权已记录。现在可以将角色与声音关联到项目。");
});
bindForm("#assets-form", async (form) => {
  const projectId = state.projectId;
  const values = new FormData(form);
  await request("assets", {method: "PUT", params: {project_id: projectId}, body: {character_id: values.get("character_id"), voice_id: values.get("voice_id")}});
  notify("角色与声音已关联到当前项目。"); await refreshProject(projectId);
});
bindForm("#script-form", async (form) => { await callTool("write_script", {brief: new FormData(form).get("brief")}); });
bindForm("#budget-form", async (form) => {
  const values = new FormData(form);
  const projectId = state.projectId;
  await request("budget", {method: "POST", params: {project_id: projectId}, body: {amount: Number(values.get("amount")), approved_by: values.get("approved_by")}});
  notify(`已授权当前内容支出 $${Number(values.get("amount")).toFixed(2)}；可以继续生成。`);
  form.reset(); await refreshProject(projectId);
});
bindForm("#chat-form", async (form) => {
  const projectId = state.projectId;
  const message = new FormData(form).get("message");
  try {
    const response = await request("chat", {method: "POST", params: {project_id: projectId}, body: {message}});
    if (state.projectId === projectId) {
      renderChat(response.history);
      if ($("#chat-message").value === message) $("#chat-message").value = "";
      $("#chat-history").scrollTop = $("#chat-history").scrollHeight;
    }
  } finally { await refreshProject(projectId); }
});
$("#project-select").addEventListener("change", () => void selectProject($("#project-select").value).catch(showError));
$("#refresh").addEventListener("click", () => void runAction($("#refresh"), async () => {
  await Promise.all([loadProjects(), loadAssets(), refreshProject()]);
  if (!state.projectId && state.projects.length) await selectProject(state.projects[0].id);
  else if (state.projectId && !state.stream) openEvents(state.projectId);
  notify("项目状态已更新。");
}));
$("#review-actor").addEventListener("input", updateControls);
$("#character-select").addEventListener("change", updateControls);
$("#voice-select").addEventListener("change", updateControls);
$("#consent-select").addEventListener("change", () => {
  $("#consent-form").elements.confirmed.checked = false; updateControls();
});
for (const [id, tool] of [["#analyze", "analyze_video"], ["#frames", "generate_frames"], ["#rebuild", "rebuild"]]) {
  $(id).addEventListener("click", () => void runAction($(id), () => callTool(tool, tool === "rebuild" ? {retry_failed: true} : {})));
}
$("#chat-message").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault(); if (!$("#chat-form button").disabled) $("#chat-form").requestSubmit();
  }
});
window.addEventListener("beforeunload", () => state.stream?.close());
setInterval(() => {
  // SSE reports terminal jobs; polling also discovers queued/running transitions.
  if (state.projectId && !document.hidden && Date.now() - state.lastRefresh >= 4000) void refreshProject().catch(showError);
}, 4000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.projectId) void refreshProject().catch(showError);
});
async function initialize() {
  renderCheckpoints(); updateControls();
  await Promise.all([loadProjects(), loadAssets()]);
  let saved = "";
  try { saved = localStorage.getItem("avd.project") || ""; } catch { /* Optional convenience. */ }
  const selected = state.projects.some((project) => project.id === saved) ? saved : state.projects[0]?.id || "";
  await selectProject(selected);
}
void initialize().catch((error) => { setConnection("连接失败 · 点击刷新重试"); showError(error); });
