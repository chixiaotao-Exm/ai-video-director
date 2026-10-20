import {api} from '../api.js';
import {h, button, link, field, input, select, form, section, pageHeader, money, date, table, empty} from '../components.js';
import {t} from '../i18n/zh.js';

const PAGE_SIZE = 30;

export async function render(ctx) {
  let filters = {project_id: '', role: '', provider: '', model: ''};
  let offset = 0;
  let serial = 0;
  let loading = false;
  let count = 0;
  const budgetCards = new Map();
  const controls = {
    project_id: select([{value: '', label: t('costs.allProjects')}, ...ctx.projects.map(project => ({value: project.id, label: project.title}))], ''),
    role: input('text', '', {placeholder: 'director'}),
    provider: input('text', '', {placeholder: 'openai'}),
    model: input('text', '', {placeholder: t('costs.modelPlaceholder')}),
  };
  const filtersForm = form([
    h('div', {class: 'grid-2'}, ...Object.entries(controls).map(([name, control]) => field(t(`costs.filter.${name}`), control))),
  ], async () => {
    filters = Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, control.value.trim()]));
    offset = 0;
    await refresh();
  }, t('costs.applyFilters'));
  filtersForm.querySelector('.form-actions').append(button(t('costs.clearFilters'), async () => {
    for (const control of Object.values(controls)) control.value = '';
    filters = {project_id: '', role: '', provider: '', model: ''};
    offset = 0;
    await refresh();
  }));
  const totals = h('div', {class: 'grid-3'});
  const groups = h('div', {class: 'grid-2'});
  const ledger = h('div');
  const budgets = h('div', {class: 'grid-2'});
  const budgetEmpty = h('p', {class: 'muted'}, t('costs.noProjects'));
  const status = h('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
  const pageLabel = h('span', {class: 'muted'});
  const previous = button(t('costs.previous'), async () => { offset = Math.max(0, offset - PAGE_SIZE); await refresh(); });
  const next = button(t('costs.next'), async () => { if (offset + PAGE_SIZE < count) { offset += PAGE_SIZE; await refresh(); } });
  const root = h('div', {class: 'page'},
    pageHeader(t('costs.title'), t('costs.subtitle'), button(t('costs.refresh'), () => refresh())),
    section(t('costs.filterTitle'), filtersForm), totals,
    groups,
    section(t('costs.ledger'), status, ledger, h('div', {class: 'toolbar'}, previous, pageLabel, next)),
    section(t('costs.budgets'), h('p', {class: 'muted'}, t('costs.budgetHint')), budgets, budgetEmpty),
  );

  function budgetCard(project) {
    let item = budgetCards.get(project.id);
    if (!item) {
      const mode = select([{value: 'limited', label: t('costs.limited')}, {value: 'unlimited', label: t('costs.unlimited')}], project.budget_limit == null ? 'unlimited' : 'limited', {name: 'mode'});
      const amount = input('number', project.budget_limit ?? '', {name: 'amount', min: 0, step: 'any', required: true});
      amount.disabled = mode.value === 'unlimited';
      const feedback = h('p', {class: 'muted', role: 'status'});
      const usage = h('div', {class: 'stack'});
      const title = link(project.title, `#/projects/${encodeURIComponent(project.id)}`);
      item = {dirty: false, mode, amount, feedback, usage, title, card: null, project};
      const editing = () => { item.dirty = true; amount.disabled = mode.value === 'unlimited'; };
      mode.addEventListener('change', editing);
      amount.addEventListener('input', editing);
      const editor = form([
        field(t('costs.budgetMode'), mode), field(t('costs.budgetAmount'), amount, t('costs.zeroBudgetHint')),
      ], async data => {
        let budget = null;
        if (data.get('mode') !== 'unlimited') {
          const value = String(data.get('amount') || '').trim();
          budget = Number(value);
          if (!value || !Number.isFinite(budget) || budget < 0) throw new Error(t('costs.invalidBudget'));
        }
        await api(`/projects/${encodeURIComponent(project.id)}/budget`, {method: 'PUT', body: {budget_limit: budget}});
        item.dirty = false;
        feedback.textContent = t('costs.budgetSaved');
        await refresh();
      }, t('costs.saveBudget'));
      item.card = h('div', {class: 'card form-stack'}, h('h3', {}, title), usage, editor, feedback);
      budgetCards.set(project.id, item);
    }
    item.project = project;
    item.title.textContent = project.title;
    item.usage.replaceChildren(
      h('span', {}, `${t('costs.spent')}: ${money(project.spent)}`),
      h('span', {class: 'muted'}, `${t('costs.reserved')}: ${money(project.reserved)} · ${t('costs.remaining')}: ${money(project.remaining)}`),
    );
    if (!item.dirty) {
      item.mode.value = project.budget_limit == null ? 'unlimited' : 'limited';
      item.amount.value = project.budget_limit ?? '';
      item.amount.disabled = item.mode.value === 'unlimited';
    }
    return item.card;
  }

  function groupCard(title, entries) {
    return section(title, entries.length ? table(
      [t('costs.groupKey'), t('costs.count'), t('costs.cost')],
      entries.map(item => [h('span', {style: 'overflow-wrap:anywhere'}, item.key), item.count, money(item.est_cost)]),
    ) : h('p', {class: 'muted'}, t('costs.noRecords')));
  }

  async function refresh() {
    const version = ++serial;
    loading = true;
    try {
      const result = await api('/costs', {query: {...filters, limit: PAGE_SIZE, offset}, signal: ctx.signal});
      if (version !== serial || ctx.signal.aborted) return;
      count = result.totals.count;
      if (offset > 0 && offset >= count) {
        offset = Math.max(0, Math.floor((count - 1) / PAGE_SIZE) * PAGE_SIZE);
        return refresh();
      }
      totals.replaceChildren(
        section(t('costs.total'), h('strong', {}, money(result.totals.est_cost))),
        section(t('costs.calls'), h('strong', {}, result.totals.count)),
        section(t('costs.tokens'), h('strong', {}, `${result.totals.input_tokens} / ${result.totals.output_tokens}`), h('small', {class: 'muted'}, t('costs.tokenHint'))),
      );
      groups.replaceChildren(groupCard(t('costs.byRole'), result.by_role), groupCard(t('costs.byModel'), result.by_model));
      ledger.replaceChildren(result.entries.length ? table(
        ['created', 'project', 'role', 'provider', 'model', 'tokens', 'cost'].map(name => t(`costs.${name}`)),
        result.entries.map(entry => [date(entry.created_at), link(entry.project_title, `#/projects/${encodeURIComponent(entry.project_id)}`), entry.role, entry.provider, h('span', {style: 'overflow-wrap:anywhere'}, entry.model), `${entry.input_tokens} / ${entry.output_tokens}`, money(entry.est_cost)]),
      ) : empty(t('costs.empty'), t('costs.emptyHint')));
      const active = document.activeElement;
      const ids = new Set(result.projects.map(project => project.id));
      for (const [id, item] of budgetCards) if (!ids.has(id)) { item.card.remove(); budgetCards.delete(id); }
      let anchor = budgets.firstElementChild;
      for (const project of result.projects) {
        const card = budgetCard(project);
        if (card === anchor) anchor = anchor.nextElementSibling;
        else budgets.insertBefore(card, anchor);
      }
      budgetEmpty.hidden = result.projects.length > 0;
      if (active?.isConnected && document.activeElement !== active) active.focus?.({preventScroll: true});
      previous.disabled = offset === 0;
      next.disabled = offset + PAGE_SIZE >= count;
      pageLabel.textContent = t('costs.page', {n: Math.floor(offset / PAGE_SIZE) + 1, total: Math.max(1, Math.ceil(count / PAGE_SIZE))});
      status.textContent = t('costs.recordCount', {n: count});
    } catch (error) {
      if (error.name !== 'AbortError' && version === serial) status.textContent = error.message;
    } finally {
      if (version === serial) loading = false;
    }
  }
  await refresh();
  ctx.poll(() => loading ? undefined : refresh(), 8000);
  return root;
}
