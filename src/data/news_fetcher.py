"""
News Fetcher Module - Free news APIs with keyword sentiment
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict
import httpx
import hashlib

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class NewsArticle:
    """News article with metadata"""
    title: str
    description: str
    url: str
    source: str
    published_at: datetime
    symbol: str = ""
    sentiment_score: float = 0.0
    keywords_matched: List[str] = field(default_factory=list)
    content_hash: str = ""
    
    def __post_init__(self):
        # Create hash for deduplication
        content = f"{self.title}{self.description}"
        self.content_hash = hashlib.md5(content.encode()).hexdigest()[:16]


class NewsFetcher:
    """Fetches news from multiple free APIs with keyword-based sentiment"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.news_config = config.get("news", {})
        self.enabled = self.news_config.get("enabled", True)
        self.lookback_hours = self.news_config.get("lookback_hours", 6)
        self.max_articles = self.news_config.get("max_articles_per_symbol", 20)
        self.keyword_boost_max = self.news_config.get("keyword_boost_max", 30)
        self.high_impact_multiplier = self.news_config.get("high_impact_multiplier", 3)
        
        # Keywords
        self.bearish_keywords = [k.lower() for k in self.news_config.get("keywords", {}).get("bearish", [])]
        self.bullish_keywords = [k.lower() for k in self.news_config.get("keywords", {}).get("bullish", [])]
        self.high_impact_keywords = [k.lower() for k in self.news_config.get("keywords", {}).get("high_impact", [])]
        
        # API Keys
        self.newsapi_key = config.get("NEWSAPI_KEY", "")
        self.alphavantage_key = config.get("ALPHA_VANTAGE_KEY", "")
        self.finnhub_key = config.get("FINNHUB_KEY", "")
        
        # Symbols to track
        self.symbols = config.get("symbols", ["NQ=F"])
        
        # Symbol to search term mapping
        self.symbol_search_terms = {
            "NQ=F": ["nasdaq", "nasdaq 100", "nq futures", "tech stocks"],
            "ES=F": ["s&p 500", "sp500", "es futures", "spx"],
            "YM=F": ["dow jones", "dow 30", "ym futures", "dji"],
            "RTY=F": ["russell 2000", "russell", "rty futures", "small cap"],
            "AAPL": ["apple", "aapl"],
            "TSLA": ["tesla", "tsla", "elon musk"],
            "NVDA": ["nvidia", "nvda", "ai chips"],
            "MSFT": ["microsoft", "msft"],
            "QQQ": ["qqq", "nasdaq etf"],
            "SPY": ["spy", "s&p etf"],
        }
        
        # Deduplication cache
        self._seen_hashes: set = set()
        self._cache: Dict[str, List[NewsArticle]] = {}
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = 300  # 5 minutes
        
