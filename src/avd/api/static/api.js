import {t} from './i18n/zh.js';

export const API_ROUTES = Object.freeze({
  dashboard: '/dashboard', projects: '/projects', source: '/projects/{project_id}/source',
  uploads: '/uploads', characters: '/characters', voices: '/voices',
  sheet: '/projects/{project_id}/character-sheet',
  characterConsent: '/characters/{asset_id}/consent', voiceConsent: '/voices/{asset_id}/consent',
  assets: '/projects/{project_id}/assets', chat: '/projects/{project_id}/chat',
  status: '/projects/{project_id}/status', versions: '/projects/{project_id}/nodes/{node_id}/versions',
  revert: '/projects/{project_id}/nodes/{node_id}/revert', projectJobs: '/projects/{project_id}/jobs',
  approve: '/projects/{project_id}/approve', budgetApproval: '/projects/{project_id}/approve-budget',
  tools: '/projects/{project_id}/tools/{tool}', events: '/projects/{project_id}/events',
  media: '/media/{path}', jobs: '/jobs', cancel: '/jobs/{job_id}/cancel', retry: '/jobs/{job_id}/retry',
  costs: '/costs', projectBudget: '/projects/{project_id}/budget', settings: '/settings',
  connectionTest: '/settings/test/{provider}', system: '/system', logs: '/system/logs',
  workers: '/workers', startWorker: '/workers/{lane}/start', stopWorker: '/workers/{lane}/stop',
  regenerate: '/projects/{project_id}/shots/{shot_index}/regenerate', health: '/health', metrics: '/metrics'
});

function responseError(status, detail, code = 'http_error') {
  const error = new Error(`${t('error.http', {status})} ${detail}`);
  error.status = status;
  error.code = code;
  return error;
}

function safeErrorText(value) {
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (/<[a-z!/][^>]*>/i.test(text)) return '';
  return text.length > 600 ? `${text.slice(0, 599)}…` : text;
}

function responseFailure(status) {
  if (status === 429) return t('error.rateLimited');
  if (status >= 500) return t('error.serverFailure');
  return t('error.failed');
}

function errorDetail(data, status) {
  const detail = data?.detail;
  if (Array.isArray(detail)) {
    const messages = detail.map(item => {
      const message = safeErrorText(item?.msg);
      const location = Array.isArray(item?.loc) ? item.loc.filter(part => typeof part === 'string' || typeof part === 'number').join('.') : '';
      return message ? `${location ? `${location}: ` : ''}${message}` : '';
    }).filter(Boolean);
    return safeErrorText(messages.join('\n')) || responseFailure(status);
  }
  if (detail && typeof detail === 'object') return safeErrorText(detail.summary) || safeErrorText(detail.message) || responseFailure(status);
  return safeErrorText(detail) || responseFailure(status);
}

export async function api(path, {method = 'GET', body, signal, query} = {}) {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error(t('error.localOnly'));
  if (query) {
    const values = Object.entries(query).filter(([,value]) => value !== '' && value != null);
    if (values.length) path += `?${new URLSearchParams(values)}`;
  }
  const options = {method, signal, headers: {Accept: 'application/json'}, credentials: 'same-origin'};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    const connectionError = new Error(t('error.connection'));
    connectionError.code = 'network_error';
    throw connectionError;
  }
  let text;
  try { text = await response.text(); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw responseError(response.status, t('error.incompleteResponse'), 'incomplete_response');
  }
  let data;
  try { data = text ? JSON.parse(text) : null; }
  catch {
    throw responseError(response.status, response.ok ? t('error.invalidResponse') : responseFailure(response.status), 'invalid_response');
  }
  if (!response.ok) {
    throw responseError(response.status, errorDetail(data, response.status), safeErrorText(data?.detail?.code) || 'http_error');
  }
  return data;
}

export async function tool(projectId, name, args = {}) {
  const data = await api(`/projects/${encodeURIComponent(projectId)}/tools/${encodeURIComponent(name)}`, {
    method: 'POST', body: {args}
  });
  try { return JSON.parse(data.result); } catch { return data.result; }
}

export function mediaURL(path) {
  const parts = String(path || '').split('/');
  if (parts.some(part => !part || part === '.' || part === '..') || /[\\:\x00]/.test(path)) {
    throw new Error(t('error.mediaPath'));
  }
  return `/media/${parts.map(encodeURIComponent).join('/')}`;
}

export async function upload(file, projectId) {
  const body = new FormData();
  body.append('file', file);
  const result = await api(projectId ? `/projects/${encodeURIComponent(projectId)}/source` : '/uploads', {
    method: 'POST', body
  });
  return result.path || result.source_video_path;
}
