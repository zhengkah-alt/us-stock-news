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
import calendar
import xml.etree.ElementTree as ET
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
# 按用户要求: 只推"刚发生且对股市影响极大"的, 故只留 high。
PUSH_LEVELS = ["high"]

# DeepSeek 模型。"deepseek-chat" 是便宜的通用模型, 足够筛新闻。
# 注: DeepSeek 官方说 deepseek-chat 这个名字将于 2026/07/24 弃用,
#     届时改成 "deepseek-v4-flash"(或按当时官方文档的名字)即可。
DEEPSEEK_MODEL = "deepseek-chat"

# 每次最多把多少条新闻发给 DeepSeek(分批调用, 防止返回 JSON 过长被截断)。
DEEPSEEK_BATCH_SIZE = 25

# 信息源: 广撒网, 覆盖大盘 / 货币政策 / 宏观数据 / 财报 / 地缘 / 龙头公司。
# 想盯自己关注的票, 在下面照葫芦画瓢加一行 query 即可。
QUERIES = [
    '(stocks OR "Wall Street" OR "S&P 500" OR Nasdaq OR "Dow Jones") when:1h',
    '("Federal Reserve" OR FOMC OR Powell OR "rate cut" OR "interest rates") when:1h',
    '(inflation OR CPI OR PCE OR "jobs report" OR GDP OR unemployment OR "nonfarm payrolls") when:1h',
    '(earnings OR guidance) (Nvidia OR Apple OR Microsoft OR Amazon OR Tesla OR Alphabet OR Meta OR Broadcom) when:1h',
    # 地缘 / 宏观风险
    '(war OR conflict OR sanctions OR tariffs OR "trade war" OR geopolitical OR OPEC OR "oil prices") when:1h',
    # 个股之间的合作 / 并购 / 大单
    '(merger OR acquisition OR "to acquire" OR partnership OR "strategic partnership" OR "joint venture" OR stake OR "deal with" OR contract) when:1h',
]

# 只推送"自身发布时间"在这么多分钟内的新闻; 太老的丢掉(没有新的就不推)。
# 用环境变量 MAX_AGE_MIN 可不改代码调整。
MAX_AGE_MIN = int(os.environ.get("MAX_AGE_MIN", "10"))

# 路透公开新闻 sitemap(给搜索引擎索引用, 无需登录, 时效约几分钟, 带精确发布时间)。
# 这是"快车道"新闻源; 只用标题, 不需要会员/正文。
REUTERS_SITEMAP = "https://www.reuters.com/arc/outboundfeeds/news-sitemap/?outputType=xml"
# 只保留与市场/宏观/地缘相关的板块; 丢掉体育/生活/外语版等噪音。
REUTERS_KEEP = ("/markets", "/business", "/world", "/legal", "/breakingviews",
                "/technology", "/sustainability", "/economy", "/graphics")
REUTERS_DROP = ("/sports", "/sport", "/lifestyle", "/es/", "/lifestyle/")
HTTP_UA = "Mozilla/5.0 (compatible; us-stock-news-bot/1.0)"

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


def _struct_to_utc(st):
    """feedparser 的 published_parsed(GMT struct_time)→ aware UTC datetime。"""
    if not st:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    except Exception:
        return None


def fetch_reuters():
    """路透公开 sitemap: 时效约几分钟, 每条带标题 + 精确发布时间。"""
    out = []
    try:
        r = requests.get(REUTERS_SITEMAP, headers={"User-Agent": HTTP_UA}, timeout=30)
        root = ET.fromstring(r.content)
    except Exception as e:
        print("路透 sitemap 抓取失败:", e)
        return out
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
          "n": "http://www.google.com/schemas/sitemap-news/0.9"}
    for u in root.findall("s:url", ns):
        loc = u.findtext("s:loc", default="", namespaces=ns) or ""
        title = u.findtext("n:news/n:title", default="", namespaces=ns) or ""
        pub = u.findtext("n:news/n:publication_date", default="", namespaces=ns) or ""
        if not loc or not title:
            continue
        low = loc.lower()
        if any(d in low for d in REUTERS_DROP):
            continue
        if REUTERS_KEEP and not any(k in low for k in REUTERS_KEEP):
            continue
        try:
            published = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            published = None
        out.append({"uid": loc, "title": clean_title(title),
                    "link": loc, "published": published})
    return out


