<script setup>
import {computed, onBeforeUnmount, onMounted, reactive, ref} from 'vue';
import {api, formatCount} from '../api.js';
import Icon from '../components/StudioIcon.vue';
import {useStudio} from '../context.js';

const ctx = useStudio();
const draftKey = 'avd.performance-plan';
const catalogue = ref(null);
const loading = ref(true);
const error = ref('');
const saved = ref(false);
const selectedId = ref('api-first');
const rightsAcknowledged = ref(false);
const plan = reactive({
  title: '我的第一支唱跳 MV', duration: 30, aspect: '9:16', language: '中文',
  style: '明亮的流行电子，副歌有记忆点', dance: '单人全身舞蹈，节拍清晰，镜头稳定',
  lyrics: '', voice: 'generated',
});
const selected = computed(() => catalogue.value?.presets.find(item => item.id === selectedId.value)
  || catalogue.value?.presets[0]);
const presetLabels = {'api-first': 'API · 推荐起步', 'laptop-hybrid': '12 GB · 混合创作', 'local-performance': '24 / 48 GB · 本地'};
const voiceLabels = {generated: '生成原创歌声', recording: '使用本人演唱录音', clone: '使用已获授权的克隆歌声'};
const needsRights = computed(() => plan.voice !== 'generated');
const shotCount = computed(() => Math.ceil(Number(plan.duration) / 5));
let controller;
let active = true;

function safeSource(url) {
  try { const parsed = new URL(url); return parsed.protocol === 'https:' ? parsed.href : undefined; }
  catch { return undefined; }
}

function restoreDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey) || 'null');
    if (!draft || draft.version !== 1 || !draft.plan || typeof draft.plan !== 'object') return;
    for (const [key, limit] of Object.entries({title: 100, language: 40, style: 500, dance: 500, lyrics: 6000})) {
      if (typeof draft.plan[key] === 'string') plan[key] = draft.plan[key].slice(0, limit);
    }
    if ([15, 30, 60].includes(draft.plan.duration)) plan.duration = draft.plan.duration;
    if (['9:16', '16:9'].includes(draft.plan.aspect)) plan.aspect = draft.plan.aspect;
    if (Object.hasOwn(voiceLabels, draft.plan.voice)) plan.voice = draft.plan.voice;
    if (Object.hasOwn(presetLabels, draft.preset)) selectedId.value = draft.preset;
    saved.value = true;
    // A saved creative plan must never become an asset consent confirmation.
    rightsAcknowledged.value = false;
  } catch { ctx.notify('上次草案无法读取，已保留默认方案。', 'error'); }
}

async function loadCatalogue() {
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = '';
  try {
    const data = await api('/static/performance-models.json', {signal: controller.signal});
    if (!Array.isArray(data.presets) || !data.presets.length || !Array.isArray(data.candidates)
      || !Array.isArray(data.sources)) throw new Error('模型选型资料格式不完整。');
    catalogue.value = data;
  } catch (cause) {
    if (cause.name !== 'AbortError' && active) {
      error.value = cause.message || '模型选型资料暂时无法读取。';
      ctx.notify(error.value, 'error');
    }
  } finally {
    if (active) { loading.value = false; ctx.ready(); }
  }
}

function saveDraft(announce = true) {
  try {
    localStorage.setItem(draftKey, JSON.stringify({version: 1, preset: selectedId.value, plan: {...plan}}));
    saved.value = true;
    if (announce) ctx.notify('制作草案已保存在当前浏览器。');
    return true;
  } catch {
    ctx.notify('浏览器未能保存草案，可先导出制作清单。', 'error');
    return false;
  }
}

function scrollToPlan() {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.getElementById('performance-plan')?.scrollIntoView({behavior: reducedMotion ? 'instant' : 'smooth'});
}

function createProject() {
  if (needsRights.value && !rightsAcknowledged.value) {
    ctx.notify('请确认录音与声音使用权；创建后仍需在资产库逐项确认授权。', 'error');
    return;
  }
  saveDraft(false);
  ctx.openProject({title: plan.title.trim() || '我的唱跳 MV'});
}

