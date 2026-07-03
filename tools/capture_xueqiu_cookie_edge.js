const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const workspace = process.cwd();
const edgePath =
  process.env.EDGE_PATH ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const userDataDir = path.join(workspace, ".xueqiu-edge-profile");
const cookieOut = path.join(workspace, "config", "xueqiu_cookie.txt");
const logPath = path.join(workspace, "output", "xueqiu_cookie_capture.log");
const port = Number(process.env.XUEQIU_CDP_PORT || 9222);
const headless = process.env.XUEQIU_COOKIE_CAPTURE_HEADLESS !== "0";

fs.mkdirSync(path.dirname(cookieOut), { recursive: true });
fs.mkdirSync(path.dirname(logPath), { recursive: true });

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}\n`;
  fs.appendFileSync(logPath, line, "utf8");
  process.stdout.write(line);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function cdpCommand(wsUrl, method, params = {}) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("CDP WebSocket timeout")), 5000);
    ws.addEventListener("open", () => {
      clearTimeout(timeout);
      resolve();
    });
    ws.addEventListener("error", (event) => {
      clearTimeout(timeout);
      reject(event.error || new Error("CDP WebSocket error"));
    });
  });

  const id = 1;
  ws.send(JSON.stringify({ id, method, params }));
  const result = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`${method} timeout`)), 5000);
    ws.addEventListener("message", (event) => {
      const payload = JSON.parse(event.data);
      if (payload.id !== id) return;
      clearTimeout(timeout);
      if (payload.error) reject(new Error(JSON.stringify(payload.error)));
      else resolve(payload.result || {});
    });
    ws.addEventListener("error", (event) => {
      clearTimeout(timeout);
      reject(event.error || new Error("CDP command error"));
    });
  });
  ws.close();
  return result;
}

async function getPageWsUrl() {
  const pages = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  const page =
    pages.find((item) => item.type === "page" && item.url.includes("xueqiu.com")) ||
    pages.find((item) => item.type === "page");
  return page && page.webSocketDebuggerUrl;
}

function xueqiuCookieHeader(cookies) {
  const filtered = cookies
    .filter((cookie) => /(^|\.)xueqiu\.com$/i.test(cookie.domain || ""))
    .sort((a, b) => a.name.localeCompare(b.name));
  const hasToken = filtered.some((cookie) => cookie.name === "xq_a_token");
  const idToken = filtered.find((cookie) => cookie.name === "xq_id_token");
  if (!hasToken || !idToken || !isLoggedInXueqiuToken(idToken.value)) return null;
  return filtered.map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
}

function isLoggedInXueqiuToken(token) {
  try {
    const payloadPart = token.split(".")[1];
    if (!payloadPart) return false;
    const normalized = payloadPart.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(normalized.length + ((4 - normalized.length % 4) % 4), "=");
    const payload = JSON.parse(Buffer.from(padded, "base64").toString("utf8"));
    return Boolean(payload.uid && payload.uid !== -1);
  } catch {
    return false;
  }
}

async function main() {
  if (!fs.existsSync(edgePath)) {
    throw new Error(`Edge not found: ${edgePath}`);
  }

  log(`Launching Edge with temporary profile: ${userDataDir} headless=${headless}`);
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--disable-sync",
    "https://xueqiu.com/",
  ];
  if (headless) args.splice(3, 0, "--headless=new", "--disable-gpu");
  else args.splice(3, 0, "--new-window");

  const child = spawn(
    edgePath,
    args,
    { detached: false, stdio: "ignore" },
  );

  child.on("exit", (code) => log(`Edge exited with code ${code}`));

  if (headless) {
    log("Refreshing Xueqiu cookie from the existing logged-in Edge profile.");
  } else {
    log("Please log in to xueqiu.com in the opened Edge window.");
  }
  const started = Date.now();
  while (Date.now() - started < 10 * 60 * 1000) {
    try {
      const wsUrl = await getPageWsUrl();
      if (!wsUrl) throw new Error("No page target yet");
      const result = await cdpCommand(wsUrl, "Network.getAllCookies");
      const header = xueqiuCookieHeader(result.cookies || []);
      if (header) {
        fs.writeFileSync(cookieOut, header, "utf8");
        log(`Saved Xueqiu cookie to ${cookieOut}`);
        try {
          await cdpCommand(wsUrl, "Browser.close");
        } catch {
          child.kill();
        }
        return;
      }
      log("Waiting for xq_a_token cookie...");
    } catch (error) {
      log(`Waiting for browser/login: ${error.message}`);
    }
    await delay(3000);
  }

  log("Timed out after 10 minutes without a Xueqiu login cookie.");
  child.kill();
  process.exitCode = 2;
}

main().catch((error) => {
  log(`FAILED: ${error.stack || error.message}`);
  process.exitCode = 1;
});
