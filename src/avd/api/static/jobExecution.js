import {h, button, dialog, notice, textarea} from './components.js';

const ROLES = {
  director: '导演对话 / 汇报', director_report: '导演汇报', subagent: '子 Agent', subagent_turn: '子 Agent',
  analysis: '参考片分析', video_analysis: '画面分析', whisper: '语音识别', transcription: '语音识别',
  script: '脚本', qa: '质检', web_search: '网络搜索', image: '图像生成', image_gen: '图像生成',
  singing_voice: '歌声转换', singing_model_probe: '歌声模型探测', vocal_separation: '人声 / 伴奏分离',
  motion_video: '动作与人物视频', performance_lipsync: '口型修正',
  performance_prepare: '参考片准备', performance_mix: '歌声混音', performance_sync: '时间轴同步', performance_final: '成片合成',
};
const SOURCES = {actual: '执行请求', resumed: '继续已提交任务', snapshot: '任务快照', queue_marker: '排队模型标记', local: '本地处理', unknown: '模型未记录'};
const safeText = value => typeof value === 'string' ? value.trim() : '';

export function executionLines(job) {
  const execution = job.execution;
  if (!execution || typeof execution !== 'object') {
    return [{role: '', provider: '', model: safeText(job.model_key), source: job.model_key ? 'queue_marker' : 'unknown', label: job.model_key ? `排队模型标记：${job.model_key}` : '此步骤未记录执行模型', note: '历史任务缺少执行请求记录；排队标记不代表本次实际执行，不能从当前设置推断。'}];
  }
  const steps = Array.isArray(execution.steps) && execution.steps.length ? execution.steps : [execution];
  return steps.map(step => {
    const source = Object.hasOwn(SOURCES, step.source || execution.source) ? step.source || execution.source : 'unknown';
    const role = safeText(step.role || execution.role);
    const provider = safeText(step.provider);
    const model = safeText(step.model);
    const identity = [provider, model].filter(Boolean).join(' · ');
    const roleLabel = ROLES[role] || role || '当前步骤';
    const phase = {probe: '模型探测', inference: '模型推理'}[step.phase] || '';
    const stage = safeText(step.stage) || phase;
    const native = provider === 'local' && source === 'actual';
    const sourceLabel = native ? '本地进程已启动' : SOURCES[source];
    const label = `${stage ? `${stage} · ` : ''}${roleLabel} · ${sourceLabel}${identity ? `：${identity}` : ''}`;
    const note = native ? '已成功启动本地模型进程；模型探测与歌声推理分开记录，启动不代表任务或效果验收通过。'
      : source === 'resumed' ? '从持久记录继续等待原有工作流任务，本次没有重复提交模型请求。'
        : source === 'actual' && provider === 'comfyui' ? '已提交视频工作流请求；模型版本来自冻结工作流声明，尚未核验服务实际加载的权重。'
          : source === 'actual' ? '这是程序发给服务的请求路由；网关内部模型以服务记录为准。'
      : source === 'snapshot' ? '任务提交时保存的配置，尚不代表已成功执行。'
        : source === 'queue_marker' ? '排队时的模型标记，不代表本次实际执行请求；切换当前设置不会改写历史记录。'
          : source === 'unknown' ? '任务没有可核实的模型记录。' : '';
    return {role, provider, model, source, label, note, attempt: step.attempt || execution.attempt};
  });
}

export function jobExecution(job) {
  return h('div', {class: 'stack compact job-execution', style: 'flex-basis:100%;min-width:0;overflow-wrap:anywhere'},
    ...executionLines(job).map(line => h('small', {class: 'muted', title: line.note}, line.label,
      line.attempt && line.source === 'actual' ? ` · 第 ${line.attempt} 次` : '',
      line.source === 'queue_marker' && h('span', {}, '（实际调用未记录）'),
    )),
  );
}

