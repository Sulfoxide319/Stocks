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
const port = Number(process.env.XUEQIU_CDP_PORT || 9223);
const symbolArg = process.argv[2] || "SH600519";
const symbols = symbolArg
  .split(",")
  .map((item) => item.trim())
  .filter(Boolean);
const count = Number(process.argv[3] || 10);
const headless = process.env.XUEQIU_BROWSER_HEADLESS !== "0";

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
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

  return {
    command,
    close: () => ws.close(),
  };
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

function parseEvaluateResult(result) {
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
  const remote = result.result || {};
  if (remote.subtype === "error") {
    throw new Error(remote.description || remote.value || "Runtime error");
  }
  return remote.value;
}

function statusExpression(symbol, count) {
  return `
    (async () => {
      const symbol = ${JSON.stringify(symbol)};
      const count = ${JSON.stringify(count)};
      await new Promise((resolve) => setTimeout(resolve, 1200));
      const apiResult = await fetchSymbol(symbol);
      if (apiResult.ok) return apiResult;
      const domStatuses = extractDomStatuses(symbol, count);
      if (domStatuses.length) {
        return {
          ok: true,
          endpoint: "dom_page",
          statuses: domStatuses,
          responses: apiResult.responses
        };
      }
      if (${JSON.stringify(process.env.XUEQIU_DEBUG_DOM === "1")}) {
        apiResult.domSample = sampleDomLines();
      }
      return apiResult;

      async function fetchSymbol(symbol) {
        const endpoints = [
          {
            name: "symbol_status_search",
            url: new URL("/query/v1/symbol/search/status.json", location.origin),
            params: {
              count: String(count),
              comment: "0",
              symbol,
              hl: "0",
              source: "all",
              sort: "alpha",
              page: "1",
              q: "",
              type: inferXueqiuType(symbol)
            }
          },
          {
            name: "status_search",
            url: new URL("/statuses/search.json", location.origin),
            params: {
              count: String(count),
              comment: "0",
              symbol,
              hl: "0",
              source: "all",
              sort: "time",
              page: "1",
              q: "",
              type: inferXueqiuType(symbol)
            }
          }
        ];
        const responses = [];
        for (const endpoint of endpoints) {
          Object.entries(endpoint.params).forEach(([key, value]) => endpoint.url.searchParams.set(key, value));
          endpoint.url.searchParams.set("_", String(Date.now()));
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 9000);
          let response;
          let text = "";
          try {
            response = await fetch(endpoint.url.toString(), {
              credentials: "include",
              signal: controller.signal,
              cache: "no-store"
            });
            text = await response.text();
          } catch (error) {
            responses.push({
              endpoint: endpoint.name,
              status: 0,
              contentType: "",
              textSample: String(error && error.message || error).slice(0, 180)
            });
            clearTimeout(timeout);
            continue;
          }
          clearTimeout(timeout);
          let payload = null;
          try { payload = JSON.parse(text); } catch {}
          responses.push({
            endpoint: endpoint.name,
            status: response.status,
            contentType: response.headers.get("content-type") || "",
            textSample: payload ? "" : text.slice(0, 180)
          });
          const statuses = extractStatuses(payload);
          if (statuses.length) {
            return {
              ok: true,
              endpoint: endpoint.name,
              statuses: statuses.slice(0, count).map(compactStatus),
              responses
            };
          }
        }
        return { ok: false, endpoint: "", statuses: [], responses };
      }

      function extractStatuses(payload) {
        if (Array.isArray(payload)) return payload.filter((item) => item && typeof item === "object");
        if (!payload || typeof payload !== "object") return [];
        for (const key of ["list", "statuses", "items"]) {
          if (Array.isArray(payload[key])) return payload[key].filter((item) => item && typeof item === "object");
        }
        const data = payload.data;
        if (Array.isArray(data)) return data.filter((item) => item && typeof item === "object");
        if (data && typeof data === "object") return extractStatuses(data);
        return [];
      }

      function compactStatus(status) {
        const user = status.user && typeof status.user === "object" ? status.user : {};
        return {
          id: status.id,
          created_at: status.created_at,
          text: status.text || status.description || "",
          title: status.title || "",
          like_count: status.like_count || 0,
          reply_count: status.reply_count || 0,
          retweet_count: status.retweet_count || 0,
          view_count: status.view_count || 0,
          user: {
            id: user.id,
            screen_name: user.screen_name || ""
          }
        };
      }

      function inferXueqiuType(symbol) {
        if (/^(SH|SZ|BJ)/.test(symbol)) return "11";
        if (/^HK/.test(symbol)) return "30";
        return "0";
      }

      function extractDomStatuses(symbol, count) {
        const lines = allDomLines();
        const blocked = new Set([
          "雪球", "登录", "注册", "首页", "行情", "沪深", "美股", "港股", "基金", "私募", "组合"
        ]);
        const candidates = [];
        for (const line of lines) {
          if (blocked.has(line) || line.length < 12 || line.length > 500) continue;
          if (/^\\$[^$]+\\([A-Z]{0,3}\\d{3,6}\\)\\$$/.test(line)) continue;
          const looksLikeDiscussion =
            line.includes("$") ||
            line.includes(symbol) ||
            /分钟前|小时前|今天|昨天|回复|转发|评论|赞|看多|看空|加仓|减仓|反弹|突破|回踩|大涨|大跌/.test(line);
          if (!looksLikeDiscussion) continue;
          if (/隐私|用户协议|免责声明|客户端下载|扫码登录|验证码/.test(line)) continue;
          candidates.push(line);
        }
        return Array.from(new Set(candidates)).slice(0, count).map((text, index) => ({
          id: "dom-" + symbol + "-" + index,
          created_at: Date.now(),
          text,
          title: "",
          like_count: 0,
          reply_count: 0,
          retweet_count: 0,
          view_count: 0,
          user: { id: null, screen_name: "" }
        }));
      }

      function sampleDomLines() {
        return allDomLines().slice(0, 260);
      }

      function allDomLines() {
        return (document.body.innerText || "")
          .split(/\\n+/)
          .map((line) => line.replace(/\\s+/g, " ").trim())
          .filter(Boolean);
      }
    })()
  `;
}

