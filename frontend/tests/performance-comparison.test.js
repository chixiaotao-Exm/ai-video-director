import test from 'node:test';
import assert from 'node:assert/strict';
import {performanceComparisonSources, synchronizePerformanceMedia, createPerformanceComparison} from '../../src/avd/api/static/performanceComparison.js';
import {singingParameterSummary, renderProject} from '../../src/avd/api/static/pages/performance.js';

class Element extends EventTarget {
  constructor(tag) {super();this.tagName = tag;this.children = [];this.dataset = {};this.attributes = new Map();this.disabled = false;this.value = '';this._text = '';}
  append(...children) {for (const child of children) {child.parentNode = this;this.children.push(child);}}
  replaceChildren(...children) {for (const child of this.children) child.parentNode = null;this.children = [];this.append(...children);this._text = '';}
  setAttribute(name, value) {this.attributes.set(name, value);if (name === 'value') this.value = value;}
  removeAttribute(name) {this.attributes.delete(name);}
  get options() {return this.children.filter(child => child.tagName === 'option');}
  contains(element) {return element === this || this.children.some(child => child.contains(element));}
  querySelectorAll(selector) {
    const tags = selector.split(',');
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    return all.filter(child => tags.includes(child.tagName));
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
  closest(selector) {return selector === '[data-performance-inputs]' && this.dataset.performanceInputs ? this : this.parentNode?.closest(selector) || null;}
  focus() {}
  showModal() {this.open = true;}
  close() {this.open = false;this.dispatchEvent(new Event('close'));}
  remove() {if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);this.parentNode = null;}
  get textContent() {return this._text + this.children.map(child => child.textContent).join('');}
  set textContent(value) {this._text = String(value);this.children = [];}
}

class Player extends Element {
  constructor(tag = 'video', duration = 10) {
    super(tag);this.duration = duration;this._time = 0;this.paused = true;this.ended = false;
    this.muted = false;this.volume = 0.1;this.playbackRate = 1;this.playCalls = 0;this.loadCalls = 0;
  }
  get currentTime() {return this._time;}
  set currentTime(value) {this._time = value;this.dispatchEvent(new Event('seeking'));this.dispatchEvent(new Event('seeked'));}
  play() {
    ++this.playCalls;
    const start = () => {this.paused = false;this.dispatchEvent(new Event('play'));globalThis.document?.dispatchMediaEvent?.('play', this);};
    if (this.delay) return this.delay.then(start);
    start();
    return this.rejectPlay ? Promise.reject(new Error('play denied')) : Promise.resolve();
  }
  pause() {if (!this.paused) {this.paused = true;this.dispatchEvent(new Event('pause'));}}
  load() {++this.loadCalls;}
}

function setup(options = {}) {
  const reference = new Player(), result = new Player(), originalAudio = new Player('audio');
  const callbacks = new Set(), errors = [], changes = [];
  const scheduler = {setInterval: callback => {callbacks.add(callback);return callback;}, clearInterval: callback => callbacks.delete(callback)};
  const controller = synchronizePerformanceMedia({reference, result, originalAudio, scheduler, onError: value => errors.push(value), onChange: value => changes.push(value), ...options});
  return {reference, result, originalAudio, callbacks, errors, changes, controller};
}

const deferred = () => {let resolve, reject;const promise = new Promise((yes, no) => {resolve = yes;reject = no;});return {promise, resolve, reject};};
const flush = () => new Promise(resolve => setImmediate(resolve));
const audible = (...players) => players.filter(player => !player.paused && !player.muted);
function node(kind, outputs, id = kind + '-v1') {
  return {id: kind, kind, status: 'clean', current_version_id: id, output_paths: {...outputs}, current_version: {id, node_id: kind, output_paths: {...outputs}}};
}
function project() {
  return {source_video_path: 'uploads/full-original.mp4', performance_config: {preparation: {start_s: 3.5}}, nodes: [
    node('performance_prepare', {motion: 'projects/one/prepare/motion.mp4', audio: 'projects/one/prepare/audio.wav'}),
    node('performance_final', {video: 'projects/one/final/video.mp4'}),
  ]};
}

