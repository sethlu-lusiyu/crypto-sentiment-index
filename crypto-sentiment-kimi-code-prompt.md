# Crypto Sentiment Index — Kimi Code 完整执行 Prompt


# 项目：纯文本加密货币情绪指数系统（Per-Coin News/Social Sentiment Index）

## 0. 项目铁律（最高优先级，违反即为失败）

1. **纯度原则**：指数只能由"文本内容本身"派生。禁止把价格、涨跌幅、波动率、资金费率、交易量、持仓量等任何市场数据混入情绪分数或指数计算。市场数据仅允许出现在面板的对比图层（可选开关）。
2. **零人工标注**：所有情绪判定由 LLM 完成。不允许出现"请人工标注"的流程。
3. **零付费依赖**：只用免费数据源和免费额度。所有外部调用失败必须有降级路径，单源失败不得中断管道。
4. **双指数体系**：每条情绪记录必须带 `family` 字段（`news` 或 `social`），两类指数完全分开计算、分开存储、分开展示。
5. **新闻作用域**：每条新闻必须分类为 `scope=coin`（影响特定币种）或 `scope=market`（影响整个市场，如监管政策、宏观、ETF、系统性事件）。
6. 先做端到端最小闭环再扩展。所有代码要有 README 和注释，commit 信息用英文。

## 1. 技术栈（固定，不要换）

- Python 3.11+，包管理用 pip + requirements.txt
- 采集：httpx (async)、feedparser；调度：GitHub Actions cron（每小时）
- 数据库：SQLite（`data/sentiment.db`，wal 模式），用 SQLAlchemy Core 或原生 sqlite3
- LLM：OpenAI Python SDK，base_url/model 走环境变量（默认 Kimi：`https://api.moonshot.cn/v1` + `kimi-k2`；须兼容 DeepSeek 等）
- 前端：纯静态（原生 JS + TradingView 官方开源库 lightweight-charts CDN 版），部署 GitHub Pages（`/docs` 目录）
- 数据流：`采集 → 去重 → LLM 评分 → 入库 → 每小时聚合 → 导出 JSON 到 docs/data/ → Pages 展示`

## 2. 数据源（以下接口已验证可用，直接实现；每源独立模块 sources/<name>.py，统一返回 RawItem）

RawItem 字段：`{source, family, url, title, text, author, published_at, fetched_at, lang, raw_hash}`

### 新闻类（family=news）
1. `news_rss`：RSS 列表读 config/feeds.yaml（预置：cointelegraph.com/rss、decrypt.co/feed、beincrypto.com/feed/、cryptoslate.com/feed/），feedparser 解析
2. `google_news`：`https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en`，query 用每个币的英文名+"crypto"轮换，控制频率（每 query 间隔 ≥2s，每小时最多 30 个 query 轮换覆盖 Top200）
3. `bing_news`：`https://www.bing.com/news/search?q={query}&format=rss`，同上轮换
4. `cryptopanic`：免费 API（env `CRYPTOPANIC_TOKEN`，无 token 则跳过该源），只取标题+摘要文本，**忽略其投票数据**
5. `binance_ann`：`https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=48&pageNo=1&pageSize=20`（上币/产品公告，强事件源）

### 社媒类（family=social）
6. `bluesky`：`https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={coin alias}`，未认证可用，限速 ≥2s/次，每小时按币种轮换查询
7. `stocktwits`：`https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.X.json`（如 BTC.X、ETH.X），免 key
8. `reddit_arctic`：`https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=cryptocurrency&limit=100&after={last_ts}` + comments 端点，增量拉取；可选增强：PRAW（env 有 REDDIT_CLIENT_ID 才启用）
9. `4chan_biz`：`https://a.4cdn.org/biz/catalog.json` 遍历 thread 取含币别名的文本
10. 预留接口基类 `SourceBase`，后续可加 Telegram/Farcaster/Nostr，但**本版本不实现**

## 3. 币种表与别名（coins 模块）

- 启动时用 CoinGecko 免费 API `api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=200` 拉 Top200（失败则用 config/coins_fallback.json 内置 Top50 兜底）
- 为每个币生成别名集：`[symbol大写, "$SYMBOL", name, name小写, 常见中文名]`；中文名和常见别名由 LLM 一次性 bootstrap 生成（prompt 见附录 A3），存 `data/aliases.json`，每周刷新
- 歧义词表 config/ambiguous_symbols.json 预置 `NEAR, LINK, ATOM, SOL, ONE, SAND, MANA, APE, RAY, BAND, COMP, DASH, REQ, FET, ALICE`：这些词只有命中 `$前缀` 或与 crypto 共现词（price, chart, pump, dump, 币, 链, token, blockchain...）同句时才归因

