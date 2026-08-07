"""
Price Fetcher Module - Multi-timeframe data from Yahoo Finance
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PriceData:
    """Container for price data with metadata"""
    symbol: str
    timeframe: str
    data: pd.DataFrame
    fetched_at: datetime
    source: str = "yfinance"


class PriceFetcher:
    """Fetches multi-timeframe price data from Yahoo Finance"""
    
    # Yahoo Finance interval mapping
    INTERVAL_MAP = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "4h": "4h",
        "1d": "1d",
        "1wk": "1wk",
        "1mo": "1mo",
    }
    
    # Period limits for each interval (yfinance restrictions)
    PERIOD_LIMITS = {
        "1m": "7d",
        "5m": "60d",
        "15m": "60d",
        "30m": "60d",
        "1h": "730d",
        "4h": "730d",
        "1d": "max",
        "1wk": "max",
        "1mo": "max",
    }
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.symbols = config.get("symbols", ["NQ=F"])
        self.timeframes = config.get("timeframes", ["1m", "5m", "15m", "1h"])
        self.max_concurrent = config.get("scanner", {}).get("max_concurrent_requests", 5)
        self.timeout = config.get("scanner", {}).get("request_timeout", 30)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._cache: Dict[str, PriceData] = {}
        self._cache_ttl = 30  # seconds
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def _fetch_single(self, symbol: str, timeframe: str) -> Optional[PriceData]:
        """Fetch data for a single symbol/timeframe combination"""
        async with self._semaphore:
            try:
                interval = self.INTERVAL_MAP.get(timeframe, "1m")
                period = self.PERIOD_LIMITS.get(timeframe, "7d")
                
                # Run yfinance in thread pool (it's synchronous)
                loop = asyncio.get_event_loop()
                ticker = yf.Ticker(symbol)
                
                # Use history() for more control
                df = await loop.run_in_executor(
                    None,
                    lambda: ticker.history(
                        period=period,
                        interval=interval,
                        prepost=True,
                        actions=False
                    )
                )
                
                if df.empty:
                    logger.warning(f"No data for {symbol} {timeframe}")
                    return None
                
                # Standardize column names
                df.columns = [col.lower().replace(" ", "_") for col in df.columns]
                
                # Ensure required columns exist
                required = ["open", "high", "low", "close", "volume"]
                for col in required:
                    if col not in df.columns:
                        logger.error(f"Missing column {col} for {symbol} {timeframe}")
                        return None
                
                # Drop any rows with NaN in critical columns
                df = df.dropna(subset=required)
                
                if len(df) < 50:  # Minimum bars for indicators
                    logger.warning(f"Insufficient data for {symbol} {timeframe}: {len(df)} bars")
                    return None
                
                return PriceData(
                    symbol=symbol,
                    timeframe=timeframe,
                    data=df,
                    fetched_at=datetime.utcnow()
                )
                
            except Exception as e:
                logger.error(f"Error fetching {symbol} {timeframe}: {e}")
                raise
    
    async def fetch_all(self) -> Dict[str, Dict[str, PriceData]]:
        """Fetch all symbols and timeframes concurrently"""
        tasks = []
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                # Check cache first
                cache_key = f"{symbol}_{timeframe}"
                if cache_key in self._cache:
                    cached = self._cache[cache_key]
                    if (datetime.utcnow() - cached.fetched_at).seconds < self._cache_ttl:
                        continue
                
                tasks.append(self._fetch_single(symbol, timeframe))
        
        if not tasks:
            return self._get_cached_data()
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Organize results
        organized: Dict[str, Dict[str, PriceData]] = {}
        idx = 0
        for symbol in self.symbols:
            organized[symbol] = {}
            for timeframe in self.timeframes:
                cache_key = f"{symbol}_{timeframe}"
                result = results[idx]
                idx += 1
                
                if isinstance(result, PriceData):
                    organized[symbol][timeframe] = result
                    self._cache[cache_key] = result
                elif isinstance(result, Exception):
                    logger.error(f"Failed to fetch {symbol} {timeframe}: {result}")
                    # Use cached if available
                    if cache_key in self._cache:
                        organized[symbol][timeframe] = self._cache[cache_key]
        
        return organized
    
    def _get_cached_data(self) -> Dict[str, Dict[str, PriceData]]:
        """Return cached data organized by symbol/timeframe"""
        organized: Dict[str, Dict[str, PriceData]] = {}
        for symbol in self.symbols:
            organized[symbol] = {}
            for timeframe in self.timeframes:
                cache_key = f"{symbol}_{timeframe}"
                if cache_key in self._cache:
                    organized[symbol][timeframe] = self._cache[cache_key]
        return organized
    
    async def fetch_latest_price(self, symbol: str) -> Optional[float]:
        """Get the most recent price for a symbol"""
        try:
            loop = asyncio.get_event_loop()
            ticker = yf.Ticker(symbol)
            info = await loop.run_in_executor(None, lambda: ticker.fast_info)
            return float(info.last_price) if info.last_price else None
        except Exception as e:
            logger.error(f"Error fetching latest price for {symbol}: {e}")
            return None
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            "cached_items": len(self._cache),
            "symbols": list(set(k.split("_")[0] for k in self._cache.keys())),
            "timeframes": list(set(k.split("_")[1] for k in self._cache.keys())),
        }


# Standalone test
async def test_price_fetcher():
    """Quick test function"""
    config = {
        "symbols": ["NQ=F", "ES=F"],
        "timeframes": ["5m", "15m", "1h"],
        "scanner": {"max_concurrent_requests": 3, "request_timeout": 30}
    }
    
    fetcher = PriceFetcher(config)
    data = await fetcher.fetch_all()
    
    for symbol, tfs in data.items():
        for tf, pd in tfs.items():
            print(f"{symbol} {tf}: {len(pd.data)} bars, last close: {pd.data['close'].iloc[-1]:.2f}")
    
    return data


if __name__ == "__main__":
    asyncio.run(test_price_fetcher())