/** Exercise offline browser workflows with headless Edge, using only Node built-ins.
 *
 * node scripts/smoke_ui.mjs --base-url http://127.0.0.1:8000
 * First run scripts/prepare_ui_demo.py, then serve its manifest's data/database.
 */
import {spawn} from "node:child_process";
import {access, mkdir, mkdtemp, readFile, realpath, rename, writeFile} from "node:fs/promises";
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
  console.log("node scripts/smoke_ui.mjs [--base-url http://127.0.0.1:8000] [--manifest data/ui-demo/ui-manifest.json] [--output docs/ui] [--edge PATH] [--timeout-ms 30000]");
  process.exit(0);
}
const base = new URL(opts["base-url"] || "http://127.0.0.1:8000");
if (base.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(base.hostname) || base.username || base.password) {
  throw new Error("Screenshots require an unauthenticated localhost HTTP server");
}
base.pathname = "/";
base.search = "";
base.hash = "";
const manifestPath = within(root, await realpath(within(root, resolve(root, opts.manifest || "data/ui-demo/ui-manifest.json"))));
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
if (typeof manifest.project_id !== "string" || !manifest.project_id) throw new Error("The UI manifest needs a project_id");
let output = within(root, resolve(root, opts.output || "docs/ui"));
const timeout = Number(opts["timeout-ms"] || 30000);
if (!Number.isFinite(timeout) || timeout < 1000 || timeout > 120000) throw new Error("timeout-ms must be between 1000 and 120000");
await mkdir(output, {recursive: true});
output = within(root, await realpath(output));
let profileRoot = within(root, join(root, "data", "ui-browser"));
await mkdir(profileRoot, {recursive: true});
profileRoot = within(root, await realpath(profileRoot));
const profile = await mkdtemp(join(profileRoot, "smoke-"));
const executable = await findEdge(opts.edge);
const browser = spawn(executable, [
  "--headless=new",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  "--window-size=1440,900",
  "--force-color-profile=srgb",
  "--disable-background-networking",
  "--disable-component-update",
  "--disable-default-apps",
  "--disable-domain-reliability",
  "--disable-sync",
  "--disable-features=AutofillServerCommunication,CertificateTransparencyComponentUpdater,OptimizationHints,MediaRouter",
  "--metrics-recording-only",
  "--no-first-run",
  "--no-default-browser-check",
  "about:blank",
], {cwd: root, stdio: "ignore", windowsHide: true});
let launchError;
browser.on("error", error => { launchError = error; });
let cdp;
let cleanupPromise;

async function cleanup() {
  if (cleanupPromise) return cleanupPromise;
  cleanupPromise = (async () => {
    if (cdp && cdp.socket.readyState === WebSocket.OPEN) {
      try { await cdp.send("Browser.close", {}, 2000); } catch { /* Browser may close before replying. */ }
    }
    try {
      await waitUntil(() => browser.exitCode !== null || browser.signalCode !== null, 2000, "Browser still exiting");
    } catch {
      if (browser.pid) {
        if (process.platform === "win32") {
          const killer = spawn("taskkill.exe", ["/PID", String(browser.pid), "/T", "/F"], {windowsHide: true, stdio: "ignore"});
          await new Promise(resolve => { killer.once("exit", resolve); killer.once("error", resolve); });
        } else browser.kill("SIGKILL");
      }
    }
    if (cdp) cdp.socket.close();
  })();
  return cleanupPromise;
}

for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => {
  void cleanup().finally(() => process.exit(signal === "SIGINT" ? 130 : 143));
});

const report = {started_at: new Date().toISOString(), browser: "Microsoft Edge", base_url: base.origin, steps: [], exceptions: [], console_errors: [], blocked_requests: [], writes: [], consent_screenshots: []};
let originalSettings;
let changedSettings = false;
let workersStarted = false;
let projectId;
let characterId;
let voiceId;
const suffix = Date.now().toString(36);
const projectTitle = `UI 离线验证 ${suffix}`;
const characterTitle = `UI 验证人物 ${suffix}`;
const voiceTitle = `UI 验证声音 ${suffix}`;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function read(path) {
  const response = await fetch(new URL(path, base), {signal: AbortSignal.timeout(timeout)});
  if (!response.ok) throw new Error(`Verification read failed: ${path} (${response.status})`);
  return response.json();
}

