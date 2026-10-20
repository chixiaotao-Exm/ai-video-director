import {api, tool, upload, mediaURL} from "../api.js";
import {h, button, form, dialog, notice, media, badge, money, date} from "../components.js";
import {t} from "../i18n/zh.js";
import {liveMindMap} from "../mindmap.js";

const TABS = ["chat", "mindmap", "overview", "storyboard", "shots", "final", "analysis"];
const SHOT_KINDS = ["shot_frame", "shot_video", "voiceover", "lipsync", "subtitles"];
const GENERATORS = ["generate_videos", "generate_voiceover", "run_lipsync", "build_subtitles", "render_final"];
const p = key => t(`project.${key}`);
const path = (id, suffix = "") => `/projects/${encodeURIComponent(id)}${suffix}`;
const route = (id, tab) => `#/projects/${encodeURIComponent(id)}/${tab}`;
const label = (text, control, hint = "") => h("label", {class: "field"}, h("span", {}, text), control, hint && h("small", {class: "muted"}, hint));
const panel = (title, ...content) => h("section", {class: "card panel stack"}, h("h2", {}, title), ...content);
const paragraph = text => h("p", {class: "muted"}, text);
const anchor = (text, href, attrs = {}) => h("a", {href, ...attrs}, text);
const nodeOf = (project, kind, index = null) => project.nodes.find(node => node.kind === kind && node.shot_index === index);
const approved = (project, stage) => Boolean(project.reviews?.[stage]?.approved);
const isReady = nodes => nodes.length > 0 && nodes.every(node => node.status === "clean" && node.current_version_id);
const currency = value => money(value ?? 0);
import {mtokens as tokens, deleteProject} from "../components.js";
export {tokens};
const serialize = value => typeof value === "string" ? value : JSON.stringify(value, null, 2);

function output(node, kind) {
  if (!node) return null;
  const extensions = {
    image: /\.(?:png|jpe?g|webp)$/i,
    video: /\.(?:mp4|mov|webm|mkv|avi)$/i,
    audio: /\.(?:wav|mp3|flac|m4a|ogg|aac)$/i,
  };
  return Object.values(node.output_paths || {}).find(value => typeof value === "string" && extensions[kind].test(value)) || null;
}

function mediaOrEmpty(node, kind) {
  const result = output(node, kind);
  return result ? media(result, kind, {class: `media project-media project-media-${kind}`, preload: "metadata"}) : h("div", {class: "media-empty"}, p(`empty.${kind}`));
}

function checkField(name, text) {
  return h("label", {class: "checkbox"}, h("input", {type: "checkbox", name, required: true}), h("span", {}, text));
}

function approvalDialog(ctx, project, stage) {
  let modal;
  const content = form([
    paragraph(p(`approve.${stage}.description`)),
    label(p("approver"), h("input", {name: "actor", required: true, maxLength: 255, autoComplete: "name", placeholder: p("approverPlaceholder")})),
    checkField("reviewed", p("reviewedCheckbox")),
  ], async data => {
    await api(path(project.id, "/approve"), {method: "POST", body: {stage, approved_by: String(data.get("actor")).trim()}});
    modal.close();
    notice(p("approvalSaved"));
    await ctx.reload();
  }, p("confirmApproval"));
  modal = dialog(p(`approve.${stage}.title`), content);
}

function approvalButton(ctx, project, stage, ready) {
  const done = approved(project, stage);
  const previous = {script: "structure", frames: "script", final: "frames"}[stage];
  const allowed = !previous || approved(project, previous);
  return button(done ? p("alreadyApproved") : p(`approve.${stage}.button`), () => approvalDialog(ctx, project, stage), {disabled: done || !ready || !allowed, small: true});
}

async function runTool(ctx, project, name, args = {}) {
  const result = await tool(project.id, name, args);
  notice(result.message || (result.job_ids?.length || result.job_id ? p("queued") : p("actionDone")));
  await ctx.reload();
  return result;
}

function jobRows(jobs) {
  const recent = [...jobs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 6);
  if (!recent.length) return paragraph(p("noJobs"));
  return h("div", {class: "job-inline-list"}, ...recent.map(job => h("div", {class: "job-inline-row"},
    h("span", {}, p(`kind.${job.job_type}`)),
    badge(job.status),
    h("small", {class: "muted"}, `${p("attempts")} ${job.attempts}/${job.max_attempts}`),
    job.error && h("details", {}, h("summary", {}, p("errorDetails")), h("pre", {}, job.error)),
  )));
}

function liveJobs(projectId, jobs) {
  return h("div", {class: "project-live-jobs", "data-project-live-jobs": projectId}, jobRows(jobs));
}

async function refreshLiveJobs(ctx, projectId) {
  const latest = await api(path(projectId, "/jobs"), {signal: ctx.signal});
  document.querySelectorAll(`[data-project-live-jobs="${projectId}"]`).forEach(element => {
    element.replaceChildren(jobRows(latest));
  });
}

