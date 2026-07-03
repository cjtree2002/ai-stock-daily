#!/usr/bin/env python3
"""
AI Stock Daily News Fetcher
Fetches previous day's news for AI sector companies and generates an HTML report.
"""
import os
import re
import sys
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from companies import AI_COMPANIES

# ── Config ──────────────────────────────────────────────────────────────────
# Beijing timezone (UTC+8) — GitHub runners use UTC, so we convert explicitly
BEIJING_TZ = timezone(timedelta(hours=8))
# Finnhub: finance-specific company news, no 24h delay (free tier: 60 calls/min).
# NOTE: the GitHub Actions secret is stored under the name NEWS_API_KEY (reused
# from the old NewsAPI setup) so we can swap the source without touching the
# workflow file. FINNHUB_API_KEY takes precedence when set (local runs).
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY") or os.environ.get("NEWS_API_KEY", "")
OUTPUT_DIR = Path(__file__).parent / "reports"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
MAX_ARTICLES_PER_COMPANY = 5

# Stay under Finnhub's 60 calls/min limit.
REQUEST_DELAY = 1.1  # seconds between requests


# Set by fetch_all_news so main() can refuse to overwrite a good page on failure.
RATE_LIMITED = False


def fetch_company_news_finnhub(symbol: str, from_date: str, to_date: str) -> list[dict]:
    """Fetch finance news for one ticker from Finnhub (already company-specific)."""
    global RATE_LIMITED
    url = "https://finnhub.io/api/v1/company-news"
    params = {"symbol": symbol, "from": from_date, "to": to_date, "token": FINNHUB_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            RATE_LIMITED = True
            print(f"  [WARN] Finnhub rate-limited on {symbol}", file=sys.stderr)
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [WARN] Finnhub error for {symbol}: {e}", file=sys.stderr)
        return []


def _normalize_finnhub(item: dict) -> dict:
    """Map a Finnhub news item to the internal article shape used downstream."""
    ts = item.get("datetime") or 0
    published = (
        datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ts else ""
    )
    return {
        "title": item.get("headline") or "",
        "description": item.get("summary") or "",
        "url": item.get("url") or "",
        "source": {"name": item.get("source") or ""},
        "publishedAt": published,
    }


def _rank_company_news(articles: list[dict], pats: list) -> list[dict]:
    """Score, de-duplicate, and keep the top-N most important items for a company.

    Finnhub's per-symbol feed includes loosely-related sector news, so we only
    keep articles that actually name the company (in headline or summary) and
    rank headline mentions highest.
    """
    seen_urls = set()
    scored = []
    for a in articles:
        if _is_junk_source(a):
            continue
        url = a.get("url")
        if not url or url in seen_urls:
            continue
        title = a.get("title") or ""
        desc = a.get("description") or ""
        content = title + " " + desc
        title_hit = any(r.search(title) for r in pats)
        content_hit = title_hit or any(r.search(content) for r in pats)
        if not content_hit:
            continue  # not actually about this company — drop sector noise
        seen_urls.add(url)
        score = 12 if title_hit else 4         # headline mention = strongly about it
        score += _source_score((a.get("source") or {}).get("name"))
        score += _finance_bonus(content)
        score += _recency_bonus(a.get("publishedAt") or "")
        if not desc.strip():
            score -= 1
        scored.append((score, a.get("publishedAt") or "", a))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    kept, kept_titles = [], []
    for _, _, a in scored:
        if _is_near_duplicate(a.get("title") or "", kept_titles):
            continue
        kept.append(a)
        kept_titles.append(a.get("title") or "")
        if len(kept) >= MAX_ARTICLES_PER_COMPANY:
            break
    return kept


# Ticker symbols that are also common English words / ubiquitous acronyms.
# Matching these as bare symbols produces garbage (e.g. "AI" appears in every
# AI article, "NOW"/"NET"/"PATH" are everyday words), so we ignore the bare
# symbol and rely on the company's full name instead.
SKIP_TICKER_TOKENS = {
    "AI", "S", "NET", "NOW", "NICE", "PATH", "OLO", "ZEN",
    "ON", "IT", "ALL", "ARE", "GM", "ARM", "IOT", "ROP", "TYL",
}


def _is_ticker_token(kw: str) -> bool:
    """True if the keyword looks like a bare ticker symbol (e.g. 'MU', 'NVDA')."""
    k = kw.strip()
    return k.isalpha() and k.isupper() and 1 <= len(k) <= 5


def _compile_keyword(kw: str):
    """Compile one keyword into (regex, ...) or return None to skip it.

    - Bare ticker symbols (MU, NVDA, AMD): matched CASE-SENSITIVELY with word
      boundaries, so the uppercase ticker 'MU' matches but the lowercase 'mu'
      inside 'immune'/'muscle' does not. Generic-word tickers are skipped.
    - Distinctive names/phrases (Micron, HBM memory, C3.ai): matched
      case-insensitively with boundaries.
    """
    k = kw.strip()
    if not k:
        return None
    if _is_ticker_token(k):
        if k in SKIP_TICKER_TOKENS:
            return None
        return re.compile(r"(?<![A-Za-z0-9.])" + re.escape(k) + r"(?![A-Za-z0-9.])")
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _build_company_patterns(companies: list[dict]) -> list[tuple]:
    """Pre-compile keyword regexes once per company."""
    out = []
    for c in companies:
        pats = [p for p in (_compile_keyword(kw) for kw in c["keywords"]) if p is not None]
        out.append((c, pats))
    return out


# ── Importance scoring ───────────────────────────────────────────────────────
# Top-tier financial/tech outlets (substring match, lowercased).
TOP_SOURCES = {
    "reuters", "bloomberg", "wall street journal", "wsj", "cnbc",
    "financial times", "ft.com", "associated press", "ap news",
    "new york times", "nytimes", "barron",
}
GOOD_SOURCES = {
    "marketwatch", "forbes", "business insider", "yahoo", "techcrunch",
    "the verge", "axios", "fortune", "motley fool", "seeking alpha",
    "benzinga", "zacks", "the information", "ars technica", "engadget",
    "cnet", "investor's business daily", "investopedia",
}
# Deal sites, press-release wires, content farms, aggregators — investor noise.
JUNK_SOURCES = {
    "ozbargain", "slickdeals", "dealnews", "techbargains", "9to5toys",
    "prtimes", "biztoc", "naturalnews", "globenewswire", "prnewswire",
    "pr newswire", "business wire", "businesswire", "accesswire",
    "einnews", "openpr", "newsfile", "prweb", "digitaljournal",
}

# Market + material-event context — signals the article matters to the stock.
# Includes finance terms AND business events (regulatory, M&A, products, legal)
# so that material news like a Tesla safety probe also scores well.
FINANCE_TERMS = [
    # markets / finance
    "stock", "shares", "share price", "earnings", "revenue", "market cap",
    "valuation", "analyst", "price target", "quarterly", "guidance",
    "wall street", "nasdaq", "nyse", "ipo", "dividend", "investor",
    "sell-off", "selloff", "rally", "billion", "forecast", "upgrade",
    "downgrade", "%", "profit", "sales", "outlook", "rating",
    # material business events
    "probe", "investigation", "lawsuit", "recall", "regulator", "antitrust",
    "ceo", "resign", "layoff", "acquire", "acquisition", "merger",
    "partnership", "contract", "unveil", "launch", "data center", "chip",
]


def _source_score(name: str) -> int:
    n = (name or "").lower()
    if any(s in n for s in JUNK_SOURCES):
        return -8
    if any(s in n for s in TOP_SOURCES):
        return 6
    if any(s in n for s in GOOD_SOURCES):
        return 3
    return 0


def _finance_bonus(content: str) -> int:
    c = content.lower()
    hits = sum(1 for t in FINANCE_TERMS if t in c)
    return min(hits, 3) * 2  # 0 .. 6


def _recency_bonus(published: str) -> float:
    """Small bonus for articles later in the day (closer to / after market close)."""
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return round(dt.hour / 24 * 2, 2)  # 0 .. ~2
    except Exception:
        return 0.0


def _score_article(article: dict, pats: list) -> float:
    """Importance score for one article w.r.t. one company's keyword patterns."""
    title = article.get("title") or ""
    desc = article.get("description") or ""
    content = title + " " + desc
    title_hit = any(p.search(title) for p in pats)          # named in headline = strong
    n_kw = sum(1 for p in pats if p.search(content))         # how many keywords matched
    score = (12 if title_hit else 0)
    score += min(n_kw, 3) * 2
    score += _source_score((article.get("source") or {}).get("name"))
    score += _finance_bonus(content)
    score += _recency_bonus(article.get("publishedAt") or "")
    # mild penalty for thin/empty articles
    if not desc.strip():
        score -= 2
    return score


def _is_junk_source(article: dict) -> bool:
    """Deal sites, PR wires, content farms — never relevant to a stock daily."""
    name = ((article.get("source") or {}).get("name") or "").lower()
    return any(s in name for s in JUNK_SOURCES)


def _title_tokens(t: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (t or "").lower()))


