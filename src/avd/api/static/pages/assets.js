import {api, upload, mediaURL} from "../api.js";
import {h, button, link, field, input, select, textarea, form, empty, badge, date, dialog, notice, media} from "../components.js";
import {t} from "../i18n/zh.js";
import {currentImageChoice, imageModelSelect, loadImageModels, parseImageChoice} from "../imageModels.js";

const views = ["front", "side", "half", "full"];

function projectOptions(ctx) {
  return [{value: "", label: t("asset.chooseProject")}, ...ctx.projects.map(project => ({value: project.id, label: project.title}))];
}

function consentDialog(asset, kind, ctx) {
  let modal;
  const confirmation = input("checkbox", "yes", {name: "consent", required: true});
  const content = form([
    h("p", {}, `${t("asset.confirmAsset")} ${asset.name}`),
    h("p", {class: "muted"}, t(kind === "characters" ? "asset.characterConsentHelp" : "asset.voiceConsentHelp")),
    field(t("asset.confirmedBy"), input("text", "", {name: "confirmed_by", required: true, maxlength: 255, autocomplete: "name"})),
    h("label", {class: "checkbox-field"}, confirmation, h("span", {}, t("asset.consentStatement"))),
  ], async data => {
    if (!confirmation.checked) return;
    await api(`/${kind}/${encodeURIComponent(asset.id)}/consent`, {
      method: "POST", body: {confirmed_by: String(data.get("confirmed_by")).trim()},
    });
    modal.close();
    notice(t("asset.consentSaved"));
    await ctx.reload();
  }, t("asset.confirmConsent"));
  modal = dialog(t("asset.consentTitle"), content);
}

function consentStatus(asset, kind, ctx) {
  if (asset.consent_confirmed) {
    return h("div", {class: "asset-consent"},
      badge("succeeded", t("asset.consented")),
      h("p", {class: "muted"}, `${t("asset.confirmedBy")} ${asset.consent_confirmed_by || "—"} · ${date(asset.consent_confirmed_at)}`),
    );
  }
  return h("div", {class: "toolbar"},
    badge("queued", t("asset.notConsented")),
    button(t("asset.confirmConsent"), () => consentDialog(asset, kind, ctx), {small: true}),
  );
}

function characterCard(asset, ctx) {
  const refs = h("div", {class: "grid-2 asset-references"}, ...views.map(view => {
    const path = asset.reference_images?.[view];
    return h("figure", {class: "asset-reference"},
      path ? media(path, "image", {alt: `${asset.name} · ${t(`asset.view.${view}`)}`, loading: "lazy"}) : h("div", {class: "media-empty"}, t("asset.noReference")),
      h("figcaption", {class: "muted"}, t(`asset.view.${view}`)),
    );
  }));
  return h("article", {class: "card asset-card"},
    h("h3", {}, asset.name),
    refs,
    h("h4", {}, t("asset.appearance")),
    h("p", {class: "asset-prompt"}, asset.fixed_appearance_prompt),
    asset.lora_path ? h("p", {class: "muted break-word"}, `${t("asset.loraPath")} ${asset.lora_path}`) : null,
    consentStatus(asset, "characters", ctx),
  );
}

function voiceCard(asset, ctx) {
  const defaults = asset.default_params || {};
  return h("article", {class: "card asset-card"},
    h("h3", {}, asset.name),
    h("dl", {class: "meta-list"},
      h("dt", {}, t("asset.engine")), h("dd", {}, asset.engine),
      h("dt", {}, t("asset.engineVoice")), h("dd", {}, asset.engine_voice_id || "—"),
      h("dt", {}, t("asset.speed")), h("dd", {}, String(defaults.speed ?? 1)),
      h("dt", {}, t("asset.emotion")), h("dd", {}, defaults.emotion || t("asset.defaultEmotion")),
    ),
    h("div", {class: "form-stack"}, ...(asset.sample_audio_paths || []).map((path, index) => h("div", {},
      h("p", {class: "muted"}, `${t("asset.sample")} ${index + 1}`),
      media(path, "audio", {controls: true, preload: "none", "aria-label": `${asset.name} ${t("asset.sample")} ${index + 1}`}),
    ))),
    consentStatus(asset, "voices", ctx),
  );
}

