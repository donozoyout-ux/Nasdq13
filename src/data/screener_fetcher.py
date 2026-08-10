"""
Universe Fetcher - automatic small-cap discovery (free Nasdaq screener API)
- No API key required
- Fetches full US stock list (NASDAQ / NYSE / AMEX)
- Filters by market cap + min price
- Caches universe to JSON for reuse between refreshes
"""
import asyncio
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

import httpx

from src.utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.nasdaq.com/api/screener/stocks"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/screener",
}


class UniverseFetcher:
    """Discovers small-cap stocks automatically and caches the universe."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sc = config.get("smallcap", {}).get("universe", {})

        self.exchanges = self.sc.get("exchanges", ["NASDAQ", "NYSE", "AMEX"])
        self.min_market_cap = float(self.sc.get("min_market_cap", 2_000_000_000))
        self.max_market_cap = float(self.sc.get("max_market_cap", 10_000_000_000))
        self.min_price = float(self.sc.get("min_price", 3.0))
        self.max_candidates = int(self.sc.get("max_candidates", 250))
        self.refresh_hours = float(self.sc.get("refresh_hours", 4))
        self.cache_file = self.sc.get("cache_file", "data/midcap_universe.json")
        self.page_size = int(self.sc.get("page_size", 500))
        self.request_timeout = int(self.sc.get("request_timeout", 30))

        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.request_timeout, headers=_HEADERS)
        return self._client

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------

    def _cache_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), self.cache_file)

    def _load_cache(self, allow_stale: bool = False) -> Optional[List[Dict[str, Any]]]:
        path = self._cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if allow_stale:
                universe = data.get("universe", [])
                return universe or None
            saved = datetime.fromisoformat(data.get("saved_at", "2000-01-01T00:00:00"))
            age_hours = (datetime.utcnow() - saved).total_seconds() / 3600
            if age_hours < self.refresh_hours:
                return data.get("universe", [])
        except Exception as e:
            logger.warning(f"Universe cache load failed: {e}")
        return None

    def _save_cache(self, universe: List[Dict[str, Any]]):
        path = self._cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"saved_at": datetime.utcnow().isoformat(), "universe": universe}, f)
        except Exception as e:
            logger.warning(f"Universe cache save failed: {e}")

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _parse_row(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        symbol = (row.get("symbol") or "").strip()
        if not symbol:
            return None

        def _num(value) -> float:
            if not value:
                return 0.0
            cleaned = str(value).replace("$", "").replace(",", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return 0.0

        return {
            "symbol": symbol.upper(),
            "name": (row.get("name") or "").strip(),
            "price": _num(row.get("lastsale")),
            "change_pct": _num(row.get("pctchange")),
            "market_cap": _num(row.get("marketCap")),
        }

    async def _fetch_exchange(self, exchange: str) -> List[Dict[str, Any]]:
        client = await self._get_client()
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            try:
                params = {
                    "tableonly": "true",
                    "limit": self.page_size,
                    "offset": offset,
                    "download": "false",
                    "exchange": exchange,
                }
                resp = await client.get(BASE_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(f"Screener {exchange}: HTTP {resp.status_code}")
                    break
                data = resp.json()
                table = (data.get("data") or {}).get("table") or {}
                page_rows = table.get("rows") or []
                rows.extend(page_rows)
                total = int((data.get("data") or {}).get("totalrecords") or 0)
                offset += len(page_rows)
                if not page_rows or offset >= total or offset >= self.max_candidates * 4:
                    break
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.error(f"Screener fetch failed ({exchange}): {e}")
                break
        return rows

    async def fetch_universe(self, force: bool = False) -> List[Dict[str, Any]]:
        """Return a fresh-or-cached list of small-cap stocks (sorted by market cap desc).
        Resilient: if the live API is unreachable (e.g. Render's data-center IP blocked
        by nasdaq.com), fall back to the cached universe even if stale, so the scanner
        always has a list to work from."""
        fresh_cache = self._load_cache()          # only fresh (< refresh_hours)
        stale_cache = self._load_cache(allow_stale=True)

        if fresh_cache and not force:
            logger.info(f"Universe: fresh cached list used ({len(fresh_cache)} stocks)")
            return fresh_cache

        if force and fresh_cache:
            # force refresh requested but we already have a fresh list; still try live,
            # fall back to the fresh cache on any failure.
            pass

        all_rows = []
        fetch_ok = True
        for exchange in self.exchanges:
            logger.info(f"Universe: fetching {exchange}...")
            try:
                rows = await self._fetch_exchange(exchange)
            except Exception as e:
                logger.error(f"Universe: {exchange} fetch error: {e}")
                rows = []
            if not rows:
                fetch_ok = False
                logger.warning(f"Universe: {exchange} returned no rows (possibly blocked)")
            all_rows.extend(rows)

        parsed = [r for r in (self._parse_row(row) for row in all_rows) if r]

        filtered = [
            s for s in parsed
            if self.min_market_cap <= s["market_cap"] <= self.max_market_cap
            and s["price"] >= self.min_price
            and s["market_cap"] > 0
        ]

        # Sort by market cap desc -> pick the largest small-caps (more liquid, safer)
        filtered.sort(key=lambda s: s["market_cap"], reverse=True)
        universe = filtered[: self.max_candidates]

        if not universe:
            fallback = stale_cache or fresh_cache
            if fallback:
                logger.warning(f"Universe: live fetch failed, using cached list ({len(fallback)} stocks)")
                return fallback
            return []

        self._save_cache(universe)
        logger.info(f"Universe: {len(parsed)} parsed, {len(filtered)} in range, {len(universe)} kept")
        return universe

    def get_universe(self) -> List[Dict[str, Any]]:
        """Synchronous access to a cached universe (empty if no cache)."""
        return self._load_cache() or []


# Standalone test
async def test_universe_fetcher():
    import yaml

    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "config", "settings.yaml"), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    fetcher = UniverseFetcher(config)
    try:
        universe = await fetcher.fetch_universe(force=True)
        print(f"Universe size: {len(universe)}")
        for s in universe[:15]:
            print(f"  {s['symbol']:6s} {s['price']:>10.2f}  cap={s['market_cap']/1e6:>12,.0f}M  {s['name'][:40]}")
    finally:
        await fetcher.close()


if __name__ == "__main__":
    asyncio.run(test_universe_fetcher())