function attachJobEvents(ctx, projectId, initialJobs) {
  if (ctx.signal?.aborted || typeof EventSource !== "function") return;
  const seen = new Set(initialJobs.filter(job => ["succeeded", "failed", "cancelled"].includes(job.status)).map(job => `${job.id}:${job.attempts}:${job.status}`));
  const stream = new EventSource(path(projectId, "/events"));
  stream.addEventListener("job_finished", event => {
    if (ctx.signal?.aborted) return;
    let payload = {};
    try { payload = JSON.parse(event.data || "{}"); } catch { payload = {}; }
    const key = `${payload.id}:${payload.attempts}:${payload.status}`;
    if (seen.has(key)) return;
    seen.add(key);
    const kind = payload.job_type ? p(`kind.${payload.job_type}`) : p("job");
    const status = payload.status ? t(`status.${payload.status}`) : p("updated");
    notice(`${p("jobFinished")}：${kind} · ${status}`);
    void refreshLiveJobs(ctx, projectId).catch(() => {});
  });
  ctx.onDispose(() => stream.close());
}

function stage(project, nodes, review) {
  if (review && approved(project, review)) return "clean";
  if (nodes.some(node => node.status === "running")) return "running";
  if (nodes.some(node => node.status === "failed")) return "failed";
  return isReady(nodes) ? "clean" : "dirty";
}

function step(number, title, description, state, ...content) {
  return h("li", {class: "workflow-step"},
    h("div", {class: "step-number", "aria-hidden": "true"}, String(number).padStart(2, "0")),
    h("div", {class: "step-content stack"},
      h("div", {class: "row spread"}, h("h3", {}, title), badge(state)),
      paragraph(description), ...content,
    ),
  );
}

async function overview(ctx, project, usage, jobs) {
  const [characters, voices] = await Promise.all([api("/characters"), api("/voices")]);
  const analysis = project.nodes.filter(node => node.kind === "analysis");
  const scripts = project.nodes.filter(node => node.kind === "script");
  const frames = project.nodes.filter(node => node.kind === "shot_frame");
  const finals = project.nodes.filter(node => node.kind === "final");
  const assetsReady = Boolean(project.character_id && project.voice_id && characters.find(item => item.id === project.character_id)?.consent_confirmed && voices.find(item => item.id === project.voice_id)?.consent_confirmed);
  const sourceForm = form([
    label(p("sourceVideo"), h("input", {name: "source", type: "file", accept: ".mp4,.mov,.webm,.mkv,.avi", required: true}), project.source_video_path ? p("replaceSourceHelp") : p("sourceHelp")),
  ], async data => {
    await upload(data.get("source"), project.id);
    notice(p("sourceUploaded"));
    await ctx.reload();
  }, project.source_video_path ? p("replaceSource") : p("uploadSource"));
  function assetSelect(name, assets, selected) {
    return h("select", {name, required: true},
      h("option", {value: ""}, p("selectAsset")),
      ...assets.map(asset => h("option", {value: asset.id, disabled: !asset.consent_confirmed, selected: selected === asset.id}, `${asset.name}${asset.consent_confirmed ? "" : p("unconsentedSuffix")}`)),
    );
  }
  const assetsForm = form([
    h("div", {class: "form-grid"},
      label(p("character"), assetSelect("character", characters, project.character_id)),
      label(p("voice"), assetSelect("voice", voices, project.voice_id)),
    ),
  ], async data => {
    await api(path(project.id, "/assets"), {method: "PUT", body: {character_id: data.get("character"), voice_id: data.get("voice")}});
    notice(p("assetsAttached"));
    await ctx.reload();
  }, p("attachAssets"));
  const briefForm = form([
    label(p("brief"), h("textarea", {name: "brief", rows: 3, required: true, placeholder: p("briefPlaceholder")})),
  ], async data => {
    await runTool(ctx, project, "write_script", {brief: String(data.get("brief")).trim()});
  }, p("writeScript"));
  if (!approved(project, "structure") || !assetsReady) {
    briefForm.querySelectorAll("button").forEach(item => { item.disabled = true; });
    briefForm.append(paragraph(p("scriptGate")));
  }
  const steps = h("ol", {class: "workflow-stepper"},
    step(1, p("step.source"), p("step.sourceDescription"), stage(project, analysis, "structure"),
      sourceForm,
      h("div", {class: "row wrap"},
        button(p("analyze"), () => runTool(ctx, project, "analyze_video"), {disabled: !project.source_video_path}),
        anchor(p("viewAnalysis"), route(project.id, "analysis")),
        approvalButton(ctx, project, "structure", isReady(analysis)),
      ),
    ),
    step(2, p("step.assets"), p("step.assetsDescription"), assetsReady ? "clean" : "dirty",
      assetsForm, anchor(p("manageAssets"), "#/assets"),
    ),
    step(3, p("step.script"), p("step.scriptDescription"), stage(project, scripts, "script"),
      briefForm,
      h("div", {class: "row wrap"}, anchor(p("viewStoryboard"), route(project.id, "storyboard")), approvalButton(ctx, project, "script", isReady(scripts))),
    ),
    step(4, p("step.frames"), p("step.framesDescription"), stage(project, frames, "frames"),
      h("div", {class: "row wrap"},
        button(p("generateFrames"), () => runTool(ctx, project, "generate_frames"), {disabled: !approved(project, "script")}),
        anchor(p("viewShots"), route(project.id, "shots")),
        approvalButton(ctx, project, "frames", isReady(frames)),
      ),
    ),
    step(5, p("step.production"), p("step.productionDescription"), stage(project, finals),
      h("div", {class: "row wrap"},
        button(p("rebuild"), () => runTool(ctx, project, "rebuild"), {primary: true, disabled: !approved(project, "frames")}),
        button(p("retryFailed"), () => runTool(ctx, project, "rebuild", {retry_failed: true}), {disabled: !approved(project, "script")}),
        button(p("viewPlan"), async () => {
          const plan = await tool(project.id, "plan_rebuild");
          dialog(p("rebuildPlan"), plan.length ? h("ul", {class: "simple-list"}, ...plan.map(item => h("li", {}, `${p(`kind.${item.kind}`)}${item.shot_index ? ` · ${p("shot")} ${item.shot_index}` : ""}`))) : paragraph(p("noRebuild")));
        }),
      ),
      h("details", {}, h("summary", {}, p("advancedGeneration")), h("div", {class: "row wrap details-content"}, ...GENERATORS.map(name => button(p(`kind.${name}`), () => runTool(ctx, project, name), {small: true, disabled: !approved(project, "frames")})))),
    ),
    step(6, p("step.review"), p("step.reviewDescription"), stage(project, finals, "final"),
      h("div", {class: "row wrap"}, anchor(p("viewFinal"), route(project.id, "final")), approvalButton(ctx, project, "final", isReady(finals)), anchor(p("requestChanges"), route(project.id, "chat"))),
    ),
  );
  return h("div", {class: "project-overview-grid"}, panel(p("workflow"), steps),
    h("aside", {class: "stack"},
      panel(p("tokenUsage"), h("dl", {class: "stat-list"},
        h("div", {}, h("dt", {}, p("inputTokens")), h("dd", {}, tokens(usage.input_tokens))),
        h("div", {}, h("dt", {}, p("outputTokens")), h("dd", {}, tokens(usage.output_tokens))),
      ), anchor(p("tokenDetails"), "#/costs")),
      panel(p("recentJobs"), liveJobs(project.id, jobs), anchor(p("allJobs"), "#/jobs")),
    ),
  );
}