def fetch_entries():
    """汇总两路新闻源: 路透公开 sitemap(快) + Google News(广), 按 uid 去重。"""
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
                "published": _struct_to_utc(getattr(e, "published_parsed", None)),
            }
    # 路透快车道, 已有的不覆盖
    for it in fetch_reuters():
        items.setdefault(it["uid"], it)
    return list(items.values())


def _fallback(items):
    return {it["uid"]: {"level": "medium", "zh": it["title"],
                        "impact": "", "sector": ""} for it in items}


def analyze_with_deepseek(items):
    """分批让 DeepSeek 判断重要性/方向/板块并翻译, 合并结果。

    一次只发 DEEPSEEK_BATCH_SIZE 条, 避免返回 JSON 过长被截断。
    某一批失败只影响那一批(退回原文), 不拖累其它批。
    """
    if not DEEPSEEK_API_KEY:
        print("警告: 未设置 DEEPSEEK_API_KEY, 跳过智能筛选, 直接推原文标题。")
        return _fallback(items)

    result = {}
    for start in range(0, len(items), DEEPSEEK_BATCH_SIZE):
        batch = items[start:start + DEEPSEEK_BATCH_SIZE]
        result.update(_analyze_batch(batch))
    # 兜底: 任何没拿到分析结果的条目, 退回原文标题
    for it in items:
        result.setdefault(it["uid"], {"level": "medium", "zh": it["title"],
                                      "impact": "", "sector": ""})
    return result


def _analyze_batch(items):
    """对一批(<= DEEPSEEK_BATCH_SIZE 条)新闻调用一次 DeepSeek。返回 dict: uid -> analysis。"""
    numbered = "\n".join(f'{idx}. {it["title"]}' for idx, it in enumerate(items))
    system = (
        "你是专业的美股市场信息筛选助手, 只挑出'刚刚发生且对股市影响极大'的硬新闻。"
        "我会给你一批最新财经新闻标题(英文)。请对每一条做四件事:\n"
        "1) 评估它对美股市场的重要性, 分三级, 标准要严格:\n"
        "   high = 真正可能立刻撼动大盘或重要板块/龙头的硬消息, 例如: 美联储利率决议或重磅讲话、"
        "重磅经济数据明显超/逊预期、重大地缘冲突或战争/制裁/关税升级、龙头公司重大并购/合作/大单/"
        "暴雷/业绩远超或远逊预期、市场剧烈波动。务必从严, 把握不准就别给 high。\n"
        "   medium = 值得关注但不至于撼动市场(普通财报、行业动态、分析师评级、一般个股消息)。\n"
        "   low = 旧闻、噪音、标题党、重复、预告/综述、对市场基本无影响。\n"
        "特别关注并优先给 high 的题材: 地缘政治冲突、公司之间的并购/战略合作/重大订单。\n"
        "宁缺毋滥: 只有内容明确、像是刚发生的实锤大事才给 high; 含糊、陈旧、纯观点的一律 low。\n"
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
        print("DeepSeek 分析失败(本批退回原文):", e)
        return _fallback(items)

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
    all_new = [it for it in entries if it["uid"] not in seen_set]

    # 只保留"自身发布时间"足够新的(刚刚发生); 没有发布时间的保守保留(避免漏掉)。
    now_utc = datetime.now(timezone.utc)
    new_items = []
    for it in all_new:
        p = it.get("published")
        if p is None or (now_utc - p).total_seconds() <= MAX_AGE_MIN * 60:
            new_items.append(it)
    print(f"抓取 {len(entries)} 条, 新增 {len(all_new)} 条, "
          f"其中 {MAX_AGE_MIN} 分钟内 {len(new_items)} 条。")

    if not new_items:
        print("无足够新的新闻, 结束。")
        # 把这次见到的(含较旧的)记为已读, 避免反复处理
        if all_new:
            for it in all_new:
                seen_set.add(it["uid"])
            save_seen(seen + [u for u in seen_set if u not in set(seen)])
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

    for it in all_new:
        seen_set.add(it["uid"])
    save_seen(seen + [u for u in seen_set if u not in set(seen)])
    print("完成。")


if __name__ == "__main__":
    main()
