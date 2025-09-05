import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import crud
from .config_manager import ConfigManager
from .scraper_manager import ScraperManager
from .timezone import get_now

logger = logging.getLogger(__name__)

class RateLimitExceededError(Exception):
    def __init__(self, message, retry_after_seconds):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

class RateLimiter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager, scraper_manager: ScraperManager):
        self._session_factory = session_factory
        self._config_manager = config_manager
        self._scraper_manager = scraper_manager
        self._period_map = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
        self.logger = logging.getLogger(self.__class__.__name__)

    async def _get_provider_quota(self, provider_name: str) -> Optional[int]:
        try:
            scraper = self._scraper_manager.get_scraper(provider_name)
            quota = getattr(scraper, 'rate_limit_quota', None)
            if quota is not None and quota > 0:
                return quota
        except (ValueError, AttributeError):
            pass
        return None

    async def _get_global_limit(self) -> tuple[int, str]:
        global_enabled_str = await self._config_manager.get("globalRateLimitEnabled", "flse")
        if global_enabled_str.lower() != 'true':
            return 0, "hour"

        global_limit_str = await self._config_manager.get("globalRateLimitCount", "50")
        global_limit = int(global_limit_str) if global_limit_str.isdigit() else 50
        global_period = await self._config_manager.get("globalRateLimitPeriod", "hour")
        return global_limit, global_period

    async def check(self, provider_name: str):
        global_limit, period_str = await self._get_global_limit()
        if global_limit <= 0:
            return
        
        period_seconds = self._period_map.get(period_str, 3600)

        async with self._session_factory() as session:
            global_state = await crud.get_or_create_rate_limit_state(session, "__global__")
            provider_state = await crud.get_or_create_rate_limit_state(session, provider_name)

            now = get_now().replace(tzinfo=None)
            time_since_reset = now - global_state.lastResetTime
            
            if time_since_reset.total_seconds() >= period_seconds:
                self.logger.info(f"全局速率限制周期已过，正在重置所有计数器。")
                await crud.reset_all_rate_limit_states(session)
                await session.commit()
                global_state = await crud.get_or_create_rate_limit_state(session, "__global__")
                provider_state = await crud.get_or_create_rate_limit_state(session, provider_name)
                time_since_reset = now - global_state.lastResetTime # Re-calculate with the new reset time

            if global_state.requestCount >= global_limit:
                retry_after = period_seconds - time_since_reset.total_seconds()
                msg = f"已达到全局速率限制 ({global_state.requestCount}/{global_limit})。"
                self.logger.warning(msg)
                raise RateLimitExceededError(msg, retry_after_seconds=max(0, retry_after))

            provider_quota = await self._get_provider_quota(provider_name)
            if provider_quota is not None and provider_state.requestCount >= provider_quota:
                retry_after = period_seconds - time_since_reset.total_seconds()
                msg = f"已达到源 '{provider_name}' 的特定配额 ({provider_state.requestCount}/{provider_quota})。"
                self.logger.warning(msg)
                raise RateLimitExceededError(msg, retry_after_seconds=max(0, retry_after))

    async def increment(self, provider_name: str):
        global_limit, _ = await self._get_global_limit()
        if global_limit <= 0:
            return

        async with self._session_factory() as session:
            await crud.increment_rate_limit_count(session, "__global__")
            await crud.increment_rate_limit_count(session, provider_name)
            await session.commit()
            self.logger.debug(f"已为 '__global__' 和 '{provider_name}' 增加下载流控计数。")
