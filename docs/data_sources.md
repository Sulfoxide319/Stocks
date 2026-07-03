# Data Sources And Stock Pool

The system now uses three signal groups:

1. Official events: SEC, CNInfo, RSS/IR feeds.
2. Market confirmation: price trend, volume expansion, traded value expansion, turnover rate.
3. Social attention: Xueqiu recent posts and simple bullish/bearish keyword counts.

Official events keep the highest weight. Xueqiu is intentionally lower weight because comments can be noisy, promotional, or delayed.

## Expanded Stock Pool

The example watchlist now includes 86 technology names:

- U.S. AI and cloud: `NVDA`, `AMD`, `MSFT`, `GOOGL`, `AMZN`, `META`, `PLTR`, `ORCL`, `SMCI`, `DELL`, `HPE`.
- U.S. semiconductor chain: `AVGO`, `MU`, `TSM`, `ASML`, `AMAT`, `LRCX`, `KLAC`, `MRVL`, `QCOM`, `INTC`, `ARM`, `CDNS`, `SNPS`, `ADI`, `TXN`, `NXPI`, `ON`.
- U.S. software/security/networking: `ANET`, `CRWD`, `NOW`, `SNOW`, `PANW`, `ZS`, `NET`, `DDOG`, `MDB`, `TEAM`, `ADBE`, `CRM`.
- U.S. data center power/infrastructure: `VRT`, `ETN`, `CEG`, `OKLO`.
- China semiconductor, AI server, optical module, PCB, robotics, software, and AI vision names such as `688981`, `688012`, `688256`, `002371`, `000977`, `300308`, `300502`, `002463`, `300476`, `002415`.

Edit:

```powershell
notepad .\config\watchlist.example.csv
```

Important columns:

- `ticker`: internal id used by the scripts.
- `yahoo_symbol`: price source symbol, such as `NVDA`, `688981.SS`, `300308.SZ`.
- `xueqiu_symbol`: Xueqiu symbol, such as `NVDA`, `SH688981`, `SZ300308`.
- `rss_urls`: optional company IR/news RSS feeds separated by `|`.

## Xueqiu Usage

The radar tries Xueqiu by default:

```powershell
python .\tech_event_radar.py --watchlist .\config\watchlist.example.csv --xueqiu-count 20
```

Disable it:

```powershell
python .\tech_event_radar.py --skip-xueqiu
```

If Xueqiu returns no posts or blocks the request, provide a browser Cookie by either environment variable or a local ignored file.

```powershell
$env:XUEQIU_COOKIE='paste raw Cookie header here'
python .\tech_event_radar.py --watchlist .\config\watchlist.example.csv
```

Or:

```powershell
copy .\config\xueqiu_cookie.example.txt .\config\xueqiu_cookie.txt
notepad .\config\xueqiu_cookie.txt
python .\tech_event_radar.py --watchlist .\config\watchlist.example.csv
```

`config/xueqiu_cookie.txt` is ignored by git. Do not commit real cookies.

You can capture a logged-in cookie with a temporary Edge profile:

```powershell
node .\tools\capture_xueqiu_cookie_edge.js
```

If `.xueqiu-edge-profile` already contains a logged-in session, this refreshes the cookie in headless/backend mode and writes only the raw Cookie header to `config/xueqiu_cookie.txt`.

For first-time login or manual re-login, run it with a visible Edge window:

```powershell
$env:XUEQIU_COOKIE_CAPTURE_HEADLESS='0'
node .\tools\capture_xueqiu_cookie_edge.js
```

You can probe a single symbol:

```powershell
python .\xueqiu_probe.py SH688981
python .\xueqiu_probe.py NVDA
```

When the JSON social endpoints return WAF HTML or `405` in normal `requests`, `tech_event_radar.py` automatically falls back to the logged-in Edge profile. The browser fallback tries the stock discussion API first, then opens the Xueqiu hashtag page such as `https://xueqiu.com/k?q=#京东方A#` and extracts rendered discussion text from the page DOM. Disable that fallback only when needed:

```powershell
python .\tech_event_radar.py --no-xueqiu-browser-fallback
```

The fallback defaults to headless Edge, so scheduled/backend runs do not need a visible browser window. If you need to debug the browser flow visually:

```powershell
$env:XUEQIU_BROWSER_HEADLESS='0'
python .\tech_event_radar.py --xueqiu-count 3
```

Direct browser-post probe:

```powershell
node .\tools\xueqiu_browser_status_fetch.js SH688981,SZ000725 3
```

Small end-to-end fallback test:

```powershell
python .\tech_event_radar.py --watchlist .\config\watchlist.xueqiu_test.csv --today 2026-07-03 --skip-price --skip-sec-docs --xueqiu-count 5 --min-score 0
```

## Market Activity Fields

The radar now adds these fields to price signals:

- `traded_value`: close price multiplied by volume when Yahoo history is available, or Xueqiu real-time amount when available.
- `traded_value_ratio`: current traded value divided by the previous 20-bar average traded value.
- `turnover_rate`: Xueqiu quote turnover rate when available.
- `volume_ratio`: current volume divided by previous 20-bar average volume.

Scoring uses these conservatively:

- traded value expansion: modest positive score,
- high absolute traded value: modest positive score,
- price below MA20: risk flag,
- Xueqiu hot posts or bullish comments: low-to-medium positive score only.

## Practical Interpretation

The best candidate is not "most discussed." It is:

1. official or company-verifiable catalyst,
2. high enough traded value to enter and exit,
3. price above key moving averages,
4. social attention increasing without obvious bearish language.

If Xueqiu is blocked, the system still works with official events and Yahoo market data.

## Weak Catalyst Blacklist

Weak catalyst rules live in:

```powershell
config\weak_catalysts.json
```

The blacklist uses two mechanisms:

- `hard_cap_terms`: caps the score, usually to `42`, for weak events like board changes, shareholder meeting votes, routine dividends, awards/rankings, and ordinary debt financing.
- `soft_penalty_terms`: subtracts points from lower-signal items like conferences, marketing presentations, and routine SEC filing items.

`rescue_terms` prevent real catalysts from being accidentally killed. For example, a title containing `strategic agreement` or `supply agreement` can still score well even if the same page has weaker wording elsewhere.

After expanding the database to 86 symbols, the one-month default backtest produced more signals but weaker returns. This is expected: a larger stock pool increases coverage, but it also increases noisy events and high-volatility losers. Use the larger pool for discovery, then add liquidity, trend, and event-type filters before trading.

## Official IR/RSS Sources Added

The U.S. technology watchlist now includes official RSS or IR/news pages for:

- NVIDIA, AMD, Broadcom, Micron, Applied Materials, CrowdStrike through validated RSS feeds.
- Microsoft, Alphabet, Amazon, Meta, TSMC, ASML, Lam Research, KLA, Marvell, Qualcomm, Intel, Arista, Palantir, ServiceNow, Snowflake, and Oracle through official IR/news HTML pages when RSS is not stable.

HTML pages are stricter than RSS:

- the page must contain a catalyst keyword,
- the script must find a date inside the scan window,
- otherwise no event is emitted.
