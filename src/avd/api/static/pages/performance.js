import {api, upload, mediaURL} from '../api.js';
import {h, button, field, input, select, textarea, form, dialog, notice, media, badge, link} from '../components.js';
import {jobExecution, jobFailure, jobCopyButton, jobErrorDetails, jobDiagnosticList} from '../jobExecution.js';
import {createPerformanceComparison} from '../performanceComparison.js';

const path = (id, suffix = '') => `/projects/${encodeURIComponent(id)}/performance${suffix}`;
const panel = (title, ...children) => h('section', {class: 'card panel stack'}, h('h2', {}, title), ...children);
const text = value => h('p', {class: 'muted'}, value);
const approved = (project, stage) => Boolean(project.reviews?.[stage]?.approved);
const labels = {
  performance_prepare: '参考片准备', vocal_separation: '人声 / 伴奏分离', scene_reference: '人物与场景参考图',
  singing_voice: '目标歌声转换', performance_mix: '歌声与伴奏混音', motion_video: '动作与人物视频',
  performance_sync: '时间轴同步', performance_lipsync: '口型修正', performance_final: '成片合成',
};
const styles = `
.performance-workbench{min-width:0}.performance-workbench .performance-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}
.performance-workbench .performance-assets{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.performance-workbench .performance-assets video,.performance-workbench .performance-assets img{width:100%;max-height:360px;object-fit:contain}.performance-workbench audio{width:100%}
.performance-workbench .performance-reasons{margin:0;padding-left:20px;overflow-wrap:anywhere}.performance-workbench .performance-actions{display:flex;gap:10px;flex-wrap:wrap}.performance-workbench .performance-stage{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--line,var(--border))}.performance-workbench .performance-stage strong{flex:1}
.performance-workbench .performance-scroll{overflow-x:auto}.performance-workbench pre{white-space:pre-wrap;overflow-wrap:anywhere}.performance-workbench .performance-source{max-height:280px;width:100%;object-fit:contain}.performance-workbench .performance-subtitle{max-width:76ch}.performance-workbench .performance-stale{border-left:3px solid var(--accent-a,#f0ae42);padding-left:12px}
@media(max-width:800px){.performance-workbench .performance-columns,.performance-workbench .performance-assets{grid-template-columns:1fr}}
`;

export function buildPerformanceConfig(values, base = {}) {
  const configuration = {
    ...base,
    mode: 'performance_remix',
    preparation: {...base.preparation, source_video_path: values.source, start_s: Number(values.start), duration_s: Number(values.duration), sample_rate: base.preparation?.sample_rate || 48000},
    character_id: values.character, voice_id: values.voice,
    voice_model_version_id: values.model || null,
    scene_prompt: values.scene.trim(), lipsync: Boolean(values.lipsync),
  };
  if (base.voice_model_version_id !== configuration.voice_model_version_id || base.voice_id !== configuration.voice_id) delete configuration.voice_parameters;
  return configuration;
}

export function buildPerformancePatch(values, base = {}) {
  const configuration = buildPerformanceConfig(values, base);
  const patch = {};
  for (const key of ['character_id', 'voice_id', 'voice_model_version_id', 'scene_prompt', 'lipsync']) {
    if (configuration[key] !== base[key]) patch[key] = configuration[key];
  }
  const preparation = Object.fromEntries(Object.entries(configuration.preparation).filter(([key, value]) => value !== base.preparation?.[key]));
  if (Object.keys(preparation).length) patch.preparation = preparation;
  if ('voice_id' in patch || 'voice_model_version_id' in patch) patch.voice_parameters = null;
  return patch;
}

export function sourceOptions(assets, projects = [], current = '') {
  const sources = new Map();
  if (current) sources.set(current, '当前参考视频');
  for (const asset of assets.filter(item => item.kind === 'video')) {
    // Split library videos are silent; their original source retains the song.
    const source = asset.source || asset.path;
    if (source && !sources.has(source)) sources.set(source, asset.source ? `${asset.name} · 原始音画` : asset.name);
  }
  for (const project of projects) {
    if (project.source_video_path && !sources.has(project.source_video_path)) sources.set(project.source_video_path, `${project.title} · 参考片`);
  }
  return [...sources].map(([value, label]) => ({value, label}));
}

export function stageReadiness(readiness, lipsync = false) {
  const voice = readiness?.voice || {};
  return [
    {label: '参考视频准备', ready: readiness?.source_preparation?.ready, reasons: readiness?.source_preparation?.reasons || []},
    {label: '人声 / 伴奏分离', ready: readiness?.separation?.ready, reasons: readiness?.separation?.reasons || []},
    {label: '歌声转换模型', ready: voice.ready_for_inference, reasons: voice.reasons || []},
    {label: '动作与换人工作流', ready: readiness?.motion?.ready, reasons: readiness?.motion?.reasons || []},
    ...(lipsync ? [{label: '口型修正工作流', ready: readiness?.lipsync?.ready, reasons: readiness?.lipsync?.reasons || []}] : []),
  ];
}

