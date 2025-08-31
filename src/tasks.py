import logging
from typing import Callable, List, Optional, Dict
import asyncio
import re
import traceback
from pathlib import Path
import shutil
import io
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

from thefuzz import fuzz
from sqlalchemy import delete, func, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError
from xml.sax.saxutils import escape as xml_escape

from . import crud, models, orm_models
from .rate_limiter import RateLimiter, RateLimitExceededError
from .image_utils import download_image
from .config import settings
from .scraper_manager import ScraperManager
from .metadata_manager import MetadataSourceManager 
from .utils import parse_search_keyword, clean_xml_string
from .crud import DANMAKU_BASE_DIR
from .task_manager import TaskManager, TaskSuccess, TaskStatus
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

def _parse_xml_content(xml_content: str) -> List[Dict[str, str]]:
    """
    使用 iterparse 高效解析XML弹幕内容，无条数限制，并规范化p属性。
    """
    comments = []
    try:
        # 使用 io.StringIO 将字符串转换为文件流，以便 iterparse 处理
        xml_stream = io.StringIO(xml_content)
        # iterparse 以事件驱动的方式解析，内存效率高，适合大文件
        for event, elem in ET.iterparse(xml_stream, events=('end',)):
            # 当一个 <d> 标签结束时处理它
            if elem.tag == 'd':
                p_attr = elem.get('p')
                text = elem.text
                if p_attr is not None and text is not None:
                    p_parts = p_attr.split(',')
                    if len(p_parts) >= 4:
                        # 提取前4个核心参数: 时间, 模式, 字体大小, 颜色
                        processed_p_attr = f"{p_parts[0]},{p_parts[1]},{p_parts[2]},{p_parts[3]},[custom_xml]"
                        comments.append({'p': processed_p_attr, 'm': text})
                    else:
                        # 如果参数不足4个，保持原样以避免数据损坏
                        comments.append({'p': p_attr, 'm': text})
                # 清理已处理的元素以释放内存
                elem.clear()
    except ET.ParseError as e:
        logger.error(f"解析XML时出错: {e}")
        # 即使解析出错，也可能已经解析了一部分，返回已解析的内容
    return comments

def _generate_episode_range_string(episode_indices: List[int]) -> str:
    """
    将分集编号列表转换为紧凑的字符串表示形式。
    例如: [1, 2, 3, 5, 8, 9, 10] -> "1-3, 5, 8-10"
    """
    if not episode_indices:
        return "无"

    indices = sorted(list(set(episode_indices)))
    if not indices:
        return "无"

    ranges = []
    start = end = indices[0]

    for i in range(1, len(indices)):
        if indices[i] == end + 1:
            end = indices[i]
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = indices[i]
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ", ".join(ranges)

def _generate_dandan_xml(comments: List[dict]) -> str:
    """
    根据弹幕字典列表生成 dandanplay 格式的 XML 字符串。
    """
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<i>',
        '  <chatserver>danmu</chatserver>',
        '  <chatid>0</chatid>',
        '  <mission>0</mission>',
        f'  <maxlimit>{len(comments)}</maxlimit>',
        '  <source>kuyun</source>'
    ]
    for comment in comments:
        # 使用标准库进行安全的XML转义
        content = xml_escape(comment.get('m', ''))
        p_attr = comment.get('p', '0,1,25,16777215')

        # 新增：健壮性修复，确保 p 属性包含字体大小（至少4个部分）
        p_parts = p_attr.split(',')
        
        # 查找可选的用户标签，以确定核心参数的数量
        core_parts_count = len(p_parts)
        for i, part in enumerate(p_parts):
            if '[' in part and ']' in part:
                core_parts_count = i
                break

        # 如果核心参数只有3个（时间,模式,颜色），则在模式和颜色之间插入默认字体大小 "25"
        if core_parts_count == 3:
            p_parts.insert(2, '25')
            p_attr = ','.join(p_parts)
            
        xml_parts.append(f'  <d p="{p_attr}">{content}</d>')
    xml_parts.append('</i>')
    return '\n'.join(xml_parts)

def _convert_text_danmaku_to_xml(text_content: str) -> str:
    """
    将非标准的、基于行的纯文本弹幕格式转换为标准的XML格式。
    支持的格式: "时间,模式,?,颜色,... | 弹幕内容"
    """
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<i>',
        '  <chatserver>danmu</chatserver>',
        '  <chatid>0</chatid>',
        '  <mission>0</mission>',
        '  <source>misaka</source>'
    ]
    comments = []
    for line in text_content.strip().split('\n'):
        if '|' not in line:
            continue
        params_str, text = line.split('|', 1)
        params = params_str.split(',')
        if len(params) >= 4:
            # 提取关键参数: 时间, 模式, 颜色
            # 格式: 756.103,1,25,16777215,...
            time_sec = params[0]
            mode     = params[1]
            fontsize = params[2]
            color    = params[3]
            p_attr = f"{time_sec},{mode},{fontsize},{color},[custom_text]"
            escaped_text = xml_escape(text.strip())
            comments.append(f'  <d p="{p_attr}">{escaped_text}</d>')
    xml_parts.insert(5, f'  <maxlimit>{len(comments)}</maxlimit>')
    xml_parts.extend(comments)
    xml_parts.append('</i>')
    return '\n'.join(xml_parts)

async def _write_danmaku_file_and_update_db(session: AsyncSession, anime_id: int, episode_id: int, comments: List[dict]) -> int:
    """
    将弹幕写入指定路径的文件，并更新数据库记录。
    路径结构: /danmaku/{animeId}/{episodeId}.xml
    """
    anime_dir = DANMAKU_BASE_DIR / str(anime_id)
    anime_dir.mkdir(parents=True, exist_ok=True)
    danmaku_file_path = anime_dir / f"{episode_id}.xml"
    
    xml_content = _generate_dandan_xml(comments)
    danmaku_file_path.write_text(xml_content, encoding='utf-8')

    # 将相对Web路径存入数据库，不包含 /data 前缀，以实现解耦。
    # 这样，如果未来静态文件服务路径改变，也无需修改数据库。
    web_path = f"/danmaku/{anime_id}/{episode_id}.xml"
    await crud.update_episode_danmaku_info(session, episode_id, web_path, len(comments))
    return len(comments)

def _delete_danmaku_file(danmaku_file_path_str: Optional[str]):
    """根据数据库中存储的Web路径，安全地删除对应的弹幕文件。"""
    if not danmaku_file_path_str:
        return
    try:
        # 从数据库读取的Web路径 (e.g., /danmaku/...) 转换为文件系统路径。
        # 移除开头的 '/'，得到 'danmaku/...'
        relative_path = Path(danmaku_file_path_str.lstrip('/'))
        full_path = DANMAKU_BASE_DIR.parent / relative_path
        if full_path.is_file():
            full_path.unlink()
    except (ValueError, FileNotFoundError):
        # 如果路径无效或文件不存在，则忽略
        pass
    except Exception as e:
        logger.error(f"删除弹幕文件 '{danmaku_file_path_str}' 时出错: {e}", exc_info=True)

