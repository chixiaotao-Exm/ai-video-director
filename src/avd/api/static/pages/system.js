import {api} from '../api.js';
import {h, button, link, field, select, input, section, pageHeader, badge, money, date, table} from '../components.js';
import {t} from '../i18n/zh.js';

const LANES = ['gpu', 'api', 'cpu'];

export async function render(ctx) {
  let loading = false;
  let serial = 0;
  const pending = new Set();
  const workerNodes = new Map();
  const health = h('div', {class: 'grid-3'});
  const connections = h('div', {class: 'grid-2'});
  const versions = h('div');
  const binaries = h('div');
  const queue = h('div');
  const running = h('div');
  const spend = h('div');
  const feedback = h('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
  const logs = h('pre', {class: 'log-viewer', tabindex: 0, 'aria-label': t('system.logViewer')});
  const pause = input('checkbox', false);
  const logLimit = select([50, 100, 200].map(value => ({value, label: t('system.logCount', {n: value})})), 100);
  const workers = h('div', {class: 'grid-3'});
  for (const lane of LANES) {
    const state = h('div');
    const pid = h('p', {class: 'muted'});
    const ownership = h('p', {class: 'muted'});
    const jobs = h('p', {class: 'muted'});
    const error = h('p', {class: 'error-text'});
    const start = button(t('system.startWorker'), () => workerAction(lane, 'start'), {small: true});
    const stop = button(t('system.stopWorker'), () => workerAction(lane, 'stop'), {small: true, danger: true});
    const card = h('div', {class: 'card form-stack'}, h('h3', {}, lane.toUpperCase()), state, pid, ownership, jobs, error, h('div', {class: 'toolbar'}, start, stop));
    workerNodes.set(lane, {state, pid, ownership, jobs, error, start, stop, current: {state: 'stopped'}});
    workers.append(card);
  }
  const startAll = button(t('system.startAll'), () => workerAction('all', 'start'));
  const stopAll = button(t('system.stopAll'), () => workerAction('all', 'stop'), {danger: true});
  const root = h('div', {class: 'page'},
    pageHeader(t('system.title'), t('system.subtitle'), button(t('system.refresh'), () => refresh())),
    health,
    section(t('system.workers'), h('div', {class: 'toolbar'}, startAll, stopAll), h('p', {class: 'muted'}, t('system.workerHint')), workers, feedback),
    section(t('system.connections'), connections, h('p', {class: 'muted'}, t('system.connectionHint')), link(t('system.openSettings'), '#/settings')),
    h('div', {class: 'grid-2'}, section(t('system.queue'), queue), section(t('system.spend'), spend)),
    section(t('system.running'), running, link(t('system.openJobs'), '#/jobs')),
    h('div', {class: 'grid-2'}, section(t('system.versions'), versions), section(t('system.tools'), binaries)),
    section(t('system.logs'), h('div', {class: 'toolbar'}, field(t('system.logLimit'), logLimit), field(t('system.pauseLogs'), pause)),
      h('p', {class: 'muted'}, t('system.logHint')), logs),
  );

  function updateWorker(lane, value) {
    const item = workerNodes.get(lane);
    const state = value?.state || value?.status || 'stopped';
    item.current = value || {state: 'stopped'};
    const busy = pending.has(lane);
    item.state.replaceChildren(badge(state, t(`system.worker.${state}`)));
    item.pid.textContent = value?.pid ? `${t('system.processId')}: ${value.pid}` : t('system.noProcess');
    item.ownership.textContent = t(value?.managed ? 'system.managed' : 'system.unmanaged');
    item.jobs.textContent = value?.active_job_id ? `${t('system.activeJob')}: ${value.active_job_id.slice(0, 8)}` :
      value?.running_job_ids?.length ? t('system.externalJobs', {n: value.running_job_ids.length}) : '';
    if (value?.quarantined_job_ids?.length) item.jobs.textContent += ` ${t('system.quarantinedJobs', {n: value.quarantined_job_ids.length})}`;
    item.error.textContent = value?.error || value?.last_error || '';
    item.start.disabled = busy || ['running', 'starting', 'stopping'].includes(state);
    item.stop.disabled = busy || !['running', 'starting', 'stopping'].includes(state);
  }

  async function workerAction(lane, action) {
    const targets = lane === 'all' ? LANES : [lane];
    for (const target of targets) {
      pending.add(target);
      const item = workerNodes.get(target);
      item.state.replaceChildren(badge(action === 'start' ? 'running' : 'queued', t(action === 'start' ? 'system.starting' : 'system.stopping')));
      item.start.disabled = true;
      item.stop.disabled = true;
    }
    startAll.disabled = true;
    stopAll.disabled = true;
    feedback.textContent = t(action === 'start' ? 'system.starting' : 'system.stopping');
    try {
      await api(`/workers/${lane}/${action}`, {method: 'POST'});
      feedback.textContent = t(action === 'start' ? 'system.started' : 'system.stopped');
    } catch (error) {
      feedback.textContent = error.message;
      throw error;
    } finally {
      for (const target of targets) pending.delete(target);
      startAll.disabled = pending.size > 0;
      stopAll.disabled = pending.size > 0;
      await refresh();
    }
  }

  function updateLogs(lines) {
    const selection = window.getSelection();
    if (selection?.toString() && (logs.contains(selection.anchorNode) || logs.contains(selection.focusNode))) return;
    const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 32;
    const position = logs.scrollTop;
    const text = lines.length ? lines.join('\n') : t('system.noLogs');
    if (logs.textContent !== text) logs.textContent = text;
    logs.scrollTop = atBottom ? logs.scrollHeight : position;
  }

  async function refresh() {
    const version = ++serial;
    loading = true;
    try {
      const [result, workerResponse, recentLogs] = await Promise.all([
        api('/system', {signal: ctx.signal}),
        api('/workers', {signal: ctx.signal}),
        pause.checked ? Promise.resolve(null) : api('/system/logs', {query: {limit: logLimit.value}, signal: ctx.signal}),
      ]);
      if (version !== serial || ctx.signal.aborted) return;
      const rawWorkers = workerResponse.workers || workerResponse;
      const managedWorkers = Array.isArray(rawWorkers) ? Object.fromEntries(rawWorkers.map(item => [item.lane, item])) : rawWorkers;
      const workerCount = LANES.filter(lane => (managedWorkers[lane]?.state || managedWorkers[lane]?.status) === 'running').length;
      health.replaceChildren(
        section(t('system.appHealth'), badge(result.health.status === 'ok' ? 'succeeded' : 'failed', result.health.status === 'ok' ? t('system.healthy') : t('system.unavailable'))),
        section(t('system.database'), badge(result.health.database === 'ok' ? 'succeeded' : 'failed', result.health.database === 'ok' ? t('system.healthy') : t('system.unavailable'))),
        section(t('system.workerCount'), h('strong', {}, `${workerCount} / ${LANES.length}`)),
      );
      for (const lane of LANES) if (!pending.has(lane)) updateWorker(lane, managedWorkers[lane]);
      const comfy = result.comfyui;
      connections.replaceChildren(
        h('div', {class: 'stack'}, h('h3', {}, `ComfyUI · ${comfy.profile}`),
          badge(comfy.status === 'ok' ? 'succeeded' : comfy.status === 'failed' ? 'failed' : 'unknown', t(`system.connection.${comfy.status}`)),
          h('span', {class: 'muted', style: 'overflow-wrap:anywhere'}, comfy.base_url),
          comfy.last_check && h('small', {class: 'muted'}, `${t('system.lastChecked')}: ${date(comfy.last_check.checked_at)}`),
        ),
        h('div', {class: 'stack'}, h('h3', {}, t('system.apiProviders')),
          ...Object.entries(result.providers).map(([name, configured]) => h('div', {class: 'row'}, h('span', {}, name), badge(configured ? 'succeeded' : 'unknown', t(configured ? 'system.keySet' : 'system.keyMissing')))),
        ),
      );
      queue.replaceChildren(table([t('system.lane'), t('system.queuedCount')], LANES.map(lane => [lane.toUpperCase(), result.metrics.queue_depth[lane] || 0])));
      const projectNames = new Map(ctx.projects.map(project => [project.id, project.title]));
      spend.replaceChildren(Object.keys(result.metrics.spent_by_project).length ? table([t('system.project'), t('system.amount')], Object.entries(result.metrics.spent_by_project).map(([id, value]) => [link(projectNames.get(id) || id.slice(0, 8), `#/projects/${encodeURIComponent(id)}`), money(value)])) : h('p', {class: 'muted'}, t('system.noSpend')));
      running.replaceChildren(result.metrics.running_jobs.length ? table([t('system.project'), t('system.job'), t('system.lane')], result.metrics.running_jobs.map(job => [job.project_id ? link(job.project_title || projectNames.get(job.project_id) || job.project_id.slice(0, 8), `#/projects/${encodeURIComponent(job.project_id)}`) : '—', job.job_type, job.lane.toUpperCase()])) : h('p', {class: 'muted'}, t('system.noRunning')));
      versions.replaceChildren(table([t('system.component'), t('system.version')], Object.entries(result.versions).map(([name, value]) => [name, value])));
      binaries.replaceChildren(badge(result.ffmpeg.ready ? 'succeeded' : 'failed', t(result.ffmpeg.ready ? 'system.toolsReady' : 'system.toolsMissing')),
        ...['ffmpeg', 'ffprobe'].map(name => h('div', {class: 'stack'}, h('strong', {}, name), h('span', {class: 'muted', style: 'overflow-wrap:anywhere'}, result.ffmpeg[name] || t('system.notFound')))),
      );
      if (recentLogs) updateLogs(recentLogs.lines);
    } catch (error) {
      if (error.name !== 'AbortError' && version === serial) feedback.textContent = error.message;
    } finally {
      if (version === serial) loading = false;
    }
  }
  logLimit.addEventListener('change', () => { if (!loading) void refresh(); });
  pause.addEventListener('change', () => { if (!pause.checked && !loading) void refresh(); });
  await refresh();
  ctx.poll(() => loading ? undefined : refresh(), 4000);
  return root;
}