## 4. 处理管道（pipeline/hourly_job.py，GitHub Actions 每小时触发）

步骤：
1. **采集**：并发跑所有源，单源 try/except 隔离，写 `raw_items` 表（url+text 的 sha256 为 raw_hash，UNIQUE 去重）
2. **预过滤**：丢弃 <20 字符、纯 URL、已知广告模板（SimHash 相似度 >0.9 的历史簇）
3. **归因**：按别名表匹配候选币种（正则 `\b(alias)\b`，区分大小写规则按 §3），生成 `pending_scores`（text, coin_candidates）
4. **LLM 评分**：
   - news 文本：逐条调用，prompt 见附录 A1（含 scope 判定）
   - social 文本：批量（每批 ≤15 条）调用，prompt 见附录 A2；每批次加 `batch_id`
   - 所有 LLM 输出必须 jsonschema 校验，解析失败重试 1 次后丢弃并记日志
   - 限流与预算：env `LLM_MAX_CALLS_PER_RUN`（默认 200），超额文本留到下小时队列
5. **入库** `scores` 表
6. **聚合**（见 §5）写 `index_hourly` 表
7. **导出** `docs/data/`：见 §7
8. **自校验**：随机抽 2% 已评分文本，用备选模型（env `LLM_MODEL_2`，未配置则同模型不同 temperature=0.7）复评，方向一致率写入 `quality_log` 表（自动化质检，不需要人）

## 5. 指数计算（必须严格按此实现）

记号：每条 score 记录含 `value ∈ [-1,1]`（由 LLM 的 direction∈[-2..2] 除以 2）、`confidence ∈ [0,1]`、`weight`、`scope`。

### 5.1 单币种双指数（每小时）
对每个 coin × family ∈ {news, social}：
```
SENT(coin, family, t) = Σᵢ valueᵢ·confᵢ·wᵢ·dᵢ  /  Σᵢ confᵢ·wᵢ·dᵢ   （仅取 scope=coin 且归因到该 coin 的记录）
```
- `w`（来源权重）：news 源=1.0，Binance 公告=1.5；social 源=0.8；LLM 判 `is_shill=true` 的 ×0.2
- `d`（时间衰减）：`d = 0.5^((t - published_at)/half_life)`；news half_life=24h，social half_life=6h
- 同时输出 `mentions(coin,family,t)` = 该小时新归因文本数
- **z-score**：`z = (SENT - mean30d) / std30d`（滚动 30 天；不满 30 天期间 z=null，前端画虚线）
- 样本不足规则：当小时有效记录 <3 时，`confidence_flag=low`，前端标注

### 5.2 全市场新闻指数（处理"政策类影响整体"的需求）
```
MARKET_NEWS(t) = Σ valueᵢ·confᵢ·wᵢ·dᵢ / Σ confᵢ·wᵢ·dᵢ   （仅取 family=news 且 scope=market 的记录）
```

### 5.3 整体指数（合成，仅由单币种指数派生，不单独采数）
```
OVERALL_NEWS(t)   = 市值加权 Σ_coin SENT(coin, news, t)  + 0.5·MARKET_NEWS(t)，再归一化到 [-1,1]
OVERALL_SOCIAL(t) = 市值加权 Σ_coin SENT(coin, social, t)
BREADTH(t)        = 净看涨币占比(SENT>0.1) - 净看跌币占比(SENT<-0.1)
```
市值权重用 CoinGecko 返回的 market_cap，每日刷新。

## 6. 数据库 Schema（data/sentiment.db）

