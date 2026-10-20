import {t} from './i18n/zh.js';
import {mediaURL} from './api.js';

export function h(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class' || key === 'className') element.className = value;
    else if (key === 'dataset') Object.assign(element.dataset, value);
    else if (['value', 'checked', 'selected', 'disabled', 'required', 'hidden', 'readOnly', 'multiple', 'controls', 'muted'].includes(key)) element[key] = value;
    else element.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

const paths = {
  logo: ['M5 5h14v14H5z', 'm10 8 5 4-5 4z'], dashboard: ['M3 3h7v7H3z','M14 3h7v7h-7z','M3 14h7v7H3z','M14 14h7v7h-7z'],
  project: ['M3 7h18v14H3z','M3 7V3h6l2 4','M8 12h8','M8 16h5'], assets: ['M3 4h18v16H3z','m3 17 5-5 4 4 3-3 6 5','M8 8h.01'],
  jobs: ['M8 6h13','M8 12h13','M8 18h13','M3 6h.01','M3 12h.01','M3 18h.01'], costs: ['M3 6h18v14H3z','M3 10h18','M15 15h3'],
  settings: ['M4 6h16','M4 12h16','M4 18h16','M8 3v6','M16 9v6','M10 15v6'], system: ['M3 4h18v12H3z','M8 21h8','M12 16v5','m7 8 2 2-2 2','M12 12h4'],
  plus: ['M12 5v14','M5 12h14'], arrow: ['M5 12h14','m14 7 5 5-5 5'], close: ['m6 6 12 12','M6 18 18 6'], menu: ['M4 6h16','M4 12h16','M4 18h16'],
  refresh: ['M20 7v5h-5','M4 17v-5h5','M6 6a8 8 0 0 1 13 1','M18 18a8 8 0 0 1-13-1'], film: ['M3 3h18v18H3z','M7 3v18','M17 3v18','M3 8h4','M3 16h4','M17 8h4','M17 16h4'],
  check: ['m5 12 4 4L19 6'], download: ['M12 3v12','m7 10 5 5 5-5','M4 16v5h16v-5'], play: ['m8 5 11 7-11 7z'], info: ['M12 11v6','M12 7h.01'],
  clock: ['M12 7v5l3 2'], anime: ['M12 3l2.4 5.6L20 9l-4.3 3.9L17 19l-5-3-5 3 1.3-6.1L4 9l5.6-.4z'], image: ['M3 3h18v18H3z','m3 17 6-6 4 4 3-3 5 5','M8 7h.01']
};
export function icon(name, size = 20) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': true})) svg.setAttribute(key, value);
  for (const d of paths[name] || paths.info) {
    const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
  }
  return svg;
}