test('comparison uses only exact clean current motion/audio and final video, already on the prepared clock', () => {
  const selected = performanceComparisonSources(project());
  assert.equal(selected.reference.video, 'projects/one/prepare/motion.mp4');
  assert.equal(selected.reference.audio, 'projects/one/prepare/audio.wav');
  assert.equal(selected.result.video, 'projects/one/final/video.mp4');
  assert.doesNotMatch(selected.key, /uploads|3\.5/);
  for (const status of ['dirty', 'running', 'failed']) {
    for (const index of [0, 1]) {
      const value = project();value.nodes[index].status = status;
      assert.equal(performanceComparisonSources(value)[index ? 'result' : 'reference'], null);
    }
  }
});

test('missing, mismatched and unsafe outputs cannot fall back to source or old versions', () => {
  for (const change of [
    value => {value.nodes[0].current_version_id = 'new-version';},
    value => {delete value.nodes[0].current_version;},
    value => {value.nodes[0].current_version.node_id = 'another-node';},
    value => {value.nodes[0].output_paths.motion = 'projects/old.mp4';},
    value => {value.nodes[0].current_version.output_paths = {video: 'projects/wrong-key.mp4'};},
    value => {value.nodes[0].current_version.output_paths.motion = '../outside.mp4';},
  ]) {
    const value = project();change(value);
    assert.equal(performanceComparisonSources(value).reference, null);
  }
  const missingAudio = project();delete missingAudio.nodes[0].current_version.output_paths.audio;
  assert.equal(performanceComparisonSources(missingAudio).reference.audio, null);
});

test('shared playback, seek, rate and pause keep both videos together with only final sound by default', async () => {
  const {controller, reference, result, originalAudio, callbacks} = setup();
  assert.equal(await controller.play(), true);
  assert.equal(callbacks.size, 1);
  assert.deepEqual(audible(reference, result, originalAudio), [result]);
  assert.equal(originalAudio.paused, true);
  assert.equal(result.volume, 0.1);
  controller.seek(4.25);
  for (const player of [reference, result, originalAudio]) assert.equal(player.currentTime, 4.25);
  result._time = 5;reference._time = 4;result.playbackRate = 0.75;
  controller.synchronize();
  assert.equal(reference.currentTime, 5);assert.equal(reference.playbackRate, 0.75);
  reference.pause();
  assert.ok([reference, result, originalAudio].every(player => player.paused));
  assert.equal(callbacks.size, 0);
  controller.dispose();
});

test('track changes retain the timeline and never play two audible tracks, including mute', async () => {
  const {controller, reference, result, originalAudio} = setup();
  await controller.play();controller.seek(6.5);
  controller.setTrack('reference');await flush();
  assert.deepEqual(audible(reference, result, originalAudio), [originalAudio]);
  assert.equal(originalAudio.currentTime, 6.5);
  controller.setTrack('mute');await flush();
  assert.equal(audible(reference, result, originalAudio).length, 0);
  controller.setTrack('result');await flush();
  assert.deepEqual(audible(reference, result, originalAudio), [result]);
  // A native unmute event cannot enable a second soundtrack.
  reference.muted = false;reference.dispatchEvent(new Event('volumechange'));
  assert.equal(reference.muted, true);
  controller.dispose();
});

test('late follower seek events cannot move the master clock backwards', () => {
  const value = setup();
  value.controller.seek(7);
  value.reference._time = 6.5;
  value.reference.dispatchEvent(new Event('seeking'));
  value.reference.dispatchEvent(new Event('seeked'));
  assert.equal(value.result.currentTime, 7);
  value.controller.synchronize();
  assert.equal(value.reference.currentTime, 7);
  value.controller.dispose();
});

test('invalid metadata blocks playback and shortest media duration bounds seeking, end and replay', async () => {
  const value = setup();value.result.duration = NaN;
  assert.equal(await value.controller.play(), false);
  assert.equal(value.result.playCalls, 0);
  value.result.duration = 9.8;value.result.dispatchEvent(new Event('loadedmetadata'));
  value.controller.seek(100);assert.equal(value.reference.currentTime, 9.8);
  assert.equal(await value.controller.play(), true);
  assert.equal(value.result.currentTime, 0);
  value.result._time = 9.81;value.controller.synchronize();
  assert.equal(value.controller.getState().playing, false);
  assert.equal(value.result.currentTime, 9.8);
  value.controller.seek(-50);assert.equal(value.reference.currentTime, 0);
  value.controller.dispose();
});