function downloadPlan() {
  if (!selected.value) return;
  const route = selected.value;
  const lines = [
    `# ${plan.title.trim() || '唱跳 MV 制作草案'}`, '',
    `模型资料核查：${catalogue.value.updated_at}；当前为制作计划，尚未执行生成。`, '',
    `- 路线：${route.name}`, `- 时长：${plan.duration} 秒`, `- 画幅：${plan.aspect}`,
    `- 歌词语言：${plan.language}`, `- 歌声方向：${voiceLabels[plan.voice]}`,
    `- 歌曲风格：${plan.style}`, `- 舞蹈方向：${plan.dance}`, '',
    '## 原创歌词 / 创作笔记', '', plan.lyrics || '待创作', '', '## 模型与镜头', '',
    ...route.stages.flatMap((stage, index) => [
      `${index + 1}. ${stage.stage}：${stage.model}`, `   - 用途：${stage.why}`,
      `   - 限制：${stage.limitations}`, `   - 官方来源：${stage.source_url}`,
    ]), '', `硬件：${route.hardware}`, `费用：${route.estimated_cost_note}`, '',
    '## 开始制作前', '',
    '- [ ] 在资产库确认人物与声音授权；本清单不改变 consent_confirmed。',
    '- [ ] 确认歌词、录音、编舞与动作参考的使用权。',
    '- [ ] 对接真正可运行的模型适配器 / Comfy 工作流。',
    '- [ ] 设置预算，先完成一个 5 秒动作片段与一个唱歌近景。',
    '- [ ] 核对歌词、节拍、人物一致性、手脚、口型与音画长度。',
    '- [ ] 记录重试、费用和耗时，保留成片 AI 标识。', '',
    '此草案不包含 API Key，也不会自动提交模型任务、购买服务或下载权重。',
  ];
  const url = URL.createObjectURL(new Blob([lines.join('\n')], {type: 'text/markdown;charset=utf-8'}));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${(plan.title || '唱跳制作草案').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/[. ]+$/, '').slice(0, 80) || '唱跳制作草案'}.md`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  ctx.notify('制作清单已导出。');
}

onMounted(() => { restoreDraft(); loadCatalogue(); });
onBeforeUnmount(() => { active = false; controller?.abort(); });
</script>

<template>
  <div class="performance-page">
    <div class="page-heading">
      <div><p class="eyebrow">PERFORMANCE LAB</p><h1>唱跳工作室</h1><p>给第一支 MV 一个清晰的开始。</p></div>
      <span class="studio-badge"><Icon name="music" :size="14"/> 制作计划 · 不自动生成</span>
    </div>

    <section class="hero-grid performance-hero" aria-labelledby="performance-title">
      <div class="hero-copy">
        <span class="studio-badge">声音、动作、人物，各有主场</span>
        <h2 id="performance-title" class="hero-title">先让音乐成立，<br><span>再让舞台动起来。</span></h2>
        <p class="hero-description">先完成一段副歌。把全身舞蹈与唱歌近景接起来，让人物、节拍和镜头形成自己的风格。</p>
        <div class="hero-actions"><button type="button" class="studio-button primary" @click="scrollToPlan">写下制作计划 <Icon name="arrow"/></button></div>
      </div>
      <div class="hero-visual music-visual" aria-hidden="true">
        <div class="stage-orbit"></div><div class="stage-orbit"></div>
        <div class="performance-record"><Icon name="music" :size="58"/><span>YOUR NEXT<br>PERFORMANCE</span><div class="waveform"><span v-for="i in 18" :key="i"></span></div></div>
        <div class="stage-label">01 / 声音先行<small>歌曲与伴奏</small></div>
        <div class="stage-chip">02 / 动作入拍<small>舞蹈与唱歌近景</small></div>
      </div>
    </section>

    <div v-if="loading" class="studio-loading" role="status">正在整理模型路线…</div>
    <div v-else-if="error" class="studio-card empty-state" role="alert"><h2>模型资料暂时不可用</h2><p>{{ error }}</p><button class="studio-button" type="button" @click="loadCatalogue"><Icon name="refresh"/> 重新读取</button></div>
    <template v-else-if="catalogue && selected">
      <div class="section-title"><h2>选择适合现在的路线</h2><span>资料核查 {{ catalogue.updated_at }} · {{ formatCount(catalogue.sources.length) }} 个官方来源</span></div>
      <div class="preset-grid" role="group" aria-label="制作路线">
        <button v-for="preset in catalogue.presets" :key="preset.id" type="button" class="model-card preset-card" :class="{featured: selected.id === preset.id}" :aria-pressed="selected.id === preset.id" @click="selectedId = preset.id; saved = false">
          <div class="model-card-header"><span class="studio-badge" :class="{ready: preset.id === 'api-first'}">{{ presetLabels[preset.id] || preset.mode }}</span><Icon :name="selected.id === preset.id ? 'check' : 'layers'"/></div>
          <h3>{{ preset.name }}</h3><p>{{ preset.tagline }}</p><small>{{ preset.fit }}</small>
        </button>
      </div>

      <div class="section-title"><h2>{{ selected.name }}的制作路径</h2><span>选型建议，不会自动购买或调用服务</span></div>
      <div class="pipeline" aria-label="建议制作步骤">
        <article v-for="(stage, index) in selected.stages" :key="`${selected.id}-${stage.stage}`" class="pipeline-step">
          <span class="pipeline-number">0{{ index + 1 }}</span><span class="studio-badge warning">待接入 / 验证</span>
          <h3>{{ stage.stage }}</h3><strong class="stage-model">{{ stage.model }}</strong><p>{{ stage.why }}</p>
          <details class="stage-details"><summary>使用边界</summary><p>{{ stage.limitations }}</p></details>
          <a :href="safeSource(stage.source_url)" target="_blank" rel="noopener noreferrer" class="source-link">官方资料 <Icon name="external" :size="13"/></a>
        </article>
        <article class="pipeline-step"><span class="pipeline-number">04</span><span class="studio-badge ready">已有基础能力</span><h3>合成与成片</h3><strong class="stage-model">FFmpeg</strong><p>已有字幕、剪辑和合成执行通道。新的歌曲音轨与舞蹈输入仍需完成接线。</p><span class="stage-footnote">先看样片，再扩展整首 MV</span></article>
      </div>

      <div class="performance-grid planning-grid">
        <section id="performance-plan" class="studio-card plan-card" aria-labelledby="plan-title">
          <div class="section-heading"><div><p class="eyebrow">YOUR BRIEF</p><h2 id="plan-title">把灵感写成计划</h2></div><span class="studio-badge">{{ saved ? '已存于此浏览器' : '尚未保存' }}</span></div>
          <form class="studio-form performance-form" @submit.prevent="saveDraft()" @input="saved = false" @change="saved = false">
            <label class="form-field field-wide" for="mv-title"><span>作品名称</span><input id="mv-title" v-model="plan.title" maxlength="100" required autocomplete="off" placeholder="给这支 MV 起个名字"></label>
            <label class="form-field" for="mv-duration"><span>目标时长</span><select id="mv-duration" v-model.number="plan.duration"><option :value="15">15 秒 · 先看感觉</option><option :value="30">30 秒 · 一段副歌</option><option :value="60">60 秒 · 完整段落</option></select></label>
            <label class="form-field" for="mv-aspect"><span>画幅</span><select id="mv-aspect" v-model="plan.aspect"><option value="9:16">9:16 · 竖屏舞台</option><option value="16:9">16:9 · 横屏 MV</option></select></label>
            <label class="form-field" for="mv-language"><span>歌词语言</span><input id="mv-language" v-model="plan.language" maxlength="40" placeholder="中文 / English / 双语"></label>
            <label class="form-field" for="mv-voice"><span>歌声方向</span><select id="mv-voice" v-model="plan.voice" @change="rightsAcknowledged = false"><option v-for="(label, value) in voiceLabels" :key="value" :value="value">{{ label }}</option></select></label>
            <label class="form-field field-wide" for="mv-style"><span>歌曲风格</span><input id="mv-style" v-model="plan.style" maxlength="500" placeholder="例如：流行电子，轻快律动，温暖的副歌"></label>
            <label class="form-field field-wide" for="mv-dance"><span>舞蹈与镜头</span><textarea id="mv-dance" v-model="plan.dance" rows="3" maxlength="500" placeholder="动作、景别、舞台氛围…"></textarea></label>
            <label class="form-field field-wide" for="mv-lyrics"><span>原创歌词 / 创作笔记 <small>可先留空</small></span><textarea id="mv-lyrics" v-model="plan.lyrics" rows="4" maxlength="6000" placeholder="从一句你想唱出来的话开始。"></textarea></label>
            <label v-if="needsRights" class="rights-check field-wide"><input v-model="rightsAcknowledged" type="checkbox"><span>我已确认这份录音与声音的使用权。这里只确认制作计划，之后仍需在人物 / 声音资产库逐项确认授权。</span></label>
            <div class="button-row field-wide"><button type="submit" class="studio-button"><Icon name="check"/> 保存草案</button><button type="button" class="studio-button secondary" @click="downloadPlan"><Icon name="download"/> 导出清单</button></div>
          </form>
          <p class="studio-note">草案只保存在当前浏览器，不含 API Key。保存不会提交生成任务，清除浏览器数据会移除此草案。</p>
        </section>

        <aside class="studio-card plan-summary" aria-labelledby="plan-summary-title">
          <p class="eyebrow">PRODUCTION NOTES</p><h2 id="plan-summary-title">先做小样，留出调整空间</h2>
          <div class="plan-metrics"><div><strong>{{ plan.duration }}<small> 秒</small></strong><span>目标成片</span></div><div><strong>{{ shotCount }}<small> 段左右</small></strong><span>按 5 秒片段规划</span></div><div><strong>{{ plan.aspect }}</strong><span>输出画幅</span></div></div>
          <p class="summary-label">这套方案的硬件</p><p>{{ selected.hardware }}</p>
          <p class="summary-label">费用怎么估</p><p>{{ selected.estimated_cost_note }}</p>
          <p class="summary-label">下一步</p><ol class="plan-next"><li v-for="step in selected.next_steps" :key="step">{{ step }}</li></ol>
          <div class="capability-notice"><Icon name="layers"/><p><strong>方案已清楚，生成仍需接入。</strong><br>当前程序具备人物与声音资产、任务队列和基础合成；上面推荐的歌曲与舞蹈模型还需要适配器或真实 ComfyUI 工作流。</p></div>
          <button type="button" class="studio-button primary" @click="createProject"><Icon name="plus"/> 创建制作项目</button>
          <p class="studio-note">打开现有项目创建流程，不会直接生成视频，也不会自动确认资产授权。</p>
        </aside>
      </div>

      <details class="studio-card model-library"><summary><span><Icon name="layers"/> 继续比较模型与使用边界</span><span class="studio-badge">{{ catalogue.candidates.length }} 个候选</span></summary><div class="candidate-grid"><article v-for="candidate in catalogue.candidates" :key="candidate.id" class="candidate-item"><div class="model-card-header"><h3>{{ candidate.name }}</h3><span class="studio-badge">{{ candidate.category }}</span></div><strong>{{ candidate.verdict }}</strong><p>{{ candidate.why }}</p><ul class="capability-list"><li><Icon name="system"/><span>{{ candidate.hardware }}</span></li><li><Icon name="shield"/><span>{{ candidate.license }}</span></li><li><Icon name="layers"/><span>{{ candidate.integration }}</span></li></ul><a :href="safeSource(candidate.source_url)" target="_blank" rel="noopener noreferrer" class="source-link">核查官方来源 <Icon name="external" :size="13"/></a></article></div></details>
      <details class="research-sources"><summary>来源与核查日期 · {{ catalogue.updated_at }}</summary><ul><li v-for="source in catalogue.sources" :key="source.url"><a :href="safeSource(source.url)" target="_blank" rel="noopener noreferrer">{{ source.label }}</a><small>{{ source.accessed }}</small></li></ul></details>
    </template>
  </div>
</template>

<style scoped>
.performance-page { min-width: 0; }
.performance-hero { min-height: 310px; }
.performance-record { display: grid; place-items: center; gap: 18px; width: 218px; min-height: 218px; padding: 28px; border: 1px solid var(--line); border-radius: 50%; background: var(--surface); box-shadow: var(--shadow-2); color: var(--accent-b); z-index: 1; }
.performance-record > span { font-size: 10px; letter-spacing: .16em; line-height: 1.7; text-align: center; color: var(--muted); }
.preset-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.preset-card { text-align: left; font: inherit; cursor: pointer; transition: border-color .18s, transform .18s; }
.preset-card:hover { transform: translateY(-2px); }
.preset-card h3, .preset-card p { margin: 0; }
.preset-card small { color: var(--muted); font-size: 11px; }
.preset-card > .model-card-header > svg { flex-shrink: 0; color: var(--accent-b); }
.stage-model { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.source-link { display: inline-flex; align-items: center; gap: 5px; color: var(--accent-a); font-size: 11px; margin-top: auto; text-underline-offset: 3px; }
.stage-details { font-size: 11px; color: var(--muted); }
.stage-details summary { cursor: pointer; }
.stage-details p { margin-top: 9px; }
.stage-footnote { font-size: 10px; color: var(--muted); margin-top: auto; }
.planning-grid { grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); align-items: start; }
.plan-card { scroll-margin-top: 100px; }
.section-heading { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 12px; justify-content: space-between; margin-bottom: 23px; }
.section-heading h2, .plan-summary h2 { font-size: 18px; margin: 0; }
.performance-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 15px; }
.form-field { display: flex; flex-direction: column; gap: 8px; min-width: 0; font-size: 12px; }
.form-field > span { font-weight: 600; }
.form-field small { margin-left: 6px; color: var(--muted); font-size: 10px; font-weight: 400; }
.performance-form input:not([type="checkbox"]), .performance-form select, .performance-form textarea { width: 100%; min-width: 0; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface-2); color: var(--text); font: inherit; padding: 11px 12px; line-height: 1.6; }
.performance-form textarea { resize: vertical; min-height: 90px; }
.performance-form input::placeholder, .performance-form textarea::placeholder { color: var(--subtle); }
.field-wide { grid-column: 1 / -1; }
.button-row { display: flex; flex-wrap: wrap; gap: 10px; }
.studio-note { color: var(--muted); font-size: 11px; line-height: 1.8; margin: 16px 0 0; }
.rights-check { display: flex; align-items: flex-start; gap: 10px; font-size: 11px; color: var(--muted); line-height: 1.8; }
.rights-check input { margin-top: 4px; accent-color: var(--accent-a); flex-shrink: 0; }
.plan-summary { display: flex; flex-direction: column; align-items: flex-start; }
.plan-summary > p:not(.eyebrow):not(.summary-label):not(.studio-note) { font-size: 12px; color: var(--muted); line-height: 1.85; margin: 0; }
.plan-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); width: 100%; gap: 10px; padding: 23px 0; border-bottom: 1px solid var(--line); margin-bottom: 2px; }
.plan-metrics > div { display: flex; flex-direction: column; gap: 6px; }
.plan-metrics strong { font-size: 24px; font-weight: 600; }
.plan-metrics small { font-size: 10px; font-weight: 400; color: var(--muted); }
.plan-metrics span { font-size: 10px; color: var(--muted); }
.summary-label { margin: 22px 0 7px; font-size: 12px; font-weight: 600; }
.plan-next { padding-left: 19px; margin: 0; font-size: 12px; color: var(--muted); line-height: 1.85; }
.plan-next li + li { margin-top: 8px; }
.capability-notice { display: flex; gap: 10px; padding: 17px; margin: 23px 0; background: var(--accent-soft); border-radius: var(--radius); width: 100%; }
.capability-notice > svg { flex-shrink: 0; color: var(--accent-b); margin-top: 2px; }
.capability-notice p { font-size: 11px; line-height: 1.85; color: var(--muted); margin: 0; }
.capability-notice strong { color: var(--text); font-weight: 600; }
.model-library { margin-top: 24px; }
.model-library > summary { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; cursor: pointer; font-size: 14px; font-weight: 600; }
.model-library > summary > span:first-child { display: flex; align-items: center; gap: 10px; }
.candidate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; margin-top: 24px; }
.candidate-item { border-top: 1px solid var(--line); padding-top: 20px; min-width: 0; }
.candidate-item .model-card-header { flex-wrap: wrap; margin-bottom: 10px; }
.candidate-item h3 { font-size: 14px; margin: 0; }
.candidate-item > strong { font-size: 11px; color: var(--accent-b); font-weight: 600; }
.candidate-item > p { font-size: 12px; color: var(--muted); line-height: 1.8; }
.candidate-item .source-link { margin-top: 15px; }
.research-sources { margin-top: 23px; font-size: 11px; color: var(--muted); }
.research-sources summary { cursor: pointer; }
.research-sources ul { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 24px; padding-left: 20px; }
.research-sources li { overflow-wrap: anywhere; }
.research-sources a { color: var(--muted); text-underline-offset: 3px; }
.research-sources small { display: block; opacity: .8; margin-top: 2px; }
.empty-state h2 { font-size: 18px; }
.empty-state p { font-size: 13px; color: var(--muted); }
@media (max-width: 1100px) { .planning-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 800px) { .preset-grid { grid-template-columns: minmax(0, 1fr); } .preset-card { gap: 10px; } }
@media (max-width: 600px) { .performance-form, .candidate-grid, .research-sources ul { grid-template-columns: minmax(0, 1fr); } .plan-metrics strong { font-size: 21px; } .performance-record { width: 184px; min-height: 184px; } .music-visual { min-height: 280px; } .performance-page .pipeline { grid-template-columns: minmax(0, 1fr); } .stage-details { width: 100%; } }
@media (prefers-reduced-motion: reduce) { .preset-card { transition: none; } .preset-card:hover { transform: none; } }
</style>
