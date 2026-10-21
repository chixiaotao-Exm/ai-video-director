import test from 'node:test';
import assert from 'node:assert/strict';
import {api} from '../../src/avd/api/static/api.js';

test('a plain-text server error retains the HTTP status without leaking its body', async context => {
  context.mock.method(globalThis, 'fetch', async () => new Response('Internal Server Error: private traceback', {status: 500}));
  await assert.rejects(api('/projects/fixture/chat', {method: 'POST', body: {message: 'fixture'}}), error => {
    assert.equal(error.status, 500);
    assert.equal(error.code, 'invalid_response');
    assert.match(error.message, /500/);
    assert.match(error.message, /系统日志/);
    assert.doesNotMatch(error.message, /traceback/);
    return true;
  });
});

test('a gateway HTML failure shows a readable HTTP error and never its raw page', async context => {
  context.mock.method(globalThis, 'fetch', async () => new Response('<html><body>private gateway diagnostics</body></html>', {status: 502}));
  await assert.rejects(api('/fixture'), error => {
    assert.equal(error.status, 502);
    assert.match(error.message, /服务器处理请求失败/);
    assert.doesNotMatch(error.message, /html|private|gateway/);
    return true;
  });
});

test('structured chat failures preserve the public summary and code', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({detail: {
    summary: '模型额度已用尽，请更换可用模型。', code: 'quota_exhausted', retryable: false,
    retry_at: null, upstream_status: 429, route: {role: 'director', provider: 'openai', model: 'fixture-model', source: 'settings'},
  }}, {status: 503}));
  await assert.rejects(api('/fixture'), error => {
    assert.equal(error.status, 503);
    assert.equal(error.code, 'quota_exhausted');
    assert.match(error.message, /503.*模型额度已用尽/);
    assert.doesNotMatch(error.message, /fixture-model|retryable/);
    return true;
  });
});

test('JSON message details and validation errors stay readable without serializing unknown fields', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({detail: {message: '保存导演路由后重试', private_body: '<html>secret</html>'}}, {status: 422}));
  await assert.rejects(api('/fixture'), /422.*保存导演路由后重试/);
  context.mock.method(globalThis, 'fetch', async () => Response.json({detail: [{loc: ['body', 'model'], msg: 'Field required'}]}, {status: 422}));
  await assert.rejects(api('/fixture'), /body.model: Field required/);
});

test('HTML embedded in a JSON detail is suppressed', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({detail: '<!DOCTYPE html><html>private diagnostics</html>'}, {status: 504}));
  await assert.rejects(api('/fixture'), error => {
    assert.equal(error.status, 504);
    assert.doesNotMatch(error.message, /private|DOCTYPE|html/);
    return true;
  });
});

test('malformed successful data retains its status and interrupted bodies explain the connection failure', async context => {
  context.mock.method(globalThis, 'fetch', async () => new Response('not JSON', {status: 200}));
  await assert.rejects(api('/fixture'), error => error.status === 200 && /未返回预期的数据/.test(error.message));
  context.mock.method(globalThis, 'fetch', async () => ({ok: false, status: 504, text: async () => {throw new Error('private transport details');}}));
  await assert.rejects(api('/fixture'), error => error.status === 504 && error.code === 'incomplete_response' && !/private/.test(error.message));
});

test('network failures have an actionable message and cancellation remains cancellation', async context => {
  context.mock.method(globalThis, 'fetch', async () => {throw new TypeError('private network address');});
  await assert.rejects(api('/fixture'), error => error.code === 'network_error' && /本地服务/.test(error.message) && !/private/.test(error.message));
  const abort = new DOMException('Cancelled', 'AbortError');
  context.mock.method(globalThis, 'fetch', async () => {throw abort;});
  await assert.rejects(api('/fixture'), error => error === abort);
});