test('buffering, decode failure or one rejected play stops the entire comparison', async () => {
  for (const event of ['waiting', 'error']) {
    const value = setup();await value.controller.play();
    value.reference.dispatchEvent(new Event(event));
    assert.equal(value.controller.getState().playing, false);
    assert.ok([value.reference, value.result, value.originalAudio].every(player => player.paused));
    assert.equal(value.callbacks.size, 0);assert.equal(value.errors.length, 1);
    value.controller.dispose();
  }
  const value = setup();value.result.rejectPlay = true;
  assert.equal(await value.controller.play(), false);
  assert.ok(value.reference.paused && value.result.paused);
  value.controller.dispose();
});

test('a late successful play cannot resume after pause, another rejection or disposal', async () => {
  for (const action of ['pause', 'reject', 'dispose']) {
    const value = setup(), delayed = deferred();value.reference.delay = delayed.promise;
    if (action === 'reject') value.result.rejectPlay = true;
    const pending = value.controller.play();
    if (action === 'reject') await flush();
    else value.controller[action]();
    delayed.resolve();
    assert.equal(await pending, false);
    assert.ok(value.reference.paused && value.result.paused, action);
    assert.equal(value.callbacks.size, 0);
    value.controller.dispose();
  }
});

test('a rapid track switch cannot let a late original audio start replace the final track', async () => {
  const value = setup(), delayed = deferred();
  await value.controller.play();value.originalAudio.delay = delayed.promise;
  value.controller.setTrack('reference');
  value.controller.setTrack('result');await flush();
  delayed.resolve();await flush();
  assert.equal(value.originalAudio.paused, true);
  assert.deepEqual(audible(value.reference, value.result, value.originalAudio), [value.result]);
  value.controller.dispose();
});

test('abort handles already-aborted signals and in-flight playback without leaked callbacks or media sources', async () => {
  const aborted = new AbortController();aborted.abort();
  const first = setup({signal: aborted.signal});
  assert.equal(await first.controller.play(), false);
  assert.equal(first.result.playCalls, 0);assert.equal(first.result.loadCalls, 1);
  const aborter = new AbortController(), value = setup({signal: aborter.signal}), delayed = deferred();
  value.reference.delay = delayed.promise;
  const pending = value.controller.play();aborter.abort();
  const count = value.changes.length;
  delayed.resolve();await pending;
  value.result.dispatchEvent(new Event('timeupdate'));
  assert.equal(value.changes.length, count);
  assert.ok(value.reference.paused && value.result.paused);
  value.controller.dispose();assert.equal(value.result.loadCalls, 1);
});

function find(element, predicate) {
  if (predicate(element)) return element;
  for (const child of element.children) {const result = find(child, predicate);if (result) return result;}
  return null;
}
async function withDOM(callback) {
  const oldNode = globalThis.Node, oldDocument = globalThis.document, oldAnimationFrame = globalThis.requestAnimationFrame;
  globalThis.Node = Element;
  const body = new Element('body'), listeners = new Map();
  globalThis.document = {
    body,
    createElement: tag => ['video', 'audio'].includes(tag) ? new Player(tag) : new Element(tag),
    createElementNS: (namespace, tag) => {const result = new Element(tag);result.namespaceURI = namespace;return result;},
    createTextNode: value => {const result = new Element('#text');result.textContent = value;return result;},
    addEventListener: (type, listener) => {if (!listeners.has(type)) listeners.set(type, new Set());listeners.get(type).add(listener);},
    removeEventListener: (type, listener) => listeners.get(type)?.delete(listener),
    dispatchMediaEvent: (type, target) => {for (const listener of listeners.get(type) || []) listener({target});},
    querySelectorAll: selector => body.querySelectorAll(selector),
    querySelector: selector => body.querySelector(selector),
  };
  globalThis.requestAnimationFrame = callback => {callback();return 0;};
  try {await callback();} finally {globalThis.Node = oldNode;globalThis.document = oldDocument;globalThis.requestAnimationFrame = oldAnimationFrame;}
}