async function step(name, action) {
  const item = {name, passed: false};
  report.steps.push(item);
  try {
    const details = await action();
    item.passed = true;
    if (details) item.details = details;
    console.log(`PASS ${name}`);
  } catch (error) {
    item.error = diagnostic(error.message);
    throw error;
  }
}

async function waitDOM(expression, message, duration = timeout) {
  await waitUntil(async () => {
    try { return await cdp.evaluate(expression); } catch { return false; }
  }, duration, message);
}

async function go(route) {
  const previous = await cdp.evaluate("location.hash");
  await cdp.evaluate("window.__AVD_READY = false;");
  if (previous === route) await cdp.send("Page.reload", {ignoreCache: true});
  else await cdp.send("Page.navigate", {url: `${base.href}${route}`});
  await waitDOM("window.__AVD_READY === true", `Route did not render: ${route}`);
  assert(await cdp.evaluate("!document.querySelector('#content').textContent.includes('页面加载失败')"), "The application displayed a load failure");
}

function query(selector) { return `document.querySelector(${JSON.stringify(selector)})`; }
function textButton(text, scope = "document") {
  return `Array.from((${scope}).querySelectorAll('button')).find(element => element.textContent.trim() === ${JSON.stringify(text)} && element.getClientRects().length && !element.closest('[hidden]'))`;
}
function assetCard(title) {
  return `Array.from(document.querySelectorAll('.asset-card')).find(element => element.querySelector('h3')?.textContent === ${JSON.stringify(title)})`;
}
function labeledControl(label) {
  return `Array.from(document.querySelectorAll('label.field')).find(element => element.querySelector('.field-label')?.textContent === ${JSON.stringify(label)} && !element.closest('[hidden]'))?.querySelector('input,select,textarea')`;
}

async function click(expression) {
  const point = await cdp.evaluate(`(() => {
    const element = (${expression});
    if (!element || element.disabled || element.closest('[hidden]')) return null;
    element.scrollIntoView({block: 'center', inline: 'center'});
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
  })()`);
  assert(point, "The requested UI control is missing, hidden, or disabled");
  await cdp.send("Input.dispatchMouseEvent", {type: "mouseMoved", ...point});
  await cdp.send("Input.dispatchMouseEvent", {type: "mousePressed", button: "left", clickCount: 1, ...point});
  await cdp.send("Input.dispatchMouseEvent", {type: "mouseReleased", button: "left", clickCount: 1, ...point});
}

async function fill(expression, value) {
  const done = await cdp.evaluate(`(() => {
    const element = (${expression});
    if (!element || element.disabled || element.closest('[hidden]')) return false;
    element.scrollIntoView({block: 'center'});
    element.focus();
    const prototype = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, ${JSON.stringify(String(value ?? ""))});
    element.dispatchEvent(new Event('input', {bubbles: true}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
    return element.value === ${JSON.stringify(String(value ?? ""))};
  })()`);
  assert(done, "The requested form control could not accept the test value");
}

async function uploadFile(selector, path) {
  const local = within(root, await realpath(path));
  const document = await cdp.send("DOM.getDocument", {depth: 1});
  const node = await cdp.send("DOM.querySelector", {nodeId: document.root.nodeId, selector});
  assert(node.nodeId, "File upload control is missing");
  await cdp.send("DOM.setFileInputFiles", {nodeId: node.nodeId, files: [local]});
  await waitDOM(`document.querySelector(${JSON.stringify(selector)})?.files.length === 1`, "The browser did not accept the local fixture file");
}

