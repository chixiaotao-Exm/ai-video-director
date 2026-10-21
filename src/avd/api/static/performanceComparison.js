import {mediaURL} from './api.js';
import {h, button, field, input, select, media} from './components.js';

function outputPath(value, pattern) {
  if (typeof value !== 'string' || !pattern.test(value)) return null;
  try {mediaURL(value);return value;} catch {return null;}
}

function currentOutput(project, kind, videoKey) {
  const node = project.nodes?.find(item => item.kind === kind);
  const version = node?.current_version;
  if (node?.status !== 'clean' || !node.current_version_id || version?.id !== node.current_version_id
    || version.node_id !== node.id) return null;
  const video = outputPath(version.output_paths?.[videoKey], /\.(mp4|webm|mov|mkv)$/i);
  if (!video || node.output_paths?.[videoKey] !== video) return null;
  const audio = kind === 'performance_prepare' ? outputPath(version.output_paths?.audio, /\.(wav|mp3|flac|m4a|ogg)$/i) : null;
  return {nodeId: node.id, versionId: version.id, video, audio: audio && node.output_paths?.audio === audio ? audio : null};
}

export function performanceComparisonSources(project) {
  const reference = currentOutput(project, 'performance_prepare', 'motion');
  const result = currentOutput(project, 'performance_final', 'video');
  return {reference, result, key: JSON.stringify([reference, result])};
}

// Both videos use the prepared interval's zero-based clock. The result is the
// master; the reference sound is separate because prepared motion is silent.
export function synchronizePerformanceMedia({reference, result, originalAudio = null, signal, onChange = () => {}, onError = () => {}, scheduler = globalThis}) {
  const players = [reference, result, originalAudio].filter(Boolean);
  const listeners = [];
  const expectedSeeks = new Map();
  let disposed = false;
  let playing = false;
  let pending = false;
  let epoch = 0;
  let timer = null;
  let track = 'result';
  let error = '';
  const duration = () => players.every(player => Number.isFinite(player.duration) && player.duration > 0)
    ? Math.min(...players.map(player => player.duration)) : 0;
  const clamp = value => Math.min(duration(), Math.max(0, Number.isFinite(value) ? value : 0));
  const state = () => ({ready: !disposed && duration() > 0 && !players.some(player => player.error), playing, pending, track, duration: duration(), time: clamp(result.currentTime), error});
  const publish = () => {if (!disposed) onChange(state());};
  function listen(player, name, callback) {
    player.addEventListener(name, callback);
    listeners.push(() => player.removeEventListener(name, callback));
  }
  function applyTrack() {
    // Mute the old track before unmuting a new one, including rapid switches.
    for (const player of players) if (!player.muted) player.muted = true;
    const audible = track === 'result' ? result : track === 'reference' ? originalAudio : null;
    if (audible) audible.muted = false;
  }
  function setTime(player, value) {
    if (Math.abs(player.currentTime - value) < 0.001) return;
    expectedSeeks.set(player, value);
    player.currentTime = value;
  }
  function pause() {
    ++epoch;playing = false;pending = false;
    if (timer !== null) {scheduler.clearInterval(timer);timer = null;}
    for (const player of players) player.pause();
    publish();
  }
  function fail(message) {pause();error = message;publish();if (!disposed) onError(message);}
  function seek(value) {
    if (disposed || !duration()) return;
    const time = clamp(Number(value));
    try {for (const player of players) setTime(player, time);}
    catch {fail('媒体尚不能定位，请等待加载后重试。');}
    publish();
  }
  function synchronize() {
    if (disposed || !duration()) return;
    const time = clamp(result.currentTime);
    if (playing && time >= duration()) {pause();seek(duration());return;}
    try {
      for (const player of players) {
        if (player !== result && Math.abs(player.currentTime - time) > 0.12) setTime(player, time);
        if (player.playbackRate !== result.playbackRate) player.playbackRate = result.playbackRate;
      }
    } catch {fail('媒体同步失败，请暂停后重试。');}
    publish();
  }
  async function play() {
    if (disposed || signal?.aborted) return false;
    if (!state().ready) {fail('请等待两段视频和原声音频加载完成。');return false;}
    const request = ++epoch;
    playing = true;pending = true;error = '';
    if (result.ended || result.currentTime >= duration() - 0.02) seek(0);
    else seek(result.currentTime);
    applyTrack();
    const active = [reference, result, ...(track === 'reference' && originalAudio ? [originalAudio] : [])];
    if (originalAudio && track !== 'reference') originalAudio.pause();
    publish();
    try {
      // Invoke all play calls in the user gesture before awaiting any promise.
      await Promise.all(active.map(player => {
        try {
          return Promise.resolve(player.play()).then(() => {
            // Another media promise may already have failed, or this player may
            // finish starting after a pause, track switch or page disposal.
            if (disposed || !playing || (player === originalAudio && track !== 'reference')) player.pause();
          });
        } catch (failure) {return Promise.reject(failure);}
      }));
      if (disposed || request !== epoch) {
        if (disposed || !playing) for (const player of players) player.pause();
        return false;
      }
      pending = false;
      if (timer === null) timer = scheduler.setInterval(synchronize, 100);
      synchronize();
      return true;
    } catch {
      if (!disposed && request === epoch) fail('浏览器未能同时播放媒体，请检查加载状态后重试。');
      return false;
    }
  }
  function setTrack(value) {
    if (disposed || !['result', 'reference', 'mute'].includes(value) || (value === 'reference' && !originalAudio)) return;
    const resume = playing;
    pause();track = value;applyTrack();publish();
    if (resume) void play();
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    for (const remove of listeners) remove();
    signal?.removeEventListener('abort', dispose);
    pause();
    for (const player of players) {player.removeAttribute('src');player.load();}
  }
  for (const player of players) {
    listen(player, 'loadedmetadata', publish);
    listen(player, 'durationchange', publish);
    listen(player, 'timeupdate', () => {if (player === result) synchronize();});
    listen(player, 'seeking', () => {
      if (player !== result) return;
      const expected = expectedSeeks.get(player);
      if (expected != null && Math.abs(player.currentTime - expected) < 0.12) return;
      seek(player.currentTime);
    });
    listen(player, 'seeked', () => {expectedSeeks.delete(player);if (player === result) synchronize();});
    listen(player, 'play', () => {
      if (player === originalAudio && track !== 'reference') {player.pause();return;}
      if (!playing) player.pause();
    });
    listen(player, 'pause', () => {
      if (playing && !pending && (player !== originalAudio || track === 'reference')) pause();
    });
    listen(player, 'ended', () => {pause();seek(duration());});
    listen(player, 'waiting', () => {if (playing && !pending) fail('媒体正在缓冲，请稍后重新播放。');});
    listen(player, 'error', () => fail('媒体加载或解码失败，请检查当前产物。'));
    listen(player, 'volumechange', () => {
      const audible = track === 'result' ? result : track === 'reference' ? originalAudio : null;
      if (players.some(item => item.muted !== (item !== audible))) applyTrack();
    });
  }
  applyTrack();
  signal?.addEventListener('abort', dispose, {once: true});
  if (signal?.aborted) dispose();
  else publish();
  return {play, pause, seek, setTrack, synchronize, dispose, getState: state};
}