test('comparison view preserves media on identical refresh, then immediately releases invalidated current versions', async () => withDOM(async () => {
  const value = project(), view = createPerformanceComparison(value);
  const result = find(view.element, item => item.attributes.get('aria-label') === '当前成片');
  const timeline = find(view.element, item => item.attributes.get('type') === 'range');
  const play = find(view.element, item => item.tagName === 'button');
  result.duration = 8;result.dispatchEvent(new Event('durationchange'));
  assert.match(view.element.textContent, /媒体时长不同/);
  play.dispatchEvent(new Event('click'));await flush();
  timeline.value = '3.25';timeline.dispatchEvent(new Event('input'));
  assert.equal(result.currentTime, 3.25);
  view.update(structuredClone(value));
  assert.equal(find(view.element, item => item.attributes.get('aria-label') === '当前成片'), result);
  value.nodes[1].status = 'dirty';view.update(value);
  assert.equal(result.paused, true);assert.equal(result.loadCalls, 1);
  assert.equal(find(view.element, item => item.tagName === 'video'), null);
  assert.match(view.element.textContent, /等待当前配置的成片/);
  view.dispose();
}));

test('comparison DOM starts on final sound, switches original sound and cleans up on navigation', async () => withDOM(async () => {
  const aborter = new AbortController(), view = createPerformanceComparison(project(), {signal: aborter.signal});
  const play = find(view.element, item => item.tagName === 'button');
  const tracks = find(view.element, item => item.tagName === 'select');
  const result = find(view.element, item => item.attributes.get('aria-label') === '当前成片');
  const original = find(view.element, item => item.tagName === 'audio');
  assert.equal(tracks.value, 'result');
  play.dispatchEvent(new Event('click'));await flush();
  assert.deepEqual(audible(result, original), [result]);
  tracks.value = 'reference';tracks.dispatchEvent(new Event('change'));await flush();
  assert.deepEqual(audible(result, original), [original]);
  aborter.abort();assert.ok(result.paused && original.paused);
  assert.equal(result.loadCalls, 1);view.dispose();assert.equal(result.loadCalls, 1);
}));

test('saved B parameters override model defaults only for the currently saved voice/model selection', () => {
  const configuration = {voice_id: 'voice', voice_model_version_id: 'baseline', voice_parameters: {diffusion_steps: 50, inference_cfg_rate: 0.7}};
  const baseline = {id: 'baseline', parameters: {diffusion_steps: 30, inference_cfg_rate: 0.7, pitch: 0, auto_f0_adjust: false}};
  assert.match(singingParameterSummary(configuration, 'voice', baseline), /^当前已保存参数：50 步 · CFG 0\.7/);
  assert.match(singingParameterSummary(configuration, 'other-voice', baseline), /^所选版本参数：30 步/);
  assert.match(singingParameterSummary(configuration, 'voice', {...baseline, id: 'different-version'}), /^所选版本参数：30 步/);
  assert.doesNotMatch(singingParameterSummary(configuration, 'voice', null), /50/);
  assert.equal(baseline.parameters.diffusion_steps, 30);
});

async function workbench() {
  const value = project();
  Object.assign(value, {id: 'one', title: 'Current performance', mode: 'performance_remix'});
  Object.assign(value.performance_config, {character_id: 'character', voice_id: 'voice', voice_model_version_id: 'baseline', scene_prompt: 'Stage one'});
  const models = [{id: 'baseline', number: 1, engine: 'seed-vc', sample_rate: 44100, fingerprint: '1234567890abcdef', parameters: {diffusion_steps: 30}}];
  const polls = [], cleanup = [], aborter = new AbortController(), requests = [];
  let latest = structuredClone(value), assetDelay = null, jobsFailure = false;
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async url => {
    requests.push(url);
    let response;
    if (url === '/characters') {
      if (assetDelay) await assetDelay;
      response = [{id: 'character', name: 'Character', consent_confirmed: true}];
    } else if (url === '/voices') response = [{id: 'voice', name: 'Voice', consent_confirmed: true}];
    else if (url === '/media-assets') response = [];
    else if (url === '/singing-models?voice_id=voice') response = {models};
    else if (url === '/projects/one/performance/readiness') response = {};
    else if (url === '/projects/one/status') response = latest;
    else if (url === '/projects/one/jobs') {if (jobsFailure) throw new Error('Jobs request failed');response = [];}
    else if (url === '/projects/one/chat') response = [];
    else if (url === '/voices/voice/singing-reference-samples') response = {reference_samples: [{exists: true, path: 'assets/voices/voice/reference.wav', duration_s: 12}]};
    else throw new Error('Unexpected API request: ' + url);
    return new Response(JSON.stringify(response), {headers: {'Content-Type': 'application/json'}});
  };
  try {
    const root = await renderProject({signal: aborter.signal, projects: [], onDispose: callback => cleanup.push(callback), poll: (callback, interval) => polls.push({callback, interval})}, value);
    document.body.append(root);
    return {root, requests, aborter, snapshot: () => structuredClone(latest), refresh: () => polls.find(item => item.interval === 4000).callback(),
      receive: next => {latest = next;}, delayAssets: promise => {assetDelay = promise;}, rejectJobs: () => {jobsFailure = true;},
      dispose: () => {aborter.abort();for (const callback of cleanup) callback();globalThis.fetch = previousFetch;}};
  } catch (error) {aborter.abort();globalThis.fetch = previousFetch;throw error;}
}