export function singingModelLabel(model) {
  const engine = {'rvc': 'RVC', 'seed-vc': 'SeedVC · 声音参考'}[model.engine] || model.engine || '未知引擎';
  return `v${model.number} · ${engine} · ${model.sample_rate / 1000} kHz · ${model.fingerprint.slice(0, 8)}`;
}

export function singingParameterSummary(configuration, voiceId, model) {
  if (!model) return '选择歌声版本后显示实际参数。';
  const saved = configuration.voice_id === voiceId && configuration.voice_model_version_id === model.id
    && configuration.voice_parameters && Object.keys(configuration.voice_parameters).length > 0;
  const parameters = {...model.parameters, ...(saved ? configuration.voice_parameters : {})};
  const parts = [];
  if (parameters.diffusion_steps != null) parts.push(`${parameters.diffusion_steps} 步`);
  if (parameters.inference_cfg_rate != null) parts.push(`CFG ${parameters.inference_cfg_rate}`);
  if (parameters.pitch != null) parts.push(`音高 ${parameters.pitch}`);
  if (parameters.auto_f0_adjust != null) parts.push(`自动音高${parameters.auto_f0_adjust ? '开' : '关'}`);
  return `${saved ? '当前已保存参数' : '所选版本参数'}：${parts.join(' · ') || '使用此版本的默认设置'}。`;
}

export function voiceAfterProbe(current, result) {
  if (result.status === 'succeeded' && result.readiness) return {...result.readiness, quality_verified: false};
  const reason = ['failed', 'cancelled'].includes(result.status) ? result.error || '模型探测未完成，请查看 GPU 任务错误后重试。'
    : result.status === 'succeeded' ? '探测任务未返回模型检查结果，请查看任务详情。'
      : '模型探测已进入 GPU 队列，等待任务完成后显示实际结果。';
  return {...current, ready_for_inference: false, quality_verified: false, reasons: [reason]};
}

export function readinessAfterRefresh(readiness, probe, modelId) {
  if (!probe || probe.model_id !== modelId) return readiness;
  const voice = readiness?.voice || {};
  // The server restores persisted probes only after validating current files,
  // configuration and runtime hashes. A browser-held result cannot override it.
  if (!['queued', 'running'].includes(probe.status)
    || (voice.probe_job_id && voice.probe_job_id !== probe.job_id)) return readiness;
  return {...readiness, voice: voiceAfterProbe(voice, probe)};
}

export function pendingProbeFromReadiness(readiness, modelId) {
  const voice = readiness?.voice;
  if (!modelId || voice?.version_id !== modelId || !voice.probe_job_id
    || !['queued', 'running'].includes(voice.probe_status)) return null;
  return {model_id: modelId, job_id: voice.probe_job_id, status: voice.probe_status};
}

export function hasActiveGeneration(jobs) {
  return jobs.some(job => ['queued', 'running'].includes(job.status)
    && (Boolean(job.node_id) || Object.hasOwn(labels, job.job_type)));
}

export function performanceVideoReady(project) {
  const required = ['scene_reference', 'singing_voice', 'motion_video', 'performance_sync', 'performance_mix', 'performance_final'];
  if (project.performance_config?.lipsync) required.push('performance_lipsync');
  if (!required.every(kind => project.nodes?.some(node => node.kind === kind && node.status === 'clean' && node.current_version_id))) return false;
  const final = project.nodes.find(node => node.kind === 'performance_final');
  return typeof final.output_paths?.video === 'string' && /\.(mp4|webm|mov|mkv)$/i.test(final.output_paths.video);
}

