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
# Finnhub: finance-specific company news, no 24h delay (free tier: 60 calls/min).
# NOTE: the GitHub Actions secret is stored under the name NEWS_API_KEY (reused
# from the old NewsAPI setup) so we can swap the source without touching the
# workflow file. FINNHUB_API_KEY takes precedence when set (local runs).
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY") or os.environ.get("NEWS_API_KEY", "")
OUTPUT_DIR = Path(__file__).parent / "reports"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
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