async function consent(kind, title, id, exerciseValidation = false) {
  await click(textButton("确认授权", assetCard(title)));
  await waitDOM("!!document.querySelector('dialog[open]')", "Consent dialog did not open");
  const before = report.writes.filter(item => item.path.endsWith("/consent")).length;
  assert(await cdp.evaluate("document.querySelector('dialog input[name=consent]').checked === false"), "Consent checkbox must be unchecked initially");
  if (exerciseValidation) {
    for (const size of [{name: "desktop", width: 1440, height: 900}, {name: "mobile", width: 390, height: 844}]) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {width: size.width, height: size.height, deviceScaleFactor: 1, mobile: size.name === "mobile"});
      await cdp.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))", true);
      const layout = await cdp.evaluate(`(() => {
        const rect = document.querySelector('dialog').getBoundingClientRect();
        const viewport = document.documentElement.clientWidth;
        return {unchecked: !document.querySelector('dialog input[name=consent]').checked, overflow: rect.left < 0 || rect.right > viewport + 1 || document.documentElement.scrollWidth > viewport + 1};
      })()`);
      assert(layout.unchecked && !layout.overflow, "Consent modal must remain unchecked and fit the viewport");
      const screenshot = await cdp.send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
      const filename = `consent-${size.name}.png`;
      const path = join(output, filename);
      await writeFile(`${path}.tmp`, Buffer.from(screenshot.data, "base64"));
      await rename(`${path}.tmp`, path);
      report.consent_screenshots.push({file: filename, width: size.width, height: size.height, ...layout});
    }
    await cdp.send("Emulation.setDeviceMetricsOverride", {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
    await click(textButton("确认授权", query("dialog")));
    assert(await cdp.evaluate("document.querySelector('dialog input[name=confirmed_by]').validity.valueMissing"), "Missing actor must be invalid");
    assert(await cdp.evaluate("document.querySelector('dialog input[name=consent]').validity.valueMissing"), "Unchecked consent must be invalid");
    await click(query("dialog input[name=consent]"));
    await click(textButton("确认授权", query("dialog")));
    assert(await cdp.evaluate("document.querySelector('dialog input[name=confirmed_by]').validity.valueMissing"), "Checked consent must still require an actor");
    await click(query("dialog input[name=consent]"));
  }
  await fill(query("dialog input[name=confirmed_by]"), "离线 UI 自动验证");
  if (exerciseValidation) {
    await click(textButton("确认授权", query("dialog")));
    assert(await cdp.evaluate("document.querySelector('dialog input[name=consent]').validity.valueMissing"), "Actor name must not bypass unchecked consent");
    await delay(150);
    assert(report.writes.filter(item => item.path.endsWith("/consent")).length === before, "An invalid consent form reached the API");
  }
  await click(query("dialog input[name=consent]"));
  await click(textButton("确认授权", query("dialog")));
  await waitUntil(async () => (await read(`/${kind}`)).find(item => item.id === id)?.consent_confirmed === true, timeout, "Consent was not persisted");
  await waitDOM(`!document.querySelector('dialog') && (${assetCard(title)})?.textContent.includes('已确认授权')`, "Confirmed asset card did not update");
}

async function restoreSettings() {
  await go("#/settings/general");
  await fill(query("#settings-panel-general [name=default_budget_limit]"), originalSettings.default_budget_limit);
  await fill(query("#settings-panel-general [name=log_format]"), originalSettings.log_format);
  await click(textButton("保存通用设置"));
  await waitUntil(async () => {
    const values = (await read("/settings")).settings;
    return values.default_budget_limit === originalSettings.default_budget_limit && values.log_format === originalSettings.log_format;
  }, timeout, "Original general settings were not restored");
  changedSettings = false;
}

