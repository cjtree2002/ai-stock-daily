#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演出开票提醒 —— 每天跑一次
==============================
做什么:
  1. 读取 watchlist.json 里你关注的艺人
  2. 从多个渠道抓取消息:
       · 微博关键词搜索(每个艺人一条)—— 官宣最快
       · 新浪娱乐          —— 媒体转载,补漏
       · 香港 TicketHK 聚合站 / SISTIC 新加坡 / 大麦 —— 落地票务(开票时间+链接)
  3. 匹配到你的艺人 + 演出关键词的,和上次记录对比:
       · 第一次出现        -> 发「🔔 预警」邮件(有演出了)
       · 后来出现开票信息  -> 发「✅ 开票」邮件(几号几点在哪买)
  4. 更新网页看板 docs/tickets/events.json
  5. 用邮件把本次的新消息汇总发给你(一封信,列出全部新条目)

设计原则:任何一个渠道挂了都不影响其它渠道;抓不到就跳过并记日志,绝不报错中断。
"""

import os
import re
import json
import html
import hashlib
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.header import Header
from xml.etree import ElementTree as ET

import requests

# ---------------------------------------------------------------- 路径与常量
ROOT        = os.path.dirname(os.path.abspath(__file__))
WATCHLIST   = os.path.join(ROOT, "watchlist.json")
STATE_FILE  = os.path.join(ROOT, "tickets_state.json")   # 记住"已经通知过什么",用于去重
OUT_DIR     = os.path.join(ROOT, "docs", "tickets")
EVENTS_JSON = os.path.join(OUT_DIR, "events.json")        # 网页看板读取的数据
DASH_HTML   = os.path.join(OUT_DIR, "index.html")         # 网页看板页面

# RSSHub 公共实例(把网站转成程序能读的格式)。若不稳定,以后可换成自建地址。
RSSHUB = os.environ.get("RSSHUB_BASE", "https://rsshub.app").rstrip("/")

# 判定"这是一条演出消息"的关键词(社交/媒体渠道用,过滤无关内容)
EVENT_KW = ["演唱会", "巡演", "巡回", "演出", "见面会", "专场", "开唱", "栋笃笑",
            "脱口秀", "livehouse", "live house", "concert", "tour", "world tour",
            "音乐会", "签售", "粉丝见面"]

# 判定"已经有开票信息了"的关键词(触发第二段「开票」提醒)
ONSALE_KW = ["开票", "开售", "公开发售", "预售", "开抢", "正式发售", "公售", "启售",
             "发售时间", "on sale", "on-sale", "sale starts", "tickets available",
             "购票", "抢票"]

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 20


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ================================================================ 抓取工具
def fetch_rss(url):
    """抓一个 RSS 地址,返回条目列表 [{title, link, summary, date}]。失败返回 []。"""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log(f"  ⚠️ RSS 抓取失败 {url} -> {e}")
        return []
    items = []
    # 兼容 RSS(<item>) 与 Atom(<entry>)
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(name):
            for ch in it:
                if ch.tag.split("}")[-1] == name:
                    return (ch.text or "").strip() if ch.text else (ch.get("href") or "")
            return ""
        link = get("link") or get("guid")
        items.append({
            "title":   html.unescape(get("title")),
            "link":    link,
            "summary": html.unescape(re.sub("<[^>]+>", " ", get("description") or get("summary"))),
            "date":    get("pubDate") or get("published") or get("updated"),
        })
    return items


def fetch_html(url):
    """抓网页原始 HTML。失败返回空串。"""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log(f"  ⚠️ 网页抓取失败 {url} -> {e}")
        return ""


# ================================================================ 各数据源
def source_weibo(artists):
    """微博:直接调手机版搜索接口 m.weibo.cn,按"艺人名 演唱会"搜。
    需要登录 cookie(环境变量 WEIBO_COOKIE),否则微博会拒绝返回。没配就跳过。"""
    import time
    cookie = os.environ.get("WEIBO_COOKIE", "").strip()
    if not cookie:
        log("  · 微博:未配置 WEIBO_COOKIE,跳过(见说明如何获取 cookie)。")
        return []
    out = []
    hdr = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": "https://m.weibo.cn/", "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
        "Cookie": cookie,
    }
    for a in artists:
        q = f"{a['zh']} 演唱会"
        url = "https://m.weibo.cn/api/container/getIndex"
        params = {"containerid": f"100103type=1&q={q}", "page_type": "searchall"}
        try:
            cards = requests.get(url, headers=hdr, params=params, timeout=TIMEOUT)\
                    .json().get("data", {}).get("cards", [])
        except Exception as e:
            log(f"  ⚠️ 微博搜索失败 [{a['zh']}] -> {e}")
            continue
        for c in cards:
            mb = c.get("mblog")
            if not mb and c.get("card_group"):
                mb = next((g.get("mblog") for g in c["card_group"] if g.get("mblog")), None)
            if not mb:
                continue
            text = re.sub("<[^>]+>", "", mb.get("text", ""))
            bid  = mb.get("bid", "")
            out.append({
                "title":   text[:50],
                "link":    f"https://m.weibo.cn/detail/{mb.get('id','')}" if mb.get("id") else "",
                "summary": text,
                "date":    mb.get("created_at", ""),
                "source":  "微博",
            })
        time.sleep(0.8)
    return out


def source_sina(artists):
    """新浪娱乐:直接调新浪滚动新闻 JSON 接口(feed.mix.sina.com.cn)。
    注意:新浪目前只有"电影"等少数频道是新鲜的,专门的"明星/演唱会"频道已废弃(返回旧闻),
    搜索接口对服务器也封锁。因此本源只作兜底 —— 靠后面的"艺人名+演出关键词"过滤,
    平时基本静默,真出现相关演出消息时才命中。"""
    out = []
    hdr = {**UA, "Referer": "https://roll.ent.sina.com.cn/"}
    ENT_CHANNELS = [2513]                              # 已实测的新鲜娱乐频道(pageid=153)
    for lid in ENT_CHANNELS:
        for page in (1, 2):
            url = (f"https://feed.mix.sina.com.cn/api/roll/get"
                   f"?pageid=153&lid={lid}&num=50&page={page}")
            try:
                data = requests.get(url, headers=hdr, timeout=TIMEOUT)\
                       .json().get("result", {}).get("data", [])
            except Exception as e:
                log(f"  ⚠️ 新浪抓取失败 lid={lid} -> {e}")
                break
            if not data:
                break
            for it in data:
                out.append({
                    "title":   (it.get("title") or "").strip(),
                    "link":    it.get("url") or it.get("wapurl") or "",
                    "summary": (it.get("intro") or "").strip(),
                    "date":    it.get("ctime", ""),
                    "source":  "新浪娱乐",
                })
    return out


def source_tickethk(artists):
    """香港 TicketHK 聚合站:列出各演唱会,标题在 <a title="..."> 里,含艺人名+场馆+日期。"""
    out = []
    htm = fetch_html("https://www.tickethk.com/concerts")
    for m in re.finditer(r'<a title="([^"]+)" href="(/concert-ticket/\d+)"', htm):
        title = html.unescape(m.group(1)).strip()
        link  = "https://www.tickethk.com" + m.group(2)
        out.append({"title": title, "link": link, "summary": "", "date": "", "source": "TicketHK香港"})
    return out


def source_sistic(artists):
    """新加坡 SISTIC:直接调它的后台 JSON 接口(client=1),分页拉全部活动。
    标题里含中/英文艺人名,可直接匹配;附场馆、日期、票价。"""
    import time
    out, api = [], "https://cms.sistic.com.sg/sistic/docroot/api/events"
    hdr = {**UA, "Accept": "application/json", "Referer": "https://www.sistic.com.sg/"}
    for first in range(0, 400, 20):                    # 最多约 400 个活动,足够覆盖
        url = f"{api}?first={first}&limit=20&sort_type=date&sort_order=ASC&index=global&client=1"
        try:
            data = requests.get(url, headers=hdr, timeout=TIMEOUT).json().get("data", [])
        except Exception as e:
            log(f"  ⚠️ SISTIC 抓取失败 first={first} -> {e}")
            break
        if not data:
            break
        for e in data:
            venue = e.get("venue_name", "")
            date  = e.get("event_date", "")
            price = f"{e.get('currency_code','')}{e.get('min_price','')}" if e.get("min_price") else ""
            out.append({
                "title":   (e.get("title") or "").strip(),
                "link":    "https://www.sistic.com.sg/events/" + (e.get("alias") or ""),
                "summary": " · ".join(x for x in [venue, date, price] if x),
                "date":    date,
                "source":  "SISTIC新加坡",
            })
        if len(data) < 20:
            break
        time.sleep(1.0)                                # 温和一点,避免被限流
    return out


def source_damai(artists):
    """内地大麦:关键词搜索(经 RSSHub)。每个艺人搜一次。"""
    out = []
    for a in artists:
        url = f"{RSSHUB}/damai/activity/{requests.utils.quote(a['zh'])}"
        for it in fetch_rss(url):
            it["source"] = "大麦"
            out.append(it)
    return out


# 数据源清单:想开关某个源,把它从这里去掉即可。
# 说明:source_damai 依赖已失效的公共 RSSHub,暂不启用(第三阶段:自建 RSSHub 后恢复)。
SOURCES = [source_weibo, source_sina, source_tickethk, source_sistic]

# 哪些源属于"票务平台"(标题里出现艺人名即算命中,不强制要演出关键词)
TICKETING_SOURCES = {"TicketHK香港", "SISTIC新加坡", "大麦"}


# ================================================================ 匹配与判定
def names_of(artist):
    """一个艺人的所有可匹配名字(简体/繁体/英文/别名)。"""
    names = [artist.get("zh"), artist.get("trad"), artist.get("en")]
    names += artist.get("aliases", [])
    return [n for n in names if n]


def match_artist(text, artists):
    """文本命中哪个艺人?返回艺人字典或 None(取第一个命中)。"""
    low = text.lower()
    for a in artists:
        for name in names_of(a):
            if name.lower() in low:
                return a
    return None


def has_onsale(text):
    """文本里是否已包含开票/发售信息?"""
    low = text.lower()
    return any(k.lower() in low for k in ONSALE_KW)


def is_event(text):
    """文本是否像一条演出消息?"""
    low = text.lower()
    return any(k.lower() in low for k in EVENT_KW)


def make_key(link, title):
    """条目唯一标识:优先用链接,没有则用标题哈希。"""
    base = link.strip() or title.strip()
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


# ================================================================ 状态存取
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================================================================ 邮件
def send_email(subject, body_html, mail_to):
    user = os.environ.get("GMAIL_USER")
    pw   = os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        log("  ✉️ 未配置 GMAIL_USER / GMAIL_APP_PASSWORD,跳过发信(仅更新网页)。")
        return False
    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = mail_to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [mail_to], msg.as_string())
        log(f"  ✉️ 邮件已发送 -> {mail_to}")
        return True
    except Exception as e:
        log(f"  ✉️ 发信失败 -> {e}")
        return False


def build_email(alerts):
    """把本次新消息拼成一封 HTML 邮件。"""
    rows = []
    for a in alerts:
        tag = "✅ 开票" if a["stage"] == "onsale" else "🔔 预警"
        color = "#0a7d28" if a["stage"] == "onsale" else "#c0392b"
        rows.append(f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;">
            <span style="color:{color};font-weight:700;">{tag}</span>
            &nbsp;<b>{html.escape(a['artist'])}</b>
            <span style="color:#888;">· {html.escape(a['source'])}</span><br>
            <a href="{html.escape(a['link'])}" style="color:#1558d6;text-decoration:none;">
              {html.escape(a['title'])}</a>
            {'<div style="color:#555;font-size:13px;margin-top:3px;">'+html.escape(a['summary'][:140])+'</div>' if a['summary'] else ''}
          </td>
        </tr>""")
    return f"""<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:auto;">
      <h2 style="margin:0 0 4px;">🎫 演出开票提醒</h2>
      <div style="color:#888;font-size:13px;margin-bottom:12px;">
        {dt.datetime.now():%Y-%m-%d %H:%M} · 本次发现 {len(alerts)} 条新消息</div>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
      <p style="color:#aaa;font-size:12px;margin-top:16px;">
        「🔔 预警」= 发现有演出消息;「✅ 开票」= 已出现开票/发售信息。<br>
        完整看板见网页版。此邮件由你的自动提醒系统发出。</p>
    </div>"""


