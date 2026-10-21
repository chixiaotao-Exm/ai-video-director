import test from 'node:test';
import assert from 'node:assert/strict';
import {jobDiagnosticText, jobsDiagnosticText, copyDiagnosticText} from '../../src/avd/api/static/jobExecution.js';

test('a diagnostic includes the full public identity, attempts, all request steps and untruncated errors', () => {
  const error = `Traceback\n${'detail '.repeat(500)}\nfinal public error`;
  const value = jobDiagnosticText({id: 'full-job-uuid-123456789', project_id: 'project-id', job_type: 'analyze_video', status: 'failed', lane: 'api', attempts: 2, max_attempts: 3,
    execution: {source: 'actual', attempt: 2, steps: [
      {role: 'transcription', provider: 'local', model: 'whisper-small', source: 'local'},
      {role: 'analysis', provider: 'openai', model: 'gateway-video', source: 'actual'},
    ]}, failure: {code: 'quota_exhausted', message: '额度已耗尽', http_status: 429, retryable: false, retry_at: '2026-10-22T04:00:00Z'}, error_summary: '任务记录的公开摘要', error});
  for (const text of ['full-job-uuid-123456789', 'project-id', 'failed', '2/3', 'whisper-small', 'gateway-video', 'source=actual', 'quota_exhausted', '429', '2026-10-22T04:00:00Z', '任务记录的公开摘要', error]) assert.ok(value.includes(text), text);
});

test('a diagnostic cannot copy unknown job fields, settings, payloads or step credentials', () => {
  const value = jobDiagnosticText({id: 'public-id', api_key: 'TOP-SECRET', settings: {token: 'SETTINGS-SECRET'}, payload: {authorization: 'PAYLOAD-SECRET'},
    execution: {source: 'actual', role: 'director', provider: 'openai', model: 'public-model', access_token: 'EXEC-SECRET', steps: [{role: 'director', provider: 'openai', model: 'public-model', source: 'actual', api_key: 'STEP-SECRET'}]}});
  assert.match(value, /public-id/);
  assert.match(value, /public-model/);
  assert.doesNotMatch(value, /SECRET|api_key|authorization|access_token/);
});

test('diagnostics distinguish recorded requests, submission snapshots and unverified historical markers', () => {
  const snapshot = jobDiagnosticText({execution: {source: 'snapshot', role: 'director', provider: 'openai', model: 'saved-model'}});
  assert.match(snapshot, /任务快照/);
  assert.match(snapshot, /尚不代表已成功执行/);
  const history = jobDiagnosticText({model_key: 'legacy-marker'});
  assert.match(history, /排队模型标记：legacy-marker/);
  assert.match(history, /不能从当前设置推断/);
  assert.doesNotMatch(history, /source=actual/);
});

test('copying a list names its scope and copies each full diagnostic once', () => {
  const value = jobsDiagnosticText([{id: 'one', status: 'succeeded'}, {id: 'two', status: 'failed', error_summary: 'public summary'}], '当前筛选第 2 页（最多 30 项）');
  assert.match(value, /^当前筛选第 2 页（最多 30 项） · 2 项任务/);
  assert.equal(value.split('任务 ID：one').length, 2);
  assert.equal(value.split('任务 ID：two').length, 2);
  assert.match(value, /public summary/);
});

test('clipboard success writes exact text and reports success without opening a fallback', async () => {
  let copied, message;
  const result = await copyDiagnosticText('line one\n<script>literal public text</script>', {clipboard: {writeText: async value => {copied = value;}}, fallback: () => assert.fail('unexpected fallback'), notify: value => {message = value;}});
  assert.equal(result, true);
  assert.equal(copied, 'line one\n<script>literal public text</script>');
  assert.match(message, /已复制/);
});

test('clipboard denial or an unavailable API preserves exact content in the manual fallback', async () => {
  for (const clipboard of [null, {writeText: async () => {throw new Error('NotAllowedError');}}]) {
    let fallbackText, message;
    assert.equal(await copyDiagnosticText('full public diagnostic\nlast line', {clipboard, fallback: value => {fallbackText = value;}, notify: value => {message = value;}}), false);
    assert.equal(fallbackText, 'full public diagnostic\nlast line');
    assert.match(message, /手动复制/);
  }
});