try {
  let port;
  await waitUntil(async () => {
    if (launchError) throw launchError;
    if (browser.exitCode !== null) throw new Error("Edge exited before debugging became available");
    try {
      port = Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).trim().split(/\r?\n/)[0]);
      return Number.isInteger(port) && port > 0;
    } catch (error) { if (error.code === "ENOENT") return false; throw error; }
  }, timeout, "Edge debugging port was not ready");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  cdp = await CDP.connect(targets.find(item => item.type === "page").webSocketDebuggerUrl);
  cdp.on("Runtime.exceptionThrown", value => report.exceptions.push(diagnostic(value.exceptionDetails?.exception?.description || value.exceptionDetails?.text)));
  cdp.on("Runtime.consoleAPICalled", value => { if (value.type === "error") report.console_errors.push(diagnostic(value.args?.map(item => item.value ?? item.description).join(" "))); });
  cdp.on("Log.entryAdded", value => { if (value.entry?.level === "error") report.console_errors.push(diagnostic(value.entry.text)); });
  cdp.on("Fetch.requestPaused", value => {
    const url = new URL(value.request.url);
    const method = value.request.method;
    const safeWrite = method === "PUT" && (url.pathname === "/settings" || /^\/projects\/[^/]+\/(?:budget|assets)$/.test(url.pathname)) || method === "POST" && (/^\/(?:projects|uploads|characters|voices)$/.test(url.pathname) || /^\/(?:characters|voices)\/[^/]+\/consent$/.test(url.pathname) || /^\/workers\/(?:all|cpu)\/(?:start|stop)$/.test(url.pathname));
    // The overview requests this deterministic read through the existing tools
    // endpoint. Generation and every other tool remain blocked by default.
    const readBudget = method === "POST" && /^\/projects\/[^/]+\/tools\/get_budget$/.test(url.pathname);
    const allowed = url.origin === base.origin && (["GET", "HEAD"].includes(method) || safeWrite || readBudget);
    if (!allowed) report.blocked_requests.push({method, path: url.pathname});
    else if (!["GET", "HEAD"].includes(method)) report.writes.push({method, path: url.pathname});
    void cdp.send(allowed ? "Fetch.continueRequest" : "Fetch.failRequest", allowed ? {requestId: value.requestId} : {requestId: value.requestId, errorReason: "BlockedByClient"}).catch(() => {});
  });
  for (const domain of ["Page", "Runtime", "DOM", "Network", "Log"]) await cdp.send(`${domain}.enable`);
  await cdp.send("Fetch.enable", {patterns: [{urlPattern: "http://*", requestStage: "Request"}, {urlPattern: "https://*", requestStage: "Request"}]});
  await cdp.send("Emulation.setDeviceMetricsOverride", {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
  const snapshot = await read("/settings");
  assert(Object.values(snapshot.secrets).every(value => !value.configured), "The smoke fixture must not contain configured provider keys");
  assert(resolve(snapshot.settings.data_dir).toLowerCase() === resolve(manifest.data_dir).toLowerCase(), "The server is not serving the isolated UI fixture");
  originalSettings = {default_budget_limit: snapshot.settings.default_budget_limit, log_format: snapshot.settings.log_format};
  const initialJobs = await read("/jobs?limit=1000");
  assert(initialJobs.every(job => !["queued", "running"].includes(job.status)), "The isolated fixture must have no pending jobs");
  const initialWorkers = await read("/workers");
  assert(initialWorkers.workers.every(worker => worker.state === "stopped"), "Workers must be stopped before the smoke begins");
  const databasePath = manifest.database_url.replace(/^sqlite:\/\/\//, "");
  const reference = within(root, join(dirname(databasePath), "reference.png"));
  const sample = within(root, join(dirname(databasePath), "sample.wav"));

  await step("dashboard creates a project", async () => {
    await go("#/");
    await click(textButton("新建项目"));
    await waitDOM("!!document.querySelector('dialog[open]')", "Project dialog did not open");
    await delay(200);
    await fill(query("dialog input[name=title]"), projectTitle);
    await fill(query("dialog input[name=budget]"), "7.5");
    await click(textButton("创建项目", query("dialog")));
    await waitUntil(async () => {
      const project = (await read("/projects")).find(item => item.title === projectTitle);
      if (!project) return false;
      projectId = project.id;
      return project.budget_limit === 7.5;
    }, timeout, "Project creation or initial budget failed");
    await waitDOM(`location.hash.includes(${JSON.stringify(projectId)}) && window.__AVD_READY`, "New project did not open");
    return {budget: 7.5};
  });

  await step("costs edits the project budget", async () => {
    await go("#/costs");
    const card = `Array.from(document.querySelectorAll('.card')).find(element => element.querySelector('h3 a')?.textContent === ${JSON.stringify(projectTitle)})`;
    await fill(`(${card}).querySelector('[name=amount]')`, "12.25");
    await click(textButton("保存预算", card));
    await waitUntil(async () => (await read("/projects")).find(item => item.id === projectId)?.budget_limit === 12.25, timeout, "Edited budget was not persisted");
    return {budget: 12.25};
  });

  await step("general settings save and restore", async () => {
    await go("#/settings/general");
    changedSettings = true;
    const changedFormat = originalSettings.log_format === "json" ? "text" : "json";
    await fill(query("#settings-panel-general [name=default_budget_limit]"), "3.25");
    await fill(query("#settings-panel-general [name=log_format]"), changedFormat);
    await click(textButton("保存通用设置"));
    await waitUntil(async () => {
      const values = (await read("/settings")).settings;
      return values.default_budget_limit === 3.25 && values.log_format === changedFormat;
    }, timeout, "Edited settings were not persisted");
    await restoreSettings();
    return {restored: true};
  });

  await step("character upload creates an unconsented asset", async () => {
    await go("#/assets");
    await click(textButton("新建人物"));
    await waitDOM("!!document.querySelector('dialog[open]')", "Character dialog did not open");
    await fill(query("dialog [name=name]"), characterTitle);
    await fill(query("dialog [name=appearance_prompt]"), "抽象演示字幕卡，仅用于离线 UI 操作验证。");
    await uploadFile("dialog input[name=front]", reference);
    await click(textButton("新建人物", query("dialog")));
    await waitUntil(async () => {
      const item = (await read("/characters")).find(asset => asset.name === characterTitle);
      if (!item) return false;
      characterId = item.id;
      return item.consent_confirmed === false;
    }, timeout, "Uploaded character was not saved without consent");
    await waitDOM(`!document.querySelector('dialog') && !!(${assetCard(characterTitle)})`, "New character card did not render");
  });

  await step("consent requires both actor and unchecked explicit confirmation", async () => {
    await consent("characters", characterTitle, characterId, true);
    return {invalid_submissions_sent: 0, initially_checked: false};
  });

  await step("voice upload and consent", async () => {
    await click(textButton("新建声音"));
    await waitDOM("!!document.querySelector('dialog[open]')", "Voice dialog did not open");
    await fill(query("dialog [name=name]"), voiceTitle);
    await fill(query("dialog [name=engine]"), "offline-sine");
    await uploadFile("dialog input[name=samples]", sample);
    await click(textButton("新建声音", query("dialog")));
    await waitUntil(async () => {
      const item = (await read("/voices")).find(asset => asset.name === voiceTitle);
      if (!item) return false;
      voiceId = item.id;
      return item.consent_confirmed === false;
    }, timeout, "Uploaded voice was not saved without consent");
    await waitDOM(`!document.querySelector('dialog') && !!(${assetCard(voiceTitle)})`, "New voice card did not render");
    await consent("voices", voiceTitle, voiceId);
  });

  await step("assigns consented assets to the new project", async () => {
    await fill(query("select[name=character_id]"), characterId);
    await fill(query("select[name=voice_id]"), voiceId);
    await click(textButton("应用到项目"));
    await waitUntil(async () => {
      const project = (await read("/projects")).find(item => item.id === projectId);
      return project.character_id === characterId && project.voice_id === voiceId;
    }, timeout, "Asset assignment was not persisted");
  });

  await step("jobs filters and clearing work", async () => {
    await go("#/jobs");
    await fill(labeledControl("通道"), "gpu");
    await fill(labeledControl("状态"), "succeeded");
    await click(textButton("应用筛选"));
    const matching = await read("/jobs?lane=gpu&status=succeeded&limit=1000");
    await waitDOM(`document.querySelectorAll('tbody tr[data-job-id]').length === ${matching.length} && Array.from(document.querySelectorAll('tbody tr[data-job-id]')).every(row => row.children[2].textContent === 'GPU' && row.children[4].textContent.includes('已完成'))`, "Lane/status filters did not update the job table");
    await fill(labeledControl("项目"), projectId);
    await click(textButton("应用筛选"));
    await waitDOM("document.querySelectorAll('tbody tr[data-job-id]').length === 0", "Empty project filter did not hide unrelated jobs");
    await click(textButton("清除筛选"));
    await waitDOM(`document.querySelectorAll('tbody tr[data-job-id]').length === ${initialJobs.length}`, "Clearing filters did not restore jobs");
    return {filtered_jobs: matching.length, restored_jobs: initialJobs.length};
  });

  await step("worker start and stop through the system UI", async () => {
    assert((await read("/jobs?limit=1000")).every(job => !["queued", "running"].includes(job.status)), "Pending work appeared before worker controls");
    await go("#/system");
    workersStarted = true;
    await click(textButton("启动全部"));
    await waitUntil(async () => (await read("/workers")).workers.every(worker => worker.state === "running" && worker.pid), timeout, "Workers did not start");
    await delay(500);
    await waitDOM(`!!(${textButton("停止全部")}) && !(${textButton("停止全部")}).disabled`, "Stop control did not become available");
    await click(textButton("停止全部"));
    await waitUntil(async () => (await read("/workers")).workers.every(worker => worker.state === "stopped" && worker.exit_code === 0), timeout, "Workers did not stop gracefully");
    workersStarted = false;
    assert((await read("/jobs?limit=1000")).length === initialJobs.length, "Worker smoke unexpectedly submitted jobs");
    return {started: 3, stopped: 3, jobs_submitted: 0};
  });

  await step("mobile menu opens and navigation closes it", async () => {
    await cdp.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    await go("#/");
    await click(query("#menu-toggle"));
    assert(await cdp.evaluate("document.querySelector('#menu-toggle').getAttribute('aria-expanded') === 'true'"), "Mobile menu did not open");
    await click(query("#navigation a[href='#/costs']"));
    await waitDOM("location.hash === '#/costs' && window.__AVD_READY && document.querySelector('#menu-toggle').getAttribute('aria-expanded') === 'false'", "Mobile navigation did not close the menu");
    assert(await cdp.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), "Mobile page has horizontal viewport overflow");
    return {width: 390, height: 844};
  });
} catch (error) {
  report.error = diagnostic(error.message);
  console.error(report.error);
  process.exitCode = 1;
} finally {
  if (changedSettings && cdp) {
    try { await restoreSettings(); report.settings_restored_after_failure = true; }
    catch (error) { report.settings_restore_error = diagnostic(error.message); }
  }
  if (workersStarted && cdp) {
    try {
      await go("#/system");
      await click(textButton("停止全部"));
      await waitUntil(async () => (await read("/workers")).workers.every(worker => worker.state === "stopped"), timeout, "Worker cleanup failed");
      report.workers_stopped_after_failure = true;
    } catch (error) { report.worker_cleanup_error = diagnostic(error.message); }
  }
  await cleanup();
  report.finished_at = new Date().toISOString();
  report.passed = report.steps.length === 10 && report.steps.every(item => item.passed) && !report.error && !report.exceptions.length && !report.console_errors.length && !report.blocked_requests.length && !report.settings_restore_error && !report.worker_cleanup_error;
  const reportPath = join(output, "smoke-report.json");
  await writeFile(`${reportPath}.tmp`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  await rename(`${reportPath}.tmp`, reportPath);
  console.log(`Browser interaction report: ${reportPath}`);
  if (!report.passed) process.exitCode = 1;
}

