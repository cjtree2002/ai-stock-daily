#!/usr/bin/env python3
"""
AI Stock Daily News Fetcher
Fetches previous day's news for AI sector companies and generates an HTML report.
"""
import os
import sys
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

from companies import AI_COMPANIES

# ── Config ──────────────────────────────────────────────────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
OUTPUT_DIR = Path(__file__).parent / "reports"
TEMPLATE_PATH = Path(__file__).parent / "template.html"
MAX_ARTICLES_PER_COMPANY = 5

# NewsAPI free tier: 100 requests/day, 1 req/sec
REQUEST_DELAY = 1.2  # seconds between requests


def fetch_articles_bulk(keywords_batch: list[str], from_date: str, to_date: str) -> list[dict]:
    """Fetch articles for a batch of keywords in one API call."""
    query = " OR ".join(f'"{kw}"' for kw in keywords_batch[:10])  # API limit
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": from_date,
        "to": to_date,
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": 100,
        "apiKey": NEWS_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("articles", [])
    except Exception as e:
        print(f"  [WARN] API error for batch: {e}", file=sys.stderr)
        return []


def assign_articles_to_companies(articles: list[dict], companies: list[dict]) -> dict:
    """Map each article to matching companies by keyword."""
    company_articles: dict[str, list] = {c["ticker"]: [] for c in companies}

    for article in articles:
        title = (article.get("title") or "").lower()
        description = (article.get("description") or "").lower()
        content = title + " " + description

        for company in companies:
            if len(company_articles[company["ticker"]]) >= MAX_ARTICLES_PER_COMPANY:
                continue
            for kw in company["keywords"]:
                if kw.lower() in content:
                    # Avoid duplicates
                    urls = [a["url"] for a in company_articles[company["ticker"]]]
                    if article.get("url") not in urls:
                        company_articles[company["ticker"]].append(article)
                    break

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
    batches = build_query_batches(AI_COMPANIES, batch_size=8)

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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Build company cards (only companies with news)
    cards_html = ""
    companies_with_news = 0
    total_articles = 0

    for company in AI_COMPANIES:
        ticker = company["ticker"]
        articles = company_articles.get(ticker, [])
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
            title = art.get("title") or "无标题"
            url = art.get("url") or "#"
            source = (art.get("source") or {}).get("name") or ""
            published = format_time(art.get("publishedAt") or "")
            description = art.get("description") or ""
            if len(description) > 120:
                description = description[:120] + "…"

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
    no_news = [c["name"] for c in AI_COMPANIES if not company_articles.get(c["ticker"])]
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
    # Default: yesterday (US Eastern previous trading day)
    target = datetime.now() - timedelta(days=1)
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d")

    company_articles = fetch_all_news(target)
    prices = fetch_stock_prices(target)

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
