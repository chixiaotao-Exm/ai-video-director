/** Capture every UI page with headless Edge, using only Node built-ins.
 *
 * node scripts/capture_ui.mjs --base-url http://127.0.0.1:8000
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
  console.log("node scripts/capture_ui.mjs [--base-url http://127.0.0.1:8000] [--manifest data/ui-demo/ui-manifest.json] [--output docs/ui] [--edge PATH] [--timeout-ms 30000]");
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
const profile = await mkdtemp(join(profileRoot, "edge-"));
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

const project = encodeURIComponent(manifest.project_id);
const pages = [
  ["dashboard", "#/"],
  ["project-overview", `#/projects/${project}/overview`],
  ["project-chat", `#/projects/${project}/chat`],
  ["project-storyboard", `#/projects/${project}/storyboard`],
  ["project-shots", `#/projects/${project}/shots`],
  ["project-final", `#/projects/${project}/final`],
  ["project-analysis", `#/projects/${project}/analysis`],
  ["assets", "#/assets"],
  ["assets-voices", "#/assets", "声音素材"],
  ["jobs", "#/jobs"],
  ["costs", "#/costs"],
  ["costs-ledger", "#/costs", "费用明细"],
  ["settings", "#/settings"],
  ["settings-prices", "#/settings", "模型价格"],
  ["settings-comfy", "#/settings/comfy"],
  ["settings-general", "#/settings/general"],
  ["settings-advanced", "#/settings/advanced"],
  ["system", "#/system"],
  ["system-logs", "#/system", "最近日志"],
];
const sizes = [
  {name: "desktop", width: 1440, height: 900, mobile: false},
  {name: "mobile", width: 390, height: 844, mobile: true},
];
const report = {captured_at: new Date().toISOString(), browser: "Microsoft Edge", base_url: base.origin, project_id: manifest.project_id, pages: []};
let active;

try {
  let port;
  await waitUntil(async () => {
    if (launchError) throw new Error(`Edge could not start: ${diagnostic(launchError.message)}`);
    if (browser.exitCode !== null) throw new Error("Edge exited before debugging became available");
    try {
      const values = (await readFile(join(profile, "DevToolsActivePort"), "utf8")).trim().split(/\r?\n/);
      port = Number(values[0]);
      return Number.isInteger(port) && port > 0;
    } catch (error) {
      if (error.code === "ENOENT") return false;
      throw error;
    }
  }, timeout, "Edge debugging port was not ready");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(item => item.type === "page" && item.webSocketDebuggerUrl);
  if (!target) throw new Error("Edge did not expose a page debugging target");
  cdp = await CDP.connect(target.webSocketDebuggerUrl);
  cdp.on("Runtime.exceptionThrown", value => {
    if (active) active.exceptions.push({text: diagnostic(value.exceptionDetails?.exception?.description || value.exceptionDetails?.text), line: value.exceptionDetails?.lineNumber});
  });
  cdp.on("Runtime.consoleAPICalled", value => {
    if (active && value.type === "error") active.console_errors.push(diagnostic(value.args?.map(item => item.value ?? item.description ?? item.type).join(" ")));
  });
  cdp.on("Log.entryAdded", value => {
    if (active && value.entry?.level === "error") active.console_errors.push(diagnostic(value.entry.text));
  });
  cdp.on("Fetch.requestPaused", value => {
    let allowed = false;
    try { allowed = new URL(value.request.url).origin === base.origin; } catch { /* Block unknown origins. */ }
    const method = allowed ? "Fetch.continueRequest" : "Fetch.failRequest";
    const params = allowed ? {requestId: value.requestId} : {requestId: value.requestId, errorReason: "BlockedByClient"};
    if (!allowed && active) active.blocked_external_requests += 1;
    void cdp.send(method, params).catch(() => {});
  });
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", {cacheDisabled: true});
  await cdp.send("Fetch.enable", {patterns: [{urlPattern: "http://*", requestStage: "Request"}, {urlPattern: "https://*", requestStage: "Request"}]});

  for (const size of sizes) {
    await cdp.send("Emulation.setDeviceMetricsOverride", {width: size.width, height: size.height, deviceScaleFactor: 1, mobile: size.mobile});
    await cdp.send("Emulation.setTouchEmulationEnabled", {enabled: size.mobile});
    for (const [page, route, heading] of pages) {
      active = {page, size: size.name, width: size.width, height: size.height, route, screenshot: `${page}-${size.name}.png`, ready: false, exceptions: [], console_errors: [], blocked_external_requests: 0};
      report.pages.push(active);
      try {
        const destination = `${base.href}${route}`;
        const previousURL = await cdp.evaluate('location.href');
        await cdp.evaluate("window.__AVD_READY = false; window.scrollTo(0, 0);");
        if (previousURL === destination) await cdp.send('Page.reload', {ignoreCache: true});
        else await cdp.send("Page.navigate", {url: destination});
        await waitUntil(async () => cdp.evaluate("document.readyState === 'complete' && window.__AVD_READY === true"), timeout, "Page did not finish rendering");
        active.ready = true;
        await cdp.evaluate(`(async () => {
          await document.fonts.ready;
          await Promise.race([
            Promise.allSettled(Array.from(document.images).filter(image => image.getBoundingClientRect().top < innerHeight).map(image => image.decode())),
            new Promise(resolve => setTimeout(resolve, 2000))
          ]);
          await Promise.allSettled(Array.from(document.querySelectorAll('video')).filter(video => video.getBoundingClientRect().top < innerHeight).map(video => new Promise(resolve => {
            if (video.readyState >= 2) { resolve(); return; }
            video.addEventListener('loadeddata', resolve, {once:true});
            video.addEventListener('error', resolve, {once:true});
            video.preload = 'auto'; video.load();
            setTimeout(resolve, 2500);
          })));
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          window.scrollTo(0, 0);
        })()`, true);
      } catch (error) {
        active.error = diagnostic(error.message);
      }
      if (heading) {
        const found = await cdp.evaluate(`(() => {
          const heading = Array.from(document.querySelectorAll('h2')).find(item => item.textContent.trim() === ${JSON.stringify(heading)});
          if (!heading) return false;
          heading.closest('section')?.scrollIntoView({block:'start'});
          return true;
        })()`);
        if (!found) active.error = 'Requested section heading was not found';
        await cdp.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))', true);
      }
      active.layout = await cdp.evaluate(`(() => {
        const viewport = document.documentElement.clientWidth;
        const outside = [];
        for (const element of document.body.querySelectorAll('*')) {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          if (!rect.width || !rect.height || style.visibility === 'hidden' || style.display === 'none') continue;
          if (rect.left >= -1 && rect.right <= viewport + 1) continue;
          let clipped = false;
          for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
            const ps = getComputedStyle(parent);
            const pr = parent.getBoundingClientRect();
            if (['auto', 'scroll', 'hidden', 'clip'].includes(ps.overflowX) && pr.left >= -1 && pr.right <= viewport + 1) { clipped = true; break; }
          }
          outside.push({element: element.tagName.toLowerCase() + (element.id ? '#' + element.id : '') + Array.from(element.classList).slice(0, 3).map(name => '.' + name).join(''), left: Math.round(rect.left), right: Math.round(rect.right), clipped_by_container: clipped});
        }
        return {
          document_width: document.documentElement.scrollWidth,
          client_width: viewport,
          content_height: document.documentElement.scrollHeight,
          viewport_overflow: document.documentElement.scrollWidth > viewport + 1,
          body_background: getComputedStyle(document.body).backgroundColor,
          application_error: document.querySelector('.empty h3')?.textContent === '页面加载失败',
          visible_unloaded_videos: Array.from(document.querySelectorAll('video')).filter(video => video.getBoundingClientRect().top < innerHeight && video.getBoundingClientRect().bottom > 0 && video.readyState < 2).length,
          failed_visible_images: Array.from(document.images).filter(image => image.getBoundingClientRect().top < innerHeight && image.getBoundingClientRect().bottom > 0 && image.complete && !image.naturalWidth).length,
          media_errors: Array.from(document.querySelectorAll('audio, video')).filter(element => element.error).map(element => ({tag: element.tagName.toLowerCase(), code: element.error.code})),
          outside_elements: outside.slice(0, 40)
        };
      })()`);
      const shot = await cdp.send("Page.captureScreenshot", {format: "png", fromSurface: true, captureBeyondViewport: false});
      const path = join(output, active.screenshot);
      await writeFile(`${path}.tmp`, Buffer.from(shot.data, "base64"));
      await rename(`${path}.tmp`, path);
      console.log(`${active.screenshot}: ready=${active.ready}, overflow=${active.layout.viewport_overflow}, errors=${active.exceptions.length + active.console_errors.length}`);
      active = undefined;
    }
  }
} catch (error) {
  report.error = diagnostic(error.message);
  process.exitCode = 1;
} finally {
  active = undefined;
  await cleanup();
  report.passed = report.pages.length === pages.length * sizes.length && !report.error && report.pages.every(page => page.ready && !page.error && !page.layout?.viewport_overflow && page.layout?.body_background === "rgb(255, 255, 255)" && !page.layout?.application_error && !page.layout?.visible_unloaded_videos && !page.layout?.failed_visible_images && !page.layout?.media_errors.length && !page.exceptions.length && !page.console_errors.length && !page.blocked_external_requests);
  const reportPath = join(output, "report.json");
  await writeFile(`${reportPath}.tmp`, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  await rename(`${reportPath}.tmp`, reportPath);
  console.log(`Capture report: ${reportPath}`);
  if (!report.passed) process.exitCode = 1;
}