function hashtagExpression(query, symbol, count) {
  return `
    (() => {
      const query = ${JSON.stringify(query)};
      const symbol = ${JSON.stringify(symbol)};
      const count = ${JSON.stringify(count)};
      const lines = (document.body.innerText || "")
        .split(/\\n+/)
        .map((line) => line.replace(/\\s+/g, " ").trim())
        .filter(Boolean);
      const candidates = [];
      for (const line of lines) {
        if (line.length < 16 || line.length > 700) continue;
        if (line === query || line === "#" + query + "#") continue;
        if (/^\\d+(\\.\\d+)?$/.test(line)) continue;
        if (/^首页|下载App|发帖|热门话题|相关股票|相关基金|风险提示|互联网违法/.test(line)) continue;
        if (/来自(雪球|Android|iPhone|Web|网页)/.test(line) && /分钟前|小时前|昨天|今天|\\d{2}:\\d{2}/.test(line)) continue;
        if (/^(昨天|今天|\\d{2}-\\d{2})?.*转发\\s*\\d+\\s*·\\s*讨论\\s*\\d+\\s*·\\s*赞\\s*\\d+/.test(line)) continue;
        const hit =
          line.includes("$" + symbol) ||
          line.includes(symbol) ||
          line.includes("#" + query + "#") ||
          line.includes(query);
        const looksPost =
          hit ||
          /分钟前|小时前|昨天|今天|回复@|转发|评论|展开|看好|看多|看空|加仓|减仓|清仓|反弹|突破|大涨|大跌/.test(line);
        if (!looksPost) continue;
        candidates.push(line.replace(/展开$/, ""));
      }
      return {
        ok: candidates.length > 0,
        endpoint: "hashtag_dom_page",
        statuses: Array.from(new Set(candidates)).slice(0, count).map((text, index) => ({
          id: "hashtag-dom-" + symbol + "-" + index,
          created_at: Date.now(),
          text,
          title: "",
          like_count: 0,
          reply_count: 0,
          retweet_count: 0,
          view_count: 0,
          user: { id: null, screen_name: "" }
        })),
        responses: []
      };
    })()
  `;
}

