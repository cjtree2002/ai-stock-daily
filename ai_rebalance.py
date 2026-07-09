#!/usr/bin/env python3
"""
Cloud AI rebalance + morning commentary.

Runs inside GitHub Actions BEFORE the report is generated. Gathers all market
data deterministically in Python (same post-market price convention as the
report page), hands a verified digest to the Claude API (single call, server-
side web search allowed for context), then validates and executes the model's
proposed trades locally. The model is never trusted with prices or arithmetic.

Fail-soft by design: any error exits non-zero; the workflow alerts Telegram
and continues, so the daily report always publishes (holdings unchanged).
"""
import os
import re
import sys
import json
from datetime import datetime, timedelta

from fetch_news import (
    AI_COMPANIES, BEIJING_TZ, PORTFOLIO_PATH, COMMENTARY_PATH,
    _is_trading_day, fetch_stock_prices, fetch_indices, fetch_all_news,
)

JOURNAL_PATH = PORTFOLIO_PATH.parent / "journal.md"
MODEL = os.environ.get("AI_REBALANCE_MODEL", "claude-sonnet-5")
FORCE = os.environ.get("AI_REBALANCE_FORCE") == "1"

SYSTEM_RULES = """你是一个 $1,000,000 美股模拟盘的组合经理（纯多头）。你会收到一份【已核实数据摘要】：其中所有个股价格均为目标交易日的盘后最终价、涨跌均相对前一交易日收盘（与公开日报页面口径完全一致），指数为收盘价。

## 纪律（必须严格遵守）
- 只能交易摘要中列出的股票；单股市值 ≤ 组合总值20%；现金占比 0~30%；不加杠杆、不做空。
- 个股相对成本回撤 ≥15% 时必须在晨评或理由中给出重估结论（持有/减仓/清仓）。
- 只有存在充分理由才交易（重大新闻、财报、趋势破位、风控触发、决策日志中预设条件达成）；没有就明确不调仓——这是常态且完全可接受。
- 你可以用网络搜索了解目标日市场驱动与个股新闻背景，但【晨评中的所有涨跌数字必须且只能来自数据摘要】，禁止使用搜索结果中的任何价格数字。

## 输出（回复的最后必须是一个 ```json 代码块，此外不要输出其他 JSON）
```json
{
  "commentary": "晨评正文：3-5句中文，可用\\n分段。结构=目标日市场判断→对组合的影响→今日操作概述或不动的理由。数字只用摘要中的。",
  "trades": [
    {"action": "买入或卖出", "ticker": "代码", "shares": 整数, "reason": "中文理由，写明依据"}
  ],
  "journal": "完整覆盖更新后的决策日志（markdown，150行内）：①每只持仓的当前论点 ②观察清单（等待的信号/价位/条件）③今日备忘。此文件会公开。"
}
```
trades 可以为空数组。shares 是股数（卖出不得超过持有数，买入以现金支付）。不要在 JSON 里写价格——成交价由系统按盘后价执行。"""


def _fmt_digest(target, pf, prices, indices, news, journal, total):
    lines = []
    ds = target.strftime("%Y-%m-%d")
    lines.append(f"# 已核实数据摘要（目标交易日 T = {ds}）\n")

    lines.append("## 指数（收盘）")
    for ix in indices:
        lines.append(f"- {ix['label']}: {ix['value']:,.2f} ({ix['change']:+.2f}%)")

    init = pf["initial_capital"]
    last = pf["history"][-1]
    lines.append(f"\n## 组合（总值 ${total:,.0f}，累计 {(total/init-1)*100:+.2f}%；"
                 f"基准累计：32股等权 {(last['eq']/init-1)*100:+.2f}%、"
                 f"QQQ {(last['qqq']/init-1)*100:+.2f}%、"
                 f"费半 {(last.get('sox',init)/init-1)*100:+.2f}%）")
    lines.append(f"现金 ${pf['cash']:,.0f}（{pf['cash']/total*100:.1f}%）")
    lines.append("持仓（价格=T日盘后最终价；当日=对前收盘；回撤=对成本）:")
    held = set()
    for h in pf["holdings"]:
        t = h["ticker"]; held.add(t)
        p = prices.get(t) or {}
        px = p.get("price", h["avg_cost"])
        ch = p.get("change")
        dd = (px / h["avg_cost"] - 1) * 100
        w = h["shares"] * px / total * 100
        flag = " ⚠️触发-15%重估线" if dd <= -15 else ""
        lines.append(f"- {t}: {h['shares']}股 成本${h['avg_cost']:,.2f} 现价${px:,.2f} "
                     f"当日{ch:+.2f}% 回撤{dd:+.1f}% 仓位{w:.1f}%{flag}")

    lines.append("\n## 未持有的自选股（可买入）")
    for c in AI_COMPANIES:
        t = c["ticker"]
        if t in held or t == "N/A":
            continue
        p = prices.get(t) or {}
        if p.get("price"):
            ch = p.get("change")
            chs = f"{ch:+.2f}%" if ch is not None else "—"
            lines.append(f"- {t} {c['name']}: ${p['price']:,.2f} 当日{chs}")

    lines.append("\n## T日相关新闻标题（供理解背景）")
    n = 0
    for c in AI_COMPANIES:
        arts = news.get(c["name"]) or []
        for a in arts[:2]:
            lines.append(f"- [{c['ticker']}] {a.get('title','')[:90]}")
            n += 1
        if n > 45:
            break

    lines.append("\n## 最近5笔交易")
    for tr in pf["trades"][-5:]:
        lines.append(f"- {tr['date']} {tr['action']} {tr['ticker']} {tr['shares']}股 @ ${tr['price']:,.2f}：{tr['reason'][:60]}")

    lines.append("\n## 你的决策日志（上次运行留下的）")
    lines.append(journal if journal.strip() else "（空——今天是日志的第一天）")

    lines.append("\n请先用网络搜索补充了解 T 日市场驱动与持仓个股动态（最多6次），然后按系统要求输出决策 JSON。")
    return "\n".join(lines)