async def delete_anime_task(animeId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete an anime and all its related data."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await progress_callback(0, f"开始删除 (尝试 {attempt + 1}/{max_retries})...")
            
            # 检查作品是否存在
            anime_exists = await session.get(orm_models.Anime, animeId)
            if not anime_exists:
                raise TaskSuccess("作品未找到，无需删除。")

            # 1. 删除关联的弹幕文件目录
            await progress_callback(50, "正在删除关联的弹幕文件...")
            anime_danmaku_dir = DANMAKU_BASE_DIR / str(animeId)
            if anime_danmaku_dir.exists() and anime_danmaku_dir.is_dir():
                shutil.rmtree(anime_danmaku_dir)
                logger.info(f"已删除作品的弹幕目录: {anime_danmaku_dir}")

            # 2. 删除作品本身 (数据库将通过级联删除所有关联记录)
            await progress_callback(90, "正在删除数据库记录...")
            await session.delete(anime_exists)
            
            await session.commit()
            raise TaskSuccess("删除成功。")
        except OperationalError as e:
            await session.rollback()
            if "Lock wait timeout exceeded" in str(e) and attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1) # 2, 4, 8 seconds
                logger.warning(f"删除作品时遇到锁超时，将在 {wait_time} 秒后重试...")
                await progress_callback(0, f"数据库锁定，将在 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
                continue # Retry the loop
            else:
                logger.error(f"删除作品任务 (ID: {animeId}) 失败: {e}", exc_info=True)
                raise # Re-raise if it's not a lock error or retries are exhausted
        except TaskSuccess:
            raise # Propagate success exception
        except Exception as e:
            await session.rollback()
            logger.error(f"删除作品任务 (ID: {animeId}) 失败: {e}", exc_info=True)
            raise

async def delete_source_task(sourceId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete a source and all its related data."""
    await progress_callback(0, "开始删除...")
    try:
        # 检查源是否存在
        source_exists = await session.get(orm_models.AnimeSource, sourceId)
        if not source_exists:
            raise TaskSuccess("数据源未找到，无需删除。")
        
        # 在删除数据库记录前，先删除关联的物理文件
        episodes_to_delete_res = await session.execute(
            select(orm_models.Episode.danmakuFilePath).where(orm_models.Episode.sourceId == sourceId)
        )
        for file_path in episodes_to_delete_res.scalars().all():
            _delete_danmaku_file(file_path)

        # 删除源记录，数据库将级联删除其下的所有分集记录
        await session.delete(source_exists)
        await session.commit()

        raise TaskSuccess("删除成功。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"删除源任务 (ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def delete_episode_task(episodeId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete an episode and its comments."""
    await progress_callback(0, "开始删除...")
    try:
        # 检查分集是否存在
        episode_exists = await session.get(orm_models.Episode, episodeId)
        if not episode_exists:
            raise TaskSuccess("分集未找到，无需删除。")

        # 在删除数据库记录前，先删除物理文件
        _delete_danmaku_file(episode_exists.danmakuFilePath)

        await session.delete(episode_exists)
        await session.commit()
        raise TaskSuccess("删除成功。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"删除分集任务 (ID: {episodeId}) 失败: {e}", exc_info=True)
        raise

async def _process_episode_list(
    episodes: List[models.ProviderEpisodeInfo],
    scraper: "BaseScraper",
    rate_limiter: RateLimiter,
    progress_callback: Callable,
    session: AsyncSession,
    anime_id: int,
    source_id: int
) -> tuple[int, int]:
    """
    一个通用的辅助函数，用于处理分集列表的弹幕获取和数据库写入。
    它封装了循环、错误处理、速率限制和进度更新的逻辑。

    :return: 一个元组 (total_comments_added, failed_episodes_count)
    """
    total_comments_added = 0
    failed_episodes_count = 0
    total_episodes = len(episodes)
    i = 0
    while i < total_episodes:
        episode = episodes[i]
        logger.info(f"--- 开始处理分集 {i+1}/{total_episodes}: '{episode.title}' (ID: {episode.episodeId}) ---")
        base_progress = 10 + int((i / total_episodes) * 85 if total_episodes > 0 else 85)
        await progress_callback(base_progress, f"正在处理: {episode.title} ({i+1}/{total_episodes})")

        comments = None
        try:
            await rate_limiter.check(scraper.provider_name)

            async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
                progress_slice = 85 / total_episodes if total_episodes > 0 else 0
                current_total_progress = base_progress + (danmaku_progress / 100) * progress_slice
                await progress_callback(int(current_total_progress), f"处理: {episode.title} - {danmaku_description}")

            comments = await scraper.get_comments(episode.episodeId, progress_callback=sub_progress_callback)
        except RateLimitExceededError as e:
            logger.warning(f"任务因达到速率限制而暂停: {e}")
            await progress_callback(base_progress, f"速率受限，将在 {e.retry_after_seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
            await asyncio.sleep(e.retry_after_seconds)
            continue  # 重试当前分集
        except httpx.RequestError as e:
            logger.error(f"获取分集 '{episode.title}' 的弹幕时发生网络错误: {e}")
            failed_episodes_count += 1
            await progress_callback(base_progress, f"处理: {episode.title} - 网络错误，已跳过", status=TaskStatus.RUNNING)
        except Exception as e:
            logger.error(f"获取分集 '{episode.title}' 的弹幕时发生未知错误: {e}", exc_info=True)
            failed_episodes_count += 1
            await progress_callback(base_progress, f"处理: {episode.title} - 未知错误，已跳过", status=TaskStatus.RUNNING)

        if comments:
            await rate_limiter.increment(scraper.provider_name)
            episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, episode.episodeIndex, episode.title, episode.url, episode.episodeId)
            added_count = await _write_danmaku_file_and_update_db(session, anime_id, episode_db_id, comments)
            total_comments_added += added_count
            logger.info(f"分集 '{episode.title}' (DB ID: {episode_db_id}) 新增 {added_count} 条弹幕。")
            await session.commit()

        i += 1
    return total_comments_added, failed_episodes_count

async def delete_bulk_episodes_task(episodeIds: List[int], session: AsyncSession, progress_callback: Callable):
    """后台任务：批量删除多个分集。"""
    total = len(episodeIds)
    await progress_callback(5, f"准备删除 {total} 个分集...")
    deleted_count = 0
    try:
        for i, episode_id in enumerate(episodeIds):
            progress = 5 + int(((i + 1) / total) * 90) if total > 0 else 95
            await progress_callback(progress, f"正在删除分集 {i+1}/{total} (ID: {episode_id}) 的数据...")

            episode = await session.get(orm_models.Episode, episode_id)
            if episode:
                _delete_danmaku_file(episode.danmakuFilePath)
                await session.delete(episode)
                deleted_count += 1
                
                # 3. 为每个分集提交一次事务，以尽快释放锁
                await session.commit()
                
                # 短暂休眠，以允许其他数据库操作有机会执行
                await asyncio.sleep(0.1)

        raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"批量删除分集任务失败: {e}", exc_info=True)
        raise

async def generic_import_task(
    provider: str,
    mediaId: str,
    animeTitle: str,
    mediaType: str,
    season: int,
    year: Optional[int],
    currentEpisodeIndex: Optional[int],
    imageUrl: Optional[str],
    doubanId: Optional[str],
    metadata_manager: MetadataSourceManager,
    tmdbId: Optional[str],
    imdbId: Optional[str],
    tvdbId: Optional[str],
    bangumiId: Optional[str],
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager, 
    task_manager: TaskManager,
    rate_limiter: RateLimiter
):
    """
    后台任务：执行从指定数据源导入弹幕的完整流程。
    """
    # 重构导入逻辑以避免创建空条目
    scraper = manager.get_scraper(provider)

    # 修正：在创建作品前，再次从标题中解析季和集，以确保数据一致性
    parsed_info = parse_search_keyword(animeTitle)
    title_to_use = parsed_info["title"]
    # 优先使用从标题解析出的季度，如果解析不出，则回退到传入的 season 参数
    season_to_use = parsed_info["season"] if parsed_info["season"] is not None else season
    normalized_title = animeTitle.replace(":", "：")

    await progress_callback(10, "正在获取分集列表...")
    episodes = await scraper.get_episodes(
        mediaId,
        target_episode_index=currentEpisodeIndex,
        db_media_type=mediaType
    )
    # 修正：即使 scraper.get_episodes 应该处理过滤，我们在此处再次强制过滤，
    # 以确保由 Webhook 触发的单集导入任务（特别是首次导入）只处理目标分集。
    # 这是一个健壮性修复，以防某些 scraper 未正确实现 target_episode_index 逻辑。
    if currentEpisodeIndex is not None and episodes:
        original_count = len(episodes)
        episodes = [ep for ep in episodes if ep.episodeIndex == currentEpisodeIndex]
        if len(episodes) < original_count:
            logger.info(
                f"已将分集列表从 {original_count} 个强制过滤为 {len(episodes)} 个 (目标集: {currentEpisodeIndex})，以匹配 Webhook 请求。"
            )

    if not episodes:
        # --- FAILOVER LOGIC ---
        logger.info(f"主源 '{provider}' 未能找到分集，尝试故障转移...")
        await progress_callback(15, "主源未找到分集，尝试故障转移...")
        
        user = models.User(id=1, username="scheduled_task") # Create a dummy user for metadata calls
        
        comments = await metadata_manager.get_failover_comments(
            title=animeTitle,
            season=season,
            episode_index=currentEpisodeIndex,
            user=user
        )
        
        if comments:
            logger.info(f"故障转移成功，找到 {len(comments)} 条弹幕。正在保存...")
            await progress_callback(20, f"故障转移成功，找到 {len(comments)} 条弹幕。")
            
            local_image_path = await download_image(imageUrl, session, manager, provider)
            image_download_failed = bool(imageUrl and not local_image_path)
            
            anime_id = await crud.get_or_create_anime(session, title_to_use, mediaType, season_to_use, imageUrl, local_image_path, year)
            await crud.update_metadata_if_empty(
                session, anime_id,
                tmdb_id=tmdbId,
                imdb_id=imdbId,
                tvdb_id=tvdbId,
                douban_id=doubanId,
                bangumi_id=bangumiId
            )
            source_id = await crud.link_source_to_anime(session, anime_id, provider, mediaId)
            
            episode_title = f"第 {currentEpisodeIndex} 集"
            episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, currentEpisodeIndex, episode_title, None, "failover")
            
            added_count = await _write_danmaku_file_and_update_db(session, anime_id, episode_db_id, comments)
            await session.commit()
            
            final_message = f"通过故障转移导入完成，共新增 {added_count} 条弹幕。" + (" (警告：海报图片下载失败)" if image_download_failed else "")
            raise TaskSuccess(final_message)
        else:
            msg = f"未能找到第 {currentEpisodeIndex} 集。" if currentEpisodeIndex else "未能获取到任何分集。"
            logger.error(f"任务失败: {msg} (provider='{provider}', media_id='{mediaId}')")
            raise ValueError(msg)

    if mediaType == "movie" and episodes:
        logger.info(f"检测到媒体类型为电影，将只处理第一个分集 '{episodes[0].title}'。")
        episodes = episodes[:1]

    anime_id: Optional[int] = None
    source_id: Optional[int] = None
    image_download_failed = False

    # 首次处理，需要创建 Anime 和 Source 记录
    logger.info("正在创建数据库主条目...")
    local_image_path = await download_image(imageUrl, session, manager, provider)
    if imageUrl and not local_image_path:
        image_download_failed = True
    anime_id = await crud.get_or_create_anime(session, title_to_use, mediaType, season_to_use, imageUrl, local_image_path, year)
    await crud.update_metadata_if_empty(
        session, anime_id,
        tmdb_id=tmdbId,
        imdb_id=imdbId,
        tvdb_id=tvdbId,
        douban_id=doubanId,
        bangumi_id=bangumiId
    )
    source_id = await crud.link_source_to_anime(session, anime_id, provider, mediaId)
    logger.info(f"主条目创建完成 (Anime ID: {anime_id}, Source ID: {source_id})。")
    await session.commit()

    # 调用通用处理器来处理分集
    total_comments_added, failed_episodes_count = await _process_episode_list(
        episodes=episodes, scraper=scraper, rate_limiter=rate_limiter,
        progress_callback=progress_callback, session=session,
        anime_id=anime_id, source_id=source_id
    )

    episode_indices = [ep.episodeIndex for ep in episodes]
    episode_range_str = _generate_episode_range_string(episode_indices)

    final_message = ""
    if total_comments_added == 0 and failed_episodes_count == 0:
        final_message = f"导入完成，导入集: < {episode_range_str} >，但未找到任何新弹幕。"
    else:
        final_message = f"导入完成，导入集: < {episode_range_str} >，新增 {total_comments_added} 条弹幕。"
    if failed_episodes_count > 0:
        final_message += f" {failed_episodes_count} 个分集因网络或解析错误获取失败。"
    if image_download_failed:
        final_message += " (警告：海报图片下载失败)"
    raise TaskSuccess(final_message)
    
async def edited_import_task(
    request_data: "models.EditedImportRequest",
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager,
    rate_limiter: RateLimiter,
    metadata_manager: MetadataSourceManager
):
    """后台任务：处理编辑后的导入请求。"""
    scraper = manager.get_scraper(request_data.provider)
    normalized_title = request_data.animeTitle.replace(":", "：")
    
    episodes = request_data.episodes
    if not episodes:
        raise TaskSuccess("没有提供任何分集，任务结束。")

    # 从标题中解析季和集，以确保数据一致性
    parsed_info = parse_search_keyword(normalized_title)
    title_to_use = parsed_info["title"]
    season_to_use = parsed_info["season"] if parsed_info["season"] is not None else request_data.season


    anime_id: Optional[int] = None
    source_id: Optional[int] = None
    image_download_failed = False

    # 创建或获取主条目
    local_image_path = await download_image(request_data.imageUrl, session, manager, request_data.provider)
    if request_data.imageUrl and not local_image_path:
        image_download_failed = True
    anime_id = await crud.get_or_create_anime(
        session, title_to_use, request_data.mediaType,
        season_to_use, request_data.imageUrl, local_image_path, request_data.year
    )
    await crud.update_metadata_if_empty( # 修正：使用 snake_case 关键字参数
        session, anime_id,
        tmdb_id=request_data.tmdbId,
        imdb_id=request_data.imdbId,
        tvdb_id=request_data.tvdbId,
        douban_id=request_data.doubanId,
        bangumi_id=request_data.bangumiId,
        tmdb_episode_group_id=request_data.tmdbEpisodeGroupId
    )
    source_id = await crud.link_source_to_anime(session, anime_id, request_data.provider, request_data.mediaId)
    await session.commit()

    # 调用通用处理器
    total_comments_added, failed_episodes_count = await _process_episode_list(
        episodes=episodes, scraper=scraper, rate_limiter=rate_limiter,
        progress_callback=progress_callback, session=session, anime_id=anime_id, source_id=source_id
    )

    episode_indices = [ep.episodeIndex for ep in episodes]
    episode_range_str = _generate_episode_range_string(episode_indices)

    final_message = ""
    if total_comments_added == 0 and failed_episodes_count == 0:
        final_message = f"导入完成，导入集: < {episode_range_str} >，但未找到任何新弹幕。"
    else:
        final_message = f"导入完成，导入集: < {episode_range_str} >，新增 {total_comments_added} 条弹幕。"
    if failed_episodes_count > 0:
        final_message += f" {failed_episodes_count} 个分集因网络或解析错误获取失败。"
    if image_download_failed:
        final_message += " (警告：海报图片下载失败)"
    raise TaskSuccess(final_message)

async def full_refresh_task(sourceId: int, session: AsyncSession, scraper_manager: ScraperManager, task_manager: TaskManager, rate_limiter: RateLimiter, progress_callback: Callable, metadata_manager: MetadataSourceManager):
    """    
    后台任务：全量刷新一个已存在的番剧，采用先获取后删除的安全策略。
    """
    logger.info(f"开始刷新源 ID: {sourceId}")
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        await progress_callback(100, "失败: 找不到源信息")
        logger.error(f"刷新失败：在数据库中找不到源 ID: {sourceId}")
        raise TaskSuccess("刷新失败: 找不到源信息")

    scraper = scraper_manager.get_scraper(source_info["providerName"])

    # 步骤 1: 获取所有新数据，但不写入数据库
    await progress_callback(10, "正在获取新分集列表...")
    current_media_id = source_info["mediaId"]
    new_episodes = await scraper.get_episodes(current_media_id)
    
    # --- Start of new failover logic ---
    if not new_episodes:
        logger.info(f"主源 '{source_info['providerName']}' 未能找到分集，尝试使用元数据源进行故障转移以查找新的 mediaId...")
        await progress_callback(15, "主源未找到分集，尝试故障转移...")
        
        new_media_id = await metadata_manager.find_new_media_id(source_info)
        
        if new_media_id and new_media_id != current_media_id:
            logger.info(f"通过故障转移为 '{source_info['title']}' 找到新的 mediaId: '{new_media_id}'，将重试获取分集。")
            await progress_callback(18, f"找到新的媒体ID，正在重试...")
            await crud.update_source_media_id(session, sourceId, new_media_id)
            new_episodes = await scraper.get_episodes(new_media_id)
    # --- End of new failover logic ---

    if not new_episodes:
        raise TaskSuccess("刷新失败：未能从源获取任何分集信息。旧数据已保留。")

    await progress_callback(20, f"获取到 {len(new_episodes)} 个新分集，正在获取弹幕...")
    
    new_data_package = []

    # 调用通用处理器来获取所有弹幕
    total_comments_fetched, failed_episodes_count = await _process_episode_list(
        episodes=new_episodes, scraper=scraper, rate_limiter=rate_limiter,
        progress_callback=progress_callback, session=session,
        anime_id=source_info["animeId"], source_id=sourceId
    )

    if total_comments_fetched == 0 and failed_episodes_count == len(new_episodes):
        raise TaskSuccess("刷新完成，但未找到任何新弹幕。旧数据已保留。")

    # 全量刷新任务的逻辑是先获取所有数据，然后一次性清空并写入，所以这里不需要再处理数据库
    # 最终的消息由 _process_episode_list 的返回值构建
    final_message = f"全量刷新完成，共处理 {len(new_episodes)} 个分集，新增 {total_comments_fetched} 条弹幕。"
    if failed_episodes_count > 0:
        final_message += f" {failed_episodes_count} 个分集因网络或解析错误获取失败。"
    raise TaskSuccess(final_message)

async def delete_bulk_sources_task(sourceIds: List[int], session: AsyncSession, progress_callback: Callable):
    """Background task to delete multiple sources."""
    total = len(sourceIds)
    deleted_count = 0
    for i, sourceId in enumerate(sourceIds):
        progress = int((i / total) * 100)
        await progress_callback(progress, f"正在删除源 {i+1}/{total} (ID: {sourceId})...")
        try:
            source = await session.get(orm_models.AnimeSource, sourceId)
            if source:
                await session.delete(source)
                await session.commit()
                deleted_count += 1
        except Exception as e:
            logger.error(f"批量删除源任务中，删除源 (ID: {sourceId}) 失败: {e}", exc_info=True)
            # Continue to the next one
    await session.commit()
    raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")

async def refresh_episode_task(episodeId: int, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter, progress_callback: Callable):
    """后台任务：刷新单个分集的弹幕"""
    logger.info(f"开始刷新分集 ID: {episodeId}")
    try:
        await progress_callback(0, "正在获取分集信息...")
        # 1. 获取分集的源信息
        info = await crud.get_episode_provider_info(session, episodeId)
        if not info or not info.get("providerName") or not info.get("providerEpisodeId"):
            logger.error(f"刷新失败：在数据库中找不到分集 ID: {episodeId} 的源信息")
            await progress_callback(100, "失败: 找不到源信息")
            return

        provider_name = info["providerName"]
        provider_episode_id = info["providerEpisodeId"]
        scraper = manager.get_scraper(provider_name)
        try:
            await rate_limiter.check(provider_name)
        except RateLimitExceededError as e:
            raise TaskSuccess(f"达到速率限制。请在 {e.retry_after_seconds:.0f} 秒后重试。")

        await progress_callback(30, "正在从源获取新弹幕...")

        async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
            # 30% for setup, 65% for download, 5% for db write
            current_total_progress = 30 + (danmaku_progress / 100) * 65
            await progress_callback(current_total_progress, danmaku_description)

        all_comments_from_source = await scraper.get_comments(provider_episode_id, progress_callback=sub_progress_callback)

        if not all_comments_from_source:
            await crud.update_episode_fetch_time(session, episodeId)
            raise TaskSuccess("未找到任何弹幕。")

        await rate_limiter.increment(provider_name)

        await progress_callback(96, f"正在写入 {len(all_comments_from_source)} 条新弹幕...")
        
        # 获取 animeId 用于文件路径
        anime_id = info["animeId"]
        added_count = await _write_danmaku_file_and_update_db(session, anime_id, episodeId, all_comments_from_source)
        
        await session.commit()
        raise TaskSuccess(f"刷新完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        # 任务成功完成，直接重新抛出，由 TaskManager 处理
        raise
    except Exception as e:
        logger.error(f"刷新分集 ID: {episodeId} 时发生严重错误: {e}", exc_info=True)
        raise # Re-raise so the task manager catches it and marks as FAILED

async def reorder_episodes_task(sourceId: int, session: AsyncSession, progress_callback: Callable):
    """后台任务：重新编号一个源的所有分集，并同步更新其ID和物理文件。"""
    logger.info(f"开始重整源 ID: {sourceId} 的分集顺序。")
    await progress_callback(0, "正在获取分集列表...")

    dialect_name = session.bind.dialect.name
    is_mysql = dialect_name == 'mysql'
    is_postgres = dialect_name == 'postgresql'

    try:
        # 根据数据库方言，暂时禁用外键检查
        if is_mysql:
            await session.execute(text("SET FOREIGN_KEY_CHECKS=0;"))
        elif is_postgres:
            await session.execute(text("SET session_replication_role = 'replica';"))
        
        # 在某些数据库/驱动中，执行此类命令后需要提交
        await session.commit()

        try:
            # 1. 获取计算新ID所需的信息
            source_info = await crud.get_anime_source_info(session, sourceId)
            if not source_info:
                raise ValueError(f"找不到源ID {sourceId} 的信息。")
            anime_id = source_info['animeId']

            # 确定 source_order。我们假设它是基于源ID的升序排列。
            all_sources_res = await session.execute(
                select(orm_models.AnimeSource.id)
                .where(orm_models.AnimeSource.animeId == anime_id)
                .order_by(orm_models.AnimeSource.id)
            )
            all_source_ids = all_sources_res.scalars().all()
            try:
                # 1-based index
                source_order = all_source_ids.index(sourceId) + 1
            except ValueError:
                raise ValueError(f"源 ID {sourceId} 未在作品 ID {anime_id} 的源列表中找到。")

            # 2. 获取所有分集ORM对象，按现有顺序排序
            episodes_orm_res = await session.execute(
                select(orm_models.Episode)
                .where(orm_models.Episode.sourceId == sourceId)
                .order_by(orm_models.Episode.episodeIndex, orm_models.Episode.id)
            )
            episodes_to_migrate = episodes_orm_res.scalars().all()

            if not episodes_to_migrate:
                raise TaskSuccess("没有找到分集，无需重整。")

            await progress_callback(10, "正在计算新的分集编号...")

            old_episodes_to_delete = []
            new_episodes_to_add = []
            
            for i, old_ep in enumerate(episodes_to_migrate):
                new_index = i + 1
                new_id = int(f"25{anime_id:06d}{source_order:02d}{new_index:04d}")
                
                if old_ep.id == new_id and old_ep.episodeIndex == new_index:
                    continue

                new_danmaku_web_path = f"/data/danmaku/{anime_id}/{new_id}.xml" if old_ep.danmakuFilePath else None
                if old_ep.danmakuFilePath:
                    old_full_path = DANMAKU_BASE_DIR.parent / Path(old_ep.danmakuFilePath).relative_to('/data')
                    new_full_path = DANMAKU_BASE_DIR.parent / Path(new_danmaku_web_path).relative_to('/data')
                    if old_full_path.is_file() and old_full_path != new_full_path:
                        new_full_path.parent.mkdir(parents=True, exist_ok=True)
                        old_full_path.rename(new_full_path)

                new_episodes_to_add.append(orm_models.Episode(id=new_id, sourceId=old_ep.sourceId, episodeIndex=new_index, title=old_ep.title, sourceUrl=old_ep.sourceUrl, providerEpisodeId=old_ep.providerEpisodeId, fetchedAt=old_ep.fetchedAt, commentCount=old_ep.commentCount, danmakuFilePath=new_danmaku_web_path))
                old_episodes_to_delete.append(old_ep)

            if not old_episodes_to_delete:
                raise TaskSuccess("所有分集顺序和ID都正确，无需重整。")

            await progress_callback(30, f"准备迁移 {len(old_episodes_to_delete)} 个分集...")

            for old_ep in old_episodes_to_delete:
                await session.delete(old_ep)
            await session.flush()
            session.add_all(new_episodes_to_add)
            
            await session.commit()
            raise TaskSuccess(f"重整完成，共迁移了 {len(new_episodes_to_add)} 个分集的记录。")
        except Exception as e:
            await session.rollback()
            logger.error(f"重整分集任务 (源ID: {sourceId}) 事务中失败: {e}", exc_info=True)
            raise
        finally:
            # 务必重新启用外键检查/恢复会话角色
            if is_mysql:
                await session.execute(text("SET FOREIGN_KEY_CHECKS=1;"))
            elif is_postgres:
                await session.execute(text("SET session_replication_role = 'origin';"))
            await session.commit()
    except Exception as e:
        logger.error(f"重整分集任务 (源ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def incremental_refresh_task(sourceId: int, nextEpisodeIndex: int, session: AsyncSession, manager: ScraperManager, task_manager: TaskManager, rate_limiter: RateLimiter, metadata_manager: MetadataSourceManager, progress_callback: Callable, animeTitle: str):
    """后台任务：增量刷新一个已存在的番剧。"""
    logger.info(f"开始增量刷新源 ID: {sourceId}，尝试获取第{nextEpisodeIndex}集")
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        progress_callback(100, "失败: 找不到源信息")
        logger.error(f"刷新失败：在数据库中找不到源 ID: {sourceId}")
        return
    try:
        # 重新执行通用导入逻辑, 只导入指定的一集
        await generic_import_task(
            provider=source_info["providerName"], mediaId=source_info["mediaId"],
            animeTitle=animeTitle, mediaType=source_info["type"],
            season=source_info.get("season", 1), year=source_info.get("year"),
            currentEpisodeIndex=nextEpisodeIndex, imageUrl=None,
            doubanId=None, tmdbId=source_info.get("tmdbId"), metadata_manager=metadata_manager,
            imdbId=None, tvdbId=None, bangumiId=source_info.get("bangumiId"),
            progress_callback=progress_callback,
            session=session,
            manager=manager, # type: ignore
            task_manager=task_manager,
            rate_limiter=rate_limiter)
    except Exception as e:
        logger.error(f"增量刷新源任务 (ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def manual_import_task(
    sourceId: int, animeId: int, title: Optional[str], episodeIndex: int, content: str, providerName: str,
    progress_callback: Callable, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter
):
    """后台任务：从URL手动导入弹幕。"""
    logger.info(f"开始手动导入任务: sourceId={sourceId}, title='{title or '未提供'}' ({providerName})")
    await progress_callback(10, "正在准备导入...")
    
    try:
        # Case 1: Custom source with XML data
        if providerName == 'custom':
            # 新增：自动检测内容格式。如果不是XML，则尝试从纯文本格式转换。
            content_to_parse = content.strip()
            if not content_to_parse.startswith('<'):
                logger.info("检测到非XML格式的自定义内容，正在尝试从纯文本格式转换...")
                content_to_parse = _convert_text_danmaku_to_xml(content_to_parse)
            await progress_callback(20, "正在解析XML文件...")
            cleaned_content = clean_xml_string(content_to_parse)
            comments = _parse_xml_content(cleaned_content)
            if not comments:
                raise TaskSuccess("未从XML中解析出任何弹幕。")
            
            await progress_callback(80, "正在写入数据库...")
            final_title = title if title else f"第 {episodeIndex} 集"
            episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, episodeIndex, final_title, "from_xml", "custom_xml")
            added_count = await _write_danmaku_file_and_update_db(session, animeId, episode_db_id, comments)
            await session.commit()
            raise TaskSuccess(f"手动导入完成，从XML新增 {added_count} 条弹幕。")

        # Case 2: Scraper source with URL
        scraper = manager.get_scraper(providerName)
        if not hasattr(scraper, 'get_id_from_url'):
            raise NotImplementedError(f"搜索源 '{providerName}' 不支持从URL手动导入。")

        provider_episode_id = await scraper.get_id_from_url(content)
        if not provider_episode_id:
            raise ValueError(f"无法从URL '{content}' 中解析出有效的视频ID。")

        episode_id_for_comments = scraper.format_episode_id_for_comments(provider_episode_id)
        await progress_callback(20, f"已解析视频ID: {episode_id_for_comments}")

        # Auto-generate title if not provided
        final_title = title
        if not final_title:
            if hasattr(scraper, 'get_title_from_url'):
                try:
                    final_title = await scraper.get_title_from_url(content)
                except Exception:
                    pass # Ignore errors, fallback to default
            if not final_title:
                final_title = f"第 {episodeIndex} 集"

        try:
            await rate_limiter.check(providerName)
        except RateLimitExceededError as e:
            raise TaskSuccess(f"达到速率限制。请在 {e.retry_after_seconds:.0f} 秒后重试。")

        comments = await scraper.get_comments(episode_id_for_comments, progress_callback=progress_callback)
        if not comments:
            raise TaskSuccess("未找到任何弹幕。")

        await rate_limiter.increment(providerName)

        await progress_callback(90, "正在写入数据库...")
        episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, episodeIndex, final_title, content, episode_id_for_comments)
        added_count = await _write_danmaku_file_and_update_db(session, animeId, episode_db_id, comments)
        await session.commit()
        raise TaskSuccess(f"手动导入完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        raise
    except Exception as e:
        logger.error(f"手动导入任务失败: {e}", exc_info=True)
        raise

async def batch_manual_import_task(
    sourceId: int, animeId: int, providerName: str, items: List[models.BatchManualImportItem],
    progress_callback: Callable, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter
):
    """后台任务：批量手动导入弹幕。"""
    total_items = len(items)
    logger.info(f"开始批量手动导入任务: sourceId={sourceId}, provider='{providerName}', items={total_items}")
    await progress_callback(5, f"准备批量导入 {total_items} 个条目...")

    total_added_comments = 0
    failed_items = 0

    i = 0
    while i < total_items:
        item = items[i]
        progress = 5 + int(((i + 1) / total_items) * 90) if total_items > 0 else 95
        item_desc = item.episodeTitle or f"第 {item.episodeIndex} 集"
        await progress_callback(progress, f"正在处理: {item_desc} ({i+1}/{total_items})")

        try:
            if providerName == 'custom':
                content_to_parse = item.content.strip()
                if not content_to_parse.startswith('<'):
                    logger.info(f"批量导入条目 '{item_desc}' 检测到非XML格式，正在尝试从纯文本格式转换...")
                    content_to_parse = _convert_text_danmaku_to_xml(content_to_parse)

                cleaned_content = clean_xml_string(content_to_parse)
                comments = _parse_xml_content(cleaned_content)
                if not comments: continue
                final_title = item.episodeTitle or f"第 {item.episodeIndex} 集"
                episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, item.episodeIndex, final_title, "from_xml_batch", "custom_xml")
                added_count = await _write_danmaku_file_and_update_db(session, animeId, episode_db_id, comments)
                total_added_comments += added_count
            else:
                scraper = manager.get_scraper(providerName)
                provider_episode_id = await scraper.get_id_from_url(item.content)
                if not provider_episode_id: raise ValueError("无法解析ID")
                episode_id_for_comments = scraper.format_episode_id_for_comments(provider_episode_id)
                final_title = item.episodeTitle or f"第 {item.episodeIndex} 集"
                
                await rate_limiter.check(providerName)
                comments = await scraper.get_comments(episode_id_for_comments)
                
                if comments:
                    await rate_limiter.increment(providerName)
                    episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, item.episodeIndex, final_title, item.content, episode_id_for_comments)
                    added_count = await _write_danmaku_file_and_update_db(session, animeId, episode_db_id, comments)
                    total_added_comments += added_count
            
            await session.commit()
            i += 1 # 成功处理，移动到下一个
        except RateLimitExceededError as e:
            logger.warning(f"批量导入任务因达到速率限制而暂停: {e}")
            await progress_callback(progress, f"速率受限，将在 {e.retry_after_seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
            await asyncio.sleep(e.retry_after_seconds)
            continue # 不增加 i，以便重试当前条目
        except Exception as e:
            logger.error(f"处理批量导入条目 '{item_desc}' 时失败: {e}", exc_info=True)
            failed_items += 1
            await session.rollback()
            i += 1 # 处理失败，移动到下一个
    
    final_message = f"批量导入完成。共处理 {total_items} 个条目，新增 {total_added_comments} 条弹幕。"
    if failed_items > 0:
        final_message += f" {failed_items} 个条目处理失败。"
    raise TaskSuccess(final_message)

async def auto_search_and_import_task(
    payload: "models.ControlAutoImportRequest",
    progress_callback: Callable,
    session: AsyncSession,
    scraper_manager: ScraperManager,
    metadata_manager: MetadataSourceManager,
    task_manager: TaskManager,
    rate_limiter: Optional[RateLimiter] = None,
    api_key: Optional[str] = None,
):
    """
    全自动搜索并导入的核心任务逻辑。
    """
    try:
        # 防御性检查：确保 rate_limiter 已被正确传递。
        if rate_limiter is None:
            error_msg = "任务启动失败：内部错误（速率限制器未提供）。请检查任务提交处的代码。"
            logger.error(f"auto_search_and_import_task was called without a rate_limiter. This is a bug. Payload: {payload}")
            raise ValueError(error_msg)

        search_type = payload.searchType
        search_term = payload.searchTerm
        
        await progress_callback(5, f"开始处理，类型: {search_type}, 搜索词: {search_term}")

        aliases = {search_term}
        main_title = search_term
        media_type = payload.mediaType
        season = payload.season
        image_url = None
        tmdb_id, bangumi_id, douban_id, tvdb_id, imdb_id = None, None, None, None, None

        # 为后台任务创建一个虚拟用户对象
        user = models.User(id=1, username="admin")

        # 1. 获取元数据和别名
        if search_type != "keyword":
            # --- Start of fix for TMDB/TVDB mediaType ---
            # 如果是TMDB或TVDB搜索，且没有提供mediaType，则根据有无季/集信息进行推断
            # 同时，将内部使用的 'tv_series'/'movie' 转换为特定提供商需要的格式
            provider_media_type = media_type
            if search_type in ["tmdb", "tvdb"]:
                # TVDB API v4 使用 'series' 和 'movies'
                provider_specific_tv_type = "tv" if search_type == "tmdb" else "series"
                provider_specific_movie_type = "movie" if search_type == "tmdb" else "movies"

                if not media_type:
                    # 修正：只要提供了季度信息，就应推断为电视剧
                    if payload.season is not None:
                        provider_media_type = provider_specific_tv_type
                        media_type = "tv_series" # 更新内部使用的类型
                        logger.info(f"{search_type.upper()} 搜索未提供 mediaType，根据季/集信息推断为 '{provider_specific_tv_type}'。")
                    else:
                        provider_media_type = provider_specific_movie_type
                        media_type = "movie" # 更新内部使用的类型
                        logger.info(f"{search_type.upper()} 搜索未提供 mediaType 和季/集信息，默认推断为 '{provider_specific_movie_type}'。")
                elif media_type == "tv_series":
                    provider_media_type = provider_specific_tv_type
                elif media_type == "movie":
                    provider_media_type = provider_specific_movie_type
            # --- End of fix ---
            try:
                await progress_callback(10, f"正在从 {search_type.upper()} 获取元数据...")
                details = await metadata_manager.get_details(
                    provider=search_type, item_id=search_term, user=user, mediaType=provider_media_type
                )
                
                if details:
                    main_title = details.title or main_title
                    image_url = details.imageUrl
                    aliases.add(main_title)
                    aliases.update(details.aliasesCn or [])
                    aliases.add(details.nameEn)
                    aliases.add(details.nameJp)
                    tmdb_id, bangumi_id, douban_id, tvdb_id, imdb_id = (
                        details.tmdbId, details.bangumiId, details.doubanId,
                        details.tvdbId, details.imdbId
                    )
                    # 修正：从元数据源获取最准确的媒体类型
                    if hasattr(details, 'type') and details.type:
                        media_type = details.type
                    
                    # 新增：从其他启用的元数据源获取更多别名，以提高搜索覆盖率
                    logger.info(f"正在为 '{main_title}' 从其他源获取更多别名...")
                    enriched_aliases = await metadata_manager.search_aliases_from_enabled_sources(main_title, user)
                    if enriched_aliases:
                        aliases.update(enriched_aliases)
                        logger.info(f"别名已扩充: {aliases}")
            except Exception as e:
                logger.error(f"从 {search_type.upper()} 获取元数据失败: {e}\n{traceback.format_exc()}")
                # Don't fail the whole task, just proceed with the original search term

        # 2. 检查媒体库中是否已存在
        await progress_callback(20, "正在检查媒体库...")
        existing_anime = await crud.find_anime_by_title_and_season(session, main_title, season)
        if existing_anime:
            favorited_source = await crud.find_favorited_source_for_anime(session, main_title, season)
            if favorited_source:
                source_to_use = favorited_source
                logger.info(f"媒体库中已存在作品，并找到精确标记源: {source_to_use['providerName']}")
            else:
                all_sources = await crud.get_anime_sources(session, existing_anime['id'])
                if all_sources:
                    ordered_settings = await crud.get_all_scraper_settings(session)
                    provider_order = {s['providerName']: s['displayOrder'] for s in ordered_settings}
                    all_sources.sort(key=lambda s: provider_order.get(s['providerName'], 999))
                    source_to_use = all_sources[0]
                    logger.info(f"媒体库中已存在作品，选择优先级最高的源: {source_to_use['providerName']}")
                else: source_to_use = None
            
            if source_to_use:
                await progress_callback(30, f"已存在，使用源: {source_to_use['providerName']}")
                unique_key = f"import-{source_to_use['providerName']}-{source_to_use['mediaId']}"
                task_coro = lambda s, cb: generic_import_task(
                    provider=source_to_use['providerName'], mediaId=source_to_use['mediaId'],
                    animeTitle=main_title, mediaType=media_type, season=season,
                    year=source_to_use.get('year'), currentEpisodeIndex=payload.episode, imageUrl=image_url,
                    metadata_manager=metadata_manager,
                    doubanId=douban_id, tmdbId=tmdb_id, imdbId=imdb_id, tvdbId=tvdb_id, bangumiId=bangumi_id,
                    progress_callback=cb, session=s, manager=scraper_manager, task_manager=task_manager,
                    rate_limiter=rate_limiter
                )
                await task_manager.submit_task(task_coro, f"自动导入 (库内): {main_title}", unique_key=unique_key)
                raise TaskSuccess("作品已在库中，已为已有源创建导入任务。")

        # 3. 如果库中不存在，则进行全网搜索
        await progress_callback(40, "媒体库未找到，开始全网搜索...")
        episode_info = {"season": season, "episode": payload.episode} if payload.episode else {"season": season}
        
        # 使用主标题进行搜索
        logger.info(f"将使用主标题 '{main_title}' 进行全网搜索...")
        all_results = await scraper_manager.search_all([main_title], episode_info=episode_info)
        logger.info(f"直接搜索完成，找到 {len(all_results)} 个原始结果。")

        # 使用所有别名进行过滤
        def normalize_for_filtering(title: str) -> str:
            if not title: return ""
            title = re.sub(r'[\[【(（].*?[\]】)）]', '', title)
            return title.lower().replace(" ", "").replace("：", ":").strip()

        normalized_filter_aliases = {normalize_for_filtering(alias) for alias in aliases if alias}
        filtered_results = []
        for item in all_results:
            normalized_item_title = normalize_for_filtering(item.title)
            if not normalized_item_title: continue
            if any((alias in normalized_item_title) or (normalized_item_title in alias) for alias in normalized_filter_aliases):
                filtered_results.append(item)
        logger.info(f"别名过滤: 从 {len(all_results)} 个原始结果中，保留了 {len(filtered_results)} 个相关结果。")
        all_results = filtered_results

        if not all_results:
            raise ValueError("全网搜索未找到任何结果。")

        # 4. 选择最佳源
        ordered_settings = await crud.get_all_scraper_settings(session)
        provider_order = {s['providerName']: s['displayOrder'] for s in ordered_settings}
        
        # 修正：使用更智能的排序逻辑来选择最佳匹配
        # 1. 媒体类型是否匹配
        # 2. 标题相似度 (使用 a_main_title 确保与原始元数据标题比较)
        # 3. 用户设置的源优先级
        all_results.sort(
            key=lambda item: (
                1 if item.type == media_type else 0,  # 媒体类型匹配得1分，否则0分
                fuzz.token_set_ratio(main_title, item.title), # 标题相似度得分
                -provider_order.get(item.provider, 999) # 源优先级，取负数因为值越小优先级越高
            ),
            reverse=True # 按得分从高到低排序
        )
        best_match = all_results[0]

        await progress_callback(80, f"选择最佳源: {best_match.provider}")
        unique_key = f"import-{best_match.provider}-{best_match.mediaId}"
        task_coro = lambda s, cb: generic_import_task(
            provider=best_match.provider, mediaId=best_match.mediaId,
            animeTitle=main_title, mediaType=media_type, season=season, year=best_match.year,
            metadata_manager=metadata_manager,
            currentEpisodeIndex=payload.episode, imageUrl=image_url,
            doubanId=douban_id, tmdbId=tmdb_id, imdbId=imdb_id, tvdbId=tvdb_id, bangumiId=bangumi_id,
            progress_callback=cb, session=s, manager=scraper_manager, task_manager=task_manager,
            rate_limiter=rate_limiter
        )
        await task_manager.submit_task(task_coro, f"自动导入 (新): {main_title}", unique_key=unique_key)
        raise TaskSuccess("已为最佳匹配源创建导入任务。")
    finally:
        if api_key:
            await scraper_manager.release_search_lock(api_key)
            logger.info(f"自动导入任务已为 API key 释放搜索锁。")
async def database_maintenance_task(session: AsyncSession, progress_callback: Callable):
    """
    执行数据库维护的核心任务：清理旧日志和优化表。
    """
    logger.info("开始执行数据库维护任务...")
    
    # --- 1. 应用日志清理 ---
    await progress_callback(10, "正在清理旧日志...")
    
    try:
        # 日志保留天数，默认为30天。
        retention_days_str = await crud.get_config_value(session, "logRetentionDays", "3")
        retention_days = int(retention_days_str)
    except (ValueError, TypeError):
        retention_days = 3
    
    if retention_days > 0:
        logger.info(f"将清理 {retention_days} 天前的日志记录。")
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
        
        tables_to_prune = {
            "任务历史": (orm_models.TaskHistory, orm_models.TaskHistory.createdAt),
            "Token访问日志": (orm_models.TokenAccessLog, orm_models.TokenAccessLog.accessTime),
            "外部API访问日志": (orm_models.ExternalApiLog, orm_models.ExternalApiLog.accessTime),
        }
        
        total_deleted = 0
        for name, (model, date_column) in tables_to_prune.items():
            deleted_count = await crud.prune_logs(session, model, date_column, cutoff_date)
            if deleted_count > 0:
                logger.info(f"从 {name} 表中删除了 {deleted_count} 条旧记录。")
            total_deleted += deleted_count
        await progress_callback(40, f"应用日志清理完成，共删除 {total_deleted} 条记录。")
    else:
        logger.info("日志保留天数设为0或无效，跳过清理。")
        await progress_callback(40, "日志保留天数设为0，跳过清理。")

    # --- 2. Binlog 清理 (仅MySQL) ---
    db_type = settings.database.type.lower()
    if db_type == "mysql":
        await progress_callback(50, "正在清理 MySQL Binlog...")
        try:
            # 用户指定清理3天前的日志
            binlog_cleanup_message = await crud.purge_binary_logs(session, days=3)
            logger.info(binlog_cleanup_message)
            await progress_callback(60, binlog_cleanup_message)
        except OperationalError as e:
            # 检查是否是权限不足的错误 (MySQL error code 1227)
            if e.orig and hasattr(e.orig, 'args') and len(e.orig.args) > 0 and e.orig.args[0] == 1227:
                binlog_cleanup_message = "Binlog 清理失败: 数据库用户缺少 SUPER 或 BINLOG_ADMIN 权限。此为正常现象，可安全忽略。"
                logger.warning(binlog_cleanup_message)
                await progress_callback(60, binlog_cleanup_message)
            else:
                # 其他操作错误，仍然记录详细信息
                binlog_cleanup_message = f"Binlog 清理失败: {e}"
                logger.error(binlog_cleanup_message, exc_info=True)
                await progress_callback(60, binlog_cleanup_message)
        except Exception as e:
            # 记录错误，但不中断任务
            binlog_cleanup_message = f"Binlog 清理失败: {e}"
            logger.error(binlog_cleanup_message, exc_info=True)
            await progress_callback(60, binlog_cleanup_message)

    # --- 3. 数据库表优化 ---
    await progress_callback(70, "正在执行数据库表优化...")
    
    try:
        optimization_message = await crud.optimize_database(session, db_type)
        logger.info(f"数据库优化结果: {optimization_message}")
    except Exception as e:
        optimization_message = f"数据库优化失败: {e}"
        logger.error(optimization_message, exc_info=True)
        # 即使优化失败，也不应导致整个任务失败，仅记录错误

    await progress_callback(90, optimization_message)

    final_message = f"数据库维护完成。{optimization_message}"
    raise TaskSuccess(final_message)