async function extractStockNameFromPage(page, symbol) {
  try {
    const result = await page.command("Runtime.evaluate", {
      expression: `
        (() => {
          const lines = (document.body.innerText || "").split(/\\n+/)
            .map((line) => line.replace(/\\s+/g, " ").trim())
            .filter(Boolean);
          const symbol = ${JSON.stringify(symbol)};
          for (const line of lines.slice(0, 120)) {
            const normalized = line.replace(":", "");
            if (!normalized.includes(symbol.replace(":", "")) && !line.includes(symbol.replace(/^S[HZ]/, ""))) continue;
            const match = line.match(/^(.+?)\\((?:SH|SZ|BJ|HK|NYSE|NASDAQ|US)?[:A-Z]*\\d*[A-Z]*\\)$/);
            if (match && match[1] && match[1].length <= 24) return match[1];
          }
          const title = document.title || "";
          const titleMatch = title.match(/^(.+?)[(（]/);
          return titleMatch ? titleMatch[1].trim() : symbol;
        })()
      `,
      returnByValue: true,
    });
    return String(parseEvaluateResult(result) || symbol).trim() || symbol;
  } catch {
    return symbol;
  }
}

function statusEndpoints(symbol, count) {
  const base = "https://xueqiu.com";
  const first = new URL("/query/v1/symbol/search/status.json", base);
  Object.entries({
    count: String(count),
    comment: "0",
    symbol,
    hl: "0",
    source: "all",
    sort: "alpha",
    page: "1",
    q: "",
    type: /^(SH|SZ|BJ)/.test(symbol) ? "11" : /^HK/.test(symbol) ? "30" : "0",
    _: String(Date.now()),
  }).forEach(([key, value]) => first.searchParams.set(key, value));
  const second = new URL("/statuses/search.json", base);
  Object.entries({
    count: String(count),
    comment: "0",
    symbol,
    hl: "0",
    source: "all",
    sort: "time",
    page: "1",
    q: "",
    type: /^(SH|SZ|BJ)/.test(symbol) ? "11" : /^HK/.test(symbol) ? "30" : "0",
    _: String(Date.now()),
  }).forEach(([key, value]) => second.searchParams.set(key, value));
  return [
    ["top_level_symbol_status_search", first.toString()],
    ["top_level_status_search", second.toString()],
  ];
}

function extractStatusesNode(payload) {
  if (Array.isArray(payload)) return payload.filter((item) => item && typeof item === "object");
  if (!payload || typeof payload !== "object") return [];
  for (const key of ["list", "statuses", "items"]) {
    if (Array.isArray(payload[key])) return payload[key].filter((item) => item && typeof item === "object");
  }
  const data = payload.data;
  if (Array.isArray(data)) return data.filter((item) => item && typeof item === "object");
  if (data && typeof data === "object") return extractStatusesNode(data);
  return [];
}

function compactStatusNode(status) {
  const user = status.user && typeof status.user === "object" ? status.user : {};
  return {
    id: status.id,
    created_at: status.created_at,
    text: status.text || status.description || "",
    title: status.title || "",
    like_count: status.like_count || 0,
    reply_count: status.reply_count || 0,
    retweet_count: status.retweet_count || 0,
    view_count: status.view_count || 0,
    user: {
      id: user.id,
      screen_name: user.screen_name || "",
    },
  };
}

async function fetchViaTopLevelNavigation(page, symbol, count) {
  const responses = [];
  for (const [endpoint, url] of statusEndpoints(symbol, count)) {
    await page.command("Page.navigate", { url });
    await delay(3500);
    const result = await page.command(
      "Runtime.evaluate",
      {
        expression: "document.body ? document.body.innerText : document.documentElement.innerText",
        awaitPromise: false,
        returnByValue: true,
      },
      15000,
    );
    const text = String(parseEvaluateResult(result) || "");
    let payload = null;
    try {
      payload = JSON.parse(text);
    } catch {}
    responses.push({
      endpoint,
      status: payload ? 200 : 0,
      contentType: payload ? "application/json" : "text/plain",
      textSample: payload ? "" : text.slice(0, 180),
    });
    const statuses = extractStatusesNode(payload);
    if (statuses.length) {
      return {
        ok: true,
        endpoint,
        statuses: statuses.slice(0, count).map(compactStatusNode),
        responses,
      };
    }
  }
  return { ok: false, endpoint: "", statuses: [], responses };
}