```sql
CREATE TABLE raw_items(
  id INTEGER PRIMARY KEY, source TEXT, family TEXT CHECK(family IN('news','social')),
  url TEXT, title TEXT, text TEXT, author TEXT, lang TEXT,
  published_at TEXT, fetched_at TEXT, raw_hash TEXT UNIQUE);
CREATE TABLE scores(
  id INTEGER PRIMARY KEY, raw_id INTEGER REFERENCES raw_items(id),
  family TEXT, scope TEXT CHECK(scope IN('coin','market')),
  coin TEXT,              -- scope=market 时为 'MARKET'
  direction REAL,         -- -1..1
  confidence REAL, event_type TEXT, magnitude INTEGER,
  is_shill INTEGER DEFAULT 0, model TEXT, batch_id TEXT, scored_at TEXT,
  UNIQUE(raw_id, coin));
CREATE TABLE index_hourly(
  ts TEXT, family TEXT, scope TEXT, coin TEXT,
  sent REAL, sent_z REAL, mentions INTEGER, confidence_flag TEXT,
  PRIMARY KEY(ts, family, coin));
CREATE TABLE overall_hourly(
  ts TEXT PRIMARY KEY, overall_news REAL, overall_social REAL,
  market_news REAL, breadth REAL);
CREATE TABLE quality_log(
  ts TEXT, sample_n INTEGER, agreement_rate REAL, model_a TEXT, model_b TEXT);
CREATE INDEX idx_scores_coin_ts ON scores(coin, scored_at);
CREATE INDEX idx_raw_ts ON raw_items(published_at);
```
保留策略：raw_items 只留 14 天（每天 job 末尾 prune），scores/index_hourly/overall_hourly 永久保留。

## 7. GitHub Pages 面板（/docs，纯静态）

文件：`docs/index.html`、`docs/app.js`、`docs/data/*.json`
每次 hourly job 末尾导出：
- `docs/data/meta.json`：币种列表、最后更新时间、质量摘要
- `docs/data/overall.json`：overall_hourly 最近 90 天全量
- `docs/data/coins/{SYMBOL}.json`：该币 index_hourly 最近 90 天（news+social 两条线 + mentions）

