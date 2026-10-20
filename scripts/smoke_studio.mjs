/** Exercise the Vue studio with an isolated, network-fenced headless Edge, using only Node built-ins.
 *
 * node scripts/smoke_studio.mjs --base-url http://127.0.0.1:8010 --manifest data/studio-preview/ui-manifest.json
 * First run scripts/prepare_ui_demo.py, then serve its manifest's data/database.
 */
import {spawn} from "node:child_process";
import {access, mkdir, mkdtemp, readFile, readdir, realpath, rename, writeFile} from "node:fs/promises";
import {dirname, isAbsolute, join, relative, resolve, sep} from "node:path";
import {fileURLToPath} from "node:url";
import {setTimeout as delay} from "node:timers/promises";

async function repoRoot() {
  let current = dirname(fileURLToPath(import.meta.url));
  while (true) {
    try {
      await Promise.all([access(join(current, "AGENTS.md")), access(join(current, "pyproject.toml"))]);
      return await realpath(current);
    } catch {
      const parent = dirname(current);
      if (parent === current) throw new Error("The capture script must live inside the repository");
      current = parent;
    }
  }
}

function within(root, path) {
  const target = resolve(path);
  const rel = relative(root, target);
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error("Capture paths must remain inside this repository");
  }
  return target;
}

function options(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === "--help") return {help: true};
    if (!["--base-url", "--manifest", "--output", "--edge", "--timeout-ms"].includes(key) || !argv[index + 1]) {
      throw new Error(`Unknown or incomplete argument: ${key}`);
    }
    result[key.slice(2)] = argv[++index];
  }
  return result;
}