def _call_claude(digest):
    import anthropic
    client = anthropic.Anthropic()
    kwargs = dict(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_RULES,
        messages=[{"role": "user", "content": digest}],
    )
    try:
        resp = client.messages.create(
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            **kwargs,
        )
    except anthropic.BadRequestError as e:
        print(f"[WARN] web_search unavailable ({e}); retrying without tools", file=sys.stderr)
        resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    usage = getattr(resp, "usage", None)
    if usage:
        print(f"  API usage: in={usage.input_tokens} out={usage.output_tokens}")
    return text


def _parse_decision(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if blocks:
        return json.loads(blocks[-1])
    # fallback: widest brace span
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError("no JSON found in model output")


def _execute_trades(pf, trades, prices, total, today_str):
    """Validate each proposed trade against the discipline and execute at OUR
    post-market prices. Invalid trades are dropped with a log, never fatal."""
    valid_tickers = {c["ticker"] for c in AI_COMPANIES if c["ticker"] != "N/A"}
    executed = 0
    for tr in (trades or [])[:6]:
        try:
            t = str(tr["ticker"]).upper()
            action = tr["action"]
            shares = int(tr["shares"])
            reason = str(tr.get("reason", "")).strip()[:300]
            if t not in valid_tickers or shares <= 0 or action not in ("买入", "卖出") or not reason:
                print(f"  [drop] 非法交易: {tr}")
                continue
            px = (prices.get(t) or {}).get("price")
            if not px:
                print(f"  [drop] {t} 无价格")
                continue
            h = next((x for x in pf["holdings"] if x["ticker"] == t), None)
            if action == "卖出":
                if not h or shares > h["shares"]:
                    print(f"  [drop] 卖出超持仓: {tr}")
                    continue
                h["shares"] -= shares
                pf["cash"] = round(pf["cash"] + shares * px, 2)
                if h["shares"] == 0:
                    pf["holdings"].remove(h)
            else:  # 买入
                cost = shares * px
                if cost > pf["cash"]:
                    print(f"  [drop] 现金不足: {tr}")
                    continue
                new_mv = (h["shares"] + shares if h else shares) * px
                if new_mv / total > 0.205:
                    print(f"  [drop] 超20%单股上限: {tr}")
                    continue
                if h:
                    h["avg_cost"] = round((h["shares"] * h["avg_cost"] + cost) / (h["shares"] + shares), 2)
                    h["shares"] += shares
                else:
                    pf["holdings"].append({"ticker": t, "shares": shares,
                                           "avg_cost": round(px, 2), "opened": today_str})
                pf["cash"] = round(pf["cash"] - cost, 2)
            pf["trades"].append({"date": today_str, "action": action, "ticker": t,
                                 "shares": shares, "price": round(px, 2), "reason": reason})
            executed += 1
            print(f"  ✓ {action} {t} {shares}股 @ ${px:,.2f}")
        except Exception as e:
            print(f"  [drop] 交易解析失败 {tr}: {e}")
    # 现金带不能为负（上面已保证），若>30.5%仅提示（卖出导致的被动超限允许）
    return executed


def main():
    target = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(days=1)
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    ds = target.strftime("%Y-%m-%d")
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    if not _is_trading_day(target):
        print(f"{ds} 非交易日，AI调仓跳过。")
        return

    pf = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))

    # Dedup guards (skip unless forced)
    if not FORCE:
        if any(t["date"] == today_str for t in pf["trades"]):
            print("今日已有交易记录，跳过（防重复）。")
            return
        if COMMENTARY_PATH.exists():
            try:
                c = json.loads(COMMENTARY_PATH.read_text(encoding="utf-8"))
                if c.get("date") == ds and c.get("text"):
                    print("今日晨评已存在，跳过（防重复）。")
                    return
            except Exception:
                pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY 未设置", file=sys.stderr)
        sys.exit(1)

    print(f"AI rebalance for {ds} (model={MODEL})")
    prices = fetch_stock_prices(target)
    indices = fetch_indices(target)
    news = fetch_all_news(target)
    journal = JOURNAL_PATH.read_text(encoding="utf-8") if JOURNAL_PATH.exists() else ""

    total = pf["cash"] + sum(h["shares"] * (prices.get(h["ticker"]) or {}).get("price", h["avg_cost"])
                             for h in pf["holdings"])
    digest = _fmt_digest(target, pf, prices, indices, news, journal, total)
    print(f"  digest: {len(digest)} chars")

    text = _call_claude(digest)
    decision = _parse_decision(text)

    commentary = str(decision.get("commentary", "")).strip()
    if not commentary:
        raise ValueError("model returned empty commentary")

    n = _execute_trades(pf, decision.get("trades"), prices, total, today_str)
    PORTFOLIO_PATH.write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")
    COMMENTARY_PATH.write_text(json.dumps({"date": ds, "text": commentary}, ensure_ascii=False),
                               encoding="utf-8")
    j = str(decision.get("journal", "")).strip()
    if j:
        JOURNAL_PATH.write_text("\n".join(j.splitlines()[:200]) + "\n", encoding="utf-8")

    print(f"完成：{n} 笔交易，晨评 {len(commentary)} 字，日志已更新。")


if __name__ == "__main__":
    main()
