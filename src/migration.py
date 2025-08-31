import asyncio
import logging
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import select, inspect, text, func
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlalchemy.orm import selectinload, DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import BigInteger, ForeignKey, Integer, String, TEXT

logger = logging.getLogger(__name__)

# --- 弹幕文件存储配置 ---
DANMAKU_BASE_DIR = Path(__file__).parent.parent / "config" / "danmaku"

# --- 临时的 ORM 模型定义，仅用于此脚本，以避免与已修改的主模型冲突 ---
class TmpBase(DeclarativeBase):
    pass

class TmpAnime(TmpBase):
    __tablename__ = "anime"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sources: Mapped[List["TmpAnimeSource"]] = relationship(back_populates="anime")

class TmpAnimeSource(TmpBase):
    __tablename__ = "anime_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    animeId: Mapped[int] = mapped_column("anime_id", ForeignKey("anime.id"))
    episodes: Mapped[List["TmpEpisode"]] = relationship(back_populates="source")
    anime: Mapped["TmpAnime"] = relationship(back_populates="sources")

class TmpEpisode(TmpBase):
    __tablename__ = "episode"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sourceId: Mapped[int] = mapped_column("source_id", ForeignKey("anime_sources.id"))
    danmakuFilePath: Mapped[str] = mapped_column("danmaku_file_path", String(512), nullable=True)
    commentCount: Mapped[int] = mapped_column("comment_count", Integer)
    comments: Mapped[List["TmpComment"]] = relationship(back_populates="episode")
    source: Mapped["TmpAnimeSource"] = relationship(back_populates="episodes")

class TmpComment(TmpBase):
    __tablename__ = "comment"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    episodeId: Mapped[int] = mapped_column("episode_id", ForeignKey("episode.id"))
    p: Mapped[str] = mapped_column(String(255))
    m: Mapped[str] = mapped_column(TEXT)
    episode: Mapped["TmpEpisode"] = relationship(back_populates="comments")

def _generate_xml_from_comments(comments: List[TmpComment], episode_id: int) -> str:
    """根据弹幕对象列表生成符合dandanplay标准的XML字符串。"""
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<i>',
        '  <chatserver>danmu.misaka-mikoto.jp</chatserver>',
        '  <chatid>0</chatid>',
        '  <mission>0</mission>',
        f'  <maxlimit>{len(comments)}</maxlimit>',
        '  <source>kuyun</source>'
    ]
    for comment in comments:
        content = xml_escape(comment.m or '')
        p_attr = comment.p or '0,1,25,16777215'

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

async def _add_danmaku_path_column_if_not_exists(session: AsyncSession):
    """如果 episode 表中不存在 danmaku_file_path 列，则添加它。"""
    def check_columns_sync(conn):
        inspector = inspect(conn.connection())
        columns = inspector.get_columns('episode')
        return any(c['name'] == 'danmaku_file_path' for c in columns)

    has_column = await session.run_sync(check_columns_sync)

    if not has_column:
        logger.info("检测到 'episode' 表中缺少 'danmaku_file_path' 列，正在添加...")
        await session.execute(text("ALTER TABLE episode ADD COLUMN danmaku_file_path VARCHAR(512);"))
        await session.commit()
        logger.info("'danmaku_file_path' 列已成功添加。")


async def run_db_migration(session_factory: async_sessionmaker[AsyncSession]):
    """
    在应用启动时执行数据库迁移。
    """
    logger.info("--- 正在检查数据库迁移需求 ---")

    # 首先，只检查一次 comment 表是否存在
    async with session_factory() as session:
        def check_table_sync(conn):
            inspector = inspect(conn.connection())
            return inspector.has_table('comment')

        has_comment_table = await session.run_sync(check_table_sync)
        if not has_comment_table:
            logger.info("✅ 未找到 'comment' 表，无需迁移。")
            return

    logger.info("检测到旧的 'comment' 表，将开始执行数据迁移...")

    # 1. 确保新列存在
    async with session_factory() as session:
        await _add_danmaku_path_column_if_not_exists(session)


    # 2. 轻量级查询，只获取需要迁移的分集ID列表
    async with session_factory() as session:
        logger.info("正在查询需要迁移的分集ID列表...")
        stmt = (
            select(TmpEpisode.id)
            .join(TmpEpisode.comments)
            .where(TmpEpisode.danmakuFilePath.is_(None))
            .distinct()
        )
        result = await session.execute(stmt)
        episode_ids_to_migrate = result.scalars().all()

    if not episode_ids_to_migrate:
        logger.info("✅ 数据库中没有找到需要迁移的弹幕数据。")
        async with session_factory() as session:
            logger.info("正在删除空的 'comment' 表...")
            await session.execute(text("DROP TABLE comment;"))
            await session.commit()
            logger.info("'comment' 表已删除。")
        return

    total_episodes = len(episode_ids_to_migrate)
    logger.info(f"共找到 {total_episodes} 个分集需要迁移。将逐一处理以降低服务器负载。")

    migrated_count = 0
    # 3. 逐个处理每个分集，每个都在自己的事务中
    for i, episode_id in enumerate(episode_ids_to_migrate):
        async with session_factory() as session:
            try:
                # 获取单个分集的完整数据
                stmt = (
                    select(TmpEpisode)
                    .options(
                        selectinload(TmpEpisode.comments),
                        selectinload(TmpEpisode.source).selectinload(TmpAnimeSource.anime)
                    )
                    .where(TmpEpisode.id == episode_id)
                )
                result = await session.execute(stmt)
                episode = result.scalar_one_or_none()

                if not episode or not episode.comments:
                    logger.warning(f"跳过分集 ID {episode_id}，因为它没有弹幕或已不存在。")
                    continue

                anime_id = episode.source.anime.id

                xml_content = _generate_xml_from_comments(episode.comments, episode_id)
                
                # 修正：存储不含 /data 前缀的相对路径，以与新系统保持一致
                web_path = f"/danmaku/{anime_id}/{episode_id}.xml"
                absolute_path = DANMAKU_BASE_DIR / str(anime_id) / f"{episode_id}.xml"
                
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                absolute_path.write_text(xml_content, encoding='utf-8')

                episode.danmakuFilePath = web_path
                episode.commentCount = len(episode.comments)
                
                # 清理当前分集的旧弹幕数据
                await session.execute(text("DELETE FROM comment WHERE episode_id = :id").bindparams(id=episode_id))
                
                await session.commit()
                
                migrated_count += 1
                logger.info(f"({migrated_count}/{total_episodes}) 成功迁移分集 ID: {episode_id}，并已清理其旧弹幕数据。")

            except Exception as e:
                logger.error(f"迁移分集 ID {episode_id} 时发生错误: {e}", exc_info=True)
                await session.rollback()
                continue

    # 4. 最终检查并尝试删除 comment 表
    async with session_factory() as session:
        remaining_comments_count_res = await session.execute(select(func.count()).select_from(TmpComment))
        remaining_comments_count = remaining_comments_count_res.scalar_one()
        
        if remaining_comments_count == 0:
            logger.info("所有弹幕已迁移，正在删除 'comment' 表...")
            await session.execute(text("DROP TABLE comment;"))
            await session.commit()
            logger.info("'comment' 表已成功删除。")
        else:
            logger.warning(f"'comment' 表中仍有 {remaining_comments_count} 条弹幕未被迁移（可能由于处理错误），将不会被删除。")

    logger.info(f"🎉 --- 弹幕数据迁移完成！共成功迁移了 {migrated_count}/{total_episodes} 个分集的弹幕。 ---")