export function notice(message, error = false) {
  const target = document.querySelector('#notice');
  if (!target) return;
  target.replaceChildren(h('span', {}, message), button(t('common.close'), () => { target.hidden = true; }, {small: true}));
  target.hidden = false;
  target.classList.toggle('is-error', error);
  target.setAttribute('role', error ? 'alert' : 'status');
}
export async function run(element, action) {
  if (element?.dataset.busy === 'true') return;
  if (element) { element.dataset.busy = 'true'; element.setAttribute('aria-busy', 'true'); }
  try { return await action(); }
  catch (error) { if (error.name !== 'AbortError') notice(error.message || String(error), true); }
  finally { if (element) { element.dataset.busy = 'false'; element.removeAttribute('aria-busy'); } }
}
export function button(text, onClick, {primary = false, danger = false, small = false, ...attrs} = {}) {
  const element = h('button', {type: 'button', class: ['button', primary && 'primary', danger && 'danger', small && 'small'].filter(Boolean).join(' '), ...attrs}, text);
  if (onClick) element.addEventListener('click', event => void run(element, () => onClick(event)));
  return element;
}
export function link(text, href, attrs = {}) { return h('a', {href, class: 'text-link', ...attrs}, text); }
export function input(type = 'text', value = '', attrs = {}) { return h('input', {type, ...(type === 'checkbox' ? {checked: value === true} : {value: value ?? ''}), ...attrs}); }
export function textarea(value = '', attrs = {}) { return h('textarea', {rows: 3, value: value ?? '', ...attrs}); }
export function select(options, value = '', attrs = {}) {
  const result = h('select', attrs, options.map(item => h('option', {value: item.value, disabled: item.disabled}, item.label)));
  result.value = String(value ?? ''); return result;
}
export function field(label, control, hint) { return h('label', {class: 'field'}, h('span', {class: 'field-label'}, label), control, hint && h('small', {class: 'muted'}, hint)); }
export function form(children, submit, submitText = t('common.save')) {
  const save = button(submitText, null, {primary: true, type: 'submit'});
  const element = h('form', {class: 'form-stack'}, children, h('div', {class: 'form-actions'}, save));
  element.addEventListener('submit', event => {
    event.preventDefault();
    if (element.reportValidity()) void run(save, () => submit(new FormData(element), element));
  });
  return element;
}
export function section(title, ...children) { return h('section', {class: 'card'}, title && h('div', {class: 'section-heading'}, h('h2', {}, title)), children); }
export function pageHeader(title, subtitle, actions) { return h('header', {class: 'page-heading'}, h('div', {}, h('h1', {}, title), subtitle && h('p', {class: 'muted'}, subtitle)), actions && h('div', {class: 'toolbar'}, actions)); }
export function empty(title, description, action) { return h('div', {class: 'empty'}, icon('film', 32), h('h3', {}, title), description && h('p', {class: 'muted'}, description), action); }
export function badge(status, label) { return h('span', {class: `status status-${status || 'unknown'}`}, h('span', {class: 'status-dot'}), label || t(`status.${status || 'unknown'}`)); }
// Token counts are always shown in millions, e.g. 0.0016M, 0.250M, 12.35M.
export function mtokens(value) {
  const number = Number(value || 0);
  const digits = number >= 1e6 ? 2 : number >= 1e4 ? 3 : 4;
  return `${(number / 1e6).toFixed(digits)}M`;
}
export function money(value) { return value == null ? t('common.unlimited') : new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4}).format(value); }
export function date(value) { return value ? new Date(value.endsWith?.('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—'; }
export function formatDuration(value) { return value == null ? '—' : t('common.seconds', {n: Math.round(value * 10) / 10}); }
export function media(path, kind = 'image', attrs = {}) {
  if (!path) return h('div', {class: 'media-empty'}, icon('image', 28), t('common.noMedia'));
  const tag = kind === 'image' || kind === 'img' ? 'img' : kind;
  return h(tag, {class: `media media-${tag}`, src: mediaURL(path), ...(tag === 'img' ? {alt: t('common.preview'), loading: 'lazy'} : {controls: true, preload: 'metadata', playsinline: true}), ...attrs});
}
export function table(headers, rows) { return h('div', {class: 'table-scroll', tabindex: 0}, h('table', {}, h('thead', {}, h('tr', {}, headers.map(title => h('th', {scope: 'col'}, title)))), h('tbody', {}, rows.map(row => row instanceof Node ? row : h('tr', {}, row.map(cell => h('td', {}, cell))))))); }
export function details(title, content, open = false) { return h('details', {class: 'details', open}, h('summary', {}, title), content); }
export function dialog(title, content) {
  const previous = document.activeElement;
  const element = h('dialog', {class: 'dialog', 'aria-label': title});
  const close = () => { element.close(); element.remove(); previous?.focus?.(); };
  element.append(h('header', {class: 'dialog-header'}, h('h2', {}, title), button(icon('close'), close, {'aria-label': t('common.close'), small: true})), content);
  element.addEventListener('cancel', event => {event.preventDefault(); close();});
  document.body.append(element); element.showModal();
  return {element, close};
}