function storyboard(ctx, project) {
  const script = nodeOf(project, "script");
  const shots = script?.recipe?.shots || [];
  if (!shots.length) return panel(p("tabs.storyboard"), paragraph(p("noStoryboard")), anchor(p("returnOverview"), route(project.id, "overview")));
  const fields = ["line", "visual_prompt", "camera", "emotion", "duration_s"];
  const rows = shots.map(shot => {
    const controls = {};
    for (const field of fields) {
      controls[field] = field === "line" || field === "visual_prompt"
        ? h("textarea", {rows: 3, value: shot[field] ?? "", "aria-label": `${p("shot")} ${shot.index} ${p(`field.${field}`)}`, required: field !== "line"})
        : h("input", {type: field === "duration_s" ? "number" : "text", value: String(shot[field] ?? ""), required: true, ...(field === "duration_s" ? {min: "0.01", step: "0.01"} : {}), "aria-label": `${p("shot")} ${shot.index} ${p(`field.${field}`)}`});
    }
    const shotNodes = project.nodes.filter(node => node.shot_index === shot.index);
    const save = button(p("saveShot"), async () => {
      if (!Object.values(controls).every(control => control.reportValidity())) return;
      const changes = {};
      for (const field of fields) {
        const value = field === "duration_s" ? Number(controls[field].value) : controls[field].value;
        if (value !== shot[field]) changes[field] = value;
      }
      if (!Object.keys(changes).length) { notice(p("noChanges")); return; }
      await tool(project.id, "update_shot", {shot_index: shot.index, changes});
      notice(p("shotSaved"));
      await ctx.reload();
    }, {small: true});
    return h("tr", {},
      h("th", {scope: "row"}, h("span", {}, `${p("shot")} ${shot.index}`), badge(stage(project, shotNodes))),
      ...fields.map(field => h("td", {}, controls[field])), h("td", {}, save),
    );
  });
  return panel(script.recipe.title || p("tabs.storyboard"),
    paragraph(p("storyboardHelp")),
    h("div", {class: "table-scroll table-wrap storyboard-table"}, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, p("shot")), ...fields.map(field => h("th", {}, p(`field.${field}`))), h("th", {}, p("actions")))),
      h("tbody", {}, ...rows),
    )),
    h("div", {class: "row wrap"}, approvalButton(ctx, project, "script", isReady([script])), anchor(p("continueFrames"), route(project.id, "overview"))),
  );
}