页面要求：
1. 顶部：两个仪表盘卡片（OVERALL_NEWS、OVERALL_SOCIAL）+ MARKET_NEWS + BREADTH，显示当前值、24h 变化、近 30 天迷你折线
2. 币种选择器（搜索框）：选中后 lightweight-charts 画该币双指数曲线（news 实线/social 虚线）+ 下方 mentions 柱状副图；z-score 缺失段画虚线
3. 排行榜表格：当前 24h 平均 SENT 最高/最低各 10 个币（news/social 分列），confidence_flag=low 的灰显
4. 配色：低饱和、浅色背景、绿涨红跌；不要蓝紫渐变
5. 所有数据从 docs/data/*.json 客户端加载，无后端

## 8. GitHub Actions（.github/workflows/hourly.yml）

- `schedule: cron: "7 * * * *"`（每小时第 7 分钟）+ workflow_dispatch 手动触发
- 步骤：checkout → setup-python 3.11 缓存 pip → 恢复 data/sentiment.db（用 actions/cache，key 带日期）→ 跑 pipeline/hourly_job.py（timeout 15min）→ 保存 cache → commit docs/data/ 和 data/index_export/（指数导出 CSV 备份）回仓库（git-auto-commit 模式，message: `data: hourly update {ts}`）
- Secrets：`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、可选 `CRYPTOPANIC_TOKEN`、`REDDIT_CLIENT_ID/SECRET`
- raw sqlite 不 commit（太大）；只 commit 导出 JSON/CSV
- README 写清楚：fork 后只需配 3 个 secret + 开 Pages（source=/docs）即可运行

## 9. 附录 A：LLM Prompts（原样实现进 prompts.py，不要改写）

### A1 新闻分析 prompt（逐条）
```
System: 你是加密市场新闻分析器。只输出合法 JSON，不要输出任何其他内容。
User: 分析下面的加密相关新闻，输出 JSON：
{
 "scope": "coin" | "market",
   // coin=主要影响特定币种；market=影响整个市场的政策/宏观/监管/ETF/系统性事件
 "coins": [{"symbol": "BTC", "direction": -2|-1|0|1|2, "confidence": 0.0-1.0}],
   // direction: 该新闻【对币价前景】的方向，-2强利空..+2强利好；0=纯事实无方向
   // 只列真正受影响的币；scope=market 时 coins 可为空数组
 "event_type": "regulation"|"hack"|"exploit"|"lawsuit"|"etf"|"listing"|"delisting"|"partnership"|"upgrade"|"macro"|"funding"|"exchange_issue"|"commentary"|"other",
 "magnitude": 1|2|3,        // 事件力度：1小 2中 3重大
 "time_sensitivity": "breaking"|"recent"|"dated",  // dated=旧闻重提→confidence要降低
 "summary_zh": "不超过30字的一句话"
}
规则：判断的是事件对币的影响方向，不是文章语气；中性报道的利空事件仍是利空；
     无法判断方向用 0 并降低 confidence；广告/软文/价格预测软文 is 直接忽略返回 {"skip":true}。
标题: {title}
正文: {text}
候选币种: {coin_candidates}
```

### A2 社媒批量 ABSA prompt（每批≤15条）
```
System: 你是加密社媒情绪分析器，精通币圈俚语(rekt/moon/ngmi/wagmi/FUD/shill/ape in)、反讽和emoji。只输出合法 JSON。
User: 对每条帖子，判断其【对每个提及币种】的情绪，输出 JSON 数组：
[{"id": 0, "coins": [{"symbol":"BTC","direction":-2..2,"confidence":0-1}],
  "is_shill": true|false,   // 疑似付费喊单/机器人模板
  "sarcasm": true|false}]
规则：direction 是针对该币的看多/看空程度而非全文情绪；一条帖可对应多个币、方向可相反；
     纯转发新闻无观点=0；与 crypto 无关的帖子 coins 为空；
     注意："NEAR/LINK/ATOM/SOL" 等只有明确指代币时才归因。
候选币种: {coin_candidates}
帖子列表: [{id, text}, ...]
```

### A3 别名 bootstrap prompt（每周一次）
```
System: 你是加密货币知识库。只输出 JSON。
User: 为以下每个币种生成常见别名（英文名、常见英文缩写、中文社区常用名），输出:
{"BTC": ["Bitcoin","比特币","大饼"], ...}
注意中文社区俗称（如 大饼=BTC、二饼/姨太=ETH、柚子=EOS 等）。
币种列表: {top200 symbol+name}
```

## 10. 执行顺序（按里程碑做，每个里程碑跑通验证后再做下一个）

- M1 仓库骨架 + coins 模块 + SQLite schema + 2 个新闻源 + 2 个社媒源 + 去重，干跑（LLM mock）落库成功
- M2 接入真 LLM：A1/A2 prompt + jsonschema 校验 + 限流预算 + scores 落库
- M3 指数计算 §5 全量 + index_hourly/overall_hourly + 导出 JSON
- M4 面板 §7 完整功能，Pages 可见
- M5 Actions 定时 + commit 自动化 + README（fork 即用）
- M6 自校验模块 §4.8 + 别名周刷 + 歧义词规则单测

## 11. 验收标准（全部满足才算完成）

1. `python -m pipeline.hourly_job --dry-run` 本地跑通：采集≥100 条、LLM mock 评分、指数写入、JSON 导出
2. 配真 key 后一次运行：scores 表有真实分数，scope=market 的政策类新闻能被正确归类（用一条美联储加息新闻做测试用例断言 scope=market）
3. 歧义词测试：文本 "I live near you" 不得归因 NEAR；"$NEAR breaking out" 必须归因 NEAR
4. 纯度测试：全代码库 grep 不得出现 price/close/funding 参与 scores 或 index 计算（允许在面板对比层）
5. 面板打开能看到 overall 卡片 + 至少 BTC/ETH 的双指数曲线 + 排行榜
6. Actions 连续 3 小时跑通，docs/data 自动更新
7. README 含架构图、配置说明、数据源清单、公式说明、免责说明

===== MASTER PROMPT 结束 =====

---

## 使用建议（给开发者本人）

1. **分段投喂**：如果 Kimi Code 一次吃不下，按 M1→M6 里程碑分段喂，每段开头加「继续按之前的 MASTER PROMPT 实现 M{n}」
2. **LLM 成本**：每小时约 200 次调用（Kimi/DeepSeek 级别单价），月成本预估 < ¥30；想压到零：把 social 批量评分换成 HF 小模型（crypto-bert 类），LLM 只评 news——让 Kimi Code 加 `SCORER=local|llm` 开关即可
3. **GitHub Actions 注意**：cron 有排队延迟（可能晚 5-20 分钟），对小时级指数无影响；raw sqlite 走 actions/cache 有 7 天未命中过期风险，真正长期跑建议把 scores/index 导出 CSV commit 进 `data/index_export/` 作为持久层（已含在 prompt 中）
4. **后续升级路径**：Telegram(Telethon)/Farcaster/Nostr 源、Bluesky Jetstream 实时流、LoRA 自有模型——都有预留接口，直接提需求让 Kimi Code 加模块
