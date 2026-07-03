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
const port = Number(process.env.XUEQIU_CDP_PORT || 9224);
const symbol = process.argv[2] || "SZ000725";
const headless = process.env.XUEQIU_BROWSER_HEADLESS === "1";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function openWs(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  const listeners = new Map();

  ws.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.id && pending.has(payload.id)) {
      const { resolve, reject, timeout } = pending.get(payload.id);
      clearTimeout(timeout);
      pending.delete(payload.id);
      if (payload.error) reject(new Error(JSON.stringify(payload.error)));
      else resolve(payload.result || {});
      return;
    }
    if (payload.method && listeners.has(payload.method)) {
      for (const listener of listeners.get(payload.method)) listener(payload.params || {});
    }
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

  function on(method, listener) {
    if (!listeners.has(method)) listeners.set(method, []);
    listeners.get(method).push(listener);
  }

  return { command, on, close: () => ws.close() };
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
  const page =
    pages.find((item) => item.type === "page" && item.url.includes("xueqiu.com")) ||
    pages.find((item) => item.type === "page");
  return page && page.webSocketDebuggerUrl;
}

function interestingUrl(url) {
  return /status|timeline|comment|query|stock|quote|search|portfolio|symbol/i.test(url);
}

function parseEvaluateResult(result) {
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return (result.result || {}).value;
}

async function main() {
  if (!fs.existsSync(edgePath)) throw new Error(`Edge not found: ${edgePath}`);
  if (!fs.existsSync(userDataDir)) throw new Error(`Xueqiu Edge profile not found: ${userDataDir}`);

  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--disable-background-networking",
    "--disable-sync",
    "--new-window",
  ];
  if (headless) args.push("--headless=new", "--disable-gpu");
  args.push(`https://xueqiu.com/S/${encodeURIComponent(symbol)}`);

  const child = spawn(edgePath, args, { detached: false, stdio: "ignore" });
  let page;
  let browser;
  const responses = [];
  const bodies = [];
  try {
    await waitForVersion();
    const wsUrl = await getPageWsUrl();
    if (!wsUrl) throw new Error("No page target found");
    page = openWs(wsUrl);
    await page.command("Network.enable");
    await page.command("Page.enable");
    await page.command("Runtime.enable");

    page.on("Network.responseReceived", async (params) => {
      const response = params.response || {};
      const url = response.url || "";
      if (!interestingUrl(url)) return;
      responses.push({
        status: response.status,
        mimeType: response.mimeType,
        url,
      });
      if (/json|javascript|text/i.test(response.mimeType || "")) {
        try {
          const body = await page.command("Network.getResponseBody", { requestId: params.requestId }, 5000);
          const text = body.base64Encoded ? Buffer.from(body.body, "base64").toString("utf8") : body.body;
          bodies.push({ status: response.status, mimeType: response.mimeType, url, sample: text.slice(0, 600) });
        } catch {}
      }
    });

    await page.command("Page.navigate", { url: `https://xueqiu.com/S/${encodeURIComponent(symbol)}` });
    await delay(7000);
    const clickResult = await page.command("Runtime.evaluate", {
      expression: `
        (() => {
          const labels = ["讨论", "新帖", "热帖", "全部"];
          const nodes = Array.from(document.querySelectorAll("a,button,span,div"));
          const clicked = [];
          for (const label of labels) {
            const node = nodes.find((item) => (item.innerText || "").trim() === label);
            if (node) {
              node.click();
              clicked.push(label);
            }
          }
          window.scrollTo(0, 2500);
          setTimeout(() => window.scrollTo(0, 6000), 1000);
          return clicked;
        })()
      `,
      returnByValue: true,
    });
    await delay(9000);
    const dom = await page.command("Runtime.evaluate", {
      expression: "(document.body && document.body.innerText || '').slice(0, 5000)",
      returnByValue: true,
    });
    process.stdout.write(
      JSON.stringify(
        {
          symbol,
          clicked: parseEvaluateResult(clickResult),
          responses,
          bodies: bodies.slice(0, 30),
          domSample: parseEvaluateResult(dom),
        },
        null,
        2,
      ),
    );
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
