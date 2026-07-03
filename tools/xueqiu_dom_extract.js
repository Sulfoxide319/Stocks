const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const workspace = process.cwd();
const edgePath =
  process.env.EDGE_PATH ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const userDataDir = process.env.XUEQIU_EDGE_PROFILE
  ? path.resolve(process.env.XUEQIU_EDGE_PROFILE)
  : path.join(workspace, ".xueqiu-edge-profile");
const port = Number(process.env.XUEQIU_CDP_PORT || 9225);
const url = process.argv[2] || "https://xueqiu.com/";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(target) {
  const response = await fetch(target);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function openWs(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (!payload.id || !pending.has(payload.id)) return;
    const { resolve, reject, timeout } = pending.get(payload.id);
    clearTimeout(timeout);
    pending.delete(payload.id);
    if (payload.error) reject(new Error(JSON.stringify(payload.error)));
    else resolve(payload.result || {});
  });
  const ready = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("CDP WebSocket timeout")), 8000);
    ws.addEventListener("open", () => {
      clearTimeout(timeout);
      resolve();
    });
    ws.addEventListener("error", (event) => {
      clearTimeout(timeout);
      reject(event.error || new Error("CDP WebSocket error"));
    });
  });
  async function command(method, params = {}, timeoutMs = 15000) {
    await ready;
    const id = nextId++;
    const result = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} timeout`));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timeout });
    });
    ws.send(JSON.stringify({ id, method, params }));
    return result;
  }
  return { command, close: () => ws.close() };
}

async function waitForVersion() {
  const started = Date.now();
  while (Date.now() - started < 15000) {
    try {
      return await fetchJson(`http://127.0.0.1:${port}/json/version`);
    } catch {
      await delay(250);
    }
  }
  throw new Error("Timed out waiting for Edge remote debugging port");
}

async function getPageWsUrl() {
  const pages = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  const page = pages.find((item) => item.type === "page") || pages[0];
  return page && page.webSocketDebuggerUrl;
}

function parseEvaluateResult(result) {
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return (result.result || {}).value;
}

async function main() {
  if (!fs.existsSync(edgePath)) throw new Error(`Edge not found: ${edgePath}`);
  if (!fs.existsSync(userDataDir)) throw new Error(`Xueqiu Edge profile not found: ${userDataDir}`);
  const child = spawn(
    edgePath,
    [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${userDataDir}`,
      "--no-first-run",
      "--disable-sync",
      "--new-window",
      url,
    ],
    { detached: false, stdio: "ignore" },
  );
  let page;
  let browser;
  try {
    await waitForVersion();
    const wsUrl = await getPageWsUrl();
    page = openWs(wsUrl);
    await page.command("Runtime.enable");
    await page.command("Page.enable");
    await page.command("Page.navigate", { url });
    await delay(6000);
    for (const y of [1200, 3000, 6000, 9000]) {
      await page.command("Runtime.evaluate", { expression: `window.scrollTo(0, ${y})`, returnByValue: true });
      await delay(1200);
    }
    const result = await page.command("Runtime.evaluate", {
      expression: `
        (() => {
          const lines = (document.body.innerText || "").split(/\\n+/)
            .map((line) => line.replace(/\\s+/g, " ").trim())
            .filter(Boolean);
          return {
            title: document.title,
            url: location.href,
            lineCount: lines.length,
            lines: lines.slice(0, 360)
          };
        })()
      `,
      returnByValue: true,
    });
    process.stdout.write(JSON.stringify(parseEvaluateResult(result), null, 2));
  } finally {
    if (page) page.close();
    try {
      const version = await fetchJson(`http://127.0.0.1:${port}/json/version`);
      browser = version.webSocketDebuggerUrl ? openWs(version.webSocketDebuggerUrl) : null;
      if (browser) await browser.command("Browser.close", {}, 5000);
    } catch {
      child.kill();
    } finally {
      if (browser) browser.close();
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