function createCharacter(ctx) {
  let modal;
  const content = form([
    field(t("asset.name"), input("text", "", {name: "name", required: true, maxlength: 255})),
    field(t("asset.appearance"), textarea("", {name: "appearance_prompt", required: true, rows: 4, placeholder: t("asset.appearancePlaceholder")})),
    h("div", {class: "grid-2"}, ...views.map(view => field(
      t(`asset.view.${view}`),
      input("file", "", {name: view, accept: ".png,.jpg,.jpeg,.webp", required: view === "front"}),
      t(view === "front" ? "asset.frontRequired" : "asset.optional"),
    ))),
    h("p", {class: "muted"}, t("asset.createConsentHint")),
  ], async data => {
    const reference_images = {};
    for (const view of views) {
      const file = data.get(view);
      if (file instanceof File && file.size) reference_images[view] = await upload(file);
    }
    await api("/characters", {method: "POST", body: {
      name: String(data.get("name")).trim(),
      appearance_prompt: String(data.get("appearance_prompt")).trim(),
      reference_images,
    }});
    modal.close();
    notice(t("asset.characterCreated"));
    await ctx.reload();
  }, t("asset.createCharacter"));
  modal = dialog(t("asset.createCharacter"), content);
}

// Voice engines the GPU server can run; each explains itself when chosen.
const VOICE_ENGINES = ["cosyvoice", "gpt-sovits", "f5-tts", "fish-speech"];
const EMOTIONS = ["neutral", "happy", "excited", "calm", "serious", "sad"];

function createVoice(ctx, preset = null) {
  let modal;
  const engine = select(VOICE_ENGINES.map(value => ({value, label: t(`asset.engineName.${value}`)})), "cosyvoice", {name: "engine", required: true});
  const explain = h("div", {class: "engine-explain"});
  const describe = () => {
    explain.replaceChildren(
      h("p", {}, t(`asset.engineAbout.${engine.value}`)),
      h("p", {class: "muted"}, t(`asset.engineFit.${engine.value}`)),
    );
  };
  engine.addEventListener("change", describe);
  describe();
  const content = form([
    field(t("asset.name"), input("text", "", {name: "name", required: true, maxlength: 255, placeholder: t("asset.voiceNamePlaceholder")})),
    preset ? h("p", {class: "muted"}, `${t("asset.presetSample")} ${preset.name}`) : field(t("asset.samples"), input("file", "", {name: "samples", required: true, multiple: true, accept: ".wav,.mp3,.m4a,.flac"}), t("asset.samplesHint")),
    field(t("asset.engine"), engine, t("asset.engineHint")),
    explain,
    h("details", {}, h("summary", {}, t("asset.voiceAdvanced")), h("div", {class: "form-stack details-content"},
      field(t("asset.engineVoice"), input("text", "", {name: "engine_voice_id", maxlength: 255}), t("asset.engineVoiceHint")),
      h("div", {class: "grid-2"},
        field(t("asset.speed"), input("number", "1", {name: "speed", min: 0.1, max: 4, step: 0.05, required: true}), t("asset.speedHint")),
        field(t("asset.emotion"), select(EMOTIONS.map(value => ({value, label: t(`asset.emotionName.${value}`)})), "neutral", {name: "emotion", required: true})),
      ),
    )),
    h("p", {class: "muted"}, t("asset.createConsentHint")),
  ], async data => {
    const sample_paths = preset ? [preset.path] : [];
    for (const file of data.getAll("samples")) {
      if (file instanceof File && file.size) sample_paths.push(await upload(file));
    }
    await api("/voices", {method: "POST", body: {
      name: String(data.get("name")).trim(), sample_paths,
      engine: String(data.get("engine")).trim(),
      engine_voice_id: String(data.get("engine_voice_id")).trim() || null,
      default_params: {speed: Number(data.get("speed")), emotion: String(data.get("emotion")).trim()},
    }});
    modal.close();
    notice(t("asset.voiceCreated"));
    await ctx.reload();
  }, t("asset.createVoice"));
  modal = dialog(t("asset.createVoice"), content);
}