async function createSeedReference(ctx, voiceId, onCreated) {
  const data = await api(`/voices/${encodeURIComponent(voiceId)}/singing-reference-samples`, {signal: ctx.signal});
  const samples = (data.reference_samples || []).filter(sample => sample.exists);
  if (!samples.length) throw new Error('这个声音没有可用的参考音频，请先在素材空间添加声音样本。');
  const reference = select(samples.map(sample => ({value: sample.path, label: `${sample.path.split('/').at(-1)} · ${Number(sample.duration_s).toFixed(1)} 秒`})), samples[0].path, {name: 'reference', required: true});
  const preview = h('div', {});
  const stopSample = () => {for (const player of preview.querySelectorAll('audio')) player.pause();};
  const showSample = () => {stopSample();preview.replaceChildren(media(reference.value, 'audio', {preload: 'metadata'}));};
  reference.addEventListener('change', showSample);showSample();
  const provenance = textarea('用户选择的目标声音样本，用于零样本歌声音色转换。', {name: 'provenance', required: true, maxlength: 2000, rows: 2});
  const errorText = h('p', {class: 'error-text', role: 'alert', hidden: true});
  let modal;
  let creating = false;
  const content = form([
    text('SeedVC 使用这段声音作为音色参考，保留原歌曲与节奏。无需训练专属 RVC 权重；保存后仍需探测并试听。'),
    field('目标声音参考', reference, '仅可选择这个声音在素材空间登记的样本。'), preview,
    field('版本说明', provenance), errorText,
  ], async () => {
    if (creating) return;
    creating = true;
    errorText.hidden = true;
    try {
      const result = await api('/singing-models/import', {method: 'POST', signal: ctx.signal, body: {
        voice_id: voiceId, engine: 'seed-vc', reference_path: reference.value, sample_rate: 44100,
        parameters: {diffusion_steps: 30, inference_cfg_rate: 0.7, pitch: 0, auto_f0_adjust: false, fp16: true, seed: 20261021},
        provenance: provenance.value.trim(),
      }});
      if (ctx.signal?.aborted) return;
      modal.close();
      await onCreated(result.id);
      notice('歌声参考版本已保存，请探测运行环境并生成试听。');
    } catch (error) {
      if (error.name !== 'AbortError') {errorText.textContent = error.message;errorText.hidden = false;}
      throw error;
    } finally {creating = false;}
  }, '保存歌声参考版本');
  modal = dialog('用声音样本创建歌声参考版本', content);
  const cleanup = () => {stopSample();ctx.signal?.removeEventListener('abort', cleanup);};
  modal.element.addEventListener('close', cleanup, {once: true});
  ctx.signal?.addEventListener('abort', cleanup, {once: true});
}