export function createPerformanceComparison(project, {signal} = {}) {
  const element = h('section', {class: 'card panel stack performance-comparison', 'aria-label': '原片与成片同步对照'});
  let signature;
  let controller = null;
  let disposed = false;
  function update(latest) {
    if (disposed) return;
    const sources = performanceComparisonSources(latest);
    if (signature === sources.key) return;
    signature = sources.key;
    controller?.dispose();controller = null;
    const title = h('h2', {}, '原片与成片同步对照');
    if (!sources.reference || !sources.result) {
      element.replaceChildren(title, h('p', {class: 'muted', role: 'status'}, !sources.reference
        ? '准备参考片后，再生成当前区间成片即可同步对照。'
        : '参考片已准备，等待当前配置的成片。历史或失败结果不会进入同步对照。'));
      return;
    }
    const reference = media(sources.reference.video, 'video', {controls: false, muted: true, 'aria-label': '原片动作'});
    const result = media(sources.result.video, 'video', {controls: false, 'aria-label': '当前成片'});
    const originalAudio = sources.reference.audio ? media(sources.reference.audio, 'audio', {controls: false, hidden: true, muted: true}) : null;
    const playButton = button('同时播放', () => controller?.getState().playing ? controller.pause() : controller?.play(), {disabled: true});
    const timeline = input('range', 0, {min: 0, max: 0, step: 0.01, disabled: true, 'aria-label': '同步播放进度'});
    const clock = h('span', {class: 'muted'}, '0.00 / 0.00 秒');
    const status = h('p', {class: 'muted', role: 'status'}, '正在读取媒体…');
    const tracks = select([{value: 'result', label: '只听成片'}, {value: 'reference', label: '只听原唱', disabled: !originalAudio}, {value: 'mute', label: '全部静音'}], 'result', {'aria-label': '对照音轨'});
    timeline.addEventListener('input', () => controller?.seek(Number(timeline.value)));
    tracks.addEventListener('change', () => controller?.setTrack(tracks.value));
    element.replaceChildren(title,
      h('p', {class: 'muted'}, '两侧从所选区间的起点同步播放；默认只听成片。观看对照不会自动确认音色或画面质量。'),
      h('div', {class: 'performance-assets'}, h('article', {class: 'stack'}, h('h3', {}, '原片动作与原唱'), reference), h('article', {class: 'stack'}, h('h3', {}, '当前人物、场景与歌声'), result)),
      originalAudio, h('div', {class: 'performance-actions'}, playButton, field('听哪条音轨', tracks)),
      field('同步进度', timeline), clock, status,
    );
    controller = synchronizePerformanceMedia({reference, result, originalAudio, signal, onChange: state => {
      playButton.disabled = !state.ready;playButton.textContent = state.playing ? '同时暂停' : '同时播放';
      timeline.disabled = !state.ready;timeline.max = state.duration;timeline.value = state.time;
      clock.textContent = `${state.time.toFixed(2)} / ${state.duration.toFixed(2)} 秒`;
      const differentLengths = state.ready && Math.max(reference.duration, result.duration, originalAudio?.duration || 0) - state.duration > 0.1;
      const message = state.error || (!state.ready ? '正在读取媒体…' : state.pending ? '正在同步启动…' : differentLengths
        ? '媒体时长不同，同步对照会在较短媒体的结尾停止。' : '共用一条时间轴，每次只播放所选音轨。');
      if (status.textContent !== message) status.textContent = message;
    }});
  }
  function dispose() {
    if (disposed) return;
    disposed = true;controller?.dispose();signal?.removeEventListener('abort', dispose);
  }
  signal?.addEventListener('abort', dispose, {once: true});
  if (signal?.aborted) dispose();else update(project);
  return {element, update, pause: () => controller?.pause(), dispose};
}