export function failureSummary(job) {
  const failure = job.failure || {};
  const finalLine = safeText(job.error).split('\n').map(line => line.trim()).filter(Boolean).at(-1) || '';
  const fallback = finalLine.length > 400 ? `${finalLine.slice(0, 399)}…` : finalLine;
  const message = safeText(failure.message) || safeText(job.error_summary) || fallback || '';
  if (!message) return null;
  let recovery = '';
  if (failure.retry_at) {
    const raw = String(failure.retry_at);
    const timestamp = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw}Z`);
    if (!Number.isNaN(timestamp.getTime())) recovery = `预计可重试：${timestamp.toLocaleString('zh-CN', {timeZoneName: 'short'})}`;
  }
  return {message, recovery, retryable: failure.retryable, code: failure.code || '', httpStatus: failure.http_status};
}

export function jobFailure(job) {
  const summary = failureSummary(job);
  if (!summary) return null;
  return h('div', {class: 'stack compact', style: 'flex-basis:100%;min-width:0;overflow-wrap:anywhere'},
    h('small', {class: 'error-text'}, summary.message),
    summary.recovery && h('small', {class: 'muted'}, summary.recovery),
    summary.retryable === false && h('small', {class: 'muted'}, '请先处理错误原因，再重新提交任务。'),
  );
}

// Deliberately copy only the public diagnostic fields, never a whole job/settings object.
export function jobDiagnosticText(job) {
  const summary = failureSummary(job);
  const lines = [
    `任务 ID：${job.id || '未记录'}`, `项目 ID：${job.project_id || '无'}`,
    ...(job.project_title ? [`项目：${job.project_title}`] : []),
    `任务类型：${job.job_type || '未记录'}`, `状态：${job.status || 'unknown'}`,
    `队列：${job.lane || '未记录'}`, `尝试：${job.attempts ?? 0}/${job.max_attempts ?? '未记录'}`,
    ...['created_at', 'started_at', 'finished_at'].filter(key => job[key]).map(key => `${key}：${job[key]}`),
    ...(job.duration_seconds != null ? [`耗时（秒）：${job.duration_seconds}`] : []),
    '执行步骤：',
    ...executionLines(job).flatMap((step, index) => [
      `${index + 1}. ${step.label} [source=${step.source}]${step.attempt ? ` · 第 ${step.attempt} 次` : ''}`,
      ...(step.note ? [`   ${step.note}`] : []),
    ]),
  ];
  if (summary) lines.push(`错误摘要：${summary.message}`,
    ...(safeText(job.error_summary) && safeText(job.error_summary) !== summary.message ? [`错误摘要（任务记录）：${job.error_summary}`] : []),
    ...(summary.code ? [`错误代码：${summary.code}`] : []),
    ...(summary.httpStatus != null ? [`HTTP 状态：${summary.httpStatus}`] : []),
    ...(summary.retryable != null ? [`可重试：${summary.retryable ? '是' : '否'}`] : []),
    ...(job.failure?.retry_at ? [`预计可重试时间：${job.failure.retry_at}`] : []));
  if (job.error) lines.push('错误详情（API 已脱敏）：', String(job.error));
  return lines.join('\n');
}

export function jobsDiagnosticText(jobs, scope = '当前列表') {
  return `${scope} · ${jobs.length} 项任务\n\n${jobs.map(jobDiagnosticText).join('\n\n--------------------\n\n')}`;
}

function manualCopy(value) {
  const control = textarea(value, {readOnly: true, rows: 18, 'aria-label': '待复制的任务诊断', style: 'width:100%;white-space:pre;min-height:16rem'});
  dialog('手动复制任务诊断', h('div', {class: 'stack'}, h('p', {}, '浏览器未允许访问剪贴板。内容已选中，请按 Ctrl+C（Mac 使用 ⌘C）复制。'), control,
    button('全选内容', () => {control.focus();control.select();}, {small: true})));
  control.focus();control.select();
}

export async function copyDiagnosticText(value, {clipboard = globalThis.navigator?.clipboard, fallback = manualCopy, notify = notice} = {}) {
  try {
    if (!clipboard?.writeText) throw new Error('clipboard unavailable');
    await clipboard.writeText(value);
    notify('已复制任务诊断。');
    return true;
  } catch {
    fallback(value);
    notify('无法自动复制，已打开手动复制窗口。');
    return false;
  }
}

export function jobCopyButton(job) {
  return button('复制诊断', () => copyDiagnosticText(jobDiagnosticText(job)), {small: true, 'aria-label': `复制任务 ${job.id} 的诊断`});
}

export function jobErrorDetails(job) {
  if (!job.error) return null;
  return h('details', {class: 'details job-error-details', style: 'flex-basis:100%;min-width:0'},
    h('summary', {}, '错误详情'),
    button('复制错误详情', () => copyDiagnosticText(`任务 ID：${job.id}\n错误详情（API 已脱敏）：\n${job.error}`), {small: true}),
    h('pre', {tabindex: 0, style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%;user-select:text'}, job.error));
}

export function hasJobSelection(root, selection = globalThis.document?.getSelection?.()) {
  if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
  for (let index = 0; index < selection.rangeCount; index++) {
    if (selection.getRangeAt(index).intersectsNode(root)) return true;
  }
  return false;
}

// Keep unchanged rows intact. A selection defers changes until it is released; details
// stay open even when a changed task really does need a new row.
export function jobListUpdater(root, render, {signal} = {}) {
  const rows = new Map();
  let latest = [];
  let rendered = [];
  function paint() {
    if (signal?.aborted || hasJobSelection(root)) return;
    const visible = new Set(latest.map(job => job.id));
    for (const [id, item] of rows) if (!visible.has(id)) {item.element.remove();rows.delete(id);}
    let anchor = root.firstElementChild;
    for (const job of latest) {
      const signature = jobDiagnosticText(job);
      let item = rows.get(job.id);
      if (!item || item.signature !== signature) {
        const opened = item?.element.querySelector('details')?.open;
        const element = render(job);
        element.dataset.jobId = job.id;
        if (opened && element.querySelector('details')) element.querySelector('details').open = true;
        if (item) {
          if (anchor === item.element) anchor = element;
          item.element.replaceWith(element);
        }
        item = {element, signature};rows.set(job.id, item);
      }
      if (item.element === anchor) anchor = anchor.nextElementSibling;
      else root.insertBefore(item.element, anchor);
    }
    rendered = latest;
  }
  const onSelection = () => {if (root.isConnected) paint();};
  document.addEventListener('selectionchange', onSelection, signal ? {signal} : undefined);
  return {
    update(jobs) {latest = jobs;paint();},
    get jobs() {return rendered;},
    dispose() {document.removeEventListener('selectionchange', onSelection);},
  };
}

export function jobDiagnosticList({render, signal, scope = '当前列表', empty = '尚未提交任务。', className = 'stack', limit} = {}) {
  const body = h('div', {class: className});
  const updater = jobListUpdater(body, render, {signal});
  const copy = button('复制本列表全部', () => copyDiagnosticText(jobsDiagnosticText(updater.jobs, scope)), {small: true});
  const placeholder = h('p', {class: 'muted'}, empty);
  const element = h('div', {class: 'job-diagnostic-list stack'}, h('div', {class: 'toolbar'}, copy), body, placeholder);
  element.updateJobs = jobs => {
    const sorted = [...jobs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    const visible = limit ? sorted.slice(0, limit) : sorted;
    updater.update(visible);copy.disabled = visible.length === 0;placeholder.hidden = visible.length > 0;
  };
  return element;
}
