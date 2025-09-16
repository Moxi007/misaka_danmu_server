import asyncio
import logging
import json
from pathlib import Path
import hashlib
import base64
import re
from datetime import datetime, timezone

from gmssl import sm2, sm3, func

from typing import Any, Dict, List, Optional, Union

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import crud
from .scraper_manager import ScraperManager
from .timezone import get_now

logger = logging.getLogger(__name__)

class RateLimitExceededError(Exception):
    def __init__(self, message, retry_after_seconds):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

class ConfigVerificationError(Exception):
    """当配置文件验证失败时引发。"""
    pass

XOR_KEY = b"T3Nn@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S@pT^K!v8&s$U@w#Z&e3S"

original_sm3_z = sm2.CryptSM2._sm3_z
def fixed_sm3_z(self, uid: Union[str, bytes]): 
    if isinstance(uid, str):
        uid_bytes = uid.encode('utf-8')
    else:
        uid_bytes = uid
    return original_sm3_z(self, uid_bytes)

sm2.CryptSM2._sm3_z = fixed_sm3_z 

original_verify = sm2.CryptSM2.verify
def fixed_verify(self, sign: str, data: bytes, uid: Union[str, bytes]) -> bool:
    """
    一个包装函数，它在内部计算 Z 值和消息的哈希，然后调用原始的 verify 方法。
    这使得本地验证逻辑与服务器端完全一致。
    """
    z_hex = self._sm3_z(uid=uid)
    message_bytes = z_hex.encode('utf-8') + data
    hash_to_verify = sm3.sm3_hash(func.bytes_to_list(message_bytes))
    return original_verify(self, sign, bytes.fromhex(hash_to_verify))

sm2.CryptSM2.verify = fixed_verify 

def _extract_hex_from_pem(pem_content: str) -> str:
    """
    从PEM格式的公钥字符串中稳健地提取十六进制公钥。
    此函数能够正确解析ASN.1 DER编码结构。
    """
    try:
        pem_lines = pem_content.strip().split('\n')
        base64_str = "".join(line for line in pem_lines if not line.startswith("-----"))

        der_data = base64.b64decode(base64_str)

        if der_data[0] != 0x30:
            raise ValueError("PEM内容不是一个有效的DER SEQUENCE。")

        bit_string_tag_index = der_data.find(b'\x03')
        if bit_string_tag_index == -1:
            raise ValueError("在DER编码中未找到BIT STRING。")
        public_key_bytes = der_data[-65:] 
        return public_key_bytes.hex()
    except Exception as e:
        logger.error(f"解析PEM公钥时发生错误: {e}", exc_info=True)
        raise ValueError("无法解析PEM公钥。") from e

