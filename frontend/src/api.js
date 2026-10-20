export async function api(path, {method = 'GET', body, signal} = {}) {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error('只允许访问当前工作室');
  const headers = {Accept: 'application/json'};
  const options = {method, headers, signal, credentials: 'same-origin'};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    const message = Array.isArray(detail) ? detail.map(item => item.msg).join('；') : typeof detail === 'object' ? detail?.summary : detail;
    throw new Error(message || `请求未完成（${response.status}）`);
  }
  return data;
}

export function mediaURL(path) {
  const parts = String(path || '').split('/');
  if (parts.some(part => !part || part === '.' || part === '..') || /[\\:\x00]/.test(path)) return '';
  return `/media/${parts.map(encodeURIComponent).join('/')}`;
}

export function routeInfo(hash = '') {
  const url = new URL(hash.replace(/^#/, '') || '/', 'http://localhost');
  const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  return {key: parts[0] || 'dashboard', params: parts.slice(1), query: url.searchParams, path: url.pathname};
}

export const formatCount = value => new Intl.NumberFormat('zh-CN', {notation: Number(value) > 9999 ? 'compact' : 'standard', maximumFractionDigits: 1}).format(Number(value) || 0);