async function versions(ctx, project, node, {history = false} = {}) {
  const items = await api(path(project.id, `/nodes/${encodeURIComponent(node.id)}/versions`));
  if (!items.length) return paragraph(p("noVersions"));
  const chooser = h("select", {"aria-label": p("selectVersion")}, ...[...items].reverse().map(item => h("option", {value: item.number, selected: item.id === node.current_version_id}, `${p("version")} ${item.number} · ${date(item.created_at)}`)));
  const restore = button(p("revert"), async () => {
    await api(path(project.id, `/nodes/${encodeURIComponent(node.id)}/revert`), {method: "POST", body: {version_number: Number(chooser.value)}});
    notice(p("reverted"));
    await ctx.reload();
  }, {small: true, disabled: Number(chooser.value) === node.current_version?.number});
  chooser.addEventListener("change", () => { restore.disabled = Number(chooser.value) === node.current_version?.number; });
  const controls = h("div", {class: "version-control row"}, chooser, restore);
  if (!history) return controls;
  return h("div", {class: "stack"}, controls,
    h("div", {class: "table-scroll table-wrap"}, h("table", {},
      h("thead", {}, h("tr", {}, ...["version", "created", "cost", "note"].map(key => h("th", {}, p(key))))),
      h("tbody", {}, ...[...items].reverse().map(item => h("tr", {},
        h("td", {}, `${p("version")} ${item.number}${item.id === node.current_version_id ? ` · ${p("current")}` : ""}`),
        h("td", {}, date(item.created_at)), h("td", {}, currency(item.cost)), h("td", {}, item.note || p("none")),
      ))),
    )),
  );
}

function regenerateDialog(ctx, project, index) {
  let modal;
  const content = form([
    paragraph(p("regenerateHelp")),
    label(p("regenerateFrom"), h("select", {name: "kind"}, ...SHOT_KINDS.map(kind => h("option", {value: kind}, p(`kind.${kind}`))))),
    checkField("confirm", p("regenerateCheckbox")),
  ], async data => {
    const result = await api(path(project.id, `/shots/${index}/regenerate`), {method: "POST", body: {kind: data.get("kind")}});
    modal.close();
    notice(result.requires_review ? `${p("regeneratePending")} ${result.requires_review}` : p("regenerateQueued"));
    await ctx.reload();
  }, p("regenerate"));
  modal = dialog(`${p("shot")} ${index} · ${p("regenerate")}`, content);
}

async function shots(ctx, project) {
  const indexes = [...new Set(project.nodes.map(node => node.shot_index).filter(index => index != null))].sort((a, b) => a - b);
  if (!indexes.length) return panel(p("tabs.shots"), paragraph(p("noShots")), anchor(p("returnOverview"), route(project.id, "overview")));
  const cards = await Promise.all(indexes.map(async index => {
    const nodes = project.nodes.filter(node => node.shot_index === index);
    const frame = nodeOf(project, "shot_frame", index);
    const video = ["subtitles", "lipsync", "shot_video"].map(kind => nodeOf(project, kind, index)).find(node => output(node, "video"));
    const voice = nodeOf(project, "voiceover", index);
    const nodeDetails = await Promise.all(SHOT_KINDS.map(async kind => {
      const node = nodeOf(project, kind, index);
      if (!node) return null;
      return h("section", {class: "stack version-section"}, h("div", {class: "row spread"}, h("h4", {}, p(`kind.${kind}`)), badge(node.status)), await versions(ctx, project, node));
    }));
    return h("article", {class: "card panel shot-card stack"},
      h("div", {class: "row spread"}, h("h2", {}, `${p("shot")} ${index}`), badge(stage(project, nodes))),
      mediaOrEmpty(frame, "image"),
      h("div", {class: "stack"}, h("h3", {}, p("shotVideo")), mediaOrEmpty(video, "video")),
      h("div", {class: "stack"}, h("h3", {}, p("shotVoice")), mediaOrEmpty(voice, "audio")),
      button(p("regenerateShot"), () => regenerateDialog(ctx, project, index), {small: true}),
      h("details", {}, h("summary", {}, p("versionsAndOutputs")), h("div", {class: "stack details-content"}, ...nodeDetails)),
    );
  }));
  return h("div", {class: "stack"},
    h("div", {class: "row spread wrap"}, paragraph(p("shotsHelp")), approvalButton(ctx, project, "frames", isReady(project.nodes.filter(node => node.kind === "shot_frame")))),
    h("div", {class: "shot-grid"}, ...cards),
  );
}

