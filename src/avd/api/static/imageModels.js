import {api} from './api.js';
import {h, field, select, form, dialog} from './components.js';
import {t} from './i18n/zh.js';

export function currentImageChoice(settings) {
  const provider = settings.image_provider || 'openai';
  const model = provider === 'gemini'
    ? settings.gemini_image_model
    : provider === 'grok'
      ? settings.grok_image_model
      : settings.image_model;
  return model ? `${provider}/${model}` : '';
}

export function imageSettingChanges(provider, model) {
  const changes = {image_provider: provider};
  if (provider === 'gemini') changes.gemini_image_model = model;
  else if (provider === 'grok') changes.grok_image_model = model;
  else changes.image_model = model;
  return changes;
}

export function parseImageChoice(value) {
  const text = String(value || '');
  const slash = text.indexOf('/');
  if (slash <= 0 || slash === text.length - 1) return null;
  return {provider: text.slice(0, slash), model: text.slice(slash + 1)};
}

export async function loadImageModels(signal) {
  const result = await api('/settings/image-models', {signal});
  return result.models || [];
}

export function fillImageModelSelect(element, models, current) {
  const options = models.map(item => ({value: `${item.provider}/${item.model}`, label: item.label}));
  if (current && !options.some(item => item.value === current)) {
    options.unshift({value: current, label: current});
  }
  element.replaceChildren(...options.map(item => h('option', {value: item.value}, item.label)));
  element.value = options.some(item => item.value === current) ? current : (options[0]?.value || '');
}

export function imageModelSelect(models, current, attrs = {}) {
  const element = select([{value: current || '', label: current || t('settings.imageModelsLoading')}], current, attrs);
  fillImageModelSelect(element, models, current);
  return element;
}

export function chooseImageModel(models, current) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let modal;
    const content = form([
      h('p', {class: 'muted'}, t('settings.hint.image_model_choice')),
      field(t('settings.field.image_model_choice'), imageModelSelect(models, current, {name: 'image_model', required: true})),
    ], data => {
      const choice = parseImageChoice(data.get('image_model'));
      modal.close();
      finish(choice);
    }, t('settings.useImageModel'));
    modal = dialog(t('settings.field.image_model_choice'), content);
    modal.element.addEventListener('close', () => finish(null));
  });
}
