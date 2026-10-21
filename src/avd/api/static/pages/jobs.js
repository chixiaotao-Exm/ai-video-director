import {api} from '../api.js';
import {h, button, link, field, input, select, form, section, pageHeader, badge, date, formatDuration, empty} from '../components.js';
import {t} from '../i18n/zh.js';
import {jobExecution, jobFailure, jobCopyButton, jobErrorDetails, jobListUpdater, jobsDiagnosticText, copyDiagnosticText} from '../jobExecution.js';

const LANES = ['gpu', 'api', 'cpu'];
const PAGE_SIZE = 30;

export async function render(ctx) {
  let filters = {project_id: '', lane: '', status: '', job_type: ''};
  let offset = 0;
  let serial = 0;
  let loading = false;
  let hasNext = false;
  const body = h('tbody');
  const rowUpdater = jobListUpdater(body, jobRow, {signal: ctx.signal});
  const statusLine = h('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
  const workerSummary = h('div', {class: 'toolbar'});
  const workerLabels = new Map();
  for (const lane of LANES) {
    const label = h('span', {class: 'row'}, h('strong', {}, lane.toUpperCase()), badge('stopped', t('jobs.worker.stopped')));
    workerLabels.set(lane, label);
    workerSummary.append(label);
  }
  workerSummary.append(link(t('jobs.manageWorkers'), '#/system'));
  const controls = {
    project_id: select([{value: '', label: t('jobs.allProjects')}, ...ctx.projects.map(project => ({value: project.id, label: project.title}))], ''),
    lane: select([{value: '', label: t('jobs.allLanes')}, ...LANES.map(value => ({value, label: value.toUpperCase()}))], ''),
    status: select([{value: '', label: t('jobs.allStatuses')}, ...['queued', 'running', 'failed', 'succeeded', 'cancelled'].map(value => ({value, label: t(`status.${value}`)}))], ''),
    job_type: input('text', '', {placeholder: 'image_gen', list: 'jobs-types'}),
  };
  const filterForm = form([
    h('div', {class: 'filter-grid'},
      field(t('jobs.project'), controls.project_id), field(t('jobs.lane'), controls.lane),
      field(t('jobs.status'), controls.status), field(t('jobs.type'), controls.job_type),
    ),
    h('datalist', {id: 'jobs-types'}, ['analyze_video', 'write_script', 'character_sheet', 'image_gen', 'image_to_video', 'tts', 'lipsync', 'subtitles', 'final'].map(value => h('option', {value}))),
  ], async () => {
    filters = Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, control.value.trim()]));
    offset = 0;
    await refresh();
  }, t('jobs.applyFilters'));
  const clear = button(t('jobs.clearFilters'), async () => {
    for (const control of Object.values(controls)) control.value = '';
    filters = {project_id: '', lane: '', status: '', job_type: ''};
    offset = 0;
    await refresh();
  });
  filterForm.querySelector('.form-actions').append(clear);
  const headers = ['project', 'type', 'lane', 'model', 'status', 'attempts', 'duration', 'created', 'actions'];
  const table = h('div', {class: 'table-scroll', tabindex: 0, 'aria-label': t('jobs.table')}, h('table', {},
    h('thead', {}, h('tr', {}, headers.map(name => h('th', {scope: 'col'}, t(`jobs.${name}`))))), body,
  ));
  const emptyState = empty(t('jobs.empty'), t('jobs.emptyHint'));
  const pageLabel = h('span', {class: 'muted', 'aria-live': 'polite'});
  const previous = button(t('jobs.previous'), async () => { offset = Math.max(0, offset - PAGE_SIZE); await refresh(); });
  const next = button(t('jobs.next'), async () => { if (hasNext) { offset += PAGE_SIZE; await refresh(); } });
  const root = h('div', {class: 'page'},
    pageHeader(t('jobs.title'), t('jobs.subtitle'), button(t('jobs.refresh'), () => refresh())),
    section(t('jobs.workers'), workerSummary),
    section(t('jobs.filterTitle'), filterForm),
    section(t('jobs.queueTitle'), h('div', {class: 'toolbar'}, button('复制当前筛选页全部', () => copyDiagnosticText(jobsDiagnosticText(rowUpdater.jobs, `任务队列 · 当前筛选第 ${Math.floor(offset / PAGE_SIZE) + 1} 页（最多 ${PAGE_SIZE} 项）`)), {small: true})), statusLine, table, emptyState, h('div', {class: 'toolbar'}, previous, pageLabel, next)),
  );

  function jobRow(job) {
    const actions = h('div', {class: 'stack'}, jobCopyButton(job));
    if (['queued', 'failed'].includes(job.status)) {
      const action = job.status === 'queued' ? 'cancel' : 'retry';
      actions.append(button(t(`jobs.${action}`), async () => {
        await api(`/jobs/${encodeURIComponent(job.id)}/${action}`, {method: 'POST'});
        await refresh();
        statusLine.textContent = t(`jobs.${action}Success`);
      }, {small: true, danger: action === 'cancel'}));
    }
    return h('tr', {dataset: {jobId: job.id}},
      h('td', {}, job.project_id ? link(job.project_title || job.project_id.slice(0, 8), `#/projects/${encodeURIComponent(job.project_id)}`) : t('jobs.noProject')),
      h('td', {style: 'max-width:32rem'}, h('div', {class: 'stack'}, h('span', {}, job.job_type), h('small', {class: 'muted', title: job.id}, job.id.slice(0, 8)), jobFailure(job), jobErrorDetails(job))),
      h('td', {}, job.lane.toUpperCase()), h('td', {style: 'overflow-wrap:anywhere'}, jobExecution(job)),
      h('td', {}, badge(job.status)), h('td', {}, `${job.attempts} / ${job.max_attempts}`),
      h('td', {}, formatDuration(job.duration_seconds)), h('td', {}, date(job.created_at)), h('td', {}, actions));
  }

  async function refresh() {
    const version = ++serial;
    loading = true;
    statusLine.textContent = t('jobs.loading');
    try {
      const [data, workersResponse] = await Promise.all([
        api('/jobs', {query: {...filters, limit: PAGE_SIZE + 1, offset}, signal: ctx.signal}),
        api('/workers', {signal: ctx.signal}),
      ]);
      if (version !== serial || ctx.signal.aborted) return;
      const rawWorkers = workersResponse.workers || workersResponse;
      const workers = Array.isArray(rawWorkers) ? Object.fromEntries(rawWorkers.map(item => [item.lane, item])) : rawWorkers;
      for (const lane of LANES) {
        const worker = workers[lane] || {state: 'stopped'};
        const state = worker.state || worker.status || 'stopped';
        workerLabels.get(lane).replaceChildren(h('strong', {}, lane.toUpperCase()), badge(state, t(`jobs.worker.${state}`)),
          ...(!worker.managed && worker.running_job_ids?.length ? [h('small', {class: 'muted'}, t('jobs.externalWorker'))] : []));
      }
      hasNext = data.length > PAGE_SIZE;
      const page = data.slice(0, PAGE_SIZE);
      rowUpdater.update(page);
      table.hidden = page.length === 0;
      emptyState.hidden = page.length !== 0;
      previous.disabled = offset === 0;
      next.disabled = !hasNext;
      pageLabel.textContent = t('jobs.page', {n: Math.floor(offset / PAGE_SIZE) + 1});
      statusLine.textContent = t('jobs.updated', {n: page.length});
    } catch (error) {
      if (error.name !== 'AbortError' && version === serial) statusLine.textContent = error.message;
    } finally {
      if (version === serial) loading = false;
    }
  }
  await refresh();
  ctx.poll(() => loading ? undefined : refresh(), 4000);
  return root;
}
