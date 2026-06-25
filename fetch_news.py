#!/usr/bin/env python3
"""
AI Stock Daily News Fetcher
Fetches previous day's news for AI sector companies and generates an HTML report.
"""
import os
import re
import sys
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
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
OUTPUT_DIR = Path(__file__).parent / "reports"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
MAX_ARTICLES_PER_COMPANY = 5

# NewsAPI free tier: 100 requests/day, 1 req/sec
REQUEST_DELAY = 1.2  # seconds between requests


# Set by fetch_all_news so main() can refuse to overwrite a good page on rate-limit.
RATE_LIMITED = False


def fetch_articles_bulk(keywords_batch: list[str], from_date: str, to_date: str) -> list[dict]:
    """Fetch articles whose TITLE contains one of the keywords (one API call).

    searchIn=title is the key relevance filter: it drops articles that only
    mention the company in passing (e.g. gaming/deal posts that name 'AMD' in
    the body but not the headline), keeping articles that are actually about it.
    """
    global RATE_LIMITED
    query = " OR ".join(f'"{kw}"' for kw in keywords_batch[:10])  # API limit
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "searchIn": "title",
        "from": from_date,
        "to": to_date,
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": 100,
        "apiKey": NEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("status") == "error":
            if data.get("code") == "rateLimited":
                RATE_LIMITED = True
            print(f"  [WARN] API {data.get('code')}: {data.get('message')}", file=sys.stderr)
            return []
        return data.get("articles", [])
    except Exception as e:
        print(f"  [WARN] API error for batch: {e}", file=sys.stderr)
        return []


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
    """Fetch closing price and daily change for all companies with real tickers."""
    tickers = list({c["ticker"] for c in AI_COMPANIES if c["ticker"] != "N/A"})
    print(f"Fetching stock prices for {len(tickers)} tickers...")

    # Fetch 5 days to ensure we get data even around weekends/holidays
    end = target_date + timedelta(days=1)
    start = target_date - timedelta(days=5)

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
        for ticker in tickers:
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            if len(series) < 1:
                continue
            # Most recent close on or before target_date
            target_ts = target_date.strftime("%Y-%m-%d")
            sub = series[series.index <= target_ts]
            if sub.empty:
                continue
            price = float(sub.iloc[-1])
            prev = float(sub.iloc[-2]) if len(sub) >= 2 else None
            change = ((price - prev) / prev * 100) if prev else None
            prices[ticker] = {
                "price": price,
                "change": change,
                "date": sub.index[-1].strftime("%m/%d"),
            }
    except Exception as e:
        print(f"  [WARN] Stock price fetch error: {e}", file=sys.stderr)

    print(f"  Got prices for {len(prices)} tickers")
    return prices


def fetch_all_news(target_date: datetime) -> dict:
    """Fetch all news and assign to companies."""
    if not NEWS_API_KEY:
        print("ERROR: NEWS_API_KEY not set. Export it before running.", file=sys.stderr)
        sys.exit(1)

    from_dt = target_date.replace(hour=0, minute=0, second=0)
    to_dt = target_date.replace(hour=23, minute=59, second=59)
    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S")
    to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Fetching news for {target_date.strftime('%Y-%m-%d')}...")

    all_articles: list[dict] = []
    batches = build_query_batches(AI_COMPANIES, batch_size=5)

    for i, batch in enumerate(batches):
        print(f"  Batch {i+1}/{len(batches)}: {', '.join(batch[:3])}...")
        articles = fetch_articles_bulk(batch, from_str, to_str)
        all_articles.extend(articles)
        if i < len(batches) - 1:
            time.sleep(REQUEST_DELAY)

    # Deduplicate by URL
    seen_urls: set[str] = set()
    unique_articles = []
    for a in all_articles:
        url = a.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_articles.append(a)

    print(f"  Total unique articles: {len(unique_articles)}")
    company_articles = assign_articles_to_companies(unique_articles, AI_COMPANIES)
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


def render_stock_price(ticker: str, prices: dict) -> str:
    if ticker == "N/A" or ticker not in prices:
        return ""
    p = prices[ticker]
    price_str = f"${p['price']:,.2f}"
    change = p.get("change")
    if change is None:
        change_html = '<span class="price-change flat">—</span>'
    elif change >= 0:
        change_html = f'<span class="price-change up">▲ {change:+.2f}%</span>'
    else:
        change_html = f'<span class="price-change down">▼ {change:.2f}%</span>'
    return f'''
        <div class="stock-price">
          <span class="price-current">{price_str}</span>
          {change_html}
          <span class="price-meta">{p["date"]} 收盘</span>
        </div>'''


def generate_html(company_articles: dict, prices: dict, target_date: datetime) -> str:
    date_str = target_date.strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday_str = weekdays[target_date.weekday()]
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M") + " 北京时间"

    # Build company cards (only companies with news)
    cards_html = ""
    companies_with_news = 0
    total_articles = 0

    for company in AI_COMPANIES:
        ticker = company["ticker"]
        articles = company_articles.get(company["name"], [])
        if not articles:
            continue

        companies_with_news += 1
        total_articles += len(articles)

        ticker_badge = (
            f'<span class="ticker">{ticker}</span>'
            if ticker != "N/A"
            else '<span class="ticker private">私有</span>'
        )

        stock_html = render_stock_price(ticker, prices)
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

        cards_html += f"""
      <div class="company-card">
        <div class="card-header">
          <span class="company-name">{company["name"]}</span>
          {ticker_badge}
          <span class="article-count">{len(articles)} 条</span>
        </div>{stock_html}
        <div class="articles">{articles_html}
        </div>
      </div>"""

    # No-news companies list
    no_news = [c["name"] for c in AI_COMPANIES if not company_articles.get(c["name"])]
    no_news_html = ", ".join(no_news) if no_news else "（全部有新闻）"

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{DATE_STR}}", date_str)
        .replace("{{WEEKDAY}}", weekday_str)
        .replace("{{GENERATED_AT}}", now_str)
        .replace("{{COMPANIES_COUNT}}", str(companies_with_news))
        .replace("{{ARTICLES_COUNT}}", str(total_articles))
        .replace("{{CARDS_HTML}}", cards_html)
        .replace("{{NO_NEWS_LIST}}", no_news_html)
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
    pretranslate_displayed(company_articles)

    OUTPUT_DIR.mkdir(exist_ok=True)
    html = generate_html(company_articles, prices, target)

    date_slug = target.strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{date_slug}.html"
    out_path.write_text(html, encoding="utf-8")

    # Also write as "latest.html" for easy access
    latest_path = OUTPUT_DIR / "latest.html"
    latest_path.write_text(html, encoding="utf-8")

    print(f"Report saved: {out_path}")
    print(f"Latest:       {latest_path}")


if __name__ == "__main__":
    main()
