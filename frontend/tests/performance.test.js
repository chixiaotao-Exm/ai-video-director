import test from 'node:test';
import assert from 'node:assert/strict';
import {buildPerformanceConfig, buildPerformancePatch, sourceOptions, stageReadiness, voiceAfterProbe, readinessAfterRefresh, pendingProbeFromReadiness, hasActiveGeneration, singingModelLabel, performanceVideoReady} from '../../src/avd/api/static/pages/performance.js';

const inputs = {
  source: 'uploads/performance.mp4', start: '0', duration: '10', character: 'target-person',
  voice: 'target-voice', model: 'model-one', scene: '  新舞台，保留动作与节拍  ', lipsync: false,
};

test('scene and converted singing references cannot enable video-preview approval', () => {
  const nodes = ['scene_reference', 'singing_voice'].map(kind => ({kind, status: 'clean', current_version_id: kind}));
  assert.equal(performanceVideoReady({nodes}), false);
  for (const kind of ['motion_video', 'performance_sync', 'performance_mix', 'performance_final']) nodes.push({kind, status: 'clean', current_version_id: kind});
  assert.equal(performanceVideoReady({nodes}), false);
  nodes.at(-1).output_paths = {video: 'projects/preview/final.mp4'};
  assert.equal(performanceVideoReady({nodes}), true);
  nodes.find(node => node.kind === 'motion_video').status = 'dirty';
  assert.equal(performanceVideoReady({nodes}), false);
});

test('enabled lip correction must complete before video preview can be reviewed', () => {
  const project = {performance_config: {lipsync: true}, nodes: ['scene_reference', 'singing_voice', 'motion_video', 'performance_sync', 'performance_mix', 'performance_final'].map(kind => ({kind, status: 'clean', current_version_id: kind, output_paths: {video: 'projects/preview/final.mp4'}}))};
  assert.equal(performanceVideoReady(project), false);
  project.nodes.push({kind: 'performance_lipsync', status: 'running', current_version_id: 'old-lip'});
  assert.equal(performanceVideoReady(project), false);
  project.nodes.at(-1).status = 'clean';
  assert.equal(performanceVideoReady(project), true);
});

test('singing versions identify trained RVC and sample conditioned SeedVC separately', () => {
  const model = {number: 2, sample_rate: 44100, fingerprint: '1234567890abcdef'};
  assert.equal(singingModelLabel({...model, engine: 'seed-vc'}), 'v2 · SeedVC · 声音参考 · 44.1 kHz · 12345678');
  assert.equal(singingModelLabel({...model, engine: 'rvc', sample_rate: 48000}), 'v2 · RVC · 48 kHz · 12345678');
  assert.match(singingModelLabel({...model, engine: 'future-engine'}), /future-engine/);
});

test('performance inputs save a reference performance independently of remake and TTS', () => {
  const config = buildPerformanceConfig(inputs);
  assert.equal(config.mode, 'performance_remix');
  assert.deepEqual(config.preparation, {source_video_path: 'uploads/performance.mp4', start_s: 0, duration_s: 10, sample_rate: 48000});
  assert.equal(config.scene_prompt, '新舞台，保留动作与节拍');
  assert.equal(config.voice_model_version_id, 'model-one');
  assert.equal(config.lipsync, false);
  assert.equal(config.character_id, 'target-person');
  assert.equal(config.voice_id, 'target-voice');
});

test('saving a scene edit retains effective audio settings and never changes the original draft', () => {
  const base = {
    voice_id: 'target-voice', voice_model_version_id: 'model-one',
    voice_parameters: {pitch: 2, speaker_id: 3, f0_method: 'pm', index_rate: 0.6},
    mix: {vocal_gain_db: -2, accompaniment_gain_db: -4, peak_limit_db: -1},
    preparation: {sample_rate: 44100},
  };
  const before = structuredClone(base);
  const config = buildPerformanceConfig({...inputs, scene: '另一个舞台'}, base);
  assert.deepEqual(config.voice_parameters, base.voice_parameters);
  assert.deepEqual(config.mix, base.mix);
  assert.equal(config.preparation.sample_rate, 44100);
  assert.deepEqual(base, before);
});

