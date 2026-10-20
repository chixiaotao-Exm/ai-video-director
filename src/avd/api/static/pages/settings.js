import {api} from '../api.js';
import {h, button, field, input, select, textarea, form, section, pageHeader} from '../components.js';
import {t} from '../i18n/zh.js';

const PROVIDERS = [
  {name: 'anthropic', models: ['director_model']},
  {name: 'openai', models: ['script_model', 'qa_model', 'image_model']},
  {name: 'gemini', models: ['gemini_model']},
];

function statusLine() {
  return h('p', {class: 'form-feedback muted', role: 'status', 'aria-live': 'polite'});
}

function asError(error) {
  return error instanceof Error ? error.message : String(error);
}

async function report(status, operation) {
  status.className = 'form-feedback muted';
  status.textContent = t('settings.saving');
  try {
    const result = await operation();
    status.textContent = t('settings.saved');
    return result;
  } catch (error) {
    status.className = 'form-feedback error-text';
    status.textContent = `${t('settings.failed')} ${asError(error)}`;
    return null;
  }
}

function num(data, name, nullable = false) {
  const raw = String(data.get(name) ?? '').trim();
  if (nullable && !raw) return null;
  const value = Number(raw);
  if (!raw || !Number.isFinite(value)) throw new Error(t('settings.numberRequired'));
  return value;
}

function jsonValue(data, name) {
  try {
    const result = JSON.parse(String(data.get(name) || '{}'));
    if (!result || Array.isArray(result) || typeof result !== 'object') throw new Error();
    return result;
  } catch {
    throw new Error(`${t('settings.invalidJson')} ${name}`);
  }
}

