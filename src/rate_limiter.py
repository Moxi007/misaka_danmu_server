import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import crud
from .config_manager import ConfigManager
from .scraper_manager import ScraperManager

logger = logging.getLogger(__name__)

class RateLimitExceededError(Exception):
    """当达到速率限制时引发的自定义异常。"""
    def __init__(self, message, retry_after_seconds):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

class RateLimiter:
    """管理全局和各源的下载速率限制，基于成功下载的分集数量。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager, scraper_manager: ScraperManager):
        self._session_factory = session_factory
        self._config_manager = config_manager
        self._scraper_manager = scraper_manager
        self._period_map = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
        self.logger = logging.getLogger(self.__class__.__name__)

    async def _get_provider_quota(self, provider_name: str) -> Optional[int]:
        """
        获取提供商的特定配额。
        """
        try:
            scraper = self._scraper_manager.get_scraper(provider_name)
            # 配额在 scraper 类中作为类属性定义
            quota = getattr(scraper, 'rate_limit_quota', None)
            if quota is not None and quota > 0:
                return quota
        except (ValueError, AttributeError):
            # 找不到搜索源或属性，则没有特定配额
            pass
        return None

    async def _get_global_limit(self) -> tuple[int, str]:
        """获取全局限制设置。"""
        global_enabled_str = await self._config_manager.get("globalRateLimitEnabled", "false")
        if global_enabled_str.lower() != 'true':
            return 0, "hour"  # 0 表示无限制

        global_limit_str = await self._config_manager.get("globalRateLimitCount", "50")
        global_limit = int(global_limit_str) if global_limit_str.isdigit() else 50
        global_period = await self._config_manager.get("globalRateLimitPeriod", "hour")
        return global_limit, global_period

    async def check(self, provider_name: str):
        """
        检查是否允许为指定源下载一个新分集。
        如果超出限制，则引发 RateLimitExceededError。
        - 首先检查全局限制。
        - 然后检查该提供商的特定配额。
        """
        global_limit, period_str = await self._get_global_limit()
        if global_limit <= 0:
            return  # 全局限制已禁用或为0，无需检查
        
        period_seconds = self._period_map.get(period_str, 3600)

        async with self._session_factory() as session:
            # 使用 get_or_create 确保状态记录存在
            global_state = await crud.get_or_create_rate_limit_state(session, "__global__")
            provider_state = await crud.get_or_create_rate_limit_state(session, provider_name)

            now = datetime.now(timezone.utc)
            time_since_reset = now - global_state.lastResetTime
            
            # 检查是否需要重置所有计数器
            if time_since_reset.total_seconds() >= period_seconds:
                self.logger.info(f"全局速率限制周期已过，正在重置所有计数器。")
                await crud.reset_all_rate_limit_states(session)
                await session.commit()
                # 重新获取状态以反映重置
                global_state = await crud.get_or_create_rate_limit_state(session, "__global__")
                provider_state = await crud.get_or_create_rate_limit_state(session, provider_name)
                time_since_reset = now - global_state.lastResetTime

            # 1. 检查全局限制
            if global_state.requestCount >= global_limit:
                retry_after = period_seconds - time_since_reset.total_seconds()
                msg = f"已达到全局速率限制 ({global_state.requestCount}/{global_limit})。"
                self.logger.warning(msg)
                raise RateLimitExceededError(msg, retry_after_seconds=retry_after)

            # 2. 检查特定源配额
            provider_quota = await self._get_provider_quota(provider_name)
            if provider_quota is not None and provider_state.requestCount >= provider_quota:
                retry_after = period_seconds - time_since_reset.total_seconds()
                msg = f"已达到源 '{provider_name}' 的特定配额 ({provider_state.requestCount}/{provider_quota})。"
                self.logger.warning(msg)
                raise RateLimitExceededError(msg, retry_after_seconds=retry_after)

    async def increment(self, provider_name: str):
        """
        为全局和特定提供商增加请求计数。
        此方法应在成功下载一个分集的弹幕后调用。
        """
        global_limit, _ = await self._get_global_limit()
        if global_limit <= 0:
            return  # 如果全局限制被禁用，则不增加计数

        async with self._session_factory() as session:
            # 增加全局计数
            await crud.increment_rate_limit_count(session, "__global__")
            # 增加特定源计数
            await crud.increment_rate_limit_count(session, provider_name)
            await session.commit()
            self.logger.debug(f"已为 '__global__' 和 '{provider_name}' 增加下载流控计数。")
