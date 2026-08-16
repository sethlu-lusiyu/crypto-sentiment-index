"""LLM prompts — kept verbatim from project specification."""
from __future__ import annotations

from typing import Any


NEWS_SYSTEM = "你是加密市场新闻分析器。只输出合法 JSON，不要输出任何其他内容。"


def build_news_prompt(title: str, text: str, coin_candidates: list[str]) -> str:
    candidates = ", ".join(coin_candidates) if coin_candidates else "无"
    return f"""分析下面的加密相关新闻，输出 JSON：
{{
 "scope": "coin" | "market",
   // coin=主要影响特定币种；market=影响整个市场的政策/宏观/监管/ETF/系统性事件
 "coins": [{{"symbol": "BTC", "direction": -2|-1|0|1|2, "confidence": 0.0-1.0}}],
   // direction: 该新闻【对币价前景】的方向，-2强利空..+2强利好；0=纯事实无方向
   // 只列真正受影响的币；scope=market 时 coins 可为空数组
 "event_type": "regulation"|"hack"|"exploit"|"lawsuit"|"etf"|"listing"|"delisting"|"partnership"|"upgrade"|"macro"|"funding"|"exchange_issue"|"commentary"|"other",
 "magnitude": 1|2|3,        // 事件力度：1小 2中 3重大
 "time_sensitivity": "breaking"|"recent"|"dated",  // dated=旧闻重提→confidence要降低
 "summary_zh": "不超过30字的一句话"
}}
规则：判断的是事件对币的影响方向，不是文章语气；中性报道的利空事件仍是利空；
     无法判断方向用 0 并降低 confidence；广告/软文/价格预测软文 is 直接忽略返回 {{"skip":true}}。
标题: {title}
正文: {text}
候选币种: {candidates}
"""


SOCIAL_SYSTEM = "你是加密社媒情绪分析器，精通币圈俚语(rekt/moon/ngmi/wagmi/FUD/shill/ape in)、反讽和emoji。只输出合法 JSON。"


def build_social_prompt(posts: list[dict[str, Any]], coin_candidates: list[str]) -> str:
    candidates = ", ".join(coin_candidates) if coin_candidates else "无"
    posts_json = "\n".join(
        f'  {{"id": {p["id"]}, "text": {repr(p["text"])}}}' for p in posts
    )
    return f"""对每条帖子，判断其【对每个提及币种】的情绪，输出 JSON 数组：
[{{"id": 0, "coins": [{{"symbol":"BTC","direction":-2..2,"confidence":0-1}}],
  "is_shill": true|false,   // 疑似付费喊单/机器人模板
  "sarcasm": true|false}}]
规则：direction 是针对该币的看多/看空程度而非全文情绪；一条帖可对应多个币、方向可相反；
     纯转发新闻无观点=0；与 crypto 无关的帖子 coins 为空；
     注意："NEAR/LINK/ATOM/SOL" 等只有明确指代币时才归因。
候选币种: {candidates}
帖子列表: [
{posts_json}
]
"""