test('switching the voice or model cannot reuse a previous models speaker and pitch overrides', () => {
  const base = {voice_id: 'target-voice', voice_model_version_id: 'model-one', voice_parameters: {speaker_id: 7, pitch: 12}, mix: {vocal_gain_db: 1}};
  for (const patch of [{model: 'model-two'}, {voice: 'other-voice'}, {model: ''}]) {
    const config = buildPerformanceConfig({...inputs, ...patch}, base);
    assert.equal(Object.hasOwn(config, 'voice_parameters'), false);
    assert.deepEqual(config.mix, {vocal_gain_db: 1});
  }
});

test('a scene form save only patches the scene and cannot recapture source, stems or frozen workflows', () => {
  const base = {
    ...buildPerformanceConfig(inputs),
    motion_workflow: {template_path: 'an-export-that-has-since-been-deleted.json'},
    motion_parameters: {seed: 7},
    lipsync_workflow: {template_path: 'frozen-lipsync.json'},
    voice_parameters: {pitch: 3, diffusion_steps: 45},
    mix: {vocal_gain_db: -2},
  };
  assert.deepEqual(buildPerformancePatch({...inputs, scene: '另一个舞台'}, base), {scene_prompt: '另一个舞台'});
  assert.deepEqual(buildPerformancePatch(inputs, base), {});
  assert.deepEqual(buildPerformancePatch({...inputs, start: '2'}, base), {preparation: {start_s: 2}});
  assert.equal(base.voice_parameters.pitch, 3);
});

test('changing a selected singing version explicitly clears overrides on the shared patch API', () => {
  const base = {...buildPerformanceConfig(inputs), voice_parameters: {speaker_id: 7, pitch: 12}};
  assert.deepEqual(buildPerformancePatch({...inputs, model: 'new-seed-reference'}, base), {
    voice_model_version_id: 'new-seed-reference', voice_parameters: null,
  });
  assert.deepEqual(buildPerformancePatch({...inputs, voice: 'other-voice', model: ''}, base), {
    voice_id: 'other-voice', voice_model_version_id: null, voice_parameters: null,
  });
});

test('a reference can be prepared before a singing model has been imported', () => {
  const config = buildPerformanceConfig({...inputs, model: '', start: '2.375', duration: '7.5'});
  assert.equal(config.voice_model_version_id, null);
  assert.equal(config.preparation.start_s, 2.375);
  assert.equal(config.preparation.duration_s, 7.5);
});

test('a separated silent library clip selects its original performance including the song', () => {
  const choices = sourceOptions([
    {kind: 'video', name: '表演 · 无声画面', path: 'assets/media/one/video.mp4', source: 'uploads/original.mp4'},
    {kind: 'audio', name: '原人声', path: 'assets/media/one/vocals.wav', source: 'uploads/original.mp4'},
    {kind: 'video', name: '同一素材', path: 'assets/media/two/video.mp4', source: 'uploads/original.mp4'},
    {kind: 'video', name: '完整唱跳', path: 'uploads/second.mp4'},
  ], [{title: '已有项目', source_video_path: 'uploads/original.mp4'}]);
  assert.deepEqual(choices.map(choice => choice.value), ['uploads/original.mp4', 'uploads/second.mp4']);
  assert.match(choices[0].label, /原始音画/);
});

test('an existing source remains selectable when it is not listed in the asset library', () => {
  const choices = sourceOptions([], [{title: '另一个项目', source_video_path: 'uploads/other.mp4'}], 'uploads/current.mp4');
  assert.equal(choices[0].value, 'uploads/current.mp4');
  assert.equal(choices[1].value, 'uploads/other.mp4');
});

test('files present or a successful queue response do not certify RVC inference or quality', () => {
  const checks = stageReadiness({
    source_preparation: {ready: true, reasons: []},
    separation: {ready: true, reasons: []},
    voice: {imported_files_valid: true, runtime_files_available: true, ready_for_inference: false, reasons: ['Probe the imported model']},
    motion: {ready: false, reasons: ['No real motion workflow']},
    job_ids: ['queued-job'], model_generated: false,
  });
  assert.deepEqual(checks.map(check => Boolean(check.ready)), [true, true, false, false]);
  assert.deepEqual(checks[2].reasons, ['Probe the imported model']);
  assert.deepEqual(checks[3].reasons, ['No real motion workflow']);
});