test('the real workbench invalidates a playing comparison before refreshed asset requests settle or fail', async () => withDOM(async () => {
  const page = await workbench();
  try {
    const comparison = find(page.root, item => item.attributes.get('aria-label') === '原片与成片同步对照');
    const result = find(comparison, item => item.attributes.get('aria-label') === '当前成片');
    find(comparison, item => item.tagName === 'button').dispatchEvent(new Event('click'));await flush();
    assert.equal(result.paused, false);
    const next = page.snapshot();next.performance_config.scene_prompt = 'Changed stage';next.nodes[1].status = 'dirty';
    page.receive(next);
    const assets = deferred();page.delayAssets(assets.promise);
    const refreshed = page.refresh();await flush();
    assert.equal(page.requests.filter(url => url === '/characters').length, 2, 'refresh is blocked on an actual asset request');
    assert.equal(result.paused, true);
    assert.equal(result.loadCalls, 1);
    assert.equal(find(comparison, item => item.tagName === 'video'), null);
    assets.reject(new Error('Asset request failed'));
    await assert.rejects(refreshed);
    assert.equal(result.paused, true);
  } finally {page.dispose();}
}));

test('body-mounted voice sample dialog and the workbench comparison share playback focus and navigation cleanup', async () => withDOM(async () => {
  const page = await workbench();
  try {
    const comparison = find(page.root, item => item.attributes.get('aria-label') === '原片与成片同步对照');
    const result = find(comparison, item => item.attributes.get('aria-label') === '当前成片');
    const play = find(comparison, item => item.tagName === 'button');
    play.dispatchEvent(new Event('click'));await flush();
    assert.equal(result.paused, false);
    const create = find(page.root, item => item.tagName === 'button' && item.textContent === '用声音样本创建版本');
    create.dispatchEvent(new Event('click'));await flush();
    const dialog = document.body.querySelector('dialog');
    assert.ok(dialog && !page.root.contains(dialog));
    const sample = dialog.querySelector('audio');
    await sample.play();
    assert.equal(result.paused, true);assert.equal(sample.paused, false);
    play.dispatchEvent(new Event('click'));await flush();
    assert.equal(result.paused, false);assert.equal(sample.paused, true);
    await sample.play();dialog.close();
    assert.equal(sample.paused, true, 'closing a sample dialog stops its audio');
    await sample.play();page.aborter.abort();
    assert.ok(result.paused && sample.paused, 'navigation stops dialog and workbench media');
  } finally {page.dispose();}
}));

test('jobs failure followed by an actual form edit cannot revive an invalidated comparison', async () => withDOM(async () => {
  const page = await workbench();
  try {
    const comparison = find(page.root, item => item.attributes.get('aria-label') === '原片与成片同步对照');
    const oldResult = find(comparison, item => item.attributes.get('aria-label') === '当前成片');
    find(comparison, item => item.tagName === 'button').dispatchEvent(new Event('click'));await flush();
    assert.equal(oldResult.paused, false);
    const next = page.snapshot();next.nodes[1].status = 'dirty';
    page.receive(next);page.rejectJobs();
    await assert.rejects(page.refresh());await flush();
    assert.equal(oldResult.paused, true);
    assert.equal(find(comparison, item => item.tagName === 'video'), null);
    const scene = find(page.root, item => item.tagName === 'textarea' && item.attributes.get('name') === 'scene');
    scene.value = 'My unsaved new scene';
    const edit = new Event('input', {bubbles: true});
    Object.defineProperty(edit, 'target', {value: scene});
    page.root.dispatchEvent(edit);
    assert.match(page.root.textContent, /输入已修改，请先保存配置/, 'the edit reaches the real paint path');
    assert.equal(Boolean(find(comparison, item => item.tagName === 'video')), false, 'paint must retain the newest received dirty status');
  } finally {page.dispose();}
}));