async function findEdge(explicit) {
  const candidates = explicit ? [explicit] : [
    process.env.AVD_EDGE_EXECUTABLE,
    process.env["ProgramFiles(x86)"] && join(process.env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe"),
    process.env.ProgramFiles && join(process.env.ProgramFiles, "Microsoft", "Edge", "Application", "msedge.exe"),
    process.env.LOCALAPPDATA && join(process.env.LOCALAPPDATA, "Microsoft", "Edge", "Application", "msedge.exe"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { await access(candidate); return candidate; } catch { /* Try the next installed path. */ }
  }
  throw new Error("Microsoft Edge was not found; pass --edge with its executable path");
}

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener("message", event => {
      const value = JSON.parse(String(event.data));
      if (value.id) {
        const waiting = this.pending.get(value.id);
        if (!waiting) return;
        clearTimeout(waiting.timer);
        this.pending.delete(value.id);
        if (value.error) waiting.reject(new Error(`${waiting.method}: ${value.error.message}`));
        else waiting.resolve(value.result || {});
      } else {
        for (const handler of this.listeners.get(value.method) || []) handler(value.params || {});
      }
    });
    socket.addEventListener("close", () => {
      for (const waiting of this.pending.values()) {
        clearTimeout(waiting.timer);
        waiting.reject(new Error(`Browser closed during ${waiting.method}`));
      }
      this.pending.clear();
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, {once: true});
      socket.addEventListener("error", () => reject(new Error("Could not connect to Edge debugging socket")), {once: true});
    });
    return new CDP(socket);
  }

  on(method, handler) {
    const values = this.listeners.get(method) || [];
    values.push(handler);
    this.listeners.set(method, values);
  }

  send(method, params = {}, timeout = 30000) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timed out waiting for ${method}`));
      }, timeout);
      this.pending.set(id, {resolve, reject, timer, method});
      this.socket.send(JSON.stringify({id, method, params}));
    });
  }

  async evaluate(expression, awaitPromise = false) {
    const result = await this.send("Runtime.evaluate", {expression, awaitPromise, returnByValue: true});
    if (result.exceptionDetails) throw new Error("Browser evaluation failed; see report exceptions");
    return result.result?.value;
  }
}

function diagnostic(value) {
  return String(value || "")
    .replace(/\b(?:sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+)\b/g, "[redacted]")
    .replace(/Bearer\s+\S+/gi, "Bearer [redacted]")
    .replace(/((?:api[_-]?key|token|secret|authorization)["'\s:=]+)[^\s,"'}]+/gi, "$1[redacted]")
    .replace(/(https?:\/\/[^\s?#]+)[?#][^\s]*/g, "$1")
    .slice(0, 1000);
}

async function waitUntil(check, timeout, message) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await delay(100);
  }
  throw new Error(message);
}

const root = await repoRoot();
const opts = options(process.argv.slice(2));
if (opts.help) {
  console.log('node scripts/smoke_studio.mjs [--base-url http://127.0.0.1:8010] [--manifest data/studio-preview/ui-manifest.json] [--output docs/ui/studio] [--edge PATH] [--timeout-ms 30000]');
  process.exit(0);
}
const base = new URL(opts['base-url'] || 'http://127.0.0.1:8010');
if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(base.hostname) || base.username || base.password) {
  throw new Error('Studio smoke requires an unauthenticated localhost HTTP server');
}
base.pathname = '/'; base.search = ''; base.hash = '';
const manifestPath = within(root, await realpath(within(root, resolve(root, opts.manifest || 'data/studio-preview/ui-manifest.json'))));
const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
if (!manifest.offline || typeof manifest.project_id !== 'string' || !manifest.project_id) throw new Error('An explicitly offline fixture manifest is required');
let output = within(root, resolve(root, opts.output || 'docs/ui/studio'));
await mkdir(output, {recursive: true}); output = within(root, await realpath(output));
const timeout = Number(opts['timeout-ms'] || 30000);
if (!Number.isFinite(timeout) || timeout < 1000 || timeout > 120000) throw new Error('timeout-ms must be between 1000 and 120000');
let profileRoot = within(root, join(root, 'data', 'ui-browser'));
await mkdir(profileRoot, {recursive: true}); profileRoot = within(root, await realpath(profileRoot));
const profile = await mkdtemp(join(profileRoot, 'studio-smoke-'));
const downloadPath = within(root, join(profile, 'downloads'));
await mkdir(downloadPath);
const browser = spawn(await findEdge(opts.edge), [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--window-size=1440,900',
  '--force-color-profile=srgb', '--disable-background-networking', '--disable-component-update',
  '--disable-default-apps', '--disable-domain-reliability', '--disable-sync',
  '--disable-features=AutofillServerCommunication,CertificateTransparencyComponentUpdater,OptimizationHints,MediaRouter',
  '--metrics-recording-only', '--no-first-run', '--no-default-browser-check', 'about:blank',
], {cwd: root, stdio: 'ignore', windowsHide: true});
let launchError;
browser.on('error', error => { launchError = error; });
let cdp;
let cleanupPromise;
const report = {
  started_at: new Date().toISOString(), browser: 'Microsoft Edge', base_url: base.origin,
  fixture: relative(root, manifestPath).replaceAll('\\', '/'), steps: [], exceptions: [],
  console_errors: [], expected_network_errors: [], blocked_requests: [], simulated_requests: [], writes: [],
  delete_confirmations: [], unexpected_dialogs: [],
};
const suffix = Date.now().toString(36);
const title = `Studio 离线唱跳验收 ${suffix}`;
const lyrics = '灯亮起来，我跟着节拍\n把今天写成自己的舞台';
const style = '明亮电子流行，中文副歌，轻快但不急促';
const chatPath = `/projects/${manifest.project_id}/chat`;
const expectedFailureURLs = new Set();
const requestCounts = new Map();
let failChat = false;
let initialJobs;
let initialConsent;
let initialSpend;
let createdProjectId;
let initialProjectIds;
let allowFixtureDelete = false;
let expectDeleteConfirm = false;
let failProjectList = 0;

function assert(value, message) { if (!value) throw new Error(message); }
function query(selector) { return `document.querySelector(${JSON.stringify(selector)})`; }
function button(text, scope = 'document') {
  return `Array.from((${scope}).querySelectorAll('button')).find(item => item.textContent.trim() === ${JSON.stringify(text)} && item.getClientRects().length && !item.closest('[hidden]'))`;
}
async function read(path) {
  const response = await fetch(new URL(path, base), {signal: AbortSignal.timeout(timeout)});
  if (!response.ok) throw new Error(`Read-only fixture verification failed: ${path} (${response.status})`);
  return response.json();
}
async function step(name, action) {
  const item = {name, passed: false}; report.steps.push(item);
  try { const details = await action(); item.passed = true; if (details) item.details = details; console.log(`PASS ${name}`); }
  catch (error) { item.error = diagnostic(error.message); throw error; }
}
async function waitDOM(expression, message) {
  await waitUntil(async () => { try { return await cdp.evaluate(expression); } catch { return false; } }, timeout, message);
}
async function go(route) {
  const previous = await cdp.evaluate('location.hash');
  await cdp.evaluate('window.__AVD_READY = false');
  if (previous === route) await cdp.send('Page.reload', {ignoreCache: true});
  else await cdp.send('Page.navigate', {url: `${base.href}${route}`});
  await waitDOM('window.__AVD_READY === true && !!document.querySelector("#studio-app .studio-shell")', `Studio route did not mount: ${route}`);
  assert(await cdp.evaluate('!document.querySelector(".legacy-host .empty-state h2")'), 'The legacy workspace reported a load error');
}
async function reload() {
  await cdp.evaluate('window.__AVD_READY = false');
  await cdp.send('Page.reload', {ignoreCache: true});
  await waitDOM('window.__AVD_READY === true', 'Reload did not become ready');
}
async function click(expression) {
  const found = await cdp.evaluate(`(() => {
    const element = (${expression});
    if (!element || element.disabled || element.closest('[hidden]')) return false;
    element.scrollIntoView({block:'center', inline:'center'});
    return true;
  })()`);
  assert(found, `The UI control is missing, hidden, or disabled: ${expression}`);
  let point;
  let previous;
  await waitUntil(async () => {
    point = await cdp.evaluate(`(() => {
      const element = (${expression});
      if (!element || element.disabled) return null;
      const rect = element.getBoundingClientRect();
      const x = rect.x + rect.width/2, y = rect.y + rect.height/2;
      const hit = document.elementFromPoint(x,y);
      return rect.width && rect.height && (hit === element || element.contains(hit)) ? {x,y} : null;
    })()`);
    const stable = point && previous && Math.abs(point.x - previous.x) < .5 && Math.abs(point.y - previous.y) < .5;
    previous = point;
    return stable;
  }, timeout, `The UI control did not become visible and stable: ${expression}`);
  await cdp.send('Input.dispatchMouseEvent', {type:'mouseMoved', ...point});
  await cdp.send('Input.dispatchMouseEvent', {type:'mousePressed', button:'left', clickCount:1, ...point});
  await cdp.send('Input.dispatchMouseEvent', {type:'mouseReleased', button:'left', clickCount:1, ...point});
}
async function fill(expression, value) {
  const accepted = await cdp.evaluate(`(() => {
    const element = (${expression});
    if (!element || element.disabled || element.closest('[hidden]')) return false;
    element.scrollIntoView({block:'center'}); element.focus();
    const prototype = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,'value').set.call(element,${JSON.stringify(String(value))});
    element.dispatchEvent(new Event('input',{bubbles:true}));
    element.dispatchEvent(new Event('change',{bubbles:true}));
    return element.value === ${JSON.stringify(String(value))};
  })()`);
  assert(accepted, 'The form did not accept the intended value');
}
async function theme(id) {
  await click(query('.theme-trigger'));
  await waitDOM(`!!${query(`[data-theme-option="${id}"]`)}`, 'Theme menu did not open');
  await click(query(`[data-theme-option="${id}"]`));
  await waitDOM(`document.documentElement.dataset.theme === ${JSON.stringify(id)} && localStorage.getItem('avd.studio.theme') === ${JSON.stringify(id)} && !document.querySelector('.theme-popover')`, `Theme ${id} did not apply and persist`);
}
async function cleanup() {
  if (cleanupPromise) return cleanupPromise;
  cleanupPromise = (async () => {
    if (cdp?.socket.readyState === WebSocket.OPEN) {
      try { await cdp.send('Browser.close', {}, 2000); } catch { /* Browser may close first. */ }
    }
    try { await waitUntil(() => browser.exitCode !== null || browser.signalCode !== null, 2000, 'Browser still exiting'); }
    catch {
      if (browser.pid && process.platform === 'win32') {
        const killer = spawn('taskkill.exe', ['/PID', String(browser.pid), '/T', '/F'], {windowsHide:true, stdio:'ignore'});
        await new Promise(resolve => { killer.once('exit', resolve); killer.once('error', resolve); });
      } else if (browser.pid) browser.kill('SIGKILL');
    }
    cdp?.socket.close();
  })();
  return cleanupPromise;
}
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { void cleanup().finally(() => process.exit(signal === 'SIGINT' ? 130 : 143)); });

try {
  await step('isolated fixture has no keys, workers, or pending jobs', async () => {
    const settings = await read('/settings');
    assert(Object.values(settings.secrets).every(value => !value.configured), 'Configured provider keys are not allowed in this fixture');
    assert(resolve(settings.settings.data_dir).toLowerCase() === resolve(manifest.data_dir).toLowerCase(), 'The server is not serving the isolated manifest data directory');
    within(root, await realpath(manifest.data_dir));
    initialJobs = await read('/jobs?limit=1000');
    assert(initialJobs.every(job => !['queued', 'running'].includes(job.status)), 'Pending jobs are forbidden');
    assert((await read('/workers')).workers.every(worker => worker.state === 'stopped'), 'Workers must remain stopped');
    const [characters, voices] = await Promise.all([read('/characters'), read('/voices')]);
    initialConsent = JSON.stringify([characters, voices].map(items => items.map(item => [item.id, item.consent_confirmed])));
    const initialProjects = await read('/projects');
    initialProjectIds = initialProjects.map(project => project.id).sort();
    const fixtureProject = initialProjects.find(project => project.id === manifest.project_id);
    assert(fixtureProject, 'The fixture project is not present on this server');
    initialSpend = fixtureProject.spent;
    return {offline:true, provider_keys_configured:false, workers_running:0, pending_jobs:0};
  });
  let port;
  await waitUntil(async () => {
    if (launchError) throw launchError;
    if (browser.exitCode !== null) throw new Error('Edge exited before debugging became available');
    try { port = Number((await readFile(join(profile,'DevToolsActivePort'),'utf8')).trim().split(/\r?\n/)[0]); return port > 0; }
    catch (error) { if (error.code === 'ENOENT') return false; throw error; }
  }, timeout, 'Edge debugging port was not ready');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  cdp = await CDP.connect(targets.find(item => item.type === 'page').webSocketDebuggerUrl);
  cdp.on('Runtime.exceptionThrown', value => report.exceptions.push(diagnostic(value.exceptionDetails?.exception?.description || value.exceptionDetails?.text)));
  cdp.on('Runtime.consoleAPICalled', value => { if (value.type === 'error') report.console_errors.push(diagnostic(value.args?.map(item => item.value ?? item.description).join(' '))); });
  cdp.on('Log.entryAdded', value => {
    const entry = value.entry;
    if (entry?.level !== 'error') return;
    if (entry.source === 'network' && expectedFailureURLs.has(entry.url) && /502/.test(entry.text)) report.expected_network_errors.push({path:new URL(entry.url).pathname, status:502});
    else report.console_errors.push(diagnostic(entry.text));
  });
  cdp.on('Page.javascriptDialogOpening', value => {
    const accept = expectDeleteConfirm && value.type === 'confirm' && createdProjectId
      && createdProjectId !== manifest.project_id && value.message.includes(title);
    if (accept) {
      expectDeleteConfirm = false;
      report.delete_confirmations.push({project_id:createdProjectId, accepted:true, fixture_created_by_this_run:true});
    } else report.unexpected_dialogs.push({type:value.type, accepted:false});
    void cdp.send('Page.handleJavaScriptDialog', {accept:!!accept}).catch(() => {});
  });
  cdp.on('Fetch.requestPaused', value => {
    const url = new URL(value.request.url);
    const method = value.request.method;
    requestCounts.set(url.pathname, (requestCounts.get(url.pathname) || 0) + 1);
    if (url.origin === base.origin && failProjectList > 0 && url.pathname === '/projects' && method === 'GET') {
      failProjectList -= 1;
      expectedFailureURLs.add(url.href);
      report.simulated_requests.push({method,path:url.pathname,status:502,forwarded:false});
      void cdp.send('Fetch.fulfillRequest', {requestId:value.requestId,responseCode:502,
        responseHeaders:[{name:'Content-Type',value:'application/json'}],
        body:Buffer.from(JSON.stringify({detail:'离线浏览器验收：模拟项目列表刷新失败。'})).toString('base64')}).catch(() => {});
      return;
    }
    if (url.origin === base.origin && failChat && url.pathname === chatPath && ['GET','POST'].includes(method)) {
      expectedFailureURLs.add(url.href);
      report.simulated_requests.push({method, path:url.pathname, status:502, forwarded:false});
      void cdp.send('Fetch.fulfillRequest', {requestId:value.requestId, responseCode:502,
        responseHeaders:[{name:'Content-Type',value:'application/json'}],
        body:Buffer.from(JSON.stringify({detail:'离线浏览器验收：模拟连接中断，未调用模型。'})).toString('base64')}).catch(() => {});
      return;
    }
    let projectWrite = false;
    if (method === 'POST' && url.pathname === '/projects') {
      try { const body = JSON.parse(value.request.postData || '{}'); projectWrite = body.title === title && body.mode === 'remake' && Object.keys(body).every(key => ['title','mode'].includes(key)); } catch { /* Reject unknown writes. */ }
    }
    const safeBudgetRead = method === 'POST' && /^\/projects\/[^/]+\/tools\/get_budget$/.test(url.pathname);
    const fixtureDelete = allowFixtureDelete && method === 'DELETE' && createdProjectId
      && createdProjectId !== manifest.project_id && url.pathname === `/projects/${createdProjectId}`;
    const allowed = url.origin === base.origin && (['GET','HEAD'].includes(method) || projectWrite || safeBudgetRead || fixtureDelete);
    if (!allowed) report.blocked_requests.push({method, path:url.pathname, external:url.origin !== base.origin});
    else if (!['GET','HEAD'].includes(method)) {
      report.writes.push({method,path:url.pathname,operation:projectWrite ? 'create-empty-fixture-project' : fixtureDelete ? 'delete-own-empty-fixture-project' : 'read-budget'});
      if (fixtureDelete) allowFixtureDelete = false;
    }
    void cdp.send(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest', allowed ? {requestId:value.requestId} : {requestId:value.requestId,errorReason:'BlockedByClient'}).catch(() => {});
  });
  for (const domain of ['Page','Runtime','DOM','Network','Log']) await cdp.send(`${domain}.enable`);
  await cdp.send('Fetch.enable', {patterns:[{urlPattern:'http://*',requestStage:'Request'},{urlPattern:'https://*',requestStage:'Request'}]});
  await cdp.send('Browser.setDownloadBehavior', {behavior:'allow',downloadPath,eventsEnabled:true});
  await cdp.send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false});

  await step('quick theme menu applies all four themes and persists on reload', async () => {
    await go('#/');
    for (const id of ['midnight','glass','acid','editorial']) await theme(id);
    await reload();
    assert(await cdp.evaluate('document.documentElement.dataset.theme === "editorial"'), 'Reload lost the selected theme');
    return {themes:['midnight','glass','acid','editorial'], persisted:'editorial'};
  });

  await step('quick theme changes preserve an unsent legacy chat draft', async () => {
    await go(`#/projects/${manifest.project_id}/chat`);
    const draft = '这段草稿只用于主题切换验证，不发送给导演。';
    await fill(query('.chat-composer textarea[name="message"]'), draft);
    await cdp.evaluate('document.querySelector(".chat-composer textarea[name=message]").dataset.smokeDraft = "preserve"');
    for (const id of ['midnight','glass','acid','editorial']) {
      await theme(id);
      assert(await cdp.evaluate(`${query('.chat-composer textarea[name="message"]')}?.value === ${JSON.stringify(draft)} && ${query('.chat-composer textarea[name="message"]')}?.dataset.smokeDraft === 'preserve'`), 'A theme switch remounted the composer or lost its unsent draft');
    }
    return {themes_checked:4, draft_preserved:true, chat_post_requests:0};
  });

  await step('appearance page selects exactly one of four full templates', async () => {
    await go('#/appearance');
    assert(await cdp.evaluate('document.querySelectorAll(".theme-card").length === 4'), 'Appearance must expose four templates');
    for (const id of ['midnight','glass','acid','editorial']) {
      await click(`${query(`[data-preview-theme="${id}"]`)}.closest('button')`);
      await waitDOM(`document.documentElement.dataset.theme === ${JSON.stringify(id)} && document.querySelectorAll('.theme-card[aria-pressed="true"]').length === 1 && ${query(`[data-preview-theme="${id}"]`)}.closest('button').getAttribute('aria-pressed') === 'true'`, 'Appearance selection did not match the root theme');
    }
    return {templates:4, selected_aria_pressed:1};
  });

  await step('performance presets update the actual selected model pipeline', async () => {
    await go('#/performance');
    assert(await cdp.evaluate('document.querySelectorAll(".preset-card").length === 3'), 'Performance must expose three presets');
    for (const [index, model] of [[0,'Eleven Music'],[1,'ACE-Step'],[2,'Wan Dancer']]) {
      await click(`document.querySelectorAll('.preset-card')[${index}]`);
      await waitDOM(`document.querySelectorAll('.preset-card[aria-pressed="true"]').length === 1 && document.querySelectorAll('.preset-card')[${index}].getAttribute('aria-pressed') === 'true' && document.querySelector('.pipeline').textContent.includes(${JSON.stringify(model)})`, 'Selected preset and pipeline did not agree');
    }
    assert(await cdp.evaluate('document.querySelector(".pipeline").textContent.includes("待接入") && document.querySelector(".pipeline").textContent.includes("FFmpeg")'), 'Pipeline must distinguish recommendations from existing FFmpeg capability');
    return {presets:3, selected:'local-performance', capability_boundaries_visible:true};
  });

  await step('performance draft restores fields but never restores consent', async () => {
    for (const [selector, value] of [['#mv-title',title],['#mv-style',style],['#mv-lyrics',lyrics],['#mv-duration',60],['#mv-aspect','16:9'],['#mv-voice','clone']]) await fill(query(selector), value);
    await waitDOM('!!document.querySelector(".rights-check input")', 'Voice rights checkbox did not appear');
    assert(await cdp.evaluate('!document.querySelector(".rights-check input").checked'), 'Voice rights checkbox must start unchecked');
    await click(query('.rights-check input'));
    await click(button('保存草案'));
    const draft = await cdp.evaluate('JSON.parse(localStorage.getItem("avd.performance-plan"))');
    assert(draft.version === 1 && draft.preset === 'local-performance' && draft.plan.title === title && draft.plan.style === style && draft.plan.lyrics === lyrics && draft.plan.voice === 'clone', 'Draft storage did not contain the entered values');
    assert(!Object.keys(draft).some(key => /consent|right|key|secret/i.test(key)) && !Object.keys(draft.plan).some(key => /consent|right|key|secret/i.test(key)), 'Draft must not persist consent or credentials');
    await reload();
    const restored = await cdp.evaluate('({title:document.querySelector("#mv-title").value,style:document.querySelector("#mv-style").value,lyrics:document.querySelector("#mv-lyrics").value,duration:document.querySelector("#mv-duration").value,aspect:document.querySelector("#mv-aspect").value,voice:document.querySelector("#mv-voice").value,checked:document.querySelector(".rights-check input").checked})');
    assert(restored.title === title && restored.style === style && restored.lyrics === lyrics && restored.duration === '60' && restored.aspect === '16:9' && restored.voice === 'clone' && !restored.checked, 'Reload lost plan fields or incorrectly restored rights confirmation');
    return {restored_fields:7, voice:'clone', consent_after_reload:false, storage_key:'avd.performance-plan'};
  });

  await step('plan download contains the brief and stays inside the repository', async () => {
    await click(button('导出清单'));
    let downloaded;
    await waitUntil(async () => { downloaded = (await readdir(downloadPath)).find(name => name.endsWith('.md')); return !!downloaded; }, timeout, 'The planning checklist was not downloaded');
    const file = within(root, await realpath(join(downloadPath,downloaded)));
    const markdown = await readFile(file,'utf8');
    assert(markdown.includes(title) && markdown.includes(lyrics) && markdown.includes(style) && markdown.includes('consent_confirmed') && markdown.includes('尚未执行生成'), 'The exported checklist omitted the plan or capability boundary');
    return {file:relative(root,file).replaceAll('\\','/'), checked:true};
  });

  await step('create project requires fresh plan acknowledgement and submits no generation', async () => {
    await click(button('创建制作项目'));
    assert(await cdp.evaluate('!document.querySelector("dialog[aria-label=新建作品][open]")'), 'Unchecked clone rights should not open the creation flow');
    await click(query('.rights-check input'));
    await click(button('创建制作项目'));
    await waitDOM('!!document.querySelector("dialog[aria-label=新建作品][open]")', 'Ordinary project dialog did not open');
    assert(await cdp.evaluate(`${query('dialog[aria-label="新建作品"] input:not([type="file"])')}.value === ${JSON.stringify(title)} && ${query('dialog[aria-label="新建作品"] input[type="file"]')}.files.length === 0`), 'Project creation did not receive the plan title or unexpectedly attached a source');
    await click(button('进入创作工作台', query('dialog[aria-label="新建作品"]')));
    await waitUntil(async () => { const project = (await read('/projects')).find(item => item.title === title); if (!project) return false; createdProjectId = project.id; return project.mode === 'remake'; }, timeout, 'The ordinary fixture project was not created');
    await waitDOM(`location.hash.includes(${JSON.stringify(createdProjectId)}) && window.__AVD_READY`, 'Created project did not open');
    assert((await read(`/projects/${createdProjectId}/jobs`)).length === 0, 'Creating an empty project unexpectedly scheduled jobs');
    return {project_id:createdProjectId, mode:'remake', source_uploaded:false, jobs_submitted:0};
  });

  await step('Ctrl K command palette navigates to the version center', async () => {
    await cdp.send('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'k',code:'KeyK',windowsVirtualKeyCode:75,modifiers:2});
    await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'k',code:'KeyK',windowsVirtualKeyCode:75,modifiers:0});
    await waitDOM('!!document.querySelector(".command-dialog[open]")','Ctrl K did not open the command palette');
    await fill(query('.command-dialog input'), '版本中心');
    await waitDOM('document.querySelectorAll(".command-dialog .command-row").length === 1','Command search did not narrow to the version center');
    await click(query('.command-dialog .command-row'));
    await waitDOM('location.hash === "#/versions" && window.__AVD_READY && !document.querySelector(".command-dialog[open]")','Command navigation did not finish');
    return {keyboard:'Ctrl+K', destination:'#/versions'};
  });

  await step('version center reflects local Git and refreshes without remote fetch', async () => {
    const expected = await read('/version-control');
    assert(expected.available && expected.branch === 'main', 'Expected a readable local main branch in this checkout');
    assert(await cdp.evaluate(`document.querySelector('.version-page').textContent.includes(${JSON.stringify(expected.short_head)}) && document.querySelector('.branch-value').textContent.trim() === 'main'`), 'Version center did not reflect the actual local branch and HEAD');
    assert(await cdp.evaluate('document.querySelector(".version-page").textContent.includes("不向 GitHub 拉取最新状态") && document.querySelectorAll(".version-entry").length > 0'), 'Local-reference boundary or commit timeline is missing');
    const before = requestCounts.get('/version-control');
    await click(button('刷新本地状态'));
    await waitUntil(() => requestCounts.get('/version-control') > before, timeout, 'Version refresh did not reread the endpoint');
    await waitDOM(`!!(${button('刷新本地状态')}) && !(${button('刷新本地状态')}).disabled`, 'Version refresh did not finish');
    return {branch:expected.branch, head:expected.short_head, local_commits:expected.recent_commits.length, source:expected.sync.source};
  });

  await step('mobile menu navigates at 390 pixels and closes without overflow', async () => {
    await cdp.send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await go('#/');
    await click(query('#menu-toggle'));
    await waitDOM('document.querySelector("#menu-toggle").getAttribute("aria-expanded") === "true"','Mobile menu did not open');
    await click(query('.studio-sidebar a[href="#/performance"]'));
    await waitDOM('location.hash === "#/performance" && window.__AVD_READY && document.querySelector("#menu-toggle").getAttribute("aria-expanded") === "false"','Mobile navigation did not close the menu');
    assert(await cdp.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1'),'Mobile performance page overflows the viewport');
    return {width:390,height:844,destination:'#/performance',horizontal_overflow:false};
  });

  await step('intercepted POST and history GET failures preserve the unsent chat draft', async () => {
    await cdp.send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
    await go(`#/projects/${manifest.project_id}/chat`);
    const initialHistory = JSON.stringify(await read(chatPath));
    const draft = '离线验收：发送失败后请保留这段原始草稿。';
    await fill(query('.chat-composer textarea[name="message"]'),draft);
    failChat = true;
    await click(query('.chat-composer .send-button'));
    await waitUntil(() => report.simulated_requests.some(item => item.method === 'POST' && item.path === chatPath) && report.simulated_requests.some(item => item.method === 'GET' && item.path === chatPath),timeout,'Chat did not exercise both the failed send and failed history refresh');
    await waitDOM(`${query('.chat-composer textarea[name="message"]')}.value === ${JSON.stringify(draft)} && !${query('.chat-composer .send-button')}.disabled`,'Chat error recovery lost the draft or left sending disabled');
    assert(JSON.stringify(await read(chatPath)) === initialHistory,'The simulated chat request unexpectedly reached persistent history');
    failChat = false;
    await go('#/versions');
    return {post_status:502,history_status:502,forwarded_to_server:false,draft_restored:true,send_enabled:true,history_unchanged:true};
  });

  await step('deleting only this run empty project clears every shell reference', async () => {
    const disposable = (await read('/projects')).find(project => project.id === createdProjectId);
    assert(disposable && disposable.id !== manifest.project_id && disposable.title === title && !disposable.source_video_path, 'Only this run newly created empty fixture project may be deleted');
    assert((await read(`/projects/${createdProjectId}/jobs`)).length === 0, 'The disposable fixture project must still have no jobs');
    await go(`#/projects/${createdProjectId}/chat`);
    await waitDOM(`${query('.topbar-project')}.value === ${JSON.stringify(createdProjectId)}`, 'The disposable project was not selected');
    expectDeleteConfirm = true;
    allowFixtureDelete = true;
    await click(button('删除项目',query('.legacy-host .project-page')));
    await waitUntil(async () => !(await read('/projects')).some(project => project.id === createdProjectId),timeout,'The confirmed disposable project was not deleted');
    await waitDOM(`location.hash === '#/' && window.__AVD_READY && !Array.from(document.querySelectorAll('.topbar-project option')).some(option=>option.value === ${JSON.stringify(createdProjectId)}) && document.querySelector('.topbar-project').value !== ${JSON.stringify(createdProjectId)} && localStorage.getItem('avd.project') !== ${JSON.stringify(createdProjectId)}`,'Deletion left stale selector or persisted project references');
    assert(await cdp.evaluate(`!Array.from(document.querySelectorAll('.studio-sidebar .nav-item')).some(link=>link.getAttribute('href')?.includes(${JSON.stringify(createdProjectId)}))`),'Current-project navigation still points at the deleted project');
    await click(query('.command-trigger'));
    await waitDOM('!!document.querySelector(".command-dialog[open]")','Command palette did not open after deletion');
    await fill(query('.command-dialog input'),title);
    await waitDOM('document.querySelectorAll(".command-dialog .command-row").length === 0','Command search retained the deleted project');
    await click(query('.command-dialog button[aria-label="关闭搜索"]'));
    assert(report.delete_confirmations.length === 1 && !report.unexpected_dialogs.length,'Exactly the disposable project confirmation should be accepted');
    return {deleted_project_id:createdProjectId, manifest_project_untouched:true, selector_cleared:true,current_project_link_cleared:true,command_search_cleared:true,persisted_reference_cleared:true};
  });

  await step('project list refresh failure after deletion keeps navigation usable and recovers', async () => {
    failProjectList = 1;
    await go('#/appearance');
    await waitDOM('document.querySelector(".studio-toast.error")?.textContent.includes("项目列表暂未刷新") && document.querySelector(".local-state")?.textContent.includes("服务暂未连接")','Failed navigation refresh was not reported as offline');
    assert(await cdp.evaluate(`document.querySelectorAll('.theme-card').length === 4 && !Array.from(document.querySelectorAll('.topbar-project option')).some(option=>option.value === ${JSON.stringify(createdProjectId)})`),'A failed refresh broke the current page or restored deleted project state');
    await go('#/versions');
    await waitDOM('document.querySelector(".local-state")?.textContent.includes("本地服务已连接")','A later navigation did not recover project-list connectivity');
    return {simulated_status:502,prior_cache_preserved:true,deleted_reference_restored:false,navigation_usable:true,recovered_on_next_navigation:true};
  });

  await step('final fixture has unchanged jobs, spend, workers, and asset consent', async () => {
    const jobs = await read('/jobs?limit=1000');
    assert(jobs.length === initialJobs.length && jobs.every(job => !['queued','running'].includes(job.status)), 'The smoke introduced generated work');
    assert((await read('/workers')).workers.every(worker => worker.state === 'stopped'),'The smoke started workers');
    assert((await read('/projects')).find(project => project.id === manifest.project_id)?.spent === initialSpend,'The smoke changed project spend');
    const [characters,voices] = await Promise.all([read('/characters'),read('/voices')]);
    assert(JSON.stringify([characters,voices].map(items => items.map(item => [item.id,item.consent_confirmed]))) === initialConsent,'Plan acknowledgement changed real asset consent');
    assert(Object.values((await read('/settings')).secrets).every(value => !value.configured),'The fixture gained provider credentials');
    assert(report.writes.filter(item => item.operation === 'create-empty-fixture-project').length === 1,'The smoke must create exactly one empty fixture project');
    assert(report.writes.filter(item => item.operation === 'delete-own-empty-fixture-project').length === 1,'The smoke must delete only its own empty fixture project');
    assert(JSON.stringify((await read('/projects')).map(project=>project.id).sort()) === JSON.stringify(initialProjectIds),'The smoke altered pre-existing projects or left its empty fixture project behind');
    assert(!report.unexpected_dialogs.length,'An unexpected confirmation dialog was opened');
    assert(!report.blocked_requests.length,'Unexpected or external request attempted; see blocked_requests');
    assert(!report.exceptions.length && !report.console_errors.length,'Unexpected browser errors were recorded');
    return {jobs_added:0,spend_changed:false,workers_started:0,asset_consent_changed:false,paid_calls:0,external_requests:0,empty_projects_created:1,own_empty_projects_deleted:1,pre_existing_projects_unchanged:true};
  });
} catch (error) {
  report.error = diagnostic(error.message); console.error(report.error); process.exitCode = 1;
  if (cdp?.socket.readyState === WebSocket.OPEN) {
    try {
      report.failure_state = await cdp.evaluate(`({hash:location.hash,ready:window.__AVD_READY,viewport:[innerWidth,innerHeight],menu:document.querySelector('#menu-toggle')?.getAttribute('aria-expanded'),openDialogs:Array.from(document.querySelectorAll('dialog[open]')).map(item=>item.getAttribute('aria-label')),headings:Array.from(document.querySelectorAll('h1,h2')).map(item=>item.textContent).slice(0,15),notice:document.querySelector('#notice')?.textContent})`);
      const shot = await cdp.send('Page.captureScreenshot',{format:'png',fromSurface:true,captureBeyondViewport:false});
      const failureScreenshot = within(root,join(profile,'failure.png'));
      await writeFile(failureScreenshot,Buffer.from(shot.data,'base64'));
      report.failure_screenshot = relative(root,failureScreenshot).replaceAll('\\','/');
      console.error(JSON.stringify(report.failure_state));
    } catch { /* Preserve the original error if the browser is already closed. */ }
  }
} finally {
  failChat = false;
  await cleanup();
  report.finished_at = new Date().toISOString();
  report.passed = report.steps.length === 15 && report.steps.every(item => item.passed) && !report.error && !report.exceptions.length && !report.console_errors.length && !report.blocked_requests.length && !report.unexpected_dialogs.length;
  const path = join(output,'smoke-report.json');
  await writeFile(`${path}.tmp`,`${JSON.stringify(report,null,2)}\n`,'utf8');
  await rename(`${path}.tmp`,path);
  console.log(`Studio interaction report: ${path}`);
  if (!report.passed) process.exitCode = 1;
}