# ================================================================ 主流程
def main():
    cfg = load_json(WATCHLIST, {})
    artists = cfg.get("artists", [])
    mail_to = cfg.get("邮箱") or os.environ.get("MAIL_TO", "")
    if not artists:
        log("watchlist.json 里没有艺人,结束。")
        return
    log(f"关注艺人 {len(artists)} 位,开始抓取 {len(SOURCES)} 个数据源…")

    # 1) 抓取所有源
    raw = []
    for src in SOURCES:
        try:
            got = src(artists)
            log(f"  · {src.__name__}: {len(got)} 条")
            raw.extend(got)
        except Exception as e:
            log(f"  · {src.__name__} 异常: {e}")

    # 2) 匹配艺人 + 演出关键词
    matched = []
    for it in raw:
        text = f"{it.get('title','')} {it.get('summary','')}"
        a = match_artist(text, artists)
        if not a:
            continue
        # 票务平台列表页:命中艺人名即可;社交/媒体:还要像一条演出消息
        is_ticketing = it["source"] in TICKETING_SOURCES
        if not is_ticketing and not is_event(text):
            continue
        # 票务平台列表页只知道"有这场演出",确切开票时间在详情页里 -> 归为「预警」;
        # 社交/媒体正文若已明说开票信息 -> 归为「开票」
        onsale = (not is_ticketing) and has_onsale(text)
        matched.append({
            "key":     make_key(it.get("link", ""), it.get("title", "")),
            "artist":  a["zh"],
            "type":    a.get("type", ""),
            "title":   it.get("title", "").strip(),
            "link":    it.get("link", "").strip(),
            "summary": it.get("summary", "").strip(),
            "source":  it["source"],
            "date":    it.get("date", ""),
            "stage":   "onsale" if onsale else "announce",
            "found_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    log(f"匹配到 {len(matched)} 条与关注艺人相关的演出消息。")

    # 3) 与历史对比,挑出需要提醒的新消息
    state = load_json(STATE_FILE, {"seen": {}})
    seen = state["seen"]
    alerts = []
    for m in matched:
        k = m["key"]
        if k not in seen:
            seen[k] = {"stage": m["stage"], "first_seen": m["found_at"]}
            alerts.append(m)                      # 全新消息 -> 提醒
        elif m["stage"] == "onsale" and seen[k].get("stage") != "onsale":
            seen[k]["stage"] = "onsale"
            alerts.append(m)                      # 从"预警"升级到"开票" -> 再提醒一次

    # 4) 更新网页看板数据(保留最近的匹配结果,最多 200 条)
    board = load_json(EVENTS_JSON, {"events": []})
    known = {e["key"]: e for e in board.get("events", [])}
    for m in matched:
        known[m["key"]] = m
    events = sorted(known.values(), key=lambda e: e.get("found_at", ""), reverse=True)[:200]
    save_json(EVENTS_JSON, {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "events": events})
    render_dashboard(events, cfg)
    save_json(STATE_FILE, state)

    # 5) 发邮件
    if alerts:
        log(f"本次有 {len(alerts)} 条新消息,发送邮件…")
        subj = f"🎫 {len(alerts)} 条新演出消息 · {dt.datetime.now():%m-%d}"
        send_email(subj, build_email(alerts), mail_to)
    else:
        log("本次没有新消息,不发邮件。")

    log("完成。")


# ================================================================ 网页看板
def render_dashboard(events, cfg):
    def card(e):
        stage = e.get("stage")
        tag   = ("✅ 开票", "#0a7d28") if stage == "onsale" else ("🔔 预警", "#c0392b")
        summ  = f'<div class="summary">{html.escape(e.get("summary","")[:160])}</div>' if e.get("summary") else ""
        return f"""
        <a class="card" href="{html.escape(e.get('link','#'))}" target="_blank">
          <div class="top"><span class="badge" style="background:{tag[1]}">{tag[0]}</span>
            <span class="artist">{html.escape(e.get('artist',''))}</span>
            <span class="type">{html.escape(e.get('type',''))}</span>
            <span class="src">{html.escape(e.get('source',''))}</span></div>
          <div class="title">{html.escape(e.get('title',''))}</div>
          {summ}
          <div class="foot">发现于 {html.escape(e.get('found_at',''))}</div>
        </a>"""
    cards = "\n".join(card(e) for e in events) or '<p class="empty">暂无消息。系统每天自动检查一次,一有动静就会出现在这里,并给你发邮件。</p>'
    updated = load_json(EVENTS_JSON, {}).get("updated", "")
    names = "、".join(a["zh"] for a in cfg.get("artists", []))
    htm = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🎫 演出开票提醒</title>
<style>
  body{{font-family:-apple-system,"PingFang SC",Helvetica,Arial,sans-serif;background:#f5f6f8;margin:0;color:#222;}}
  .wrap{{max-width:760px;margin:0 auto;padding:20px 16px 60px;}}
  h1{{font-size:22px;margin:0 0 2px;}}
  .sub{{color:#888;font-size:13px;margin-bottom:4px;}}
  .watch{{color:#aaa;font-size:12px;margin-bottom:18px;line-height:1.6;}}
  .card{{display:block;background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:12px;
        text-decoration:none;color:inherit;box-shadow:0 1px 3px rgba(0,0,0,.06);transition:.15s;}}
  .card:hover{{box-shadow:0 3px 10px rgba(0,0,0,.12);transform:translateY(-1px);}}
  .top{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;margin-bottom:6px;}}
  .badge{{color:#fff;font-weight:700;font-size:12px;padding:2px 8px;border-radius:20px;}}
  .artist{{font-weight:700;}} .type{{color:#999;}} .src{{color:#bbb;margin-left:auto;}}
  .title{{font-size:15px;font-weight:600;line-height:1.5;}}
  .summary{{color:#666;font-size:13px;margin-top:5px;line-height:1.5;}}
  .foot{{color:#bbb;font-size:12px;margin-top:8px;}}
  .empty{{color:#999;text-align:center;padding:60px 20px;background:#fff;border-radius:12px;}}
</style></head><body><div class="wrap">
  <h1>🎫 演出开票提醒</h1>
  <div class="sub">最近更新:{updated} · 每天自动检查一次</div>
  <div class="watch">关注中:{html.escape(names)}</div>
  {cards}
</div></body></html>"""
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DASH_HTML, "w", encoding="utf-8") as f:
        f.write(htm)


if __name__ == "__main__":
    main()
