import {api} from '../api.js';
import {h, button, link, field, input, select, form, section, pageHeader, badge, date, formatDuration, empty} from '../components.js';
import {t} from '../i18n/zh.js';

const LANES = ['gpu', 'api', 'cpu'];
const PAGE_SIZE = 30;

export async function render(ctx) {
  let filters = {project_id: '', lane: '', status: '', job_type: ''};
  let offset = 0;
  let serial = 0;
  let loading = false;
  let hasNext = false;
  const rows = new Map();
  const body = h('tbody');
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
    section(t('jobs.queueTitle'), statusLine, table, emptyState, h('div', {class: 'toolbar'}, previous, pageLabel, next)),
  );

  function jobRow(job) {
    let item = rows.get(job.id);
    if (!item) {
      const project = h('td');
      const typeText = h('span');
      const errorText = h('pre', {style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-width:32rem'});
      const error = h('details', {class: 'details'}, h('summary', {}, t('jobs.viewError')), errorText);
      const type = h('td', {}, h('div', {class: 'stack'}, typeText, h('small', {class: 'muted', title: job.id}, job.id.slice(0, 8)), error));
      const lane = h('td');
      const model = h('td');
      const status = h('td');
      const attempts = h('td');
      const duration = h('td');
      const created = h('td');
      const actions = h('td');
      const row = h('tr', {dataset: {jobId: job.id}}, project, type, lane, model, status, attempts, duration, created, actions);
      item = {row, project, typeText, error, errorText, lane, model, status, attempts, duration, created, actions, value: {}};
      rows.set(job.id, item);
    }
    const previousValue = item.value;
    if (previousValue.project_id !== job.project_id || previousValue.project_title !== job.project_title) {
      item.project.replaceChildren(job.project_id ? link(job.project_title || job.project_id.slice(0, 8), `#/projects/${encodeURIComponent(job.project_id)}`) : t('jobs.noProject'));
    }
    item.typeText.textContent = job.job_type;
    item.lane.textContent = job.lane.toUpperCase();
    item.model.textContent = job.model_key || '—';
    item.model.style.overflowWrap = 'anywhere';
    item.error.hidden = !job.error;
    if (previousValue.error !== job.error) item.errorText.textContent = job.error || '';
    item.attempts.textContent = `${job.attempts} / ${job.max_attempts}`;
    item.duration.textContent = formatDuration(job.duration_seconds);
    item.created.textContent = date(job.created_at);
    if (previousValue.status !== job.status) {
      item.status.replaceChildren(badge(job.status));
      item.actions.replaceChildren();
      if (['queued', 'failed'].includes(job.status)) {
        const action = job.status === 'queued' ? 'cancel' : 'retry';
        item.actions.append(button(t(`jobs.${action}`), async () => {
          await api(`/jobs/${encodeURIComponent(job.id)}/${action}`, {method: 'POST'});
          await refresh();
          statusLine.textContent = t(`jobs.${action}Success`);
        }, {small: true, danger: action === 'cancel'}));
      } else item.actions.textContent = '—';
    }
    item.value = job;
    return item.row;
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
      const active = document.activeElement;
      const visible = new Set(page.map(job => job.id));
      for (const [id, item] of rows) if (!visible.has(id)) { item.row.remove(); rows.delete(id); }
      let anchor = body.firstElementChild;
      for (const job of page) {
        const row = jobRow(job);
        if (row === anchor) anchor = anchor.nextElementSibling;
        else body.insertBefore(row, anchor);
      }
      if (active?.isConnected && document.activeElement !== active) active.focus?.({preventScroll: true});
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