async function main() {
  if (!fs.existsSync(edgePath)) {
    throw new Error(`Edge not found: ${edgePath}`);
  }
  if (!fs.existsSync(userDataDir)) {
    throw new Error(`Xueqiu Edge profile not found: ${userDataDir}`);
  }
  if (!symbols.length) {
    throw new Error("No symbols provided");
  }

  const firstSymbol = symbols[0];
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--disable-background-networking",
    "--disable-sync",
    "--new-window",
  ];
  if (headless) args.push("--headless=new", "--disable-gpu");
  args.push(`https://xueqiu.com/S/${encodeURIComponent(firstSymbol)}`);

  const child = spawn(edgePath, args, { detached: false, stdio: "ignore" });
  let browser;
  let page;
  try {
    await waitForVersion();
    const wsUrl = await getPageWsUrl();
    if (!wsUrl) throw new Error("No page target found");
    page = openWs(wsUrl);
    await page.command("Runtime.enable");
    await page.command("Page.enable");
    const results = {};
    for (const item of symbols) {
      await page.command("Page.navigate", {
        url: `https://xueqiu.com/S/${encodeURIComponent(item)}`,
      });
      await delay(3200);
      await page.command("Runtime.evaluate", {
        expression: "window.scrollTo(0, Math.min(document.body.scrollHeight, 1600))",
        awaitPromise: false,
        returnByValue: true,
      });
      await delay(1000);
      await page.command("Runtime.evaluate", {
        expression: "window.scrollTo(0, Math.min(document.body.scrollHeight, 3600))",
        awaitPromise: false,
        returnByValue: true,
      });
      await delay(1200);
      await page.command("Runtime.evaluate", {
        expression: "window.scrollTo(0, Math.min(document.body.scrollHeight, 6200))",
        awaitPromise: false,
        returnByValue: true,
      });
      await delay(1400);
      const result = await page.command(
        "Runtime.evaluate",
        {
          expression: statusExpression(item, count),
          awaitPromise: true,
          returnByValue: true,
        },
        60000,
      );
      let itemResult = parseEvaluateResult(result);
      const stockName = await extractStockNameFromPage(page, item);
      const sawWaf405 = (itemResult.responses || []).some((response) => response.status === 405);
      if (!sawWaf405 && (!itemResult.ok || !itemResult.statuses || !itemResult.statuses.length)) {
        const topLevelResult = await fetchViaTopLevelNavigation(page, item, count);
        if (topLevelResult.ok) {
          itemResult = topLevelResult;
        } else {
          itemResult.responses = [
            ...(itemResult.responses || []),
            ...(topLevelResult.responses || []),
          ];
        }
      }
      if (!itemResult.ok || !itemResult.statuses || !itemResult.statuses.length) {
        const hashtagQuery = stockName && stockName !== item ? stockName : item;
        await page.command("Page.navigate", {
          url: `https://xueqiu.com/k?q=%23${encodeURIComponent(hashtagQuery)}%23`,
        });
        await delay(5000);
        for (const y of [1400, 3600, 7000]) {
          await page.command("Runtime.evaluate", {
            expression: `window.scrollTo(0, ${y})`,
            returnByValue: true,
          });
          await delay(900);
        }
        const hashtagResult = await page.command(
          "Runtime.evaluate",
          {
            expression: hashtagExpression(hashtagQuery, item, count),
            awaitPromise: false,
            returnByValue: true,
          },
          30000,
        );
        const parsedHashtag = parseEvaluateResult(hashtagResult);
        if (parsedHashtag.ok && parsedHashtag.statuses && parsedHashtag.statuses.length) {
          itemResult = {
            ...parsedHashtag,
            responses: [...(itemResult.responses || []), ...(parsedHashtag.responses || [])],
          };
        }
      }
      results[item] = itemResult;
      await delay(2200);
    }
    const payload =
      symbols.length === 1
        ? { ok: results[symbols[0]].ok, symbol: symbols[0], ...results[symbols[0]] }
        : { ok: Object.values(results).some((item) => item.ok), results };
    process.stdout.write(JSON.stringify(payload, null, 2));
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
