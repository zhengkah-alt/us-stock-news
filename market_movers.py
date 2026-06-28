#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股各时段(盘前 / 盘中 / 盘后 / 夜盘)"成交量前 N + 短时暴力拉升"提醒 → PushPlus 微信。

思路:
  用 Yahoo(yfinance)免费数据取"最活跃(成交量最大)"的前 N 只,
  对每只看最近几分钟(含盘前盘后)的涨跌幅;
  若短时涨/跌超过阈值, 且数据是"刚刚"的(避免休市旧数据误报), 就推一条提醒。
  带去重/冷却: 同一只在再拉/再砸一个台阶之前不重复提醒, 每个交易日重置。

边界(免费数据天花板):
  Yahoo "最活跃" 是当日常规成交量排名; 真正 8pm–4am 通宵盘多数票无数据, 这段基本静默。
  盘前(4:00–9:30) / 盘中 / 盘后(16:00–20:00) 能正常工作。

依赖: pip install yfinance
密钥: PUSHPLUS_TOKEN(与新闻脚本共用一个)

可用环境变量微调(都有默认值, 不设也能跑):
  TOP_N            取成交量前几名            (默认 10)
  MOVE_THRESHOLD   短时涨跌多少% 算暴力拉升  (默认 2)
  WINDOW_MIN       "短时"看最近几分钟        (默认 5)
  FRESH_MIN        最新K线须在几分钟内才算"刚刚"(默认 12)
"""

import os
import json
import warnings
from datetime import datetime, timezone, timedelta

import requests

warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import yfinance as yf
except ImportError:
    raise SystemExit("缺少依赖, 请先运行: pip install yfinance")


# ============================ 配置区 ============================

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()

TOP_N = int(os.environ.get("TOP_N", "10"))                 # 取成交量前几名
MOVE_THRESHOLD = float(os.environ.get("MOVE_THRESHOLD", "2"))  # 短时涨跌 % 阈值
WINDOW_MIN = int(os.environ.get("WINDOW_MIN", "5"))        # "短时" = 最近几分钟
FRESH_MIN = int(os.environ.get("FRESH_MIN", "12"))         # 最新K线须在 N 分钟内
REALERT_STEP = 2.0          # 同一只再变动多少 % 才再次提醒(冷却)
STATE_FILE = "movers_state.json"

ET = timezone(timedelta(hours=-4))   # 美东夏令时 EDT

# ===============================================================


def now_et():
    return datetime.now(ET)


def session_label(dt):
    hm = dt.hour * 60 + dt.minute
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "盘前"
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "盘中"
    if 16 * 60 <= hm < 20 * 60:
        return "盘后"
    return "夜盘"


def load_state():
    today = now_et().strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            if st.get("date") == today:
                return st
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": today, "alerted": {}}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def top_active(n):
    """成交量最大的前 n 只。"""
    res = yf.screen("most_actives", count=max(n, 10))
    quotes = res.get("quotes", []) if isinstance(res, dict) else []
    out = []
    for q in quotes[:n]:
        sym = q.get("symbol")
        if sym:
            out.append({
                "symbol": sym,
                "vol": q.get("regularMarketVolume"),
                "day_chg": q.get("regularMarketChangePercent"),
            })
    return out


def short_move(symbol):
    """返回 (现价, 近 WINDOW_MIN 分钟涨跌%, 最新K线UTC时间) 或 None。"""
    h = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
    if h is None or len(h) < 2:
        return None
    closes = h["Close"].dropna()
    if len(closes) < 2:
        return None
    last_ts = closes.index[-1]                       # tz-aware Timestamp
    cur = float(closes.iloc[-1])
    cutoff = last_ts - pd.Timedelta(minutes=WINDOW_MIN)
    past_part = closes[closes.index <= cutoff]
    past = float(past_part.iloc[-1]) if len(past_part) else float(closes.iloc[0])
    if past <= 0:
        return None
    pct = (cur - past) / past * 100.0
    last_utc = last_ts.tz_convert("UTC").to_pydatetime()
    return cur, pct, last_utc


def push(title, content):
    if not PUSHPLUS_TOKEN:
        print("[未设置 PUSHPLUS_TOKEN, 跳过推送]", title)
        return
    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": PUSHPLUS_TOKEN, "title": title,
                  "content": content, "template": "html"},
            timeout=20,
        )
        d = r.json()
        if d.get("code") != 200:
            print("推送返回异常:", d)
    except Exception as e:
        print("推送失败:", e)


def main():
    state = load_state()
    alerted = state["alerted"]
    now = now_et()
    now_utc = datetime.now(timezone.utc)
    sess = session_label(now)

    try:
        movers = top_active(TOP_N)
    except Exception as e:
        print("取成交量榜失败:", e)
        return
    print(f"[{now.strftime('%m-%d %H:%M')} ET / {sess}] 成交量前{len(movers)}: "
          + ", ".join(m["symbol"] for m in movers))

    hits = []
    for rank, m in enumerate(movers, 1):
        sym = m["symbol"]
        try:
            r = short_move(sym)
        except Exception:
            continue
        if not r:
            continue
        cur, pct, last_utc = r
        age_min = (now_utc - last_utc).total_seconds() / 60.0
        if age_min > FRESH_MIN:          # 旧数据(休市), 不算"刚刚"
            continue
        if abs(pct) < MOVE_THRESHOLD:
            continue
        prev = alerted.get(sym)
        if prev and abs(cur - prev) / prev * 100.0 < REALERT_STEP:
            continue                     # 冷却: 没再走出一个台阶
        alerted[sym] = cur
        hits.append({"rank": rank, "sym": sym, "pct": pct,
                     "price": cur, "day": m["day_chg"]})

    if hits:
        lines = [f"<b>⚡ 暴力拉升提醒</b> &nbsp;"
                 f"<span style='color:#888'>{now.strftime('%m-%d %H:%M')} ET · {sess}</span><br>"]
        for h in hits:
            arrow = "🚀" if h["pct"] > 0 else "💥"
            day = h["day"]
            day_txt = f" · 当日 {day:+.1f}%" if isinstance(day, (int, float)) else ""
            lines.append(
                f"<br>{arrow} <b>{h['sym']}</b> 成交量第{h['rank']}名 · "
                f"近{WINDOW_MIN}分钟 <b>{h['pct']:+.1f}%</b>"
                f"<br>&nbsp;&nbsp;现价 {h['price']:.2f}{day_txt}"
            )
        push(f"⚡ 暴力拉升 {len(hits)} 只", "".join(lines))
        try:
            open(".need_commit", "w").close()   # 通知 workflow: 推送了, 请保存状态
        except OSError:
            pass
        print(f"已推送 {len(hits)} 只暴力拉升。")
    else:
        print("本轮无达到阈值的暴力拉升。")

    save_state(state)
    print("完成。")


if __name__ == "__main__":
    main()