# Session
        self._session: Optional[httpx.AsyncClient] = None

    async def _get_session(self) -> httpx.AsyncClient:
        """Get or create httpx session (matches the rest of the bot stack)"""
        if self._session is None or self._session.is_closed:
            self._session = httpx.AsyncClient(timeout=30.0)
        return self._session

    async def close(self):
        """Close the session"""
        if self._session and not self._session.is_closed:
            await self._session.aclose()
            self._session = None
    
    def _get_search_terms(self, symbol: str) -> List[str]:
        """Get search terms for a symbol"""
        return self.symbol_search_terms.get(symbol, [symbol.replace("=F", "").lower()])
    
    def _calculate_sentiment(self, title: str, description: str) -> tuple:
        """Calculate sentiment score based on keywords"""
        text = f"{title} {description}".lower()
        
        score = 0
        matched = []
        
        # Check bearish keywords
        for kw in self.bearish_keywords:
            if kw in text:
                score -= 1
                matched.append(f"BEARISH:{kw}")
        
        # Check bullish keywords
        for kw in self.bullish_keywords:
            if kw in text:
                score += 1
                matched.append(f"BULLISH:{kw}")
        
        # Check high impact keywords (multiplier)
        high_impact_count = 0
        for kw in self.high_impact_keywords:
            if kw in text:
                high_impact_count += 1
                matched.append(f"HIGH_IMPACT:{kw}")
        
        # Apply high impact multiplier
        if high_impact_count > 0:
            score *= self.high_impact_multiplier
        
        # Cap the score
        score = max(-self.keyword_boost_max, min(self.keyword_boost_max, score))
        
        return score, matched
    
    async def _fetch_newsapi(self, query: str) -> List[NewsArticle]:
        """Fetch from NewsAPI.org"""
        if not self.newsapi_key:
            return []
        
        articles = []
        try:
            session = await self._get_session()
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "apiKey": self.newsapi_key,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": self.max_articles,
                "from": (datetime.utcnow() - timedelta(hours=self.lookback_hours)).isoformat(),
            }

            resp = await session.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("articles", []):
                    if item.get("title") and item.get("description"):
                        articles.append(NewsArticle(
                            title=item["title"],
                            description=item["description"],
                            url=item.get("url", ""),
                            source=item.get("source", {}).get("name", "NewsAPI"),
                            published_at=datetime.fromisoformat(
                                item["publishedAt"].replace("Z", "+00:00")
                            ) if item.get("publishedAt") else datetime.utcnow(),
                        ))
            elif resp.status_code == 429:
                logger.warning("NewsAPI rate limited")
            else:
                logger.warning(f"NewsAPI error: {resp.status_code}")
        except Exception as e:
            logger.error(f"NewsAPI fetch error: {e}")
        
        return articles
    
    async def _fetch_alphavantage(self, symbol: str) -> List[NewsArticle]:
        """Fetch from Alpha Vantage News & Sentiment"""
        if not self.alphavantage_key:
            return []
        
        articles = []
        try:
            session = await self._get_session()
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "NEWS_SENTIMENT",
                "tickers": symbol.replace("=F", ""),
                "apikey": self.alphavantage_key,
                "limit": self.max_articles,
            }
            
            resp = await session.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("feed", []):
                    articles.append(NewsArticle(
                        title=item.get("title", ""),
                        description=item.get("summary", ""),
                        url=item.get("url", ""),
                        source=item.get("source", "AlphaVantage"),
                        published_at=datetime.fromisoformat(
                            item["time_published"].replace("Z", "+00:00")
                        ) if item.get("time_published") else datetime.utcnow(),
                    ))
            elif resp.status_code == 429:
                logger.warning("Alpha Vantage rate limited")
        except Exception as e:
            logger.error(f"Alpha Vantage fetch error: {e}")
        
        return articles
    
    async def _fetch_finnhub(self, symbol: str) -> List[NewsArticle]:
        """Fetch from Finnhub"""
        if not self.finnhub_key:
            return []
        
        articles = []
        try:
            session = await self._get_session()
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": symbol.replace("=F", ""),
                "token": self.finnhub_key,
                "from": (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d"),
                "to": datetime.utcnow().strftime("%Y-%m-%d"),
            }
            
            resp = await session.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for item in data[:self.max_articles]:
                    articles.append(NewsArticle(
                        title=item.get("headline", ""),
                        description=item.get("summary", ""),
                        url=item.get("url", ""),
                        source=item.get("source", "Finnhub"),
                        published_at=datetime.fromtimestamp(item.get("datetime", 0)),
                    ))
            elif resp.status_code == 429:
                logger.warning("Finnhub rate limited")
            else:
                logger.warning(f"Finnhub error: {resp.status_code}")
        except Exception as e:
            logger.error(f"Finnhub fetch error: {e}")
        
        return articles
    
    async def fetch_for_symbol(self, symbol: str) -> List[NewsArticle]:
        """Fetch news for a specific symbol from all sources"""
        if not self.enabled:
            return []
        
        search_terms = self._get_search_terms(symbol)
        query = " OR ".join(search_terms)
        
        # Fetch from all sources concurrently
        tasks = [
            self._fetch_newsapi(query),
            self._fetch_alphavantage(symbol),
            self._fetch_finnhub(symbol),
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_articles = []
        for result in results:
            if isinstance(result, list):
                all_articles.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"News fetch error for {symbol}: {result}")
        
        # Deduplicate by content hash
        unique_articles = []
        for article in all_articles:
            if article.content_hash not in self._seen_hashes:
                self._seen_hashes.add(article.content_hash)
                article.symbol = symbol
                score, matched = self._calculate_sentiment(article.title, article.description)
                article.sentiment_score = score
                article.keywords_matched = matched
                unique_articles.append(article)
        
        # Sort by recency and sentiment magnitude
        unique_articles.sort(
            key=lambda x: (x.published_at, abs(x.sentiment_score)),
            reverse=True
        )
        
        return unique_articles[:self.max_articles]
    
    async def fetch_all(self) -> Dict[str, List[NewsArticle]]:
        """Fetch news for all symbols"""
        # Check cache
        if (self._cache_time and 
            (datetime.utcnow() - self._cache_time).seconds < self._cache_ttl):
            return self._cache
        
        tasks = [self.fetch_for_symbol(symbol) for symbol in self.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        organized = {}
        for symbol, result in zip(self.symbols, results):
            if isinstance(result, list):
                organized[symbol] = result
            else:
                organized[symbol] = []
                logger.error(f"News fetch failed for {symbol}: {result}")
        
        self._cache = organized
        self._cache_time = datetime.utcnow()
        
        # Log summary
        total = sum(len(v) for v in organized.values())
        logger.info(f"Fetched {total} news articles for {len(self.symbols)} symbols")
        
        return organized

    async def fetch_for_tickers(self, tickers: List[str]) -> Dict[str, List[NewsArticle]]:
        """Fetch news for an arbitrary list of tickers (used by the mid-cap scanner to
        enrich candidates independently of the main symbols list). The result is merged
        into the cache so get_aggregate_sentiment() works for each ticker."""
        if not self.enabled or not tickers:
            return {}

        tasks = [self.fetch_for_symbol(t) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        organized = {}
        for ticker, result in zip(tickers, results):
            if isinstance(result, list):
                organized[ticker] = result
            else:
                organized[ticker] = []
                logger.error(f"News fetch failed for {ticker}: {result}")

        if not self._cache:
            self._cache = {}
        for ticker, articles in organized.items():
            if articles:
                self._cache[ticker] = articles

        total = sum(len(v) for v in organized.values())
        logger.info(f"Fetched {total} news articles for {len(tickers)} mid-cap tickers")
        return organized
    
    def get_aggregate_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Get aggregate sentiment for a symbol"""
        articles = self._cache.get(symbol, [])
        
        if not articles:
            return {"score": 0, "article_count": 0, "top_keywords": []}
        
        total_score = sum(a.sentiment_score for a in articles)
        avg_score = total_score / len(articles)
        
        # Collect top keywords
        keyword_counts = defaultdict(int)
        for a in articles:
            for kw in a.keywords_matched:
                keyword_counts[kw] += 1
        
        top_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        return {
            "score": round(avg_score, 2),
            "total_score": round(total_score, 2),
            "article_count": len(articles),
            "top_keywords": [{"keyword": k, "count": c} for k, c in top_keywords],
            "latest_article": articles[0].title if articles else None,
            "latest_time": articles[0].published_at.isoformat() if articles else None,
        }


# Standalone test
async def test_news_fetcher():
    """Quick test function"""
    config = {
        "symbols": ["NQ=F", "ES=F"],
        "news": {
            "enabled": True,
            "lookback_hours": 6,
            "max_articles_per_symbol": 10,
            "keyword_boost_max": 30,
            "high_impact_multiplier": 3,
            "keywords": {
                "bearish": ["trump tariff", "fed hawkish", "rate hike", "recession"],
                "bullish": ["fed dovish", "rate cut", "earnings beat"],
                "high_impact": ["trump", "powell", "fomc", "cpi"],
            }
        },
        "NEWSAPI_KEY": "",
        "ALPHA_VANTAGE_KEY": "",
        "FINNHUB_KEY": "",
    }
    
    fetcher = NewsFetcher(config)
    try:
        data = await fetcher.fetch_all()
        
        for symbol, articles in data.items():
            print(f"\n{symbol}: {len(articles)} articles")
            for a in articles[:3]:
                print(f"  [{a.sentiment_score:+.1f}] {a.title[:80]}...")
                if a.keywords_matched:
                    print(f"    Keywords: {a.keywords_matched}")
        
        # Test aggregate
        for symbol in config["symbols"]:
            agg = fetcher.get_aggregate_sentiment(symbol)
            print(f"\n{symbol} Aggregate: {agg}")
    finally:
        await fetcher.close()


if __name__ == "__main__":
    asyncio.run(test_news_fetcher())