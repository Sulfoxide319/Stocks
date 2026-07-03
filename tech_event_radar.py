#!/usr/bin/env python3
"""Tech stock event radar for 1-5 day catalyst discovery.

The script collects official filings, announcements, configurable RSS/news
feeds, and price confirmation data, then scores near-term technology stock
events. It is designed as a research assistant, not an automated trading
system.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from market_universe import DEFAULT_BUYABLE_PREFIXES, filter_symbols

try:
    import requests
except ImportError as exc:  # pragma: no cover - friendly runtime error
    raise SystemExit("Missing dependency: pip install requests") from exc


DEFAULT_HEADERS = {
    "User-Agent": "tech-event-radar/0.1 research-tool",
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SEC_HEADERS = {
    "User-Agent": os.getenv(
        "SEC_USER_AGENT",
        "tech-event-radar/0.1 contact@example.com",
    ),
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

CNINFO_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.cninfo.com.cn",
    "Referer": "https://www.cninfo.com.cn/new/disclosure/stock",
}

XUEQIU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://xueqiu.com",
}

DEFAULT_EDGE_PATH = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"

BULLISH_TERMS = {
    "buy",
    "bullish",
    "breakout",
    "beat",
    "long",
    "upside",
    "\u4e70\u5165",
    "\u770b\u591a",
    "\u7a81\u7834",
    "\u6da8",
    "\u5f3a\u52bf",
    "\u8d85\u9884\u671f",
}

BEARISH_TERMS = {
    "sell",
    "bearish",
    "breakdown",
    "miss",
    "short",
    "downside",
    "\u5356\u51fa",
    "\u770b\u7a7a",
    "\u7834\u4f4d",
    "\u8dcc",
    "\u51cf\u4ed3",
    "\u4e0d\u53ca\u9884\u671f",
}


HIGH_SIGNAL_KEYWORDS = {
    "definitive agreement": 28,
    "material definitive agreement": 35,
    "strategic partnership": 28,
    "strategic agreement": 28,
    "strategic collaboration": 28,
    "partnership": 18,
    "collaboration": 16,
    "supply agreement": 30,
    "customer win": 24,
    "major contract": 28,
    "multi-year": 16,
    "award": 14,
    "purchase order": 20,
    "data center": 12,
    "ai infrastructure": 18,
    "gpu": 12,
    "semiconductor": 10,
    "战略合作": 30,
    "合作协议": 26,
    "重大合同": 35,
    "框架协议": 20,
    "中标": 24,
    "订单": 20,
    "算力": 16,
    "数据中心": 14,
    "人工智能": 12,
    "芯片": 10,
    "半导体": 10,
}

EVENT_KEYWORDS = {
    "earnings": 18,
    "earnings call": 20,
    "guidance": 22,
    "raises outlook": 26,
    "investor day": 18,
    "analyst day": 18,
    "product launch": 16,
    "conference": 10,
    "webcast": 8,
    "业绩预告": 24,
    "业绩快报": 18,
    "业绩说明会": 16,
    "投资者关系活动": 12,
    "产品发布": 18,
    "发布会": 14,
    "电话会议": 12,
}

NEGATIVE_KEYWORDS = {
    "termination": -30,
    "terminated": -26,
    "investigation": -22,
    "subpoena": -18,
    "delisting": -35,
    "downgrade": -14,
    "cybersecurity incident": -24,
    "lawsuit": -16,
    "终止": -28,
    "立案": -24,
    "调查": -20,
    "诉讼": -16,
    "减持": -12,
    "退市": -35,
}

CORE_CONTRACT_KEYWORDS = {
    "definitive agreement",
    "material definitive agreement",
    "strategic partnership",
    "strategic agreement",
    "strategic collaboration",
    "supply agreement",
    "customer win",
    "major contract",
    "purchase order",
    "战略合作",
    "合作协议",
    "重大合同",
    "框架协议",
    "中标",
    "订单",
}

TECH_TERMS = [
    "ai",
    "artificial intelligence",
    "accelerator",
    "gpu",
    "asic",
    "semiconductor",
    "chip",
    "memory",
    "hbm",
    "cloud",
    "cybersecurity",
    "robotics",
    "data center",
    "算力",
    "人工智能",
    "芯片",
    "半导体",
    "存储",
    "机器人",
    "云计算",
    "数据中心",
]

WEAK_CATALYST_CONFIG: dict[str, Any] | None = None

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class WatchSymbol:
    market: str
    ticker: str
    name: str = ""
    cik: str = ""
    yahoo_symbol: str = ""
    xueqiu_symbol: str = ""
    cninfo_plate: str = ""
    rss_urls: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class PriceSignal:
    symbol: str
    close: float | None = None
    change_pct: float | None = None
    traded_value: float | None = None
    traded_value_ratio: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    confirms: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SocialSignal:
    source: str
    symbol: str
    post_count: int = 0
    bullish_count: int = 0
    bearish_count: int = 0
    sentiment_score: float = 0.0
    samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Event:
    ticker: str
    market: str
    name: str
    source: str
    title: str
    url: str
    published_date: str = ""
    event_date: str = ""
    snippet: str = ""
    raw_score: int = 0
    grade: str = "D"
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    price: PriceSignal | None = None
    social: SocialSignal | None = None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_date(value: str) -> dt.date | None:
    value = normalize_space(value)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed.date()
    except (TypeError, ValueError):
        return None


def extract_event_date(text: str, today: dt.date) -> dt.date | None:
    text = normalize_space(text)
    patterns = [
        r"(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})",
        r"(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return dt.date(
                    int(match.group("y")),
                    int(match.group("m")),
                    int(match.group("d")),
                )
            except ValueError:
                continue

    match = re.search(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日", text)
    if match:
        for year in (today.year, today.year + 1):
            try:
                candidate = dt.date(year, int(match.group("m")), int(match.group("d")))
            except ValueError:
                continue
            if candidate >= today - dt.timedelta(days=7):
                return candidate

    month_names = "|".join(MONTHS)
    match = re.search(
        rf"\b(?P<month>{month_names})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s*(?P<year>20\d{{2}})?\b",
        text,
        re.IGNORECASE,
    )
    if match:
        year = int(match.group("year") or today.year)
        month = MONTHS[match.group("month").lower().rstrip(".")]
        day = int(match.group("day"))
        for candidate_year in (year, year + 1):
            try:
                candidate = dt.date(candidate_year, month, day)
            except ValueError:
                continue
            if candidate >= today - dt.timedelta(days=7):
                return candidate
    return None


def date_in_window(
    value: str,
    today: dt.date,
    lookback_days: int,
    lookahead_days: int,
) -> bool:
    parsed = parse_date(value)
    if not parsed:
        return True
    return today - dt.timedelta(days=lookback_days) <= parsed <= today + dt.timedelta(days=lookahead_days)


def load_watchlist(path: Path) -> list[WatchSymbol]:
    rows: list[WatchSymbol] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            if not raw.get("ticker") or raw.get("ticker", "").startswith("#"):
                continue
            rss_urls = tuple(
                item.strip()
                for item in raw.get("rss_urls", "").split("|")
                if item.strip()
            )
            rows.append(
                WatchSymbol(
                    market=raw.get("market", "").upper().strip(),
                    ticker=raw.get("ticker", "").upper().strip(),
                    name=raw.get("name", "").strip(),
                    cik=raw.get("cik", "").strip().lstrip("0"),
                    yahoo_symbol=raw.get("yahoo_symbol", "").strip(),
                    xueqiu_symbol=raw.get("xueqiu_symbol", "").strip(),
                    cninfo_plate=raw.get("cninfo_plate", "").lower().strip(),
                    rss_urls=rss_urls,
                    notes=raw.get("notes", "").strip(),
                )
            )
    return rows


def load_weak_catalyst_config(path: Path = Path("config/weak_catalysts.json")) -> dict[str, Any]:
    default = {
        "hard_cap_score": 42,
        "hard_cap_terms": {},
        "soft_penalty_terms": {},
        "rescue_terms": list(CORE_CONTRACT_KEYWORDS),
    }
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return {**default, **payload}


def get_weak_catalyst_config() -> dict[str, Any]:
    global WEAK_CATALYST_CONFIG
    if WEAK_CATALYST_CONFIG is None:
        WEAK_CATALYST_CONFIG = load_weak_catalyst_config()
    return WEAK_CATALYST_CONFIG


def weak_catalyst_adjustment(text: str) -> tuple[int, int | None, list[str]]:
    config = get_weak_catalyst_config()
    lower = text.lower()
    rescue_terms = [str(term).lower() for term in config.get("rescue_terms", [])]
    has_rescue = any(term in lower for term in rescue_terms)
    penalty = 0
    cap: int | None = None
    reasons: list[str] = []

    for group, terms in config.get("soft_penalty_terms", {}).items():
        group_penalty = int(terms.get("penalty", 0)) if isinstance(terms, dict) else 0
        group_terms = terms.get("terms", []) if isinstance(terms, dict) else []
        if any(str(term).lower() in lower for term in group_terms):
            penalty += group_penalty
            reasons.append(f"weak_soft_{group}-{group_penalty}")

    if not has_rescue:
        hard_cap = int(config.get("hard_cap_score", 42))
        for group, terms in config.get("hard_cap_terms", {}).items():
            if any(str(term).lower() in lower for term in terms):
                cap = hard_cap if cap is None else min(cap, hard_cap)
                reasons.append(f"weak_hard_{group}_cap{hard_cap}")
    return penalty, cap, reasons


def request_json(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    response = session.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_cookie(raw_cookie: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            cookies[name] = value
    return cookies


def bootstrap_xueqiu(session: requests.Session, raw_cookie: str = "") -> None:
    session.headers.update(XUEQIU_HEADERS)
    if not raw_cookie:
        cookie_path = Path("config/xueqiu_cookie.txt")
        if cookie_path.exists():
            raw_cookie = cookie_path.read_text(encoding="utf-8").strip()
    if raw_cookie:
        session.cookies.update(parse_cookie(raw_cookie))
        return
    try:
        session.get("https://xueqiu.com/", headers=XUEQIU_HEADERS, timeout=15)
    except requests.RequestException:
        pass


def resolve_sec_ciks(session: requests.Session, symbols: list[WatchSymbol]) -> dict[str, str]:
    missing = [item.ticker for item in symbols if item.market == "US" and not item.cik]
    resolved = {item.ticker: item.cik for item in symbols if item.cik}
    if not missing:
        return resolved

    headers = dict(SEC_HEADERS)
    headers["Host"] = "www.sec.gov"
    payload = request_json(session, "https://www.sec.gov/files/company_tickers.json", headers=headers)
    for row in payload.values():
        ticker = str(row.get("ticker", "")).upper()
        if ticker in missing:
            resolved[ticker] = str(row.get("cik_str", "")).lstrip("0")
    return resolved


def fetch_sec_events(
    session: requests.Session,
    symbol: WatchSymbol,
    cik: str,
    today: dt.date,
    lookback_days: int,
    fetch_docs: bool,
) -> list[Event]:
    if not cik:
        return []
    padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        payload = request_json(session, url, headers=SEC_HEADERS)
    except requests.RequestException as exc:
        return [
            Event(
                ticker=symbol.ticker,
                market=symbol.market,
                name=symbol.name,
                source="SEC_ERROR",
                title=f"SEC fetch failed: {exc}",
                url=url,
                risk_flags=["data_fetch_failed"],
            )
        ]

    recent = payload.get("filings", {}).get("recent", {})
    events: list[Event] = []
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    items = recent.get("items", [])

    for index, form in enumerate(forms):
        filing_date = filing_dates[index] if index < len(filing_dates) else ""
        if not date_in_window(filing_date, today, lookback_days, 0):
            continue
        if form not in {"8-K", "8-K/A", "6-K", "10-Q", "10-K"}:
            continue
        accession = accession_numbers[index] if index < len(accession_numbers) else ""
        primary_doc = primary_docs[index] if index < len(primary_docs) else ""
        item_text = items[index] if index < len(items) else ""
        accession_path = accession.replace("-", "")
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{primary_doc}"
            if accession and primary_doc
            else url
        )
        title = f"{form} {item_text}".strip()
        snippet = ""
        if fetch_docs and filing_url != url:
            try:
                doc_headers = dict(DEFAULT_HEADERS)
                doc_headers["User-Agent"] = SEC_HEADERS["User-Agent"]
                text = session.get(filing_url, headers=doc_headers, timeout=20).text
                snippet = make_snippet(strip_html(text), HIGH_SIGNAL_KEYWORDS | EVENT_KEYWORDS)
                time.sleep(0.12)
            except requests.RequestException:
                snippet = ""
        events.append(
            Event(
                ticker=symbol.ticker,
                market=symbol.market,
                name=symbol.name,
                source="SEC",
                title=title,
                url=filing_url,
                published_date=filing_date,
                snippet=snippet,
            )
        )
    return events


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return normalize_space(value)


def make_snippet(text: str, keywords: dict[str, int], size: int = 260) -> str:
    lower = text.lower()
    positions = [lower.find(key.lower()) for key in keywords if lower.find(key.lower()) >= 0]
    if not positions:
        return text[:size]
    start = max(0, min(positions) - 90)
    return text[start : start + size]


def fetch_cninfo_events(
    session: requests.Session,
    symbol: WatchSymbol,
    today: dt.date,
    lookback_days: int,
) -> list[Event]:
    if symbol.market not in {"CN", "SH", "SZ", "BJ"}:
        return []
    plate = symbol.cninfo_plate or ("sh" if symbol.ticker.startswith("6") else "sz")
    column = {"sh": "sse", "sz": "szse", "bj": "bj"}.get(plate, "szse")
    start = today - dt.timedelta(days=lookback_days)
    keywords = "战略合作 重大合同 框架协议 中标 订单 算力 芯片 半导体 投资者关系 业绩说明会"
    data = {
        "pageNum": "1",
        "pageSize": "30",
        "column": column,
        "tabName": "fulltext",
        "plate": plate,
        "stock": symbol.ticker,
        "searchkey": keywords,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start:%Y-%m-%d}~{today:%Y-%m-%d}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    try:
        response = session.post(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            headers=CNINFO_HEADERS,
            data=data,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return [
            Event(
                ticker=symbol.ticker,
                market=symbol.market,
                name=symbol.name,
                source="CNINFO_ERROR",
                title=f"CNInfo fetch failed: {exc}",
                url="https://www.cninfo.com.cn/new/disclosure/stock",
                risk_flags=["data_fetch_failed"],
            )
        ]
    except ValueError:
        return []

    events: list[Event] = []
    for row in payload.get("announcements", []) or []:
        title = strip_html(row.get("announcementTitle", ""))
        adjunct_url = row.get("adjunctUrl", "")
        page_url = urljoin("https://static.cninfo.com.cn/", adjunct_url)
        published = ""
        ts = row.get("announcementTime")
        if isinstance(ts, int):
            published = dt.datetime.fromtimestamp(ts / 1000).date().isoformat()
        elif row.get("announcementTime"):
            published = str(row.get("announcementTime"))
        events.append(
            Event(
                ticker=symbol.ticker,
                market=symbol.market,
                name=symbol.name,
                source="CNINFO",
                title=title,
                url=page_url,
                published_date=published,
                snippet=title,
            )
        )
    return events


def parse_rss_date(entry: ET.Element) -> str:
    for tag in (
        "pubDate",
        "updated",
        "published",
        "dc:date",
        "{http://www.w3.org/2005/Atom}updated",
        "{http://www.w3.org/2005/Atom}published",
        "{http://purl.org/dc/elements/1.1/}date",
    ):
        node = entry.find(tag)
        if node is not None and node.text:
            parsed = parse_date(node.text)
            return parsed.isoformat() if parsed else normalize_space(node.text)
    return ""


def node_text(entry: ET.Element, tags: tuple[str, ...]) -> str:
    for tag in tags:
        node = entry.find(tag)
        if node is not None:
            if node.text:
                return normalize_space(node.text)
            href = node.attrib.get("href")
            if href:
                return href
    return ""


def fetch_rss_events(
    session: requests.Session,
    symbol: WatchSymbol,
    rss_urls: list[str],
    today: dt.date,
    lookback_days: int,
    lookahead_days: int,
) -> list[Event]:
    events: list[Event] = []
    if not rss_urls:
        return events
    symbol_terms = [symbol.ticker.lower()]
    if symbol.name:
        symbol_terms.extend(part.lower() for part in re.split(r"[\s,，]+", symbol.name) if len(part) >= 3)

    for feed_url in rss_urls:
        try:
            response = session.get(feed_url, headers=DEFAULT_HEADERS, timeout=20)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except requests.RequestException:
            continue
        except ET.ParseError:
            events.extend(fetch_html_ir_events(symbol, feed_url, response.text, today, lookback_days, lookahead_days))
            continue
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in entries[:80]:
            title = node_text(entry, ("title", "{http://www.w3.org/2005/Atom}title"))
            summary = node_text(
                entry,
                (
                    "description",
                    "summary",
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content",
                ),
            )
            link = node_text(entry, ("link", "{http://www.w3.org/2005/Atom}link"))
            published = parse_rss_date(entry)
            combined = f"{title} {summary}".lower()
            if not any(term and term in combined for term in symbol_terms):
                if not any(term in combined for term in TECH_TERMS):
                    continue
            if not published:
                detected = extract_event_date(f"{title} {summary}", today)
                if not detected or not (today <= detected <= today + dt.timedelta(days=lookahead_days)):
                    continue
                published = detected.isoformat()
            if not date_in_window(published, today, lookback_days, lookahead_days):
                continue
            events.append(
                Event(
                    ticker=symbol.ticker,
                    market=symbol.market,
                    name=symbol.name,
                    source="RSS",
                    title=title,
                    url=link or feed_url,
                    published_date=published,
                    snippet=strip_html(summary),
                )
            )
    return events


def fetch_html_ir_events(
    symbol: WatchSymbol,
    feed_url: str,
    page_text: str,
    today: dt.date,
    lookback_days: int,
    lookahead_days: int,
) -> list[Event]:
    text = strip_html(page_text)
    lower = text.lower()
    interesting_terms = {
        **HIGH_SIGNAL_KEYWORDS,
        **EVENT_KEYWORDS,
        **NEGATIVE_KEYWORDS,
    }
    matches = [term for term in interesting_terms if term.lower() in lower]
    if not matches:
        return []
    event_date = extract_event_date(text, today)
    if not event_date:
        return []
    if event_date and not (today - dt.timedelta(days=lookback_days) <= event_date <= today + dt.timedelta(days=lookahead_days)):
        return []
    snippet = make_snippet(text, interesting_terms, size=360)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page_text, re.IGNORECASE | re.DOTALL)
    title = strip_html(title_match.group(1)) if title_match else f"IR page keyword hit: {symbol.ticker}"
    return [
        Event(
            ticker=symbol.ticker,
            market=symbol.market,
            name=symbol.name,
            source="IR_HTML",
            title=title,
            url=feed_url,
            published_date=event_date.isoformat() if event_date else "",
            event_date=event_date.isoformat() if event_date else "",
            snippet=snippet,
        )
    ]


def extract_xueqiu_statuses(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "statuses", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return extract_xueqiu_statuses(data)
    return []


def clean_status_text(value: str) -> str:
    return strip_html(value).replace("\u200b", "").strip()


def compact_xueqiu_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": status.get("id"),
        "created_at": status.get("created_at"),
        "text": status.get("text") or status.get("description") or "",
        "title": status.get("title") or "",
        "like_count": status.get("like_count") or 0,
        "reply_count": status.get("reply_count") or 0,
        "retweet_count": status.get("retweet_count") or 0,
        "view_count": status.get("view_count") or 0,
        "user": {
            "id": (status.get("user") or {}).get("id") if isinstance(status.get("user"), dict) else None,
            "screen_name": (status.get("user") or {}).get("screen_name") if isinstance(status.get("user"), dict) else "",
        },
    }


def fetch_xueqiu_statuses_via_browser(xueqiu_symbol: str, count: int, timeout: int = 60) -> list[dict[str, Any]]:
    return fetch_xueqiu_statuses_batch_via_browser([xueqiu_symbol], count, timeout).get(xueqiu_symbol, [])


def fetch_xueqiu_statuses_batch_via_browser(
    xueqiu_symbols: list[str],
    count: int,
    timeout: int = 180,
) -> dict[str, list[dict[str, Any]]]:
    xueqiu_symbols = [symbol for symbol in dict.fromkeys(xueqiu_symbols) if symbol]
    if not xueqiu_symbols:
        return {}
    node_path = os.getenv(
        "NODE_EXE",
        "C:\\Users\\Sulfoxide\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\bin\\node.exe",
    )
    script_path = Path("tools/xueqiu_browser_status_fetch.js")
    edge_profile = Path(".xueqiu-edge-profile")
    if not script_path.exists() or not edge_profile.exists():
        return {}
    command = [node_path, str(script_path), ",".join(xueqiu_symbols), str(count)]
    env = os.environ.copy()
    env.setdefault("XUEQIU_BROWSER_HEADLESS", "1")
    env.setdefault("EDGE_PATH", DEFAULT_EDGE_PATH)
    try:
        completed = subprocess.run(
            command,
            cwd=Path.cwd(),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}

    if "results" in payload and isinstance(payload["results"], dict):
        output: dict[str, list[dict[str, Any]]] = {}
        for symbol, item in payload["results"].items():
            statuses = extract_xueqiu_statuses(item.get("statuses") if isinstance(item, dict) else item)
            output[symbol] = [compact_xueqiu_status(status) for status in statuses[:count]]
        return output

    symbol = str(payload.get("symbol") or xueqiu_symbols[0])
    statuses = extract_xueqiu_statuses(payload.get("statuses"))
    return {symbol: [compact_xueqiu_status(status) for status in statuses[:count]]}


def build_xueqiu_social_signal(
    xueqiu_symbol: str,
    statuses: list[dict[str, Any]],
    count: int,
    warning: str = "",
) -> SocialSignal:
    signal = SocialSignal(source="XUEQIU", symbol=xueqiu_symbol)
    if warning:
        signal.warnings.append(warning)
    if not statuses:
        signal.warnings.append("xueqiu_no_statuses_or_blocked")
        return signal

    for status in statuses[:count]:
        raw_text = str(status.get("text") or status.get("title") or status.get("description") or "")
        text = clean_status_text(raw_text)
        if not text:
            continue
        bullish, bearish = score_social_text(text)
        signal.bullish_count += bullish
        signal.bearish_count += bearish
        if len(signal.samples) < 3:
            signal.samples.append(text[:120])
    signal.post_count = len(statuses[:count])
    total_opinion = signal.bullish_count + signal.bearish_count
    if total_opinion:
        signal.sentiment_score = round((signal.bullish_count - signal.bearish_count) / total_opinion, 4)
    return signal


def score_social_text(text: str) -> tuple[int, int]:
    lower = text.lower()
    bullish = sum(1 for term in BULLISH_TERMS if term.lower() in lower)
    bearish = sum(1 for term in BEARISH_TERMS if term.lower() in lower)
    return bullish, bearish


def fetch_xueqiu_social(
    session: requests.Session,
    symbol: WatchSymbol,
    count: int,
    browser_fallback: bool = False,
) -> SocialSignal | None:
    xueqiu_symbol = symbol.xueqiu_symbol or symbol.ticker
    if not xueqiu_symbol:
        return None
    signal = SocialSignal(source="XUEQIU", symbol=xueqiu_symbol)
    endpoints = [
        (
            "https://xueqiu.com/query/v1/symbol/search/status.json",
            {"symbol": xueqiu_symbol, "count": str(count), "comment": "0", "page": "1"},
        ),
        (
            "https://xueqiu.com/statuses/search.json",
            {
                "symbol": xueqiu_symbol,
                "q": xueqiu_symbol,
                "count": str(count),
                "page": "1",
                "source": "all",
                "sort": "time",
            },
        ),
    ]
    statuses: list[dict[str, Any]] = []
    for url, params in endpoints:
        try:
            response = session.get(
                url,
                params=params,
                headers={**XUEQIU_HEADERS, "Referer": f"https://xueqiu.com/S/{xueqiu_symbol}"},
                timeout=20,
            )
            response.raise_for_status()
            statuses = extract_xueqiu_statuses(response.json())
            if statuses:
                break
        except (requests.RequestException, ValueError):
            continue
    if not statuses:
        if browser_fallback:
            statuses = fetch_xueqiu_statuses_via_browser(xueqiu_symbol, count)
            if statuses:
                return build_xueqiu_social_signal(
                    xueqiu_symbol,
                    statuses,
                    count,
                    warning="xueqiu_browser_fallback",
                )
        if not statuses:
            signal.warnings.append("xueqiu_no_statuses_or_blocked")
            return signal

    return build_xueqiu_social_signal(xueqiu_symbol, statuses, count)


def fetch_xueqiu_quote(
    session: requests.Session,
    symbol: WatchSymbol,
) -> PriceSignal | None:
    xueqiu_symbol = symbol.xueqiu_symbol or symbol.ticker
    if not xueqiu_symbol:
        return None
    try:
        response = session.get(
            "https://stock.xueqiu.com/v5/stock/realtime/quotec.json",
            params={"symbol": xueqiu_symbol},
            headers={**XUEQIU_HEADERS, "Referer": f"https://xueqiu.com/S/{xueqiu_symbol}"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return None
    signal = PriceSignal(symbol=xueqiu_symbol)
    for attr, keys in {
        "close": ("current", "last_close"),
        "change_pct": ("percent",),
        "traded_value": ("amount",),
        "turnover_rate": ("turnover_rate",),
    }.items():
        for key in keys:
            value = item.get(key)
            if isinstance(value, (int, float)):
                setattr(signal, attr, round(float(value), 4))
                break
    return signal


def fetch_yahoo_price(session: requests.Session, symbol: WatchSymbol) -> PriceSignal | None:
    yahoo_symbol = symbol.yahoo_symbol or symbol.ticker
    if not yahoo_symbol:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    params = {"range": "3mo", "interval": "1d"}
    signal = PriceSignal(symbol=yahoo_symbol)
    try:
        payload = request_json(session, url, params=params, headers=DEFAULT_HEADERS)
        result = payload["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        closes = [value for value in quote.get("close", []) if isinstance(value, (int, float))]
        volumes = [value for value in quote.get("volume", []) if isinstance(value, (int, float))]
    except (requests.RequestException, KeyError, IndexError, TypeError):
        signal.warnings.append("price_fetch_failed")
        return signal

    if len(closes) < 20:
        signal.warnings.append("not_enough_price_history")
        return signal
    signal.close = round(closes[-1], 4)
    if volumes:
        signal.traded_value = round(closes[-1] * volumes[-1], 2)
    previous = closes[-2] if len(closes) >= 2 else None
    if previous:
        signal.change_pct = round((closes[-1] / previous - 1) * 100, 2)
    signal.ma5 = round(sum(closes[-5:]) / 5, 4)
    signal.ma10 = round(sum(closes[-10:]) / 10, 4)
    signal.ma20 = round(sum(closes[-20:]) / 20, 4)
    if len(volumes) >= 21 and sum(volumes[-21:-1]) > 0:
        avg_vol = sum(volumes[-21:-1]) / 20
        signal.volume_ratio = round(volumes[-1] / avg_vol, 2)
        avg_value = sum(closes[-21 + offset] * volumes[-21 + offset] for offset in range(20)) / 20
        if avg_value > 0 and signal.traded_value is not None:
            signal.traded_value_ratio = round(signal.traded_value / avg_value, 2)
    if signal.close > signal.ma5:
        signal.confirms.append("close>MA5")
    if signal.close > signal.ma10:
        signal.confirms.append("close>MA10")
    if signal.close > signal.ma20:
        signal.confirms.append("close>MA20")
    if signal.change_pct is not None and signal.change_pct > 0:
        signal.confirms.append("positive_day")
    if signal.volume_ratio is not None and signal.volume_ratio >= 1.5:
        signal.confirms.append("volume_expansion")
    if signal.traded_value_ratio is not None and signal.traded_value_ratio >= 1.5:
        signal.confirms.append("traded_value_expansion")
    if signal.close < signal.ma20:
        signal.warnings.append("below_MA20")
    return signal


def score_event(event: Event, today: dt.date) -> Event:
    text = f"{event.title} {event.snippet}".lower()
    title_text = event.title.lower()
    score = 0
    has_catalyst = False
    actionable_sec_items = {"1.01", "2.02", "7.01", "8.01"}
    low_signal_sec = event.source == "SEC" and not any(item in title_text for item in actionable_sec_items)

    source_points = {
        "SEC": 30,
        "CNINFO": 30,
        "RSS": 12,
        "IR_HTML": 10,
        "XUEQIU": 8,
        "SEC_ERROR": 0,
        "CNINFO_ERROR": 0,
    }.get(event.source, 8)
    score += source_points
    if source_points:
        event.reasons.append(f"source={event.source}+{source_points}")

    sec_item_weights = {
        "1.01": ("sec_material_agreement", 24),
        "2.02": ("sec_results_guidance", 16),
        "7.01": ("sec_reg_fd", 10),
        "8.01": ("sec_other_event", 8),
    }
    if event.source == "SEC":
        for item, (label, weight) in sec_item_weights.items():
            if item in title_text:
                score += weight
                has_catalyst = True
                event.reasons.append(f"{label}+{weight}")

    for keyword, weight in HIGH_SIGNAL_KEYWORDS.items():
        if keyword.lower() in text:
            if low_signal_sec and keyword not in CORE_CONTRACT_KEYWORDS:
                continue
            score += weight
            has_catalyst = True
            event.reasons.append(f"{keyword}+{weight}")

    for keyword, weight in EVENT_KEYWORDS.items():
        if keyword.lower() in text:
            if low_signal_sec:
                continue
            score += weight
            has_catalyst = True
            event.reasons.append(f"{keyword}+{weight}")

    for keyword, weight in NEGATIVE_KEYWORDS.items():
        if keyword.lower() in text:
            score += weight
            event.risk_flags.append(keyword)

    if (not low_signal_sec or has_catalyst) and any(term in text for term in TECH_TERMS):
        score += 6
        event.reasons.append("tech_theme+6")

    date_text = event.title if low_signal_sec else f"{event.title} {event.snippet}"
    event_dt = parse_date(event.event_date) if event.event_date else extract_event_date(date_text, today)
    if event_dt:
        event.event_date = event_dt.isoformat()
        delta = (event_dt - today).days
        if 0 <= delta <= 5:
            score += 22
            has_catalyst = True
            event.reasons.append(f"near_future_event_{delta}d+22")
        elif -3 <= delta < 0:
            score += 12
            has_catalyst = True
            event.reasons.append("fresh_event_date+12")

    published = parse_date(event.published_date)
    if published:
        age = (today - published).days
        if 0 <= age <= 3:
            score += 14
            event.reasons.append("fresh_disclosure+14")
        elif 4 <= age <= 7:
            score += 7
            event.reasons.append("recent_disclosure+7")

    if event.price:
        if "close>MA5" in event.price.confirms:
            score += 5
            event.reasons.append("price_close_above_MA5+5")
        if "close>MA20" in event.price.confirms:
            score += 6
            event.reasons.append("price_close_above_MA20+6")
        if "volume_expansion" in event.price.confirms:
            score += 10
            event.reasons.append("volume_expansion+10")
        if "traded_value_expansion" in event.price.confirms:
            score += 8
            event.reasons.append("traded_value_expansion+8")
        if event.price.traded_value is not None:
            if event.price.traded_value >= 1_000_000_000:
                score += 5
                event.reasons.append("high_traded_value+5")
            elif event.price.traded_value >= 200_000_000:
                score += 3
                event.reasons.append("liquid_traded_value+3")
        if "below_MA20" in event.price.warnings:
            score -= 8
            event.risk_flags.append("price_below_MA20")

    if event.social:
        if event.social.post_count >= 20:
            score += 10
            has_catalyst = True
            event.reasons.append("xueqiu_hot_posts+10")
        elif event.social.post_count >= 8:
            score += 5
            event.reasons.append("xueqiu_active_posts+5")
        if event.social.sentiment_score >= 0.35 and event.social.bullish_count >= 2:
            score += 8
            event.reasons.append("xueqiu_bullish_sentiment+8")
        elif event.social.sentiment_score <= -0.35 and event.social.bearish_count >= 2:
            score -= 8
            event.risk_flags.append("xueqiu_bearish_sentiment")

    weak_penalty, weak_cap, weak_reasons = weak_catalyst_adjustment(f"{event.title} {event.snippet}")
    if weak_penalty:
        score -= weak_penalty
    if weak_cap is not None:
        score = min(score, weak_cap)
    event.reasons.extend(weak_reasons)

    if event.source == "IR_HTML":
        has_core = any(term.lower() in text for term in CORE_CONTRACT_KEYWORDS)
        if not has_core and any(reason.startswith("weak_soft_conference_and_marketing") for reason in weak_reasons):
            score = min(score, 44)
            event.reasons.append("ir_html_weak_conference_cap44")

    has_business_risk = any(
        not flag.startswith("price_") and flag != "data_fetch_failed"
        for flag in event.risk_flags
    )
    if not has_catalyst and not has_business_risk and event.source not in {"SEC_ERROR", "CNINFO_ERROR"}:
        score = min(score, 34)
        event.reasons.append("no_catalyst_cap=34")

    event.raw_score = min(100, max(0, score))
    if has_business_risk and event.raw_score < 50:
        event.grade = "RISK"
    elif event.raw_score >= 85:
        event.grade = "A"
    elif event.raw_score >= 65:
        event.grade = "B"
    elif event.raw_score >= 45:
        event.grade = "C"
    else:
        event.grade = "D"
    return event


def dedupe_events(events: list[Event]) -> list[Event]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[Event] = []
    for event in events:
        key = (event.ticker, event.source, normalize_space(event.title).lower()[:140])
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def attach_prices(events: list[Event], prices: dict[str, PriceSignal | None]) -> None:
    for event in events:
        event.price = prices.get(event.ticker)


def attach_social(events: list[Event], social: dict[str, SocialSignal | None]) -> None:
    for event in events:
        event.social = social.get(event.ticker)


def merge_price_signals(primary: PriceSignal | None, secondary: PriceSignal | None) -> PriceSignal | None:
    if not primary:
        return secondary
    if not secondary:
        return primary
    for attr in ("close", "change_pct", "traded_value", "turnover_rate"):
        if getattr(primary, attr) is None and getattr(secondary, attr) is not None:
            setattr(primary, attr, getattr(secondary, attr))
    return primary


def social_events(
    symbols: list[WatchSymbol],
    social: dict[str, SocialSignal | None],
    today: dt.date,
) -> list[Event]:
    events: list[Event] = []
    for symbol in symbols:
        signal = social.get(symbol.ticker)
        if not signal or signal.post_count <= 0:
            continue
        title = f"Xueqiu social buzz: {signal.post_count} recent posts"
        snippet = " | ".join(signal.samples)
        events.append(
            Event(
                ticker=symbol.ticker,
                market=symbol.market,
                name=symbol.name,
                source="XUEQIU",
                title=title,
                url=f"https://xueqiu.com/S/{signal.symbol}",
                published_date=today.isoformat(),
                snippet=snippet,
                social=signal,
            )
        )
    return events


def price_summary(price: PriceSignal | None) -> str:
    if not price:
        return "-"
    parts = []
    if price.close is not None:
        parts.append(f"close={price.close}")
    if price.change_pct is not None:
        parts.append(f"chg={price.change_pct}%")
    if price.volume_ratio is not None:
        parts.append(f"volx={price.volume_ratio}")
    if price.traded_value is not None:
        parts.append(f"value={price.traded_value:.0f}")
    if price.traded_value_ratio is not None:
        parts.append(f"valuex={price.traded_value_ratio}")
    if price.turnover_rate is not None:
        parts.append(f"turnover={price.turnover_rate}%")
    if price.confirms:
        parts.append(",".join(price.confirms))
    if price.warnings:
        parts.append("warn:" + ",".join(price.warnings))
    return " ".join(parts) or "-"


def social_summary(social: SocialSignal | None) -> str:
    if not social:
        return "-"
    parts = [f"posts={social.post_count}"]
    if social.bullish_count or social.bearish_count:
        parts.append(f"bull={social.bullish_count}")
        parts.append(f"bear={social.bearish_count}")
        parts.append(f"sent={social.sentiment_score}")
    if social.warnings:
        parts.append("warn:" + ",".join(social.warnings))
    return " ".join(parts)


def markdown_table(events: list[Event], today: dt.date) -> str:
    lines = [
        f"# Tech Event Radar - {today.isoformat()}",
        "",
        "This report is a catalyst screen, not a buy/sell recommendation. Trade only after price and risk rules confirm.",
        "",
        "| Grade | Ticker | Score | Source | Published | Event date | Title | Price signal | Social | Why | Link |",
        "|---|---:|---:|---|---|---|---|---|---|---|---|",
    ]
    for event in events:
        title = normalize_space(event.title).replace("|", "\\|")[:120]
        why = "; ".join(event.reasons[:5])
        if event.risk_flags:
            why += " risk=" + ",".join(event.risk_flags[:4])
        lines.append(
            "| {grade} | {ticker} | {score} | {source} | {published} | {event_date} | {title} | {price} | {social} | {why} | [open]({url}) |".format(
                grade=event.grade,
                ticker=event.ticker,
                score=event.raw_score,
                source=event.source,
                published=event.published_date or "-",
                event_date=event.event_date or "-",
                title=title,
                price=price_summary(event.price).replace("|", "\\|"),
                social=social_summary(event.social).replace("|", "\\|"),
                why=why.replace("|", "\\|") or "-",
                url=event.url,
            )
        )
    if not events:
        lines.append("| - | - | - | - | - | - | No events found | - | - | - | - |")

    lines.extend(
        [
            "",
            "## Trading Filter",
            "",
            "- A/B: review manually, then require breakout/reclaim/volume confirmation before entry.",
            "- C: watchlist only unless the company later files a stronger official disclosure.",
            "- D/RISK: ignore for short-term long trades unless there is a separate, verified thesis.",
            "- Max loss per trade should be fixed before entry; catalyst screens do not replace stops.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(events: list[Event], markdown_path: Path, json_path: Path, today: dt.date) -> None:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown_table(events, today), encoding="utf-8")
    json_path.write_text(
        json.dumps([event_to_json(event) for event in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def event_to_json(event: Event) -> dict[str, Any]:
    data = asdict(event)
    if event.price:
        data["price"] = asdict(event.price)
    return data


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and score near-term technology stock catalyst events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python tech_event_radar.py --watchlist config/watchlist.example.csv
              python tech_event_radar.py --watchlist my.csv --lookback-days 3 --lookahead-days 5 --min-score 45

            Tip:
              Set SEC_USER_AGENT to a real contact string before heavy SEC use.
            """
        ),
    )
    parser.add_argument("--watchlist", default="config/watchlist.example.csv", help="CSV watchlist path")
    parser.add_argument("--allowed-prefixes", default=",".join(DEFAULT_BUYABLE_PREFIXES), help="buyable ticker prefixes; use 'all' to disable")
    parser.add_argument("--lookback-days", type=int, default=7, help="recent filing/news window")
    parser.add_argument("--lookahead-days", type=int, default=5, help="future event window for detected dates")
    parser.add_argument("--min-score", type=int, default=35, help="minimum score to include in report")
    parser.add_argument("--today", default="", help="override today's date as YYYY-MM-DD")
    parser.add_argument("--out", default="", help="markdown report path")
    parser.add_argument("--json-out", default="", help="JSON report path")
    parser.add_argument("--skip-sec-docs", action="store_true", help="do not download SEC primary documents")
    parser.add_argument("--skip-price", action="store_true", help="skip Yahoo price confirmation")
    parser.add_argument("--skip-xueqiu", action="store_true", help="skip Xueqiu quote and social signals")
    parser.add_argument(
        "--no-xueqiu-browser-fallback",
        action="store_true",
        help="disable logged-in Edge fallback when Xueqiu social endpoints return WAF HTML",
    )
    parser.add_argument(
        "--xueqiu-browser-batch-size",
        type=int,
        default=10,
        help="symbols per logged-in Edge fallback batch",
    )
    parser.add_argument("--xueqiu-cookie", default=os.getenv("XUEQIU_COOKIE", ""), help="raw Xueqiu Cookie header")
    parser.add_argument("--xueqiu-count", type=int, default=20, help="recent Xueqiu posts to inspect per symbol")
    parser.add_argument("--rss-url", action="append", default=[], help="extra RSS/Atom feed URL, repeatable")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    today = parse_date(args.today) if args.today else dt.date.today()
    if not today:
        raise SystemExit("--today must be YYYY-MM-DD")

    watchlist_path = Path(args.watchlist)
    if not watchlist_path.exists():
        raise SystemExit(f"Watchlist not found: {watchlist_path}")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    symbols = filter_symbols(load_watchlist(watchlist_path), args.allowed_prefixes)
    if not symbols:
        raise SystemExit("Watchlist is empty")

    sec_ciks = resolve_sec_ciks(session, symbols)
    prices: dict[str, PriceSignal | None] = {}
    social: dict[str, SocialSignal | None] = {}
    xueqiu_session = requests.Session()
    if not args.skip_xueqiu:
        bootstrap_xueqiu(xueqiu_session, args.xueqiu_cookie)

    if not args.skip_price:
        for symbol in symbols:
            prices[symbol.ticker] = fetch_yahoo_price(session, symbol)
            if not args.skip_xueqiu:
                xueqiu_quote = fetch_xueqiu_quote(xueqiu_session, symbol)
                prices[symbol.ticker] = merge_price_signals(prices.get(symbol.ticker), xueqiu_quote)
            time.sleep(0.1)

    all_events: list[Event] = []
    for symbol in symbols:
        if symbol.market == "US":
            all_events.extend(
                fetch_sec_events(
                    session,
                    symbol,
                    sec_ciks.get(symbol.ticker, symbol.cik),
                    today,
                    args.lookback_days,
                    fetch_docs=not args.skip_sec_docs,
                )
            )
        if symbol.market in {"CN", "SH", "SZ", "BJ"}:
            all_events.extend(fetch_cninfo_events(session, symbol, today, args.lookback_days))

        rss_urls = list(symbol.rss_urls) + list(args.rss_url)
        all_events.extend(
            fetch_rss_events(
                session,
                symbol,
                rss_urls,
                today,
                args.lookback_days,
                args.lookahead_days,
            )
        )
        if not args.skip_xueqiu:
            if args.no_xueqiu_browser_fallback:
                social[symbol.ticker] = fetch_xueqiu_social(xueqiu_session, symbol, args.xueqiu_count)
            else:
                social[symbol.ticker] = SocialSignal(
                    source="XUEQIU",
                    symbol=symbol.xueqiu_symbol or symbol.ticker,
                    warnings=["xueqiu_browser_pending"],
                )
        time.sleep(0.15)

    if not args.skip_xueqiu and not args.no_xueqiu_browser_fallback:
        fallback_symbols: list[str] = []
        symbol_by_xueqiu: dict[str, WatchSymbol] = {}
        for symbol in symbols:
            xueqiu_symbol = symbol.xueqiu_symbol or symbol.ticker
            signal = social.get(symbol.ticker)
            if (
                xueqiu_symbol
                and signal
                and signal.post_count == 0
                and (
                    "xueqiu_no_statuses_or_blocked" in signal.warnings
                    or "xueqiu_browser_pending" in signal.warnings
                )
            ):
                fallback_symbols.append(xueqiu_symbol)
                symbol_by_xueqiu[xueqiu_symbol] = symbol

        batch_size = max(1, args.xueqiu_browser_batch_size)
        for start in range(0, len(fallback_symbols), batch_size):
            batch = fallback_symbols[start : start + batch_size]
            browser_statuses = fetch_xueqiu_statuses_batch_via_browser(
                batch,
                args.xueqiu_count,
                timeout=max(120, 25 * len(batch)),
            )
            for xueqiu_symbol in batch:
                statuses = browser_statuses.get(xueqiu_symbol, [])
                symbol = symbol_by_xueqiu.get(xueqiu_symbol)
                if symbol:
                    social[symbol.ticker] = build_xueqiu_social_signal(
                        xueqiu_symbol,
                        statuses,
                        args.xueqiu_count,
                        warning="xueqiu_browser_fallback",
                    )

    if not args.skip_xueqiu:
        all_events.extend(social_events(symbols, social, today))

    all_events = dedupe_events(all_events)
    attach_prices(all_events, prices)
    attach_social(all_events, social)
    scored = [score_event(event, today) for event in all_events]
    scored = [
        event
        for event in scored
        if event.raw_score >= args.min_score
        or any(
            not flag.startswith("price_") and flag != "data_fetch_failed"
            for flag in event.risk_flags
        )
    ]
    scored.sort(key=lambda item: (item.raw_score, item.published_date), reverse=True)

    default_name = f"tech_event_radar_{today:%Y%m%d}"
    markdown_path = Path(args.out or f"output/{default_name}.md")
    json_path = Path(args.json_out or f"output/{default_name}.json")
    write_outputs(scored, markdown_path, json_path, today)

    print(f"symbols={len(symbols)} events={len(scored)}")
    print(f"markdown={markdown_path}")
    print(f"json={json_path}")
    for event in scored[:10]:
        print(f"{event.grade} {event.ticker} score={event.raw_score} {event.source} {event.title[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
