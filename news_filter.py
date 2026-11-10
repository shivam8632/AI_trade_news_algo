# news_filter.py
import time

import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

analyzer = SentimentIntensityAnalyzer()
NEWS_FEED_URL = "https://finance.yahoo.com"  # Fallback source; parsing errors are handled safely.


def scan_live_market_news():
    print(f"\n⚡ Scanning Live Market News Wire [{time.strftime('%Y-%m-%d %H:%M:%S')}]...")

    try:
        feed = feedparser.parse(NEWS_FEED_URL)
        latest_stories = feed.entries[:5]
    except Exception as exc:
        print(f"⚠️ News feed unavailable: {exc}. Continuing in neutral risk mode.")
        return "ALLOW_TRADING"

    if not latest_stories:
        print("⚠️ News feed returned no headlines. Continuing in neutral risk mode.")
        return "ALLOW_TRADING"

    total_sentiment_score = 0

    for i, story in enumerate(latest_stories, 1):
        headline = getattr(story, "title", "") or ""
        if not headline:
            continue

        score = analyzer.polarity_scores(headline)["compound"]
        total_sentiment_score += score

        if score <= -0.15:
            status = "🚨 DANGER (Bearish/Panic)"
        elif score >= 0.15:
            status = "🟢 BULLISH (Positive)"
        else:
            status = "⚪ NEUTRAL (Safe to Trade)"

        print(f"  {i}. Headline: {headline}")
        print(f"     Score: {score:.2f} | Filter Status: {status}\n")

    avg_score = total_sentiment_score / len(latest_stories)
    print(f"📊 Overall Market Safety Score: {avg_score:.2f}")

    if avg_score < -0.20:
        print("🛑 WARNING: Global sentiment is heavily negative. Trading bot should pause entries!")
        return "PAUSE_TRADING"

    print("✅ Market environment is stable. Technical trading allowed.")
    return "ALLOW_TRADING"