def _is_near_duplicate(title: str, kept_titles: list[str]) -> bool:
    """Jaccard similarity on title words — drop repeats of the same story."""
    toks = _title_tokens(title)
    if not toks:
        return False
    for kt in kept_titles:
        other = _title_tokens(kt)
        if not other:
            continue
        inter = len(toks & other)
        union = len(toks | other)
        if union and inter / union >= 0.6:
            return True
    return False


def assign_articles_to_companies(articles: list[dict], companies: list[dict]) -> dict:
    """Pick the top-N most important articles per company (keyed by company name).

    Selection principle: collect every keyword match, score each by headline
    hit + keyword count + source authority + recency, drop near-duplicates,
    then keep the highest-scoring MAX_ARTICLES_PER_COMPANY, sorted by score.
    """
    patterns = _build_company_patterns(companies)
    name_to_pats = {c["name"]: pats for c, pats in patterns}

    # 1) Collect all matching articles per company (junk sources excluded outright).
    raw: dict[str, list] = {c["name"]: [] for c in companies}
    for article in articles:
        if _is_junk_source(article):
            continue
        title = article.get("title") or ""
        description = article.get("description") or ""
        content = title + " " + description  # original case for ticker matching
        for company, pats in patterns:
            if any(p.search(content) for p in pats):
                raw[company["name"]].append(article)

    # 2) Score, de-dup, keep top N sorted by importance.
    company_articles: dict[str, list] = {}
    for name, arts in raw.items():
        pats = name_to_pats[name]
        seen_urls = set()
        scored = []
        for art in arts:
            url = art.get("url")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            scored.append((_score_article(art, pats), art.get("publishedAt") or "", art))
        # highest score first; ties broken by most recent (ISO strings sort chronologically)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        kept, kept_titles = [], []
        for _, _, art in scored:
            if _is_near_duplicate(art.get("title") or "", kept_titles):
                continue
            kept.append(art)
            kept_titles.append(art.get("title") or "")
            if len(kept) >= MAX_ARTICLES_PER_COMPANY:
                break
        company_articles[name] = kept

    return company_articles