async function inputForm(ctx, project, {characters, voices, clips}) {
  const base = project?.performance_config || {};
  const title = input('text', project?.title || '我的唱跳换装样片', {name: 'title', required: true, maxlength: 255});
  const source = select([{value: '', label: '选择素材中的原始唱跳视频'}, ...sourceOptions(clips, ctx.projects, base.preparation?.source_video_path || project?.source_video_path)], base.preparation?.source_video_path || project?.source_video_path || '', {name: 'source'});
  const file = input('file', '', {name: 'video', accept: '.mp4,.mov,.webm,.mkv,.avi'});
  const start = input('number', base.preparation?.start_s ?? 0, {name: 'start', min: 0, step: 0.001, required: true});
  const duration = input('number', base.preparation?.duration_s ?? 10, {name: 'duration', min: 0.1, max: 10, step: 0.1, required: true});
  const assetOptions = assets => [{value: '', label: '请选择已授权资产'}, ...assets.map(asset => ({value: asset.id, label: `${asset.name}${asset.consent_confirmed ? '' : ' · 尚未授权'}`, disabled: !asset.consent_confirmed}))];
  const character = select(assetOptions(characters), base.character_id || project?.character_id || '', {name: 'character', required: true});
  const voice = select(assetOptions(voices), base.voice_id || project?.voice_id || '', {name: 'voice', required: true});
  const model = select([{value: '', label: '暂不选择 · 可先准备参考片'}], '', {name: 'model'});
  const modelHelp = h('p', {class: 'muted', role: 'status'}, '选择已保存的歌声模型或声音参考版本。');
  const parameterHelp = h('p', {class: 'muted', role: 'status'});
  const scene = textarea(base.scene_prompt || '', {name: 'scene', required: true, maxlength: 8000, rows: 3, placeholder: '例如：霓虹灯舞台，人物全身，镜头保留原参考片动作与节奏'});
  const lipsync = input('checkbox', base.lipsync === true, {name: 'lipsync'});
  const sourcePreview = h('div', {});
  const uploadStatus = h('p', {class: 'muted', role: 'status'});
  let modelRequest = 0;
  let singingModels = [];
  let createdId = project?.id;
  let uploadedPath = '';
  let saving = false;
  const createSeed = button('用声音样本创建版本', async () => {
    const selectedVoice = voice.value;
    if (!selectedVoice) throw new Error('请先选择目标声音。');
    await createSeedReference(ctx, selectedVoice, async modelId => {
      if (voice.value !== selectedVoice || ctx.signal?.aborted) return;
      await loadModels(modelId);
      model.dispatchEvent(new Event('change', {bubbles: true}));
    });
  }, {small: true, disabled: !voice.value});
  const controls = [title, source, file, start, duration, character, voice, model, createSeed, scene, lipsync];
  function showSource() {
    sourcePreview.replaceChildren(...(source.value ? [media(source.value, 'video', {class: 'media performance-source', preload: 'metadata'})] : []));
  }
  source.addEventListener('change', () => {file.value = ''; uploadedPath = ''; showSource();});
  file.addEventListener('change', () => {uploadedPath = ''; uploadStatus.textContent = file.files?.[0] ? '保存时上传所选视频；原文件会保留。' : '';});
  showSource();
  const showParameters = () => {parameterHelp.textContent = singingParameterSummary(base, voice.value, singingModels.find(item => item.id === model.value));};
  model.addEventListener('change', showParameters);
  async function loadModels(selected = '') {
    const request = ++modelRequest;
    singingModels = [];
    model.replaceChildren(h('option', {value: ''}, '暂不选择 · 可先准备参考片'));
    showParameters();
    if (!voice.value) {modelHelp.textContent = '先选择目标声音，再选择属于该声音的歌声模型版本。';return;}
    modelHelp.textContent = '正在读取歌声模型…';
    try {
      const data = await api(`/singing-models?voice_id=${encodeURIComponent(voice.value)}`, {signal: ctx.signal});
      if (request !== modelRequest || ctx.signal?.aborted) return;
      singingModels = data.models || [];
      for (const item of data.models || []) model.append(h('option', {value: item.id}, singingModelLabel(item)));
      model.value = [...model.options].some(option => option.value === selected) ? selected : '';
      showParameters();
      modelHelp.textContent = data.models?.length ? '版本保存后仍须探测运行环境，并试听验证音色。' : '这个声音还没有歌声转换版本。可先准备参考片，再使用声音样本创建参考版本，或导入 RVC 模型。';
    } catch (error) {if (request === modelRequest && !ctx.signal?.aborted && error.name !== 'AbortError') modelHelp.textContent = `歌声模型未能读取：${error.message}`;}
  }
  voice.addEventListener('change', () => {createSeed.disabled = !voice.value;void loadModels();});
  await loadModels(base.voice_model_version_id || '');
  const formError = h('p', {class: 'error-text', role: 'alert', hidden: true});
  const content = form([
    ...(!project ? [field('作品名称', title)] : []),
    field('唱跳参考视频', source, '保留原视频的动作、歌曲、节奏；分离素材会自动使用其原始音画源。'),
    field('或上传参考视频', file, '优先使用本次选择的文件。需要同时包含画面与声音。'), uploadStatus, sourcePreview,
    h('div', {class: 'grid-2'}, field('起点（秒）', start), field('预览时长（秒）', duration, '先制作最多 10 秒小样。')),
    h('div', {class: 'grid-2'}, field('目标人物', character), field('目标声音', voice)),
    link('管理人物、声音与授权', '#/assets'), field('歌声转换版本（可稍后选择）', model), parameterHelp, modelHelp, createSeed,
    field('新场景', scene), h('label', {class: 'checkbox'}, lipsync, h('span', {}, '启用口型修正（需可运行的口型工作流）')),
    formError,
  ], async () => {
    if (saving) return;
    saving = true;
    const disabled = controls.map(control => control.disabled);
    controls.forEach(control => {control.disabled = true;});
    formError.hidden = true;
    try {
      const chosen = file.files?.[0];
      if (!source.value && !chosen && !uploadedPath) throw new Error('请选择素材视频或上传唱跳参考片。');
      if (chosen && !uploadedPath) {uploadStatus.textContent = '正在上传参考片…'; uploadedPath = await upload(chosen); uploadStatus.textContent = '参考片上传完成。';}
      const configuration = buildPerformancePatch({source: uploadedPath || source.value, start: start.value, duration: duration.value, character: character.value, voice: voice.value, model: model.value, scene: scene.value, lipsync: lipsync.checked}, base);
      if (!createdId) {
        const created = await api('/projects', {method: 'POST', body: {title: title.value.trim(), mode: 'performance_remix'}});
        createdId = created.id;
      }
      await api(path(createdId), {method: 'PATCH', body: configuration, signal: ctx.signal});
      notice('唱跳输入已保存；审批输入后可试听歌声、查看场景图并生成唱跳视频预览。');
      if (project) await ctx.reload();
      else ctx.navigate(`#/projects/${createdId}/performance`);
    } catch (error) {formError.textContent = `${error.message}${createdId && !project ? ' 项目已保留，重试会继续保存。' : ''}`;formError.hidden = false;throw error;}
    finally {saving = false;controls.forEach((control, index) => {control.disabled = disabled[index];});}
  }, project ? '保存唱跳配置' : '创建唱跳项目并保存配置');
  content.dataset.performanceInputs = 'true';
  return content;
}