async function generateSheet(ctx) {
  let modal;
  if (!ctx.projects.length) {
    dialog(t("asset.generateSheet"), h("div", {class: "form-stack"},
      h("p", {}, t("asset.sheetNeedsProject")),
      link(t("asset.openDashboard"), "#/"),
    ));
    return;
  }
  const [models, settings] = await Promise.all([
    loadImageModels().catch(() => []),
    api("/settings").catch(() => ({settings: {}})),
  ]);
  const content = form([
    h("p", {class: "muted"}, t("asset.sheetHint")),
    field(t("asset.project"), select(projectOptions(ctx), ctx.projectId || "", {name: "project_id", required: true})),
    field(t("asset.imageModel"), imageModelSelect(models, currentImageChoice(settings.settings || {}), {name: "image_model", required: models.length > 0})),
    field(t("asset.name"), input("text", "", {name: "name", required: true, maxlength: 255})),
    field(t("asset.appearance"), textarea("", {name: "appearance_prompt", required: true, rows: 5, placeholder: t("asset.appearancePlaceholder")})),
  ], async data => {
    const choice = parseImageChoice(data.get("image_model"));
    await api(`/projects/${encodeURIComponent(String(data.get("project_id")))}/character-sheet`, {method: "POST", body: {
      name: String(data.get("name")).trim(),
      appearance_prompt: String(data.get("appearance_prompt")).trim(),
      ...(choice ? {image_provider: choice.provider, image_model: choice.model} : {}),
    }});
    modal.close();
    notice(t("asset.sheetQueued"));
    ctx.navigate("#/jobs");
  }, t("asset.submitSheet"));
  modal = dialog(t("asset.generateSheet"), content);
}

function assignment(characters, voices, ctx) {
  const project = ctx.projects.find(item => item.id === ctx.projectId);
  if (!project) return h("section", {class: "card"},
    h("h2", {}, t("asset.assignTitle")),
    h("p", {class: "muted"}, t("asset.chooseProjectHint")),
  );
  const characterOptions = [{value: "", label: t("asset.chooseCharacter")}, ...characters.map(asset => ({
    value: asset.id, label: asset.consent_confirmed ? asset.name : `${asset.name} · ${t("asset.notConsented")}`, disabled: !asset.consent_confirmed,
  }))];
  const voiceOptions = [{value: "", label: t("asset.chooseVoice")}, ...voices.map(asset => ({
    value: asset.id, label: asset.consent_confirmed ? asset.name : `${asset.name} · ${t("asset.notConsented")}`, disabled: !asset.consent_confirmed,
  }))];
  return h("section", {class: "card"},
    h("h2", {}, `${t("asset.assignTitle")} · ${project.title}`),
    h("p", {class: "muted"}, t("asset.assignHint")),
    form([
      h("div", {class: "grid-2"},
        field(t("asset.character"), select(characterOptions, project.character_id || "", {name: "character_id", required: true})),
        field(t("asset.voice"), select(voiceOptions, project.voice_id || "", {name: "voice_id", required: true})),
      ),
    ], async data => {
      await api(`/projects/${encodeURIComponent(project.id)}/assets`, {method: "PUT", body: {
        character_id: data.get("character_id"), voice_id: data.get("voice_id"),
      }});
      notice(t("asset.assignmentSaved"));
      await ctx.reload();
    }, t("asset.assignAction")),
  );
}

const seconds = value => value ? `${Math.round(value * 10) / 10} 秒` : "";