def build_query_batches(companies: list[dict], batch_size: int = 8) -> list[list[str]]:
    """Group all company keywords into batches for bulk API calls."""
    all_primary_keywords = [c["keywords"][0] for c in companies]
    batches = []
    for i in range(0, len(all_primary_keywords), batch_size):
        batches.append(all_primary_keywords[i:i + batch_size])
    return batches


def fetch_stock_prices(target_date: datetime) -> dict:
    """Fetch close, daily change, 52w range and a 20-day sparkline per ticker."""
    tickers = list({c["ticker"] for c in AI_COMPANIES if c["ticker"] != "N/A"})
    print(f"Fetching stock prices for {len(tickers)} tickers...")

    # ~1 year for the 52-week range and the sparkline tail.
    end = target_date + timedelta(days=1)
    start = target_date - timedelta(days=370)

    prices: dict[str, dict] = {}
    try:
        data = yf.download(
            tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
        close = data["Close"]
        target_ts = target_date.strftime("%Y-%m-%d")
        for ticker in tickers:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            sub = series[series.index <= target_ts]
            if sub.empty:
                continue
            price = float(sub.iloc[-1])
            prev = float(sub.iloc[-2]) if len(sub) >= 2 else None
            change = ((price - prev) / prev * 100) if prev else None
            year = sub[sub.index > (sub.index[-1] - timedelta(days=365))]
            tail = sub.iloc[-90:]
            prices[ticker] = {
                "price": price,
                "change": change,
                "date": sub.index[-1].strftime("%m/%d"),
                "high52": float(year.max()),
                "low52": float(year.min()),
                "spark": [round(float(x), 4) for x in sub.iloc[-20:].tolist()],
                "chart": [[d.strftime("%m/%d"), round(float(v), 2)]
                          for d, v in zip(tail.index, tail.tolist())],
            }
    except Exception as e:
        print(f"  [WARN] Stock price fetch error: {e}", file=sys.stderr)

    print(f"  Got prices for {len(prices)} tickers")
    return prices


def fetch_indices(target_date: datetime) -> list[dict]:
    """Fetch major market indices for the header bar."""
    index_defs = [
        ("标普500", "^GSPC"), ("纳斯达克", "^IXIC"), ("道琼斯", "^DJI"),
        ("费城半导体", "^SOX"), ("VIX恐慌", "^VIX"),
    ]
    symbols = [s for _, s in index_defs]
    print("Fetching market indices...")
    out = []
    try:
        end = target_date + timedelta(days=1)
        start = target_date - timedelta(days=10)
        data = yf.download(symbols, start=start.strftime("%Y-%m-%d"),
                           end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        close = data["Close"]
        target_ts = target_date.strftime("%Y-%m-%d")
        for label, sym in index_defs:
            if sym not in close.columns:
                continue
            s = close[sym].dropna()
            s = s[s.index <= target_ts]
            if len(s) < 2:
                continue
            cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
            out.append({"label": label, "value": cur, "change": (cur - prev) / prev * 100})
    except Exception as e:
        print(f"  [WARN] Index fetch error: {e}", file=sys.stderr)
    return out


def fetch_earnings_this_week(target_date: datetime) -> dict:
    """Map ticker -> earnings date for watchlist companies reporting in the next 7 days."""
    if not FINNHUB_API_KEY:
        return {}
    our = {c["ticker"] for c in AI_COMPANIES if c["ticker"] != "N/A"}
    frm = target_date.strftime("%Y-%m-%d")
    to = (target_date + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": frm, "to": to, "token": FINNHUB_API_KEY}, timeout=15)
        rows = r.json().get("earningsCalendar", []) if r.status_code == 200 else []
        return {row["symbol"]: row["date"] for row in rows if row.get("symbol") in our}
    except Exception as e:
        print(f"  [WARN] Earnings calendar error: {e}", file=sys.stderr)
        return {}


def fetch_recommendations() -> dict:
    """Map ticker -> latest analyst consensus {buy,hold,sell, label}."""
    if not FINNHUB_API_KEY:
        return {}
    tickers = list({c["ticker"] for c in AI_COMPANIES if c["ticker"] != "N/A"})
    print(f"Fetching analyst ratings for {len(tickers)} tickers...")
    recs = {}
    for tk in tickers:
        try:
            r = requests.get("https://finnhub.io/api/v1/stock/recommendation",
                             params={"symbol": tk, "token": FINNHUB_API_KEY}, timeout=15)
            data = r.json() if r.status_code == 200 else []
            if not data:
                continue
            latest = data[0]
            buy = latest.get("strongBuy", 0) + latest.get("buy", 0)
            hold = latest.get("hold", 0)
            sell = latest.get("sell", 0) + latest.get("strongSell", 0)
            total = buy + hold + sell
            if total == 0:
                continue
            if buy / total >= 0.6:
                label = "买入"
            elif sell > buy:
                label = "卖出"
            else:
                label = "持有"
            recs[tk] = {"buy": buy, "hold": hold, "sell": sell, "label": label}
        except Exception:
            pass
        time.sleep(REQUEST_DELAY)
    print(f"  Got ratings for {len(recs)} tickers")
    return recs


def fetch_all_news(target_date: datetime) -> dict:
    """Fetch per-company finance news from Finnhub (keyed by company name)."""
    if not FINNHUB_API_KEY:
        print("ERROR: FINNHUB_API_KEY not set. Export it before running.", file=sys.stderr)
        sys.exit(1)

    # Finnhub has no 24h delay, so we query the target day directly. We also
    # include the day before to catch late post-close coverage.
    from_str = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    to_str = target_date.strftime("%Y-%m-%d")
    target_str = target_date.strftime("%Y-%m-%d")

    print(f"Fetching news for {target_str} (Finnhub)...")

    company_articles: dict[str, list] = {}
    ticker_cache: dict[str, list] = {}
    name_to_pats = {c["name"]: pats for c, pats in _build_company_patterns(AI_COMPANIES)}

    for i, company in enumerate(AI_COMPANIES):
        ticker = company["ticker"]
        if ticker == "N/A":
            company_articles[company["name"]] = []  # private co. — no ticker to query
            continue
        if ticker not in ticker_cache:
            items = fetch_company_news_finnhub(ticker, from_str, to_str)
            # Keep only items actually dated on the target day (UTC)
            same_day = [it for it in items
                        if datetime.fromtimestamp(it.get("datetime") or 0, timezone.utc)
                        .strftime("%Y-%m-%d") == target_str]
            ticker_cache[ticker] = [_normalize_finnhub(it) for it in (same_day or items)]
            print(f"  [{i+1}/{len(AI_COMPANIES)}] {ticker}: {len(ticker_cache[ticker])} raw")
            time.sleep(REQUEST_DELAY)
        company_articles[company["name"]] = _rank_company_news(
            ticker_cache[ticker], name_to_pats[company["name"]]
        )

    total = sum(len(v) for v in company_articles.values())
    print(f"  Total ranked articles: {total}")
    return company_articles


# ── Translation (Google free endpoint, no API key) ──────────────────────────
_translate_cache: dict[str, str] = {}


def translate_text(text: str) -> str:
    """Translate English text to Simplified Chinese. Falls back to original on error."""
    if not text or not text.strip():
        return text
    if text in _translate_cache:
        return _translate_cache[text]
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        translated = "".join(seg[0] for seg in data[0] if seg[0])
        _translate_cache[text] = translated or text
        return _translate_cache[text]
    except Exception:
        _translate_cache[text] = text  # cache the failure to avoid retry storms
        return text


def pretranslate_displayed(company_articles: dict) -> None:
    """Pre-translate all displayed titles/descriptions concurrently to fill the cache."""
    texts: set[str] = set()
    for company in AI_COMPANIES:
        for art in company_articles.get(company["name"], [])[:MAX_ARTICLES_PER_COMPANY]:
            title = art.get("title")
            if title:
                texts.add(title)
            desc = art.get("description") or ""
            if len(desc) > 120:
                desc = desc[:120] + "…"
            if desc:
                texts.add(desc)
    if not texts:
        return
    print(f"Translating {len(texts)} text segments to Chinese...")
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(translate_text, list(texts)))
    print(f"  Translation done (cache size: {len(_translate_cache)})")


def format_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%m/%d %H:%M UTC")
    except Exception:
        return iso_str or ""


def _sparkline_svg(spark: list, change) -> str:
    if not spark or len(spark) < 2:
        return ""
    w, h = 116, 26
    lo, hi = min(spark), max(spark)
    rng = (hi - lo) or 1
    pts = " ".join(
        f"{i/(len(spark)-1)*w:.1f},{h-(v-lo)/rng*(h-3)-1.5:.1f}"
        for i, v in enumerate(spark)
    )
    color = "var(--green)" if (change or 0) >= 0 else "var(--red)"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round"/></svg>')


def render_stock_price(company: dict, prices: dict, recs: dict, earnings: dict) -> str:
    ticker = company["ticker"]
    if ticker == "N/A" or ticker not in prices:
        return ""
    p = prices[ticker]
    change = p.get("change")
    if change is None:
        change_html = '<span class="price-change flat">—</span>'
    elif change >= 0:
        change_html = f'<span class="price-change up">▲ {change:+.2f}%</span>'
    else:
        change_html = f'<span class="price-change down">▼ {change:.2f}%</span>'

    spark_html = _sparkline_svg(p.get("spark"), change)

    # 52-week position bar
    lo, hi = p.get("low52"), p.get("high52")
    pos_html = ""
    if lo is not None and hi is not None and hi > lo:
        pos = (p["price"] - lo) / (hi - lo) * 100
        pos_html = f'''
          <div class="range52" title="52周区间">
            <span class="r52-lo">${lo:,.0f}</span>
            <span class="r52-bar"><i style="left:{pos:.0f}%"></i></span>
            <span class="r52-hi">${hi:,.0f}</span>
          </div>'''

    # analyst consensus + earnings flag
    tags = ""
    rec = recs.get(ticker)
    if rec:
        cls = {"买入": "buy", "持有": "hold", "卖出": "sell"}.get(rec["label"], "hold")
        tags += (f'<span class="tag rec-{cls}" title="分析师: 买{rec["buy"]}/持{rec["hold"]}/卖{rec["sell"]}">'
                 f'分析师 {rec["label"]}</span>')
    ern = earnings.get(ticker)
    if ern:
        tags += f'<span class="tag earn">📅 {ern[5:]} 财报</span>'
    tags_html = f'<div class="stock-tags">{tags}</div>' if tags else ""

    return f'''
        <div class="stock-price">
          <span class="price-current">${p['price']:,.2f}</span>
          {change_html}
          {spark_html}
        </div>{pos_html}{tags_html}'''


def _render_indices(indices: list) -> str:
    if not indices:
        return ""
    items = ""
    for ix in indices:
        cls = "up" if ix["change"] >= 0 else "down"
        arrow = "▲" if ix["change"] >= 0 else "▼"
        items += (f'<div class="idx"><span class="idx-name">{ix["label"]}</span>'
                  f'<span class="idx-val">{ix["value"]:,.2f}</span>'
                  f'<span class="idx-chg {cls}">{arrow} {ix["change"]:+.2f}%</span></div>')
    return f'<div class="indices">{items}</div>'


def _render_movers(prices: dict) -> str:
    movers = []
    for c in AI_COMPANIES:
        p = prices.get(c["ticker"])
        if p and p.get("change") is not None:
            movers.append((c["name"], c["ticker"], p["change"]))
    if not movers:
        return ""
    movers.sort(key=lambda x: x[2], reverse=True)
    gainers = movers[:5]
    losers = [m for m in movers[::-1] if m[2] < 0][:5]

    def chips(rows, cls):
        return "".join(
            f'<span class="chip {cls}">{n}<b>{ch:+.1f}%</b></span>' for n, t, ch in rows
        )
    return f'''
    <div class="movers">
      <div class="movers-col"><div class="movers-title up">📈 涨幅榜</div>{chips(gainers, "up")}</div>
      <div class="movers-col"><div class="movers-title down">📉 跌幅榜</div>{chips(losers, "down")}</div>
    </div>'''


def _summary_bullets(company_articles: dict, prices: dict, indices: list) -> list:
    bullets = []
    if indices:
        sox = next((i for i in indices if i["label"] == "费城半导体"), None)
        if sox:
            tone = "上涨" if sox["change"] >= 0 else "下跌"
            bullets.append(f"费城半导体指数{tone} {sox['change']:+.2f}%，{'科技股情绪偏暖' if sox['change']>=0 else '芯片股承压'}")
    movers = [(c["name"], prices[c["ticker"]]["change"]) for c in AI_COMPANIES
              if prices.get(c["ticker"]) and prices[c["ticker"]].get("change") is not None]
    movers.sort(key=lambda x: x[1], reverse=True)
    if movers:
        bullets.append(f"关注列表涨幅最大：{movers[0][0]} {movers[0][1]:+.1f}%")
        if movers[-1][1] < 0:
            bullets.append(f"跌幅最大：{movers[-1][0]} {movers[-1][1]:+.1f}%")
    most = max(AI_COMPANIES, key=lambda c: len(company_articles.get(c["name"], [])), default=None)
    if most and company_articles.get(most["name"]):
        headline = translate_text(company_articles[most["name"]][0].get("title") or "")
        bullets.append(f"{most['name']} 新闻最多——{headline}")
    return bullets


def _build_summary(company_articles: dict, prices: dict, indices: list) -> str:
    bullets = _summary_bullets(company_articles, prices, indices)
    if not bullets:
        return ""
    lis = "".join(f"<li>{b}</li>" for b in bullets)
    return f'<div class="summary"><div class="summary-title">📌 今日要点</div><ul>{lis}</ul></div>'


def push_telegram(title: str, body: str, url: str) -> None:
    """Send the morning digest to the user via a Telegram bot (no-op if unset)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    text = f"{title}\n\n{body}\n\n{url}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
        print(f"  Telegram push: {r.status_code}")
    except Exception as e:
        print(f"  [WARN] Telegram push failed: {e}", file=sys.stderr)


# ── AI paper portfolio ───────────────────────────────────────────────────────
def fetch_postmarket_prices(tickers: list, target_date: datetime) -> dict:
    """Final traded price of the target date INCLUDING after-hours (per user:
    盘后结束的最后价格). Minute bars constrained to the target date so results
    are deterministic no matter when the script runs."""
    out = {}
    try:
        data = yf.download(
            tickers,
            start=target_date.strftime("%Y-%m-%d"),
            end=(target_date + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m", prepost=True, progress=False, auto_adjust=False,
        )
        close = data["Close"]
        for t in tickers:
            try:
                s = close[t].dropna()
                if len(s):
                    out[t] = float(s.iloc[-1])
            except Exception:
                pass
    except Exception as e:
        print(f"  [WARN] post-market price fetch: {e}", file=sys.stderr)
    return out


def _qqq_change(target_date: datetime) -> float:
    """QQQ daily % change on the target date (benchmark)."""
    try:
        h = yf.Ticker("QQQ").history(
            start=(target_date - timedelta(days=10)).strftime("%Y-%m-%d"),
            end=(target_date + timedelta(days=1)).strftime("%Y-%m-%d"))
        s = h["Close"].dropna()
        try:
            s.index = s.index.tz_localize(None)
        except Exception:
            pass
        s = s[s.index <= target_date.strftime("%Y-%m-%d")]
        if len(s) >= 2:
            return (float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100
    except Exception as e:
        print(f"  [WARN] QQQ benchmark fetch: {e}", file=sys.stderr)
    return 0.0


def update_portfolio(prices: dict, target_date: datetime):
    """Mark the paper portfolio to market (post-market final prices) and append
    the daily equity/benchmark history. Re-runs on the same date replace that
    date's entry; the inception baseline is never touched."""
    if not PORTFOLIO_PATH.exists():
        return None
    pf = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    init = pf["initial_capital"]
    print("Updating AI paper portfolio...")

    live = fetch_postmarket_prices([h["ticker"] for h in pf["holdings"]], target_date)
    px_map, total = {}, pf["cash"]
    for h in pf["holdings"]:
        px = live.get(h["ticker"]) or (prices.get(h["ticker"]) or {}).get("price") or h["avg_cost"]
        px_map[h["ticker"]] = px
        total += h["shares"] * px

    ds = target_date.strftime("%Y-%m-%d")
    hist = pf.get("history", [])
    before = [e for e in hist if e["date"] < ds]
    base = before[-1] if before else {"value": init, "eq": init, "qqq": init}

    if hist and ds > hist[0]["date"]:
        changes = [v["change"] for v in prices.values() if v.get("change") is not None]
        eq_ch = sum(changes) / len(changes) if changes else 0.0
        entry = {"date": ds, "value": round(total, 2),
                 "eq": round(base["eq"] * (1 + eq_ch / 100), 2),
                 "qqq": round(base["qqq"] * (1 + _qqq_change(target_date) / 100), 2)}
        pf["history"] = before + [entry]
        PORTFOLIO_PATH.write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")

    last = pf["history"][-1]
    stats = {
        "total": total,
        "day": total - base["value"],
        "day_pct": (total / base["value"] - 1) * 100 if base["value"] else 0.0,
        "cum_pct": (total / init - 1) * 100,
        "eq_cum": (last["eq"] / init - 1) * 100,
        "qqq_cum": (last["qqq"] / init - 1) * 100,
    }
    print(f"  Portfolio value: ${total:,.0f} ({stats['cum_pct']:+.2f}% cum)")
    return pf, px_map, stats


def _money(x: float) -> str:
    return ("-" if x < 0 else "") + f"${abs(x):,.0f}"


def _pct_span(p: float, digits: int = 2) -> str:
    cls = "up" if p >= 0 else "down"
    return f'<span class="{cls}">{p:+.{digits}f}%</span>'


def _equity_curve_svg(hist: list, init: float) -> str:
    if len(hist) < 2:
        return '<div class="pf-chart-empty">净值曲线将从明日起逐日累积</div>'
    series = {"value": [], "eq": [], "qqq": []}
    for e in hist:
        for k in series:
            series[k].append((e[k] / init - 1) * 100)
    W, H, padL, padR, padT, padB = 700, 160, 44, 8, 8, 18
    allv = series["value"] + series["eq"] + series["qqq"]
    lo, hi = min(allv), max(allv)
    if hi - lo < 0.5:
        lo, hi = lo - 0.5, hi + 0.5
    n = len(hist)

    def X(i): return padL + i / (n - 1) * (W - padL - padR)
    def Y(v): return padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB)
    def line(vals): return " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))

    parts = [f'<svg viewBox="0 0 {W} {H}" class="pf-chart" width="100%" height="{H}">']
    for gv in (hi, lo):
        gy = Y(gv)
        parts.append(f'<line x1="{padL}" y1="{gy:.1f}" x2="{W - padR}" y2="{gy:.1f}" stroke="var(--border-light)"/>')
        parts.append(f'<text x="{padL - 4}" y="{gy + 3:.1f}" text-anchor="end" font-size="9" fill="var(--text-dim)">{gv:+.1f}%</text>')
    if lo <= 0 <= hi:
        parts.append(f'<line x1="{padL}" y1="{Y(0):.1f}" x2="{W - padR}" y2="{Y(0):.1f}" stroke="var(--border)" stroke-dasharray="4 3"/>')
    parts.append(f'<polyline points="{line(series["eq"])}" fill="none" stroke="#94a3b8" stroke-width="1.3" stroke-dasharray="5 3"/>')
    parts.append(f'<polyline points="{line(series["qqq"])}" fill="none" stroke="#f59e0b" stroke-width="1.3"/>')
    parts.append(f'<polyline points="{line(series["value"])}" fill="none" stroke="var(--accent)" stroke-width="2.2"/>')
    parts.append(f'<text x="{padL}" y="{H - 4}" font-size="9" fill="var(--text-dim)">{hist[0]["date"][5:]}</text>')
    parts.append(f'<text x="{W - padR}" y="{H - 4}" text-anchor="end" font-size="9" fill="var(--text-dim)">{hist[-1]["date"][5:]}</text>')
    parts.append('</svg>')
    parts.append('<div class="pf-legend"><span><i style="background:var(--accent)"></i>AI模拟盘</span>'
                 '<span><i style="background:#94a3b8"></i>32股等权基准</span>'
                 '<span><i style="background:#f59e0b"></i>纳指100(QQQ)</span></div>')
    return "".join(parts)


def render_portfolio(pf: dict, px_map: dict, stats: dict) -> str:
    name_map = {c["ticker"]: c["name"] for c in AI_COMPANIES}
    total = stats["total"]

    rows = ""
    for h in sorted(pf["holdings"], key=lambda h: -h["shares"] * px_map[h["ticker"]]):
        t = h["ticker"]
        px = px_map[t]
        mv = h["shares"] * px
        pnl = (px / h["avg_cost"] - 1) * 100
        rows += (f'<tr><td>{name_map.get(t, t)}</td><td class="mono">{t}</td>'
                 f'<td>{mv / total * 100:.1f}%</td><td class="mono">${h["avg_cost"]:,.2f}</td>'
                 f'<td class="mono">${px:,.2f}</td><td>{_pct_span(pnl, 1)}</td>'
                 f'<td class="mono">{_money(mv)}</td></tr>')
    rows += (f'<tr class="cash-row"><td>💵 现金</td><td></td><td>{pf["cash"] / total * 100:.1f}%</td>'
             f'<td></td><td></td><td></td><td class="mono">{_money(pf["cash"])}</td></tr>')

    trades = list(reversed(pf.get("trades", [])))
    titems = "".join(
        f'<div class="trade-item"><b>{tr["date"]}</b> {tr["action"]} '
        f'{name_map.get(tr["ticker"], tr["ticker"])} {tr["shares"]}股 @ ${tr["price"]:,.2f}'
        f' — {tr["reason"]}</div>' for tr in trades)

    alpha = stats["cum_pct"] - stats["eq_cum"]
    return f'''
  <div class="pf-panel">
    <div class="pf-head">
      <span class="pf-title">🤖 AI 模拟盘</span>
      <span class="pf-sub">初始 $1,000,000 · {pf["inception"]} 起 · {pf["strategy"]} · 盘后最终价估值 · 纯多头不加杠杆 · 模拟盘不构成投资建议</span>
    </div>
    <div class="pf-stats">
      <div><div class="pf-stat-label">总资产</div><div class="pf-stat-value">{_money(total)}</div></div>
      <div><div class="pf-stat-label">当日盈亏</div><div class="pf-stat-value">{_pct_span(stats["day_pct"])} <span class="pf-stat-sm">{_money(stats["day"])}</span></div></div>
      <div><div class="pf-stat-label">累计收益</div><div class="pf-stat-value">{_pct_span(stats["cum_pct"])}</div></div>
      <div><div class="pf-stat-label">32股等权基准</div><div class="pf-stat-value">{_pct_span(stats["eq_cum"])}</div></div>
      <div><div class="pf-stat-label">纳指100(QQQ)</div><div class="pf-stat-value">{_pct_span(stats["qqq_cum"])}</div></div>
      <div><div class="pf-stat-label">超额收益(vs等权)</div><div class="pf-stat-value">{_pct_span(alpha)}</div></div>
    </div>
    {_equity_curve_svg(pf["history"], pf["initial_capital"])}
    <div class="pf-table-wrap"><table class="pf-table">
      <tr><th>持仓</th><th>代码</th><th>仓位</th><th>成本</th><th>现价</th><th>盈亏</th><th>市值</th></tr>
      {rows}
    </table></div>
    <details class="trade-log"><summary>📒 调仓日志（{len(trades)} 笔，含每笔理由）</summary>{titems}</details>
  </div>'''


def _available_dates(target_date: datetime) -> list:
    """List archived report dates (YYYY-MM-DD) for the history dropdown, newest first."""
    import re as _re
    dates = set()
    for d in (OUTPUT_DIR, OUTPUT_DIR.parent / "docs"):
        if d.exists():
            for f in d.glob("*.html"):
                if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem):
                    dates.add(f.stem)
    dates.add(target_date.strftime("%Y-%m-%d"))
    return sorted(dates, reverse=True)