class RateLimiter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], scraper_manager: ScraperManager):
        self._session_factory = session_factory
        self._scraper_manager = scraper_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        self._verification_failed: bool = False

        # 设置默认值为禁用流控
        self.enabled: bool = False
        self.global_limit: int = 0
        self.global_period_seconds: int = 3600 
        try:
            config_dir = Path(__file__).parent / "rate_limit"
            config_path = config_dir / "rate_limit.bin"
            sig_path = config_dir / "rate_limit.bin.sig"
            pub_key_path = config_dir / "public_key.pem"
            uid_path = config_dir / "rate_limit.uid"

            if not all([config_path.exists(), sig_path.exists(), pub_key_path.exists(), uid_path.exists()]):
                self.logger.warning("流控配置文件不完整或缺失，将使用默认值（禁用流控）。")
                return  # 直接返回，使用默认值

            try:
                uid_from_file = uid_path.read_text('utf-8').strip()
                if not uid_from_file:
                    self.logger.warning("UID 文件为空，将使用默认值（禁用流控）。")
                    return
                signing_uid = uid_from_file
                self.logger.info(f"已从 rate_limit.uid 文件加载签名UID。")
            except Exception as e:
                self.logger.warning(f"读取 rate_limit.uid 文件失败，将使用默认值（禁用流控）。错误: {e}")
                return

            obfuscated_bytes = config_path.read_bytes()
            signature = sig_path.read_bytes().decode('utf-8').strip()
            public_key_pem = pub_key_path.read_text('utf-8')
            public_key_hex = _extract_hex_from_pem(public_key_pem)
            
            # 创建 SM2 实例
            sm2_crypt = sm2.CryptSM2(public_key=public_key_hex, private_key='')
            
            try:
                # 在验证失败时，使用默认值而不是阻止所有请求
                if not sm2_crypt.verify(signature, bytes(obfuscated_bytes), uid=signing_uid):
                    self.logger.warning("速率限制配置文件签名验证失败，将使用代码默认值（禁用流控）。")
                    return  # 提前返回，跳过后续处理
                
                self.logger.info("速率限制配置文件签名验证成功。")
            except (ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"签名验证失败：无效的密钥或签名格式，将使用默认值（禁用流控）。错误: {e}")
                return
            except Exception as e:
                self.logger.warning(f"签名验证过程中发生错误，将使用默认值（禁用流控）。错误: {e}")
                return

            try:
                json_bytes = bytearray()
                for i, byte in enumerate(obfuscated_bytes):
                    json_bytes.append(byte ^ XOR_KEY[i % len(XOR_KEY)])

                config_data = json.loads(json_bytes.decode('utf-8'))

                expected_hash = config_data.get("rate_limiter_hash")
                if not expected_hash:
                    self.logger.warning("配置文件中缺少 'rate_limiter_hash'，将使用默认值（禁用流控）。")
                    return

                rate_limiter_path = Path(__file__) 
                rate_limiter_content_bytes = rate_limiter_path.read_bytes()
                actual_hash = hashlib.sha256(rate_limiter_content_bytes.replace(b'\r\n', b'\n')).hexdigest()

                if actual_hash != expected_hash:
                    self.logger.warning("rate_limiter.py 文件完整性校验失败，将使用默认值（禁用流控）。")
                    return
                
                self.logger.info("rate_limiter.py 文件完整性校验通过。")
                if config_data:
                    self.enabled = config_data.get("enabled", self.enabled)
                    self.global_limit = config_data.get("global_limit", self.global_limit)
                    if "global_period_seconds" in config_data:
                        self.global_period_seconds = config_data.get("global_period_seconds", self.global_period_seconds)
                    elif "global_period" in config_data: 
                        period_map = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
                        self.global_period_seconds = period_map.get(config_data["global_period"], 3600)
                    self.logger.info(f"成功加载并验证了速率限制配置文件。参数: 启用={self.enabled}, 限制={self.global_limit}次/{self.global_period_seconds}秒")
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self.logger.warning(f"解密或解析速率限制配置失败，将使用默认值（禁用流控）。错误: {e}")
                return

        except Exception as e:
            self.logger.warning(f"加载速率限制配置时出错，将使用默认值（禁用流控）。错误: {e}")

    async def _get_provider_quota(self, provider_name: str) -> Optional[int]:
        try:
            scraper = self._scraper_manager.get_scraper(provider_name)
            quota = getattr(scraper, 'rate_limit_quota', None)
            if quota is not None and quota > 0:
                return quota
        except (ValueError, AttributeError):
            pass
        return None

    def _get_global_limit(self) -> tuple[int, int]:
        if not self.enabled:
            return 0, 3600
        return self.global_limit, self.global_period_seconds

    async def check(self, provider_name: str):
        if self._verification_failed:
            # 即使验证失败，也不再阻止请求，而是使用默认值
            return

        global_limit, period_str = self._get_global_limit()
        if global_limit <= 0:
            return
        period_seconds = period_str

        async with self._session_factory() as session:
            global_state = await crud.get_or_create_rate_limit_state(session, "__global__")
            provider_state = await crud.get_or_create_rate_limit_state(session, provider_name)

            now = get_now()
            time_since_reset = now - global_state.lastResetTime
            
            if time_since_reset.total_seconds() >= period_seconds:
                self.logger.info(f"全局速率限制周期已过，正在重置所有计数器。")
                await crud.reset_all_rate_limit_states(session)
                await session.commit()
                
                await session.refresh(global_state)
                await session.refresh(provider_state)
                
                time_since_reset = now - global_state.lastResetTime 

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
        global_limit, _ = self._get_global_limit()
        if global_limit <= 0:
            return

        async with self._session_factory() as session:
            await crud.increment_rate_limit_count(session, "__global__")
            await crud.increment_rate_limit_count(session, provider_name)
            await session.commit()
            self.logger.debug(f"已为 '__global__' 和 '{provider_name}' 增加下载流控计数。")