function outputs(project) {
  const cards = [];
  const types = [['image', /\.(png|jpe?g|webp)$/i], ['audio', /\.(wav|mp3|flac|m4a|ogg)$/i], ['video', /\.(mp4|webm|mov|mkv)$/i]];
  for (const node of project.nodes || []) {
    if (!node.current_version_id) continue;
    for (const [name, value] of Object.entries(node.output_paths || {})) {
      if (typeof value !== 'string') continue;
      const kind = types.find(([, pattern]) => pattern.test(value))?.[0];
      if (!kind) continue;
      const stale = node.status !== 'clean';
      const note = node.kind === 'performance_prepare' ? '原视频的准备产物，尚未换人、换声或换场景。' : node.kind === 'vocal_separation' ? '原参考片分离结果，尚未转换歌声。' : node.kind === 'scene_reference' ? '人物与场景参考图，尚未生成动作视频。' : node.kind === 'performance_final' ? '成片已输出，仍需核对动作、音色、口型与 AI 标识。' : '实际任务产物，音色与画面质量待人工确认。';
      cards.push(h('article', {class: `card stack${stale ? ' performance-stale' : ''}`}, h('h3', {}, `${labels[node.kind] || node.kind} · ${name}`), stale && text('此为历史输出，配置已更新或任务尚未成功；请勿作为当前结果验收。'), media(value, kind, {preload: 'metadata'}), text(note), h('a', {href: mediaURL(value), download: '', class: 'button small'}, node.kind === 'performance_final' ? '导出 MP4' : '下载产物')));
    }
  }
  return cards.length ? h('div', {class: 'performance-assets'}, ...cards) : text('还没有实际产物。排队、配置保存和参考片准备均不代表已完成 AI 唱跳生成。');
}

function jobList(ctx, refresh) {
  return jobDiagnosticList({signal: ctx.signal, scope: '当前唱跳项目全部任务', render: job => h('div', {class: 'performance-stage'},
    h('strong', {}, labels[job.job_type] || job.job_type), badge(job.status), text(`尝试 ${job.attempts}/${job.max_attempts}`),
    jobCopyButton(job),
    jobExecution(job),
    jobFailure(job),
    jobErrorDetails(job),
    job.status === 'queued' && button('取消排队', async () => {await api(`/jobs/${job.id}/cancel`, {method: 'POST'}); await refresh();}, {small: true}),
    job.status === 'failed' && button('重试', async () => {await api(`/jobs/${job.id}/retry`, {method: 'POST'}); await refresh();}, {small: true}),
  )});
}

function approval(ctx, project, stage, title, description) {
  let modal;
  const content = form([
    text(description), field('审核人', input('text', '', {name: 'actor', required: true, maxlength: 255, autocomplete: 'name'})),
    h('label', {class: 'checkbox'}, input('checkbox', false, {required: true}), h('span', {}, '我已检查上述内容并同意继续。')),
  ], async data => {
    await api(`/projects/${project.id}/approve`, {method: 'POST', body: {stage, approved_by: String(data.get('actor')).trim()}});
    modal.close();await ctx.reload();
  }, '确认审批');
  modal = dialog(title, content);
}