def generate_html(company_articles: dict, prices: dict, indices: list,
                  recs: dict, earnings: dict, target_date: datetime,
                  portfolio_html: str = "") -> str:
    date_str = target_date.strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday_str = weekdays[target_date.weekday()]
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M") + " 北京时间"

    indices_html = _render_indices(indices)
    summary_html = _build_summary(company_articles, prices, indices)
    movers_html = _render_movers(prices)

    # Build cards for ALL companies (no-news ones show a placeholder) with
    # data-* attributes so the front-end can search / sort / filter / group.
    cards_html = ""
    companies_with_news = 0
    total_articles = 0

    for order, company in enumerate(AI_COMPANIES):
        ticker = company["ticker"]
        sector = company.get("sector", "其他")
        articles = company_articles.get(company["name"], [])
        if articles:
            companies_with_news += 1
            total_articles += len(articles)

        ticker_badge = (
            f'<span class="ticker">{ticker}</span>'
            if ticker != "N/A"
            else '<span class="ticker private">私有</span>'
        )
        count_badge = (f'<span class="article-count">{len(articles)} 条</span>'
                       if articles else '<span class="article-count muted">无新闻</span>')

        stock_html = render_stock_price(company, prices, recs, earnings)

        if articles:
            articles_html = ""
            for art in articles:
                title = translate_text(art.get("title") or "") or "无标题"
                url = art.get("url") or "#"
                source = (art.get("source") or {}).get("name") or ""
                published = format_time(art.get("publishedAt") or "")
                description = art.get("description") or ""
                if len(description) > 120:
                    description = description[:120] + "…"
                description = translate_text(description)
                articles_html += f"""
            <div class="article">
              <a href="{url}" target="_blank" class="article-title">{title}</a>
              <div class="article-meta">
                <span class="source">{source}</span>
                <span class="pub-time">{published}</span>
              </div>
              {f'<p class="article-desc">{description}</p>' if description else ''}
            </div>"""
        else:
            articles_html = '<div class="no-articles">今日无相关新闻</div>'

        p = prices.get(ticker) or {}
        change = p.get("change")
        change_attr = f"{change:.4f}" if change is not None else ""
        search_key = f'{company["name"]} {ticker}'.lower()

        cards_html += f"""
      <div class="company-card" data-name="{search_key}" data-ticker="{ticker}"
           data-sector="{sector}" data-change="{change_attr}"
           data-news="{len(articles)}" data-order="{order}">
        <div class="card-header">
          <span class="company-name">{company["name"]}</span>
          {ticker_badge}
          <span class="sector-badge">{sector}</span>
          {count_badge}
        </div>{stock_html}
        <div class="articles">{articles_html}
        </div>
      </div>"""

    # Sector tabs (in list order, de-duplicated)
    seen, sectors = set(), []
    for c in AI_COMPANIES:
        s = c.get("sector", "其他")
        if s not in seen:
            seen.add(s)
            sectors.append(s)
    tabs_html = '<button class="tab-btn active" data-sector="all">全部</button>'
    tabs_html += "".join(f'<button class="tab-btn" data-sector="{s}">{s}</button>' for s in sectors)

    # History dropdown from archived dated reports
    dates = _available_dates(target_date)
    opts = "".join(
        f'<option value="{d}"{" selected" if d == target_date.strftime("%Y-%m-%d") else ""}>{d}</option>'
        for d in dates
    )
    history_html = (f'<select id="historySelect" class="history-select">{opts}</select>'
                    if len(dates) > 1 else "")

    # Embedded price history for the interactive zoom chart (keyed by ticker).
    chart_data = {
        c["ticker"]: {"name": c["name"], "pts": prices[c["ticker"]]["chart"]}
        for c in AI_COMPANIES
        if prices.get(c["ticker"]) and prices[c["ticker"]].get("chart")
    }
    chart_json = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{DATE_STR}}", date_str)
        .replace("{{WEEKDAY}}", weekday_str)
        .replace("{{GENERATED_AT}}", now_str)
        .replace("{{COMPANIES_COUNT}}", str(companies_with_news))
        .replace("{{ARTICLES_COUNT}}", str(total_articles))
        .replace("{{INDICES_HTML}}", indices_html)
        .replace("{{SUMMARY_HTML}}", summary_html)
        .replace("{{MOVERS_HTML}}", movers_html)
        .replace("{{SECTOR_TABS}}", tabs_html)
        .replace("{{HISTORY_OPTIONS}}", history_html)
        .replace("{{CARDS_HTML}}", cards_html)
        .replace("{{PORTFOLIO_HTML}}", portfolio_html)
        .replace("{{CHART_DATA}}", chart_json)
    )


