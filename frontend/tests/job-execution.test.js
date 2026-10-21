import test from 'node:test';
import assert from 'node:assert/strict';
import {executionLines, failureSummary} from '../../src/avd/api/static/jobExecution.js';
import {effectiveDirectorRoute, directorRouteChanges, mergeRouteDraft} from '../../src/avd/api/static/directorRoute.js';

test('step execution uses its own captured request and never the directors queue marker', () => {
  const lines = executionLines({model_key: 'claude-old-director', execution: {
    role: 'image_gen', provider: 'grok', model: 'grok-imagine-image', source: 'actual', attempt: 2, steps: [],
  }});
  assert.equal(lines[0].model, 'grok-imagine-image');
  assert.match(lines[0].label, /执行请求/);
  assert.doesNotMatch(lines[0].label, /claude/);
  assert.match(lines[0].note, /网关内部模型/);
  assert.equal(lines[0].attempt, 2);
});

test('analysis displays its distinct transcription and visual request routes', () => {
  const lines = executionLines({execution: {role: 'analysis', source: 'actual', attempt: 1, steps: [
    {role: 'transcription', provider: 'local', model: 'whisper-small', source: 'local', stage: 'ASR'},
    {role: 'analysis', provider: 'openai', model: 'video-model', source: 'actual', stage: '画面'},
  ]}});
  assert.equal(lines.length, 2);
  assert.equal(lines[0].source, 'local');
  assert.match(lines[0].label, /whisper-small/);
  assert.match(lines[1].label, /video-model/);
});

test('historical model markers are explicitly unverified rather than invented from current settings', () => {
  const marker = executionLines({model_key: 'claude-opus-4-6-thinking'})[0];
  assert.equal(marker.source, 'queue_marker');
  assert.match(marker.note, /不代表本次实际执行/);
  assert.equal(executionLines({})[0].source, 'unknown');
  assert.match(executionLines({execution: {role: 'director', source: 'snapshot', provider: 'openai', model: 'gateway-model'}})[0].label, /任务快照/);
});

test('native process evidence distinguishes model probes from inference', () => {
  for (const [phase, label] of [['probe', '模型探测'], ['inference', '模型推理']]) {
    const line = executionLines({execution: {role: 'singing_voice', provider: 'local', model: 'SeedVC', source: 'actual', phase}})[0];
    assert.match(line.label, new RegExp(label));
    assert.match(line.label, /本地进程已启动/);
    assert.doesNotMatch(line.note, /网关/);
    assert.match(line.note, /不代表任务或效果验收通过/);
  }
});

test('Comfy resume evidence does not imply another dispatch or verified remote weights', () => {
  const execution = {role: 'motion_video', provider: 'comfyui', model: 'Wan', source: 'resumed'};
  const resumed = executionLines({execution})[0];
  assert.match(resumed.label, /继续已提交任务/);
  assert.match(resumed.note, /没有重复提交/);
  const actual = executionLines({execution: {...execution, source: 'actual'}})[0];
  assert.match(actual.note, /尚未核验.*权重/);
});

test('request model IDs retain gateway cross-vendor names without pretending provider is a model family', () => {
  const lines = executionLines({execution: {role: 'director', provider: 'openai', model: 'claude-opus-4-6-thinking', source: 'actual'}});
  assert.match(lines[0].label, /openai · claude-opus-4-6-thinking/);
  assert.doesNotMatch(lines[0].label, /ChatGPT/);
});

test('quota failures have readable summaries and recovery timestamps without dumping a traceback', () => {
  const summary = failureSummary({
    error: 'Traceback\nHTTPError: huge upstream text',
    failure: {code: 'quota_exhausted', message: '模型额度已耗尽', retryable: false, retry_at: '2026-10-22T04:00:00+00:00', http_status: 429},
  });
  assert.equal(summary.message, '模型额度已耗尽');
  assert.match(summary.recovery, /预计可重试/);
  assert.equal(summary.httpStatus, 429);
  assert.equal(failureSummary({error_summary: '接口未返回数据', error: 'Traceback...'}).message, '接口未返回数据');
  assert.equal(failureSummary({}), null);
});

test('the director editor displays the effective route including explicit overrides', () => {
  assert.deepEqual(effectiveDirectorRoute({director_provider: 'openai', director_model: 'claude-opus-4-6-thinking'}), {provider: 'openai', model: 'claude-opus-4-6-thinking'});
  assert.deepEqual(effectiveDirectorRoute({director_provider: 'gemini', director_model: 'irrelevant', gemini_model: 'gemini-video'}), {provider: 'gemini', model: 'gemini-video'});
  assert.deepEqual(effectiveDirectorRoute({director_provider: 'openai', director_model: 'old', llm_routes: {director: {provider: 'grok', model: 'new-director'}}}), {provider: 'grok', model: 'new-director'});
});

test('saving a director route requires an explicit model and preserves unrelated roles', () => {
  const settings = {llm_routes: {script: {provider: 'openai', model: 'writer'}}};
  assert.throws(() => directorRouteChanges(settings, 'openai', '  '), /请选择或输入/);
  const patch = directorRouteChanges(settings, 'openai', ' gateway-chat-model ');
  assert.equal(patch.director_model, 'gateway-chat-model');
  assert.deepEqual(patch.llm_routes.director, {provider: 'openai', model: 'gateway-chat-model'});
  assert.deepEqual(patch.llm_routes.script, settings.llm_routes.script);
  assert.equal(Object.hasOwn(settings.llm_routes, 'director'), false);
});

test('an older advanced JSON draft cannot overwrite a newly saved director route', () => {
  const baseline = {director: {provider: 'anthropic', model: 'old'}, script: {provider: 'openai', model: 'writer-old'}};
  const current = {...baseline, director: {provider: 'openai', model: 'new-director'}};
  const draft = {...baseline, script: {provider: 'grok', model: 'writer-new'}};
  const merged = mergeRouteDraft(draft, baseline, current);
  assert.deepEqual(merged.director, current.director);
  assert.deepEqual(merged.script, draft.script);
  assert.deepEqual(mergeRouteDraft({...draft, director: {provider: 'openai', model: 'explicit-change'}}, baseline, current).director, {provider: 'openai', model: 'explicit-change'});
});