export async function render(ctx) {
  let snapshot = await api('/settings', {signal: ctx.signal});
  const values = () => snapshot.settings;
  const root = h('div', {class: 'page settings-page'},
    pageHeader(t('settings.title'), t('settings.subtitle')),
  );
  const tabs = h('div', {class: 'tabs', role: 'tablist', 'aria-label': t('settings.tabs')});
  const panels = h('div', {class: 'settings-panels'});
  root.append(tabs, panels);
  const tabNodes = [];

  async function save(changes) {
    snapshot = await api('/settings', {method: 'PUT', body: changes});
    return snapshot;
  }

  async function connection(provider, status, profile = null) {
    if (provider !== 'comfyui' && !ctx.projectId) {
      status.className = 'form-feedback error-text';
      status.textContent = t('settings.selectProject');
      return;
    }
    status.className = 'form-feedback muted';
    status.textContent = t('settings.testing');
    try {
      await api(`/settings/test/${provider}`, {
        method: 'POST',
        body: provider === 'comfyui' ? {profile} : {project_id: ctx.projectId},
      });
      status.textContent = provider === 'comfyui' ? t('settings.connected') : t('settings.connectedBilled');
    } catch (error) {
      status.className = 'form-feedback error-text';
      status.textContent = `${t('settings.connectionFailed')} ${asError(error)}`;
    }
  }

  function addTab(key, content) {
    const id = `settings-panel-${key}`;
    const tab = button(t(`settings.tab.${key}`), () => {
      history.replaceState(null, '', `#/settings/${key}`);
      for (const item of tabNodes) {
        const selected = item.key === key;
        item.tab.setAttribute('aria-selected', String(selected));
        item.tab.tabIndex = selected ? 0 : -1;
        item.tab.classList.toggle('active', selected);
        item.panel.hidden = !selected;
      }
    });
    tab.id = `settings-tab-${key}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', id);
    tab.setAttribute('aria-selected', String(tabNodes.length === 0));
    tab.tabIndex = tabNodes.length === 0 ? 0 : -1;
    tab.classList.toggle('active', tabNodes.length === 0);
    const panel = h('div', {id, class: 'settings-panel', role: 'tabpanel', 'aria-labelledby': tab.id}, content);
    panel.hidden = tabNodes.length > 0;
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = tabNodes.findIndex(item => item.tab === tab);
      const offset = event.key === 'ArrowRight' ? 1 : -1;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNodes.length - 1 :
        (index + offset + tabNodes.length) % tabNodes.length;
      tabNodes[next].tab.click();
      tabNodes[next].tab.focus();
    });
    tabs.append(tab);
    panels.append(panel);
    tabNodes.push({key, tab, panel});
  }

  const providerCards = PROVIDERS.map(provider => {
    const keyName = `${provider.name}_api_key`;
    const baseName = `${provider.name}_base_url`;
    const keyState = h('span', {class: 'muted'});
    const refreshKey = () => {
      const secret = snapshot.secrets[keyName];
      keyState.textContent = secret.configured ? `${t('settings.keySet')} ${secret.masked}` : t('settings.keyUnset');
    };
    refreshKey();
    const secretInput = input('password', '', {name: keyName, autocomplete: 'new-password', placeholder: t('settings.keyPlaceholder')});
    const status = statusLine();
    const fields = [
      field(t('settings.baseUrl'), input('url', values()[baseName] || '', {name: baseName, placeholder: t('settings.baseUrlDefault')})),
      field(t('settings.apiKey'), secretInput, t('settings.keyHint')),
      h('div', {class: 'toolbar'}, keyState, button(t('settings.clearKey'), async () => {
        await report(status, async () => {
          await save({[keyName]: null});
          secretInput.value = '';
          refreshKey();
        });
      }, {danger: true, small: true})),
      ...provider.models.map(name => field(t(`settings.field.${name}`), input('text', values()[name] || '', {name, required: name !== 'gemini_model'}))),
    ];
    const content = form(fields, async data => {
      await report(status, async () => {
        const changes = {[baseName]: String(data.get(baseName) || '').trim() || null};
        const secret = String(data.get(keyName) || '').trim();
        if (secret) changes[keyName] = secret;
        for (const name of provider.models) changes[name] = String(data.get(name) || '').trim() || null;
        await save(changes);
        secretInput.value = '';
        refreshKey();
      });
    }, t('settings.saveProvider'));
    return section(t(`settings.provider.${provider.name}`), h('div', {class: 'form-stack'},
      content,
      button(t('settings.testProvider'), () => connection(provider.name, status)),
      h('p', {class: 'muted'}, t('settings.testProviderHint')),
      status,
    ));
  });

  const priceStatus = statusLine();
  const priceRows = h('div', {class: 'form-stack'});
  const priceControls = new Map();
  function addPrice(name = '', price = {}) {
    const model = input('text', name, {placeholder: 'provider/model', required: true, 'aria-label': t('settings.priceModel')});
    const incoming = input('number', price.input_per_million ?? 0, {min: 0, step: 'any', 'aria-label': t('settings.priceInput')});
    const outgoing = input('number', price.output_per_million ?? 0, {min: 0, step: 'any', 'aria-label': t('settings.priceOutput')});
    const row = h('div', {class: 'card form-stack'},
      field(t('settings.priceModel'), model),
      h('div', {class: 'grid-2'}, field(t('settings.priceInput'), incoming), field(t('settings.priceOutput'), outgoing)),
    );
    row.append(button(t('settings.removePrice'), () => {
      priceControls.delete(row);
      row.remove();
    }, {danger: true, small: true}));
    priceControls.set(row, {model, incoming, outgoing});
    priceRows.append(row);
  }
  for (const [name, price] of Object.entries(values().model_prices)) addPrice(name, price);
  const prices = section(t('settings.prices'), h('div', {class: 'form-stack'},
    h('p', {class: 'muted'}, t('settings.priceHint')),
    priceRows,
    h('div', {class: 'toolbar'},
      button(t('settings.addPrice'), () => addPrice()),
      button(t('settings.savePrices'), () => report(priceStatus, async () => {
        const result = Object.create(null);
        for (const {model, incoming, outgoing} of priceControls.values()) {
          if (!model.reportValidity() || !incoming.reportValidity() || !outgoing.reportValidity()) throw new Error(t('settings.priceInvalid'));
          const name = model.value.trim();
          if (!name || Object.hasOwn(result, name)) throw new Error(t('settings.priceInvalid'));
          result[name] = {input_per_million: Number(incoming.value), output_per_million: Number(outgoing.value)};
        }
        await save({model_prices: result});
      }), {primary: true}),
    ), priceStatus,
  ));
  addTab('providers', h('div', {class: 'form-stack'}, h('div', {class: 'grid-2'}, ...providerCards), prices));

  const comfyStatus = statusLine();
  const comfyRows = h('div', {class: 'form-stack'});
  const activeProfile = select([], values().comfyui_active_profile, {'aria-label': t('settings.activeProfile')});
  const profileControls = new Map();
  function refreshProfiles() {
    const chosen = activeProfile.value || values().comfyui_active_profile;
    activeProfile.replaceChildren(...[...profileControls.keys()].map(name => h('option', {value: name}, name)));
    activeProfile.value = profileControls.has(chosen) ? chosen : [...profileControls.keys()][0] || '';
  }
  function addProfile(name, profile = {}) {
    const base = input('url', profile.base_url || 'http://127.0.0.1:8188', {required: true});
    const timeout = input('number', profile.timeout ?? 600, {min: 1, step: 'any', required: true});
    const rowStatus = statusLine();
    const row = h('div', {class: 'card form-stack'}, h('h3', {}, name),
      field(t('settings.baseUrl'), base), field(t('settings.timeoutSeconds'), timeout),
      h('div', {class: 'toolbar'},
        button(t('settings.testComfy'), async () => {
          if (await report(rowStatus, saveProfiles)) await connection('comfyui', rowStatus, name);
        }),
        button(t('settings.removeProfile'), () => {
          profileControls.delete(name);
          row.remove();
          refreshProfiles();
        }, {danger: true, small: true}),
      ), rowStatus,
    );
    profileControls.set(name, {base, timeout});
    comfyRows.append(row);
    refreshProfiles();
  }
  async function saveProfiles() {
    if (!profileControls.size || !activeProfile.value) throw new Error(t('settings.profileRequired'));
    const profiles = Object.create(null);
    for (const [name, {base, timeout}] of profileControls) {
      if (!base.reportValidity() || !timeout.reportValidity()) throw new Error(t('settings.profileInvalid'));
      profiles[name] = {base_url: base.value.trim(), timeout: Number(timeout.value)};
    }
    return save({comfyui_profiles: profiles, comfyui_active_profile: activeProfile.value});
  }
  for (const [name, profile] of Object.entries(values().comfyui_profiles)) addProfile(name, profile);
  activeProfile.value = values().comfyui_active_profile;
  const newProfile = input('text', '', {placeholder: t('settings.profileName'), 'aria-label': t('settings.profileName')});
  addTab('comfy', section(t('settings.comfyTitle'), h('div', {class: 'form-stack'},
    h('p', {class: 'muted'}, t('settings.comfyHint')),
    field(t('settings.activeProfile'), activeProfile),
    comfyRows,
    h('div', {class: 'toolbar'}, newProfile, button(t('settings.addProfile'), () => {
      const name = newProfile.value.trim();
      if (!name || profileControls.has(name)) {
        comfyStatus.textContent = t('settings.profileNameInvalid');
        return;
      }
      addProfile(name);
      newProfile.value = '';
    })),
    button(t('settings.saveComfy'), () => report(comfyStatus, saveProfiles), {primary: true}),
    comfyStatus,
  )));

  const generalStatus = statusLine();
  const general = form([
    h('div', {class: 'grid-2'},
      field(t('settings.defaultBudget'), input('number', values().default_budget_limit ?? '', {name: 'default_budget_limit', min: 0, step: 'any'}), t('settings.defaultBudgetHint')),
      field(t('settings.confirmThreshold'), input('number', values().confirm_threshold, {name: 'confirm_threshold', min: 0, step: 'any', required: true}), t('settings.confirmThresholdHint')),
    ),
    h('div', {class: 'grid-2'},
      field(t('settings.logFormat'), select([{value: 'text', label: t('settings.logText')}, {value: 'json', label: 'JSON'}], values().log_format, {name: 'log_format'})),
      field(t('settings.logLevel'), select(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map(value => ({value, label: value})), values().log_level, {name: 'log_level'})),
    ),
    h('div', {class: 'grid-2'},
      field(t('settings.workerTimeout'), input('number', values().job_timeout_seconds, {name: 'job_timeout_seconds', min: 1, step: 'any', required: true})),
      field(t('settings.workerGrace'), input('number', values().worker_shutdown_grace_seconds, {name: 'worker_shutdown_grace_seconds', min: 1, step: 'any', required: true})),
      field(t('settings.uploadLimit'), input('number', values().max_upload_bytes / 1048576, {name: 'upload_mb', min: 0.001, step: 'any', required: true})),
      field(t('settings.whisperModel'), input('text', values().whisper_model, {name: 'whisper_model', required: true})),
    ),
    field(t('settings.dataDirectory'), input('text', values().data_dir, {readOnly: true}), t('settings.readOnlyHint')),
    field(t('settings.database'), input('text', values().database_url || '', {readOnly: true})),
  ], data => report(generalStatus, () => save({
    default_budget_limit: num(data, 'default_budget_limit', true),
    confirm_threshold: num(data, 'confirm_threshold'),
    log_format: String(data.get('log_format')),
    log_level: String(data.get('log_level')),
    job_timeout_seconds: num(data, 'job_timeout_seconds'),
    worker_shutdown_grace_seconds: num(data, 'worker_shutdown_grace_seconds'),
    max_upload_bytes: Math.round(num(data, 'upload_mb') * 1048576),
    whisper_model: String(data.get('whisper_model')).trim(),
  })), t('settings.saveGeneral'));
  addTab('general', section(t('settings.generalTitle'), h('div', {class: 'form-stack'}, general, generalStatus)));

  const advancedStatus = statusLine();
  const jsonFields = ['llm_routes', 'job_estimated_costs', 'job_timeouts'];
  const textFields = ['workflow_dir', 'comfyui_url', 'smoke_workflow', 'smoke_image_prompt', 'smoke_image_reference', 'smoke_media_video'];
  const advanced = form([
    h('p', {class: 'muted'}, t('settings.advancedHint')),
    field(t('settings.maxTokens'), input('number', values().llm_max_output_tokens, {name: 'llm_max_output_tokens', min: 1, step: 1, required: true})),
    ...jsonFields.map(name => field(t(`settings.field.${name}`), textarea(JSON.stringify(values()[name], null, 2), {name, rows: 6, spellcheck: false}), t(`settings.hint.${name}`))),
    ...textFields.map(name => field(t(`settings.field.${name}`), input('text', values()[name] || '', {name}))),
    h('p', {class: 'muted'}, t('settings.precedence')),
  ], data => report(advancedStatus, () => {
    const changes = {llm_max_output_tokens: num(data, 'llm_max_output_tokens')};
    for (const name of jsonFields) changes[name] = jsonValue(data, name);
    for (const name of textFields) changes[name] = String(data.get(name) || '').trim() || null;
    return save(changes);
  }), t('settings.saveAdvanced'));
  addTab('advanced', section(t('settings.advancedTitle'), h('div', {class: 'form-stack'}, advanced, advancedStatus)));
  const selectedTab = tabNodes.find(item => item.key === ctx.params?.[0]);
  if (selectedTab) selectedTab.tab.click();
  return root;
}
