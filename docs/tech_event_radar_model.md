# 科技股短线事件雷达模型

目标：每天自动查找科技股未来 1-5 天或最近 1-7 天内的可验证事件，筛出可能影响 1-3 天短线波动的候选标的。

这不是自动交易模型。它只回答三个问题：

1. 有无官方或高可信事件？
2. 事件是否和科技主线相关？
3. 价格和成交量是否已经确认？

## 数据源分层

优先级从高到低：

1. 官方监管披露：SEC 8-K/6-K/10-Q/10-K，巨潮资讯公告。
2. 公司自己发布：投资者关系页面、公司新闻稿、RSS/Atom。
3. 新闻稿渠道：Business Wire、PR Newswire 等，可通过 RSS 或公司 IR 页面接入。
4. 行情确认：Yahoo Finance 日线数据，A 股可用 `.SS` / `.SZ` 后缀。
5. 社媒和论坛：只作为低分线索，不作为买入依据。

## 事件关键词

高权重：

- 英文：`definitive agreement`、`strategic partnership`、`supply agreement`、`customer win`、`major contract`、`AI infrastructure`、`data center`。
- 中文：`战略合作`、`合作协议`、`重大合同`、`框架协议`、`中标`、`订单`、`算力`、`数据中心`。

中权重：

- 英文：`earnings`、`guidance`、`investor day`、`analyst day`、`product launch`、`conference`。
- 中文：`业绩预告`、`业绩快报`、`业绩说明会`、`投资者关系活动`、`产品发布`。

风险词：

- 英文：`termination`、`investigation`、`delisting`、`lawsuit`、`cybersecurity incident`。
- 中文：`终止`、`立案`、`调查`、`诉讼`、`减持`、`退市`。

## 打分逻辑

基础分：

- SEC / 巨潮公告：+30
- RSS / 新闻稿：+12
- 明确高权重合作或合同词：+18 到 +35
- 财报、指引、投资者日、发布会：+8 到 +26
- 科技主题命中：+6
- 事件日期在未来 0-5 天：+22
- 披露日期在最近 0-3 天：+14

价格确认：

- 收盘价站上 5 日线：+5
- 收盘价站上 20 日线：+6
- 成交量大于 20 日均量 1.5 倍：+10
- 跌破 20 日线：-8

等级：

- A：85 分以上，值得人工重点复核。
- B：65-84 分，进入短线观察池。
- C：45-64 分，只观察，等待二次确认。
- D：45 分以下，忽略。
- RISK：风险词明显，通常不做短线多头。

## 交易过滤

脚本输出候选后，还需要人工执行交易过滤：

- 事件要有官方链接，不用纯传闻。
- 合作公告当天如果高开太多，不追第一笔。
- 优先等回踩不破、突破放量、或 5/10 日线重新转强。
- 买入前确定止损，不因事件叙事临时扩大亏损。
- 单笔最大亏损建议控制在账户本金 0.5%-1%。

## 使用方式

安装依赖：

```powershell
pip install requests
```

运行示例：

```powershell
python .\tech_event_radar.py --watchlist .\config\watchlist.example.csv
```

输出：

- `output/tech_event_radar_YYYYMMDD.md`
- `output/tech_event_radar_YYYYMMDD.json`

建议每日运行两次：

- 盘前：找未来事件和隔夜公告。
- 收盘后：复盘公告和价格确认。

## 后续增强

1. 为每家公司补充精确 IR RSS 或新闻稿 URL。
2. 为 A 股 watchlist 补充巨潮 `orgId`，提高公告匹配稳定性。
3. 增加指数过滤：`QQQ`、`SMH`、科创 50、创业板指。
4. 加入邮件、微信、飞书或 Telegram 推送。
5. 保存历史事件与实际涨跌，用回测校准权重。