export async function renderProject(ctx, initialProject, initialJobs = []) {
  let project = initialProject;
  let comparisonProject = initialProject;
  let jobs = initialJobs;
  let readiness;
  let readinessError = '';
  let dirty = false;
  let actionBusy = false;
  let actionError = '';
  let probeState = null;
  let refreshRequest = 0;
  let formRequest = 0;
  let remoteConfigChanged = false;
  let formSignature = JSON.stringify(project.performance_config);
  let sourceSignature = project.source_video_path;
  let [characters, voices, clips] = await Promise.all([api('/characters', {signal: ctx.signal}), api('/voices', {signal: ctx.signal}), api('/media-assets', {signal: ctx.signal})]);
  try {readiness = await api(path(project.id, '/readiness'), {signal: ctx.signal});}
  catch (error) {if (error.name === 'AbortError') throw error;readinessError = error.message;}
  probeState = pendingProbeFromReadiness(readiness, project.performance_config?.voice_model_version_id);
  const root = h('div', {class: 'performance-workbench stack'}, h('style', {}, styles),
    h('header', {class: 'page-heading'}, h('div', {}, h('p', {class: 'eyebrow'}, 'PERFORMANCE REMIX'), h('h1', {}, project.title), text('保留参考片的动作与歌曲，替换人物、歌声音色和场景。')), h('div', {class: 'toolbar'}, link('唱跳项目', '#/performance'), button('刷新', () => ctx.reload(), {small: true}))),
  );
  const dynamic = h('div', {class: 'stack'});
  const taskList = jobList(ctx, refresh);
  const taskPanel = panel('任务进度', taskList, link('查看全部任务 / 工作进程', '#/jobs'));
  const outputPanel = panel('试听、预览与导出', outputs(project));
  const comparison = createPerformanceComparison(comparisonProject, {signal: ctx.signal});
  const stopMedia = () => {
    document.removeEventListener('play', coordinatePlayback, true);
    comparison.dispose();
    for (const player of document.querySelectorAll('video,audio')) player.pause();
  };
  ctx.onDispose?.(stopMedia);
  ctx.signal?.addEventListener('abort', stopMedia, {once: true});
  function coordinatePlayback(event) {
    if (comparison.element.contains(event.target)) {
      for (const player of document.querySelectorAll('video,audio')) if (!comparison.element.contains(player)) player.pause();
    } else comparison.pause();
  }
  // Sample dialogs live under document.body, outside the workbench root.
  document.addEventListener('play', coordinatePlayback, true);
  const inputPanel = panel('唱跳输入', await inputForm(ctx, project, {characters, voices, clips}));
  const sourcePanel = panel('你的参考片与时间轴', text('建议选一个完整的约 10 秒副歌动作段，单人、全身可见、手脚尽量不出画面。'), project.source_video_path && media(project.source_video_path, 'video', {class: 'media performance-source'}), text('更换场景或人物将重建相关画面；更换声音只重建歌声及依赖它的成片。保存配置后，旧产物和版本会保留。'));
  let signature = '';
  let outputSignature = JSON.stringify(project.nodes);
  function paint() {
    comparison.update(comparisonProject);
    taskList.updateJobs(jobs);
    const current = JSON.stringify([project.performance_config, project.nodes, project.reviews, jobs, readiness, readinessError, dirty, actionBusy, actionError, probeState, remoteConfigChanged]);
    const nextOutputs = JSON.stringify(project.nodes);
    if (nextOutputs !== outputSignature && ![...outputPanel.querySelectorAll('video,audio')].some(player => !player.paused && !player.ended)) {
      outputPanel.replaceChildren(h('h2', {}, '试听、预览与导出'), outputs(project));
      outputSignature = nextOutputs;
    }
    if (current === signature) return;
    signature = current;
    const active = actionBusy || ['queued', 'running'].includes(probeState?.status) || hasActiveGeneration(jobs);
    const inputReview = approved(project, 'performance_inputs');
    const previewReview = approved(project, 'performance_preview');
    const hasConfig = Boolean(project.performance_config);
    const previewReady = performanceVideoReady(project);
    const finalReady = project.nodes.some(node => node.kind === 'performance_final' && node.status === 'clean' && node.current_version_id);
    async function enqueue(action) {
      if (actionBusy || dirty) return;
      actionBusy = true;actionError = '';paint();
      try {
        const result = await api(path(project.id, `/${action}`), {method: 'POST'});
        notice(result.job_ids?.length ? '任务已进入队列；成功后的实际产物将在下方显示。' : '已复用当前有效产物，无需重复生成。');
        await refresh();
      } catch (error) {
        if (error.name !== 'AbortError') {actionError = error.message;await refresh(true).catch(() => {});}
        throw error;
      } finally {actionBusy = false;if (!ctx.signal?.aborted) paint();}
    }
    const checks = stageReadiness(readiness, project.performance_config?.lipsync).map(item => h('div', {class: 'stack'}, h('div', {class: 'row spread'}, h('strong', {}, item.label), badge(item.ready ? 'ok' : 'unknown', item.ready ? '可用' : '尚未就绪')),
      item.reasons.length ? h('ul', {class: 'performance-reasons'}, ...item.reasons.map(reason => h('li', {}, reason))) : text(item.ready ? '工程检查通过；实际效果仍需查看产物。' : '等待配置或运行时探测。')));
    const voiceModelId = project.performance_config?.voice_model_version_id;
    const probe = button('探测所选歌声模型', async () => {
      if (actionBusy || dirty) return;
      actionBusy = true;actionError = '';paint();
      try {
        const result = await api(`/singing-models/${encodeURIComponent(voiceModelId)}/readiness?probe=true`, {method: 'POST', signal: ctx.signal});
        if (ctx.signal?.aborted) return;
        probeState = {job_id: result.job_id, model_id: voiceModelId, status: result.status || 'queued'};
        readiness = {...readiness, voice: voiceAfterProbe(readiness?.voice, result)};
        notice('歌声模型探测已排队，将与其他 GPU 任务串行运行。');
        await refresh();
      } catch (error) {
        if (error.name !== 'AbortError') actionError = error.message;
        throw error;
      } finally {actionBusy = false;if (!ctx.signal?.aborted) paint();}
    }, {disabled: !voiceModelId || active || dirty, small: true});
    dynamic.replaceChildren(
      panel('执行环境与模型就绪', readinessError && h('p', {class: 'error-text'}, readinessError), h('div', {class: 'performance-columns'}, ...checks), probe,
        probeState && text(`模型探测任务：${{queued: '等待 GPU', running: '正在探测', succeeded: '探测完成', failed: '探测失败', cancelled: '已取消'}[probeState.status] || probeState.status}`),
        readiness?.voice?.runtime_files_available && !readiness.voice.ready_for_inference && text('歌声运行文件已就绪；模型将在 GPU 队列中验证。'),
        text('模型探测成功只代表可以尝试推理；唱跳动作、音色与口型都需要实际样片验收。')),
      panel('分阶段制作',
        text('审批输入后，可先试听歌声和查看场景图，再生成完整唱跳视频预览。预览审批需要检查实际视频中的动作、音色与口型。'),
        hasActiveGeneration(jobs) && text('生成任务进行中，可以编辑草稿；请在任务结束后保存新配置。'),
        actionError && h('p', {class: 'error-text', role: 'alert'}, actionError),
        dirty && h('p', {class: 'performance-stale', role: 'status'}, '输入已修改，请先保存配置。任务进度刷新会保留当前编辑。'),
        remoteConfigChanged && h('div', {class: 'performance-stale stack', role: 'status'}, text('已保存配置已由导演或其他操作更新。当前草稿已保留，保存时只提交你修改的字段。'), button('放弃草稿并载入最新配置', async () => {
          const wasDirty = dirty;
          actionBusy = true;dirty = false;paint();
          try {await syncInputs();}
          catch (error) {dirty = wasDirty;throw error;}
          finally {actionBusy = false;paint();}
        }, {small: true, disabled: active})),
        h('div', {class: 'performance-actions'},
          button('1 · 准备参考片', () => enqueue('prepare'), {disabled: !hasConfig || active || dirty}),
          button(inputReview ? '输入已审批' : '2 · 审批输入', () => approval(ctx, project, 'performance_inputs', '审批唱跳输入', '核对参考视频区间、目标人物、声音与新场景。审批仅确认当前配置。'), {disabled: !hasConfig || inputReview || active || dirty}),
          button('3 · 试听歌声与查看场景图', () => enqueue('preview'), {disabled: !inputReview || active || dirty || !voiceModelId || readiness?.ready_for_preview !== true || Boolean(readinessError)}),
          button('4 · 生成唱跳视频预览', () => enqueue('video-preview'), {disabled: !inputReview || active || dirty || readiness?.ready_for_video_preview !== true || Boolean(readinessError), primary: true}),
          button(previewReview ? '视频预览已审批' : '5 · 审批唱跳视频预览', () => approval(ctx, project, 'performance_preview', '审批唱跳视频预览', '请播放实际唱跳 MP4，检查舞蹈动作、人物与场景、转换后的歌声、手脚和口型。场景图与换声试听不能代替视频预览。'), {disabled: !previewReady || previewReview || active || dirty}),
          button(finalReady ? '6 · 复用已审批成片' : '6 · 生成当前区间成片', () => enqueue('generate'), {disabled: !previewReview || active || dirty || readiness?.ready_for_generation !== true || Boolean(readinessError)}),
          button(approved(project, 'performance_final') ? '成片已审批' : '7 · 审批成片', () => approval(ctx, project, 'performance_final', '审批唱跳成片', '检查起点、中段、结尾的动作与节拍、人物、场景、手脚遮挡、歌声高长音、口型及 AI 标识。'), {disabled: !finalReady || !previewReview || approved(project, 'performance_final') || active || dirty}),
        ), text('缺少模型或工作流时，接口会明确说明原因，不会用原唱或普通朗读代替换声。')),
    );
  }
  async function syncInputs() {
    const nextSignature = JSON.stringify(project.performance_config);
    if (nextSignature === formSignature) {remoteConfigChanged = false;return;}
    if (dirty) {remoteConfigChanged = true;return;}
    const request = ++formRequest;
    const latest = project;
    [characters, voices, clips] = await Promise.all([api('/characters', {signal: ctx.signal}), api('/voices', {signal: ctx.signal}), api('/media-assets', {signal: ctx.signal})]);
    const nextForm = await inputForm(ctx, latest, {characters, voices, clips});
    if (ctx.signal?.aborted || request !== formRequest) return;
    if (dirty || JSON.stringify(project.performance_config) !== nextSignature) {remoteConfigChanged = true;return;}
    inputPanel.replaceChildren(h('h2', {}, '唱跳输入'), nextForm);
    formSignature = nextSignature;remoteConfigChanged = false;
    if (latest.source_video_path !== sourceSignature) {
      const currentVideo = sourcePanel.querySelector('video');
      if (currentVideo) {currentVideo.pause();currentVideo.remove();}
      if (latest.source_video_path) sourcePanel.append(media(latest.source_video_path, 'video', {class: 'media performance-source'}));
      sourceSignature = latest.source_video_path;
    }
  }
  async function refresh(forceReadiness = false) {
    const request = ++refreshRequest;
    const pendingProbe = probeState && ['queued', 'running'].includes(probeState.status) ? {...probeState} : null;
    const next = await Promise.all([
      api(`/projects/${project.id}/status`, {signal: ctx.signal}).then(status => {
        if (!ctx.signal?.aborted && request === refreshRequest) {
          comparisonProject = status;
          comparison.update(comparisonProject);
        }
        return status;
      }), api(`/projects/${project.id}/jobs`, {signal: ctx.signal}),
      ...(pendingProbe ? [api(`/singing-models/${encodeURIComponent(pendingProbe.model_id)}/readiness/probes/${encodeURIComponent(pendingProbe.job_id)}`, {signal: ctx.signal})] : []),
    ]);
    if (ctx.signal?.aborted || request !== refreshRequest) return;
    const configChanged = JSON.stringify(project.performance_config) !== JSON.stringify(next[0].performance_config);
    [project, jobs] = next;
    if (configChanged) actionError = '';
    if (probeState?.model_id !== project.performance_config?.voice_model_version_id) probeState = null;
    if (pendingProbe && probeState?.job_id === pendingProbe.job_id && ['queued', 'running'].includes(probeState.status)) {
      const probe = next[2];
      probeState = {...probeState, ...probe};
    }
    const probeFinished = pendingProbe && !['queued', 'running'].includes(probeState?.status);
    if (configChanged || forceReadiness || readinessError || probeFinished) {
      try {
        const state = await api(path(project.id, '/readiness'), {signal: ctx.signal});
        if (ctx.signal?.aborted || request !== refreshRequest) return;
        readiness = state;readinessError = '';
        const restoredProbe = pendingProbeFromReadiness(state, project.performance_config?.voice_model_version_id);
        if (restoredProbe) probeState = restoredProbe;
        else if (state.voice?.probe_job_id !== probeState?.job_id) probeState = null;
      } catch (error) {
        if (error.name === 'AbortError') throw error;
        if (request !== refreshRequest) return;
        readinessError = error.message;
      }
    }
    readiness = readinessAfterRefresh(readiness, probeState, project.performance_config?.voice_model_version_id);
    await syncInputs();
    if (ctx.signal?.aborted || request !== refreshRequest) return;
    paint();
  }
  const {chatPanel} = await import('./project.js');
  root.append(panel('唱跳导演', text('告诉导演要换哪个人物、声音和场景，导演会按阶段配置、检查并排队制作。'), await chatPanel(ctx, project.id, {compact: true, mode: project.mode})), h('div', {class: 'performance-columns'}, inputPanel, sourcePanel), dynamic, comparison.element, taskPanel, outputPanel);
  const markDirty = event => {if (event.target.closest('[data-performance-inputs]')) {dirty = true;paint();}};
  root.addEventListener('input', markDirty);root.addEventListener('change', markDirty);
  outputPanel.addEventListener('pause', paint, true);outputPanel.addEventListener('ended', paint, true);
  paint();ctx.poll(refresh, 4000);
  return root;
}