async function finalPage(ctx, project) {
  const final = nodeOf(project, "final");
  const video = output(final, "video");
  if (!video) return panel(p("tabs.final"), paragraph(p("noFinal")), anchor(p("returnOverview"), route(project.id, "overview")));
  return h("div", {class: "stack"}, panel(p("finalPreview"),
    media(video, "video", {class: "media media-video final-player", preload: "metadata"}),
    h("div", {class: "row wrap"},
      anchor(p("downloadFinal"), mediaURL(video), {class: "button primary", download: ""}),
      approvalButton(ctx, project, "final", isReady([final])),
      anchor(p("requestChanges"), route(project.id, "chat")),
    ), paragraph(p("aiLabel")),
  ), panel(p("versionHistory"), await versions(ctx, project, final, {history: true})));
}

function analysis(ctx, project) {
  const template = project.analysis_result;
  const source = project.source_video_path;
  if (!template) return panel(p("tabs.analysis"), paragraph(p("noAnalysis")), anchor(p("uploadAndAnalyze"), route(project.id, "overview")));
  const fields = ["hook", "pacing_notes", "music_style", "voice_style"];
  return h("div", {class: "stack"},
    panel(p("analysisTemplate"), paragraph(p("analysisHelp")),
      h("div", {class: "row spread"}, h("span", {}, `${p("totalDuration")} ${template.total_duration_s} ${p("seconds")}`), approvalButton(ctx, project, "structure", isReady(project.nodes.filter(node => node.kind === "analysis")))),
      h("dl", {class: "analysis-notes"}, ...fields.map(field => h("div", {}, h("dt", {}, p(`analysis.${field}`)), h("dd", {}, template[field] || p("none"))))),
      h("div", {class: "table-scroll table-wrap"}, h("table", {},
        h("thead", {}, h("tr", {}, ...["shot", "duration", "shot_size", "camera_move", "visual_description", "spoken_line_purpose"].map(key => h("th", {}, p(`analysis.${key}`))))),
        h("tbody", {}, ...(template.shots || []).map((shot, index) => h("tr", {},
          h("td", {}, String(index + 1)), h("td", {}, `${shot.duration_s} ${p("seconds")}`),
          ...["shot_size", "camera_move", "visual_description", "spoken_line_purpose"].map(key => h("td", {}, shot[key])),
        ))),
      )),
    ),
    source && h("details", {class: "card panel"}, h("summary", {}, p("referenceVideo")), h("div", {class: "details-content"}, media(source, "video", {preload: "metadata", class: "media media-video reference-player"}))),
  );
}

function escapeHTML(text) {
  return text.replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
}