test('GPU probe queue responses stay unready until a successful job returns real readiness', () => {
  const current = {runtime_files_available: true, ready_for_inference: false, reasons: ['Not yet probed']};
  const result = {imported_files_valid: true, runtime_available: true, model_compatible: true, ready_for_inference: true, quality_verified: false, reasons: []};
  for (const status of ['queued', 'running']) {
    const waiting = voiceAfterProbe(current, {status, readiness: result, probe_queued: true});
    assert.equal(waiting.ready_for_inference, false);
    assert.equal(waiting.quality_verified, false);
    assert.match(waiting.reasons[0], /GPU/);
  }
  assert.deepEqual(voiceAfterProbe(current, {status: 'succeeded', readiness: result}), result);
  assert.equal(voiceAfterProbe(current, {status: 'succeeded', readiness: null}).ready_for_inference, false);
});

test('a page reopened during a global probe restores its polling job', () => {
  for (const status of ['queued', 'running']) {
    const readiness = {voice: {version_id: 'selected', probe_job_id: 'durable-probe', probe_status: status}};
    assert.deepEqual(pendingProbeFromReadiness(readiness, 'selected'), {
      model_id: 'selected', job_id: 'durable-probe', status,
    });
    assert.equal(pendingProbeFromReadiness(readiness, 'other-model'), null);
  }
  assert.equal(pendingProbeFromReadiness({voice: {version_id: 'selected', probe_job_id: 'done', probe_status: 'succeeded'}}, 'selected'), null);
});

test('failed and cancelled probe jobs invalidate stale readiness and expose the actual error', () => {
  const current = {ready_for_inference: true, runtime_files_available: true};
  for (const status of ['failed', 'cancelled']) {
    const result = voiceAfterProbe(current, {status, error: 'Checkpoint incompatible'});
    assert.equal(result.ready_for_inference, false);
    assert.equal(result.quality_verified, false);
    assert.deepEqual(result.reasons, ['Checkpoint incompatible']);
  }
});

test('lip sync readiness is visible whenever the saved configuration requires it', () => {
  const state = {lipsync: {ready: false, reasons: ['No frozen lip sync workflow']}};
  assert.equal(stageReadiness(state).length, 4);
  assert.deepEqual(stageReadiness(state, true).at(-1), {
    label: '口型修正工作流', ready: false, reasons: ['No frozen lip sync workflow'],
  });
});

test('a completed browser probe cannot override current server readiness', () => {
  const raw = {voice: {imported_files_valid: true, runtime_files_available: true, ready_for_inference: false, model_fingerprint: 'model-sha', cli_sha256: 'runtime-sha'}};
  const probe = {model_id: 'one', status: 'succeeded', readiness: {...raw.voice, ready_for_inference: true, quality_verified: false, reasons: []}};
  assert.equal(readinessAfterRefresh(raw, probe, 'one'), raw);
  const restored = {...raw, voice: {...raw.voice, ready_for_inference: true}};
  assert.equal(readinessAfterRefresh(restored, probe, 'one'), restored);
  assert.equal(readinessAfterRefresh(raw, probe, 'different-model'), raw);
  for (const changed of [
    {imported_files_valid: false}, {runtime_files_available: false},
    {model_fingerprint: 'new-sha'}, {cli_sha256: 'new-runtime-sha'},
  ]) {
    const state = {...raw, voice: {...raw.voice, ...changed}};
    assert.equal(readinessAfterRefresh(state, probe, 'one'), state);
  }
  const failed = readinessAfterRefresh(raw, {...probe, status: 'failed', error: 'Actual model probe failed'}, 'one');
  assert.equal(failed, raw);
  const newer = {...restored, voice: {...restored.voice, probe_job_id: 'new-probe'}};
  for (const status of ['failed', 'cancelled', 'running']) {
    assert.equal(readinessAfterRefresh(newer, {...probe, job_id: 'old-probe', status}, 'one'), newer);
  }
});

test('director progress reports cannot block performance input review after preparation finishes', () => {
  const background = [
    {job_type: 'performance_prepare', node_id: 'prepare-node', status: 'succeeded'},
    {job_type: 'director_report', node_id: null, status: 'queued'},
    {job_type: 'subagent', node_id: null, status: 'running'},
  ];
  assert.equal(hasActiveGeneration(background), false);
  assert.equal(hasActiveGeneration([...background, {job_type: 'scene_reference', status: 'queued'}]), true);
  assert.equal(hasActiveGeneration([...background, {job_type: 'singing_voice', node_id: 'singing-node', status: 'running'}]), true);
});