export async function render(ctx) {
  const projects = ctx.projects.filter(project => project.mode === 'performance_remix');
  const requested = ctx.params?.[0];
  if (requested) {
    const [project, jobs] = await Promise.all([api(`/projects/${encodeURIComponent(requested)}/status`, {signal: ctx.signal}), api(`/projects/${encodeURIComponent(requested)}/jobs`, {signal: ctx.signal})]);
    if (project.mode !== 'performance_remix') throw new Error('请选择唱跳项目。');
    return renderProject(ctx, project, jobs);
  }
  const [characters, voices, clips] = await Promise.all([api('/characters', {signal: ctx.signal}), api('/voices', {signal: ctx.signal}), api('/media-assets', {signal: ctx.signal})]);
  return h('div', {class: 'performance-workbench stack'}, h('style', {}, styles),
    h('header', {class: 'page-heading'}, h('div', {}, h('p', {class: 'eyebrow'}, 'PERFORMANCE REMIX'), h('h1', {}, '上传唱跳，换一个舞台'), h('p', {class: 'muted performance-subtitle'}, '保留动作、歌词、旋律与节奏，替换人物和歌声音色，同时换场景。从 10 秒真实样片开始。'))),
    panel('创建唱跳项目', await inputForm(ctx, null, {characters, voices, clips})),
    panel('已有唱跳项目', ...(projects.length ? projects.map(project => h('div', {class: 'row spread'}, link(project.title, `#/projects/${project.id}/performance`), link('继续制作', `#/projects/${project.id}/performance`))) : [text('尚未创建唱跳项目。配置保存后可继续准备视频和验证模型。')])),
  );
}