def main():
    # Default: yesterday (relative to Beijing date)
    target = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(days=1)
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    company_articles = fetch_all_news(target)

    # Safety guard: if the news API was rate-limited and we got essentially
    # nothing, do NOT overwrite the last good page with an empty report.
    total = sum(len(v) for v in company_articles.values())
    if RATE_LIMITED and total == 0:
        print("ERROR: news API rate-limited, no articles. Keeping previous report.",
              file=sys.stderr)
        sys.exit(1)

    prices = fetch_stock_prices(target)
    indices = fetch_indices(target)
    earnings = fetch_earnings_this_week(target)
    recs = fetch_recommendations()
    pretranslate_displayed(company_articles)

    # AI paper portfolio (fails soft — the report must never break because of it)
    portfolio_html, pf_stats = "", None
    try:
        res = update_portfolio(prices, target)
        if res:
            pf, px_map, pf_stats = res
            portfolio_html = render_portfolio(pf, px_map, pf_stats)
    except Exception as e:
        print(f"  [WARN] portfolio update failed: {e}", file=sys.stderr)

    OUTPUT_DIR.mkdir(exist_ok=True)
    html = generate_html(company_articles, prices, indices, recs, earnings, target,
                         portfolio_html)

    date_slug = target.strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{date_slug}.html"
    out_path.write_text(html, encoding="utf-8")

    # Also write as "latest.html" for easy access
    latest_path = OUTPUT_DIR / "latest.html"
    latest_path.write_text(html, encoding="utf-8")

    print(f"Report saved: {out_path}")
    print(f"Latest:       {latest_path}")

    # Morning push to phone (Telegram) — no-op if bot token/chat not set.
    bullets = _summary_bullets(company_articles, prices, indices)
    body = "\n".join(f"• {b}" for b in bullets) if bullets else f"{total} 条新闻已更新"
    if pf_stats:
        body += (f"\n\n💼 AI模拟盘 {_money(pf_stats['total'])}"
                 f"（当日 {pf_stats['day_pct']:+.2f}% · 累计 {pf_stats['cum_pct']:+.2f}%）")
    push_telegram(
        title=f"📈 AI美股日报 {target.strftime('%m/%d')}",
        body=body,
        url="https://cjtree2002.github.io/ai-stock-daily/",
    )


if __name__ == "__main__":
    main()
