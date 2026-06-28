#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股市场信息流 → DeepSeek 智能筛选 + 排重要性 + 翻译 → 简报推送到微信

工作方式:
  从 Google News 广泛抓取美股相关新闻(大盘 / 美联储 / 宏观数据 / 财报 / 地缘 / 龙头公司),
  交给 DeepSeek 判断每条对市场的重要性、利好利空、影响哪些板块, 并翻译成中文,
  再把「重要的」汇成一条简报, 通过 PushPlus 推送到微信。
  设计为在 GitHub Actions 上每隔几分钟自动运行, 不需要常开电脑。

需要两个密钥(放 GitHub Secrets, 不要写进代码):
  PUSHPLUS_TOKEN    —— 微信推送(pushplus.plus 扫码获取)
  DEEPSEEK_API_KEY  —— 调用 DeepSeek 做筛选/翻译(platform.deepseek.com 获取)

DeepSeek 是 OpenAI 兼容接口, 价格比多数模型低一截, 新账户还送 500 万 tokens 免费额度,
筛新闻这种用量基本能免费跑很久; 国内可直连, 无需特殊网络。
"""

import os
import json
from datetime import datetime, timezone, timedelta

import requests

try:
    import feedparser
except ImportError:
    raise SystemExit("缺少依赖, 请先运行: pip install feedparser")


# ============================ 配置区 ============================

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

# 推送哪些重要级别: ["high"]=只推重大; ["high","medium"]=重大+值得关注
PUSH_LEVELS = ["high", "medium"]

# DeepSeek 模型。"deepseek-chat" 是便宜的通用模型, 足够筛新闻。
# 注: DeepSeek 官方说 deepseek-chat 这个名字将于 2026/07/24 弃用,
#     届时改成 "deepseek-v4-flash"(或按当时官方文档的名字)即可。
DEEPSEEK_MODEL = "deepseek-chat"

# 信息源: 广撒网, 覆盖大盘 / 货币政策 / 宏观数据 / 财报 / 地缘 / 龙头公司。
# 想盯自己关注的票, 在下面照葫芦画瓢加一行 query 即可。
QUERIES = [
    '(stocks OR "Wall Street" OR "S&P 500" OR Nasdaq OR "Dow Jones") when:1d',
    '("Federal Reserve" OR FOMC OR Powell OR "rate cut" OR "interest rates") when:1d',
    '(inflation OR CPI OR PCE OR "jobs report" OR GDP OR unemployment OR "nonfarm payrolls") when:1d',
    '(earnings OR guidance) (Nvidia OR Apple OR Microsoft OR Amazon OR Tesla OR Alphabet OR Meta OR Broadcom) when:1d',
    '(tariffs OR "trade war" OR sanctions OR OPEC OR "oil prices") when:1d',
    'source:reuters (markets OR economy OR Fed OR stocks) when:1d',
]

SEEN_FILE = "seen_news.json"
MAX_SEEN = 1500
FIRST_RUN_MAX = 3       # 首次运行最多推几条(避免被过去一天的新闻刷屏)
MAX_PER_BRIEF = 15      # 单条简报最多放几条, 防止消息过长

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

LEVEL_EMOJI = {"high": "🔴", "medium": "🟡", "low": "⚪"}
LEVEL_NAME = {"high": "重大", "medium": "关注", "low": "一般"}
LEVEL_RANK = {"high": 0, "medium": 1, "low": 2}

# ===============================================================


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(seen):
    seen = seen[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def clean_title(t):
    t = (t or "").strip()
    if " - " in t:
        t = t.rsplit(" - ", 1)[0].strip()
    return t


def fetch_entries():
    """抓取所有 query 的新闻, 用 url 编码 + 去重。"""
    import urllib.parse
    items = {}
    for q in QUERIES:
        url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(q))
        feed = feedparser.parse(url)
        for e in feed.entries:
            uid = getattr(e, "id", None) or getattr(e, "link", "")
            if not uid:
                continue
            items[uid] = {
                "uid": uid,
                "title": clean_title(getattr(e, "title", "")),
                "link": getattr(e, "link", ""),
            }
    return list(items.values())


def analyze_with_deepseek(items):
    """让 DeepSeek 判断重要性/方向/板块并翻译。返回 dict: uid -> analysis。"""
    if not DEEPSEEK_API_KEY:
        print("警告: 未设置 DEEPSEEK_API_KEY, 跳过智能筛选, 直接推原文标题。")
        return {it["uid"]: {"level": "medium", "zh": it["title"],
                            "impact": "", "sector": ""} for it in items}

    numbered = "\n".join(f'{idx}. {it["title"]}' for idx, it in enumerate(items))
    system = (
        "你是专业的美股市场信息筛选助手。我会给你一批最新财经新闻标题(英文)。"
        "请对每一条做四件事:\n"
        "1) 评估它对美股市场的重要性, 分三级:\n"
        "   high = 可能显著影响大盘或重要板块(美联储利率决议、重磅经济数据明显超/逊预期、"
        "重大地缘冲突、龙头公司重大事件、市场剧烈波动);\n"
        "   medium = 值得关注但影响有限(单一公司财报、行业动态、分析师评级、个股消息);\n"
        "   low = 噪音、标题党、重复、对市场基本无影响。\n"
        "2) 把标题翻译成简体中文(简洁准确)。\n"
        "3) 判断方向: 利好 / 利空 / 中性。\n"
        "4) 指出主要影响的板块或标的(如'科技股''半导体''能源''美债''AAPL'等), 没有就留空。\n"
        "严格只返回一个 JSON 对象, 格式为 "
        '{"results": [{"i": 序号, "level": "high|medium|low", "zh": "中文标题", '
        '"impact": "利好|利空|中性", "sector": "板块或标的"}, ...]}。'
        "不要输出任何额外文字。"
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"新闻标题列表:\n{numbered}"},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post("https://api.deepseek.com/chat/completions",
                          json=payload, headers=headers, timeout=90)
        data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):              # 兜底: 去掉可能的代码块包裹
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        parsed = json.loads(text)
        arr = parsed.get("results", []) if isinstance(parsed, dict) else parsed
    except Exception as e:
        print("DeepSeek 分析失败, 退回原文:", e)
        return {it["uid"]: {"level": "medium", "zh": it["title"],
                            "impact": "", "sector": ""} for it in items}

    result = {}
    for obj in arr:
        i = obj.get("i")
        if isinstance(i, int) and 0 <= i < len(items):
            result[items[i]["uid"]] = {
                "level": obj.get("level", "medium"),
                "zh": obj.get("zh", items[i]["title"]),
                "impact": obj.get("impact", ""),
                "sector": obj.get("sector", ""),
            }
    return result


def build_brief(picked):
    """把挑选出的新闻汇成一条 HTML 简报。"""
    now = datetime.now(timezone(timedelta(hours=-4))).strftime("%m-%d %H:%M")  # 美东(夏令时)
    lines = [f"<b>📊 市场要闻简报</b> &nbsp;<span style='color:#888'>{now} ET</span><br>"]
    n = 1
    for level in ["high", "medium"]:
        group = [p for p in picked if p["a"]["level"] == level]
        if not group:
            continue
        lines.append(f"<br><b>{LEVEL_EMOJI[level]} {LEVEL_NAME[level]}</b>")
        for p in group:
            a = p["a"]
            tag = "·".join(x for x in [a.get("impact", ""), a.get("sector", "")] if x)
            tag = f"[{tag}] " if tag else ""
            lines.append(f"<br>{n}. {tag}<a href='{p['link']}'>{a['zh']}</a>")
            n += 1
    return "".join(lines)


def push_to_wechat(title, content):
    if not PUSHPLUS_TOKEN:
        print("[未设置 PUSHPLUS_TOKEN, 跳过推送]\n", content)
        return
    payload = {"token": PUSHPLUS_TOKEN, "title": title,
               "content": content, "template": "html"}
    try:
        r = requests.post("https://www.pushplus.plus/send", json=payload, timeout=20)
        d = r.json()
        if d.get("code") != 200:
            print("推送返回异常:", d)
    except Exception as e:
        print("推送失败:", e)


def main():
    raw_seen = load_seen()
    first_run = raw_seen is None
    seen = raw_seen or []
    seen_set = set(seen)

    entries = fetch_entries()
    new_items = [it for it in entries if it["uid"] not in seen_set]
    print(f"抓取 {len(entries)} 条, 新增 {len(new_items)} 条。")
    if not new_items:
        print("无新增, 结束。")
        return

    analyses = analyze_with_deepseek(new_items)

    picked = []
    for it in new_items:
        a = analyses.get(it["uid"])
        if a and a["level"] in PUSH_LEVELS:
            picked.append({"link": it["link"], "a": a})
    picked.sort(key=lambda p: LEVEL_RANK.get(p["a"]["level"], 9))

    if first_run:
        picked = picked[:FIRST_RUN_MAX]
        print(f"首次运行, 仅推送 {len(picked)} 条做测试。")
    picked = picked[:MAX_PER_BRIEF]

    if picked:
        push_to_wechat(f"📊 市场要闻 {len(picked)} 条", build_brief(picked))
        print(f"已推送简报, 含 {len(picked)} 条。")
    else:
        print("本次没有达到推送级别的新闻。")

    for it in new_items:
        seen_set.add(it["uid"])
    save_seen(seen + [u for u in seen_set if u not in set(seen)])
    print("完成。")


if __name__ == "__main__":
    main()