function separateDialog(ctx) {
  let modal;
  const project = (ctx.projects || []).find(item => item.id === ctx.projectId);
  const useReference = Boolean(project?.source_video_path);
  const file = input("file", "", {name: "video", accept: ".mp4,.mov,.webm,.mkv,.avi", required: !useReference});
  const status = h("p", {class: "chat-activity", role: "status"});
  const content = form([
    h("p", {class: "muted"}, t("media.separateHelp")),
    useReference ? h("p", {}, `${t("media.defaultSource")}「${project.title}」`) : null,
    field(useReference ? t("media.orUpload") : t("media.chooseVideo"), file),
    field(t("media.name"), input("text", "", {name: "name", maxLength: 200, placeholder: t("media.namePlaceholder")})),
    h("label", {class: "checkbox"}, h("input", {type: "checkbox", name: "stems"}), h("span", {}, t("media.stems"))),
    status,
  ], async data => {
    const chosen = data.get("video");
    const body = {name: String(data.get("name") || "").trim() || null, stems: data.get("stems") === "on"};
    status.replaceChildren(h("span", {class: "pulse", "aria-hidden": "true"}), t(body.stems ? "media.workingStems" : "media.working"));
    try {
      if (chosen instanceof File && chosen.size) body.path = await upload(chosen);
      else body.project_id = project.id;
      await api("/media-assets/separate", {method: "POST", body});
    } catch (error) {
      status.className = "error-text";
      status.textContent = error.message;
      throw error;
    }
    modal.close();
    notice(t("media.done"));
    await ctx.reload();
  }, t("media.separate"));
  modal = dialog(t("media.separate"), content);
}

function mediaCard(asset, ctx) {
  return h("article", {class: "card stack"},
    h("div", {class: "row spread"}, h("h3", {}, asset.name), badge(asset.kind === "video" ? "running" : "clean", t(`media.kind.${asset.kind}`))),
    media(asset.path, asset.kind, {preload: "metadata", class: `media media-${asset.kind}`}),
    h("small", {class: "muted"}, [seconds(asset.duration_s), date(asset.created_at)].filter(Boolean).join(" · ")),
    h("div", {class: "toolbar"},
      h("a", {class: "button small", href: mediaURL(asset.path), download: ""}, t("media.download")),
      asset.kind === "audio" ? button(t("media.useAsVoice"), () => createVoice(ctx, asset), {small: true}) : null,
      button(t("media.delete"), async () => {
        if (!confirm(t("media.deleteConfirm"))) return;
        await api(`/media-assets/${encodeURIComponent(asset.id)}`, {method: "DELETE"});
        notice(t("media.deleted"));
        await ctx.reload();
      }, {small: true, danger: true}),
    ),
  );
}

export async function render(ctx) {
  const [characters, voices, clips] = await Promise.all([api("/characters", {signal: ctx.signal}), api("/voices", {signal: ctx.signal}), api("/media-assets", {signal: ctx.signal})]);
  return h("div", {class: "page assets-page"},
    h("header", {class: "page-header"},
      h("div", {}, h("h1", {}, t("asset.title")), h("p", {class: "muted"}, t("asset.subtitle"))),
      h("div", {class: "toolbar"},
        button(t("media.separate"), () => separateDialog(ctx)),
        button(t("asset.generateSheet"), () => generateSheet(ctx)),
        button(t("asset.createVoice"), () => createVoice(ctx)),
        button(t("asset.createCharacter"), () => createCharacter(ctx), {primary: true}),
      ),
    ),
    assignment(characters, voices, ctx),
    h("section", {class: "page-section"},
      h("div", {class: "section-heading"}, h("h2", {}, t("asset.characters")), h("span", {class: "muted"}, `${characters.length} ${t("asset.items")}`)),
      characters.length ? h("div", {class: "grid-2"}, ...characters.map(asset => characterCard(asset, ctx))) : empty(t("asset.noCharacters"), t("asset.noCharactersHint")),
    ),
    h("section", {class: "page-section"},
      h("div", {class: "section-heading"}, h("h2", {}, t("asset.voices")), h("span", {class: "muted"}, `${voices.length} ${t("asset.items")}`)),
      voices.length ? h("div", {class: "grid-2"}, ...voices.map(asset => voiceCard(asset, ctx))) : empty(t("asset.noVoices"), t("asset.noVoicesHint")),
    ),
    h("section", {class: "page-section"},
      h("div", {class: "section-heading"}, h("h2", {}, t("media.title")), h("span", {class: "muted"}, `${clips.length} ${t("asset.items")}`)),
      clips.length ? h("div", {class: "grid-2"}, ...clips.map(asset => mediaCard(asset, ctx))) : empty(t("media.empty"), t("media.emptyHint"), button(t("media.separate"), () => separateDialog(ctx), {primary: true})),
    ),
  );
}