// Small, escape-first Markdown subset for director replies (headings, lists, bold, code, links).
function markdown(text) {
  const inline = line => escapeHTML(line)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(（])(https?:\/\/[^\s<)）]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  const html = [];
  let list = null;
  const close = () => { if (list) { html.push(`</${list}>`); list = null; } };
  let table = null;
  const flushTable = () => {
    if (!table) return;
    const [head, ...body] = table;
    const cells = (row, tag) => row.map(cell => `<${tag}>${inline(cell)}</${tag}>`).join("");
    html.push(`<div class="md-table"><table><thead><tr>${cells(head, "th")}</tr></thead><tbody>${body.map(row => `<tr>${cells(row, "td")}</tr>`).join("")}</tbody></table></div>`);
    table = null;
  };
  for (const raw of String(text).split("\n")) {
    const line = raw.trimEnd();
    let match;
    if (/^\s*\|.*\|\s*$/.test(line)) {
      close();
      if (!/^\s*\|[\s:|-]+\|\s*$/.test(line)) (table ||= []).push(line.trim().slice(1, -1).split("|").map(cell => cell.trim()));
      continue;
    }
    flushTable();
    if ((match = line.match(/^(#{1,4})\s+(.*)$/))) {
      close();
      const level = Math.min(match[1].length + 2, 6);
      html.push(`<h${level}>${inline(match[2])}</h${level}>`);
    } else if ((match = line.match(/^\s*[-*•]\s+(.*)$/))) {
      if (list !== "ul") { close(); html.push("<ul>"); list = "ul"; }
      html.push(`<li>${inline(match[1])}</li>`);
    } else if ((match = line.match(/^\s*\d+[.)、]\s*(.*)$/))) {
      if (list !== "ol") { close(); html.push("<ol>"); list = "ol"; }
      html.push(`<li>${inline(match[1])}</li>`);
    } else if (/^\s*(---|\*\*\*)\s*$/.test(line)) {
      close(); html.push("<hr>");
    } else if (!line.trim()) {
      close();
    } else {
      close(); html.push(`<p>${inline(line)}</p>`);
    }
  }
  close();
  flushTable();
  const element = h("div", {class: "message-content markdown"});
  element.innerHTML = html.join("");
  return element;
}

const toolLabel = name => {
  const label = p(`kind.${name}`);
  return label.startsWith("project.") ? name : label;
};

// --- Messenger-style conversation -----------------------------------------------
const GAP_MS = 5 * 60 * 1000;

function clock(value) {
  if (!value) return "";
  const when = new Date(/Z|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  const now = new Date();
  const time = when.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
  if (when.toDateString() === now.toDateString()) return time;
  return `${when.toLocaleDateString("zh-CN", {month: "numeric", day: "numeric"})} ${time}`;
}

function mediaBubble(item, side) {
  const kind = item.kind;
  let body;
  if (kind === "image") {
    const img = h("img", {src: mediaURL(item.path), alt: item.caption || "", loading: "lazy"});
    body = h("a", {href: mediaURL(item.path), target: "_blank", rel: "noopener", class: "bubble-image"}, img);
  } else {
    body = media(item.path, kind, {preload: "metadata", class: `bubble-${kind}`});
  }
  return h("div", {class: `bubble bubble-media bubble-${side}`}, body, item.caption ? h("div", {class: "bubble-caption"}, item.caption) : null);
}

function avatar(side) {
  return h("div", {class: `avatar avatar-${side}`, "aria-hidden": "true"}, side === "me" ? "我" : "导");
}

function row(side, ...bubbles) {
  const content = h("div", {class: "msg-stack"}, ...bubbles);
  return h("div", {class: `msg-row msg-${side}`}, side === "me" ? [content, avatar(side)] : [avatar(side), content]);
}

function chatMessages(messages) {
  if (!messages.length) return h("div", {class: "chat-empty"}, h("div", {class: "avatar avatar-director big"}, "导"), h("h2", {}, p("chatWelcome")), paragraph(p("chatWelcomeHelp")));
  const items = [];
  let last = 0;
  let pendingTools = [];
  const flushTools = () => {
    if (!pendingTools.length) return;
    const names = [...new Set(pendingTools.map(call => toolLabel(call.name)))];
    items.push(h("details", {class: "msg-system tool-trace"},
      h("summary", {}, `${p("directorDid")}${names.join("、")}`),
      h("div", {class: "tool-trace-body"}, ...pendingTools.map(call => h("div", {}, h("strong", {}, toolLabel(call.name)), h("pre", {}, serialize(call.detail)))))));
    pendingTools = [];
  };
  for (const message of messages) {
    const at = message.at ? Date.parse(/Z|[+-]\d\d:\d\d$/.test(message.at) ? message.at : `${message.at}Z`) : 0;
    if (at && at - last > GAP_MS) { flushTools(); items.push(h("div", {class: "msg-time"}, clock(message.at))); }
    if (at) last = at;
    if (message.role === "tool") {
      pendingTools.push({name: message.name, detail: message.content});
      if (message.attachments?.length) {
        flushTools();
        items.push(row("director", ...message.attachments.map(item => mediaBubble(item, "director"))));
      }
      continue;
    }
    if (message.role === "user" && message.event) {
      flushTools();
      items.push(h("div", {class: "msg-system"}, String(message.content).replace(/^\[[^\]]*\]\s*/, "").split("。")[0]));
      continue;
    }
    if (message.role === "assistant") {
      for (const call of message.tool_calls || []) pendingTools.push({name: call.name, detail: call.arguments});
      if (!message.content) continue;
      flushTools();
      items.push(row("director", h("div", {class: "bubble bubble-director"}, markdown(message.content))));
      continue;
    }
    if (message.role === "user") {
      flushTools();
      const bubbles = [];
      if (message.content) bubbles.push(h("div", {class: "bubble bubble-me"}, h("div", {class: "message-content"}, serialize(message.content))));
      for (const item of message.attachments || []) bubbles.push(mediaBubble(item, "me"));
      items.push(row("me", ...bubbles));
    }
  }
  flushTools();
  return h("div", {class: "chat-messages"}, ...items);
}

function activity(jobs) {
  const active = jobs.filter(job => ["queued", "running"].includes(job.status));
  const kinds = new Set(active.map(job => job.job_type));
  const key = kinds.has("analyze_video") ? "watching"
    : kinds.has("director_report") ? "reporting"
    : kinds.has("write_script") ? "writing"
    : active.length ? "working" : "";
  if (!key) return null;
  const count = active.length > 1 ? ` · ${active.length} ${p("jobsInFlight")}` : "";
  return h("div", {class: "chat-activity", role: "status"}, h("span", {class: "pulse", "aria-hidden": "true"}), `${p(`activity.${key}`)}${count}`);
}

const ATTACH_KINDS = {image: /\.(png|jpe?g|webp|gif)$/i, video: /\.(mp4|mov|webm|mkv)$/i, audio: /\.(wav|mp3|m4a|flac|ogg|aac)$/i};
const kindOf = name => Object.keys(ATTACH_KINDS).find(kind => ATTACH_KINDS[kind].test(name)) || null;

export async function chatPanel(ctx, projectId, {compact = false} = {}) {
  const [history, initialJobs] = await Promise.all([
    api(path(projectId, "/chat"), {signal: ctx.signal}),
    api(path(projectId, "/jobs"), {signal: ctx.signal}),
  ]);
  const log = h("div", {class: "chat-log", role: "log", "aria-live": "polite", "aria-label": p("chatHistory")}, chatMessages(history));
  const status = h("div", {class: "chat-status"}, activity(initialJobs));
  const textarea = h("textarea", {name: "message", rows: compact ? 1 : 2, placeholder: p("messagePlaceholder"), "aria-label": p("message")});
  const picker = h("input", {type: "file", multiple: true, accept: "image/*,video/*,audio/*", hidden: true});
  const chips = h("div", {class: "attach-chips"});
  let files = [];
  const renderChips = () => {
    chips.replaceChildren(...files.map((file, index) => h("span", {class: "attach-chip"},
      `${p(`attach.${kindOf(file.name)}`)} · ${file.name}`,
      h("button", {type: "button", class: "chip-remove", "aria-label": p("attach.remove"), onclick: () => { files.splice(index, 1); renderChips(); }}, "×"))));
  };
  picker.addEventListener("change", () => {
    for (const file of picker.files || []) if (kindOf(file.name)) files.push(file);
    picker.value = "";
    renderChips();
  });
  const attach = h("button", {type: "button", class: "attach-button", title: p("attach.title"), "aria-label": p("attach.title"), onclick: () => picker.click()}, "+");
  const send = h("button", {type: "submit", class: "send-button"}, p("send"));
  const composer = h("form", {class: "chat-composer"}, chips, h("div", {class: "composer-row"}, attach, textarea, send), picker);
  let signature = JSON.stringify(history);
  let sending = false;
  const scrollEnd = () => { log.scrollTop = log.scrollHeight; };
  const show = messages => {
    signature = JSON.stringify(messages);
    log.replaceChildren(chatMessages(messages));
  };
  composer.addEventListener("submit", async event => {
    event.preventDefault();
    const text = textarea.value.trim();
    if ((!text && !files.length) || sending) return;
    // Like a messenger: the box clears at once and you can keep typing while it sends.
    sending = true; send.disabled = true; attach.disabled = true;
    textarea.value = "";
    const outgoing = files;
    files = []; renderChips();
    const pending = JSON.parse(signature);
    pending.push({role: "user", content: text, at: new Date().toISOString(), attachments: outgoing.map(file => ({kind: kindOf(file.name), path: "", caption: file.name}))});
    log.replaceChildren(chatMessages(pending.map(message => ({...message, attachments: (message.attachments || []).filter(item => item.path)}))));
    if (outgoing.length) log.lastElementChild?.append(h("div", {class: "msg-system"}, p("attach.uploading")));
    log.append(h("div", {class: "chat-thinking"}, h("span", {class: "pulse", "aria-hidden": "true"}), p("thinking")));
    scrollEnd();
    try {
      const attachments = [];
      for (const file of outgoing) attachments.push(await upload(file));
      const result = await api(path(projectId, "/chat"), {method: "POST", body: {message: text, attachments}});
      show(result.history);
    } catch (error) {
      notice(error.message, true);
      show(await api(path(projectId, "/chat")));
      if (!textarea.value.trim()) textarea.value = text;
      files = outgoing; renderChips();
    } finally {
      sending = false; send.disabled = false; attach.disabled = false; textarea.focus();
      scrollEnd();
    }
  });
  textarea.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  ctx.poll(async () => {
    if (sending) return;
    const [messages, jobs] = await Promise.all([api(path(projectId, "/chat")), api(path(projectId, "/jobs"))]);
    if (ctx.signal?.aborted || sending) return;
    status.replaceChildren(activity(jobs) || "");
    if (JSON.stringify(messages) !== signature) {
      const nearEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 160;
      show(messages);
      if (nearEnd) scrollEnd();
    }
  }, 2500);
  requestAnimationFrame(scrollEnd);
  log.addEventListener("load", event => { if (event.target.tagName === "IMG") scrollEnd(); }, true);
  return h("section", {class: `chat-panel wechat${compact ? " compact" : ""}`}, log, status, composer);
}

function agentPanel(ctx, projectId) {
  const list = h("div", {class: "agent-list"}, paragraph(p("agents.empty")));
  let signature = "";
  const open = new Set();
  async function refresh() {
    const agents = await api(path(projectId, "/subagents"), {signal: ctx.signal});
    const next = JSON.stringify(agents.map(agent => [agent.id, agent.status, agent.updated_at]));
    if (next === signature) return;
    signature = next;
    list.querySelectorAll("details[open]").forEach(element => open.add(element.dataset.id));
    if (!agents.length) { list.replaceChildren(paragraph(p("agents.empty"))); return; }
    list.replaceChildren(...agents.map(agent => {
      const busy = ["working", "watching"].includes(agent.status);
      const item = h("details", {class: "agent-item", "data-id": agent.id},
        h("summary", {},
          h("span", {class: "agent-name"}, agent.name),
          h("span", {class: "agent-kind"}, p(`agents.kind.${agent.kind}`)),
          h("span", {class: "agent-state"}, busy ? h("span", {class: "pulse", "aria-hidden": "true"}) : null, p(`agents.status.${agent.status}`))),
        h("div", {class: "agent-body"},
          h("div", {class: "muted"}, `${p("agents.task")}${agent.instructions}`),
          agent.kind === "watcher" ? h("div", {class: "muted"}, `${p("agents.watching")}${agent.job_ids.length}`) : null,
          ...agent.transcript.filter(item => item.content).slice(-6).map(item => h("div", {class: `bubble ${item.role === "user" ? "bubble-me" : "bubble-director"}`}, item.role === "assistant" ? markdown(item.content) : String(item.content))),
          agent.kind === "watcher" && agent.result ? h("div", {}, agent.result) : null));
      if (open.has(agent.id)) item.open = true;
      return item;
    }));
  }
  void refresh().catch(() => {});
  ctx.poll(refresh, 3000);
  return panel(p("agents.title"), paragraph(p("agents.help")), list);
}

async function chat(ctx, project, initialJobs) {
  const panelElement = await chatPanel(ctx, project.id);
  const jobs = liveJobs(project.id, initialJobs);
  ctx.poll(async () => { jobs.replaceChildren(jobRows(await api(path(project.id, "/jobs")))); }, 4000);
  const source = project.source_video_path;
  return h("div", {class: "project-chat-grid"}, panelElement,
    h("aside", {class: "stack"},
      panel(p("tabs.mindmap"), liveMindMap(ctx, project.id, {compact: true}), anchor(p("mindmapFull"), route(project.id, "mindmap"))),
      agentPanel(ctx, project.id),
      source
        ? panel(p("referenceVideo"), media(source, "video", {preload: "metadata", class: "media media-video reference-player"}))
        : panel(p("referenceVideo"), paragraph(p("noSourceYet")), anchor(p("uploadSource"), route(project.id, "overview"))),
      panel(p("recentJobs"), jobs, anchor(p("allJobs"), "#/jobs")),
    ));
}

export async function render(ctx) {
  const id = ctx.params?.[0] || ctx.projectId;
  if (!id) return panel(p("selectProject"), paragraph(p("selectProjectHelp")), anchor(p("dashboard"), "#/dashboard"));
  const current = TABS.includes(ctx.params?.[1]) ? ctx.params[1] : "chat";
  const [project, jobs] = await Promise.all([api(path(id, "/status")), api(path(id, "/jobs"))]);
  const usage = ctx.projects?.find(item => item.id === id) || {};
  const tabs = h("nav", {class: "project-tabs", "aria-label": p("tabsLabel")}, ...TABS.map(tab => anchor(p(`tabs.${tab}`), route(id, tab), {class: current === tab ? "active" : "", "aria-current": current === tab ? "page" : null})));
  const header = h("header", {class: "page-heading row spread wrap"}, h("div", {class: "stack compact"}, h("p", {class: "eyebrow"}, project.mode === "anime_sequel" ? t("anime.eyebrow") : p("projectWorkspace")), h("h1", {}, project.title)), h("div", {class: "toolbar"}, button(p("refresh"), () => ctx.reload(), {small: true}), button(p("deleteProject"), async () => { if (await deleteProject(project)) location.hash = "#/"; }, {small: true, danger: true})));
  const mindmapTab = () => h("section", {class: "card panel stack"}, h("h2", {}, p("tabs.mindmap")), h("p", {class: "muted"}, p("mindmapHelp")), liveMindMap(ctx, id));
  const renderers = {overview, chat, storyboard, shots, final: finalPage, analysis, mindmap: mindmapTab};
  const content = current === "overview" ? await overview(ctx, project, usage, jobs) : current === "chat" ? await chat(ctx, project, jobs) : await renderers[current](ctx, project);
  attachJobEvents(ctx, id, jobs);
  const view = h("div", {class: "stack project-page"}, header, tabs, content);
  let edited = false;
  let updateNoticed = false;
  view.addEventListener("input", () => { edited = true; });
  view.addEventListener("change", () => { edited = true; });
  if (current === "overview") ctx.poll(() => refreshLiveJobs(ctx, id), 4000);
  if (!["chat", "storyboard", "mindmap"].includes(current)) {
    const signature = JSON.stringify([project.nodes.map(node => [node.id, node.status, node.current_version_id]), project.reviews]);
    ctx.poll(async () => {
      const next = await api(path(id, "/status"));
      if (ctx.signal?.aborted || document.querySelector("dialog[open]") || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (JSON.stringify([next.nodes.map(node => [node.id, node.status, node.current_version_id]), next.reviews]) !== signature) {
        if (edited || [...view.querySelectorAll("video,audio")].some(element => !element.paused && !element.ended)) {
          if (!updateNoticed) { notice(p("refreshAvailable")); updateNoticed = true; }
          return;
        }
        await ctx.reload();
      }
    }, 4000);
  }
  return view;
}
