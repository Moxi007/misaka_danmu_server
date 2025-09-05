import secrets
import string
import logging
from fastapi import FastAPI, Request
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from .config import settings
from .orm_models import Base
from .timezone import get_app_timezone, get_timezone_offset_str
# 使用模块级日志记录器
logger = logging.getLogger(__name__)

def _get_db_url(include_db_name: bool = True, for_server: bool = False) -> URL:
    """
    根据配置生成数据库连接URL。
    :param include_db_name: URL中是否包含数据库名称。
    :param for_server: 是否为连接到服务器（而不是特定数据库）生成URL，主要用于PostgreSQL。
    """
    db_type = settings.database.type.lower()
    
    if db_type == "mysql":
        drivername = "mysql+aiomysql"
        query = {"charset": "utf8mb4"}
        database = settings.database.name if include_db_name else None
    elif db_type == "postgresql":
        drivername = "postgresql+asyncpg"
        query = None
        if for_server:
            database = "postgres"
        else:
            database = settings.database.name if include_db_name else None
    else:
        raise ValueError(f"不支持的数据库类型: '{db_type}'。请使用 'mysql' 或 'postgresql'。")

    return URL.create(
        drivername=drivername,
        username=settings.database.user,
        password=settings.database.password,
        host=settings.database.host,
        port=settings.database.port,
        database=database,
        query=query,
    )

async def _migrate_add_source_order(conn, db_type, db_name):
    """
    迁移任务: 确保 anime_sources 表有持久化的 source_order 字段。
    这是一个关键迁移，用于修复因动态计算源顺序而导致的数据覆盖问题。
    """
    migration_id = "add_source_order_to_anime_sources"
    logger.info(f"正在检查是否需要执行迁移: {migration_id}...")

    # --- 1. 检查并添加 source_order 列 (初始为可空) ---
    if db_type == "mysql":
        check_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'anime_sources' AND column_name = 'source_order'")
        add_column_sql = text("ALTER TABLE anime_sources ADD COLUMN `source_order` INT NULL")
    elif db_type == "postgresql":
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'anime_sources' AND column_name = 'source_order'")
        add_column_sql = text('ALTER TABLE anime_sources ADD COLUMN "source_order" INT NULL')
    else:
        return

    column_exists = (await conn.execute(check_column_sql)).scalar_one_or_none() is not None
    if not column_exists:
        logger.info("列 'anime_sources.source_order' 不存在。正在添加...")
        await conn.execute(add_column_sql)
        logger.info("成功添加列 'anime_sources.source_order'。")

        # --- 2. 为现有数据填充 source_order ---
        logger.info("正在为现有数据填充 'source_order'...")
        distinct_anime_ids_res = await conn.execute(text("SELECT DISTINCT anime_id FROM anime_sources"))
        distinct_anime_ids = distinct_anime_ids_res.scalars().all()

        for anime_id in distinct_anime_ids:
            select_stmt = text("SELECT id FROM anime_sources WHERE anime_id = :anime_id ORDER BY id")
            sources_res = await conn.execute(select_stmt, {"anime_id": anime_id})
            sources_ids = sources_res.scalars().all()
            for i, source_id in enumerate(sources_ids):
                order = i + 1
                update_stmt = text("UPDATE anime_sources SET source_order = :order WHERE id = :source_id")
                await conn.execute(update_stmt, {"order": order, "source_id": source_id})
        logger.info("成功填充 'source_order' 数据。")

        # --- 3. 将列修改为 NOT NULL ---
        logger.info("正在将 'source_order' 列修改为 NOT NULL...")
        if db_type == "mysql":
            alter_not_null_sql = text("ALTER TABLE anime_sources MODIFY COLUMN `source_order` INT NOT NULL")
        else: # postgresql
            alter_not_null_sql = text('ALTER TABLE anime_sources ALTER COLUMN "source_order" SET NOT NULL')
        await conn.execute(alter_not_null_sql)
        logger.info("成功将 'source_order' 列修改为 NOT NULL。")

    # --- 4. 检查并添加唯一约束 ---
    # 即使列已存在，约束也可能不存在
    if db_type == "mysql":
        check_constraint_sql = text(f"SELECT 1 FROM information_schema.table_constraints WHERE table_schema = '{db_name}' AND table_name = 'anime_sources' AND constraint_name = 'idx_anime_source_order_unique'")
        add_constraint_sql = text("ALTER TABLE anime_sources ADD CONSTRAINT idx_anime_source_order_unique UNIQUE (anime_id, source_order)")
    else: # postgresql
        check_constraint_sql = text("SELECT 1 FROM pg_constraint WHERE conname = 'idx_anime_source_order_unique'")
        add_constraint_sql = text('ALTER TABLE anime_sources ADD CONSTRAINT idx_anime_source_order_unique UNIQUE (anime_id, source_order)')

    constraint_exists = (await conn.execute(check_constraint_sql)).scalar_one_or_none() is not None
    if not constraint_exists:
        logger.info("唯一约束 'idx_anime_source_order_unique' 不存在。正在添加...")
        try:
            await conn.execute(add_constraint_sql)
            logger.info("成功添加唯一约束 'idx_anime_source_order_unique'。")
        except Exception as e:
            logger.error(f"添加唯一约束失败: {e}。这可能是由于数据中存在重复的 (anime_id, source_order) 对。请手动检查并清理数据。")

    logger.info(f"迁移任务 '{migration_id}' 检查完成。")

async def _migrate_add_danmaku_file_path(conn, db_type, db_name):
    """
    迁移任务: 确保 episode 表有 danmaku_file_path 字段。
    这是为了兼容旧版本数据库，在代码更新后自动添加新列。
    """
    migration_id = "add_danmaku_file_path_to_episode"
    logger.info(f"正在检查是否需要执行迁移: {migration_id}...")

    # --- 1. 检查并添加 danmaku_file_path 列 ---
    if db_type == "mysql":
        check_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'episode' AND column_name = 'danmaku_file_path'")
        add_column_sql = text("ALTER TABLE episode ADD COLUMN `danmaku_file_path` VARCHAR(1024) NULL DEFAULT NULL")
    elif db_type == "postgresql":
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'episode' AND column_name = 'danmaku_file_path'")
        add_column_sql = text('ALTER TABLE episode ADD COLUMN "danmaku_file_path" VARCHAR(1024) NULL DEFAULT NULL')
    else:
        return

    column_exists = (await conn.execute(check_column_sql)).scalar_one_or_none() is not None
    if not column_exists:
        logger.info("列 'episode.danmaku_file_path' 不存在。正在添加...")
        await conn.execute(add_column_sql)
        logger.info("成功添加列 'episode.danmaku_file_path'。")
    
    logger.info(f"迁移任务 '{migration_id}' 检查完成。")

async def _migrate_cache_value_to_mediumtext(conn, db_type, db_name):
    """
    迁移任务: 确保 cache_data.cache_value 列有足够大的容量 (MEDIUMTEXT)。
    """
    migration_id = "migrate_cache_value_to_mediumtext"
    logger.info(f"正在检查是否需要执行迁移: {migration_id}...")

    if db_type == "mysql":
        # 检查列是否存在且类型不是MEDIUMTEXT
        check_sql = text(
            "SELECT DATA_TYPE FROM information_schema.columns "
            f"WHERE table_schema = '{db_name}' AND table_name = 'cache_data' AND column_name = 'cache_value'"
        )
        result = await conn.execute(check_sql)
        current_type = result.scalar_one_or_none()

        if current_type and current_type.lower() != 'mediumtext':
            logger.info(f"列 'cache_data.cache_value' 类型为 '{current_type}'，正在修改为 MEDIUMTEXT...")
            alter_sql = text("ALTER TABLE cache_data MODIFY COLUMN `cache_value` MEDIUMTEXT")
            await conn.execute(alter_sql)
            logger.info("成功将 'cache_data.cache_value' 列类型修改为 MEDIUMTEXT。")
        else:
            logger.info("列 'cache_data.cache_value' 类型已是 MEDIUMTEXT 或不存在，跳过迁移。")
    elif db_type == "postgresql":
        logger.info("PostgreSQL 的 TEXT 类型已支持大容量数据，无需为 cache_value 列执行迁移。")
    
    logger.info(f"迁移任务 '{migration_id}' 检查完成。")

async def _migrate_add_source_url_to_episode(conn, db_type, db_name):
    """
    迁移任务: 确保 episode 表有 source_url 字段，并处理旧的命名。
    - 如果存在旧的 'sourceUrl' 列，则将其重命名为 'source_url'。
    - 如果两者都不存在，则添加新的 'source_url' 列。
    """
    migration_id = "add_or_rename_source_url_in_episode"
    logger.info(f"正在检查是否需要执行迁移: {migration_id}...")

    old_column_name = "sourceUrl"
    new_column_name = "source_url"
    table_name = "episode"

    if db_type == "mysql":
        check_old_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = '{table_name}' AND column_name = '{old_column_name}'")
        check_new_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = '{table_name}' AND column_name = '{new_column_name}'")
        rename_column_sql = text(f"ALTER TABLE `{table_name}` CHANGE COLUMN `{old_column_name}` `{new_column_name}` TEXT NULL")
        add_column_sql = text(f"ALTER TABLE `{table_name}` ADD COLUMN `{new_column_name}` TEXT NULL")
    elif db_type == "postgresql":
        check_old_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table_name}' AND column_name = '{old_column_name}'")
        check_new_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table_name}' AND column_name = '{new_column_name}'")
        rename_column_sql = text(f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_column_name}" TO "{new_column_name}"')
        add_column_sql = text(f'ALTER TABLE "{table_name}" ADD COLUMN "{new_column_name}" TEXT NULL')
    else:
        return

    old_col_exists = (await conn.execute(check_old_column_sql)).scalar_one_or_none() is not None
    new_col_exists = (await conn.execute(check_new_column_sql)).scalar_one_or_none() is not None

    if old_col_exists and not new_col_exists:
        logger.info(f"在表 '{table_name}' 中发现旧列 '{old_column_name}'，正在将其重命名为 '{new_column_name}'...")
        await conn.execute(rename_column_sql)
        logger.info(f"成功重命名表 '{table_name}' 中的列。")
    elif not old_col_exists and not new_col_exists:
        logger.info(f"列 '{table_name}.{new_column_name}' 不存在，正在添加...")
        await conn.execute(add_column_sql)
        logger.info(f"成功添加列 '{table_name}.{new_column_name}'。")
    elif new_col_exists:
        logger.info(f"列 '{table_name}.{new_column_name}' 已存在，跳过迁移。")
    
    logger.info(f"迁移任务 '{migration_id}' 检查完成。")

async def _migrate_text_to_mediumtext(conn, db_type, db_name):
    """
    迁移任务: 将多个表中可能存在的 TEXT 字段修改为 MEDIUMTEXT (仅MySQL)。
    这是为了确保在旧版本上创建的表有足够大的容量。
    """
    migration_id = "migrate_text_to_mediumtext"
    logger.info(f"正在检查是否需要执行迁移: {migration_id}...")

    if db_type != "mysql":
        logger.info("非MySQL数据库，跳过 TEXT 到 MEDIUMTEXT 的迁移。")
        return

    tables_and_columns = {
        "cache_data": "cache_value",
        "config": "config_value",
        "task_history": "description",
        "external_api_logs": "message"
    }

    for table, column in tables_and_columns.items():
        check_sql = text(
            "SELECT DATA_TYPE FROM information_schema.columns "
            f"WHERE table_schema = '{db_name}' AND table_name = '{table}' AND column_name = '{column}'"
        )
        result = await conn.execute(check_sql)
        current_type = result.scalar_one_or_none()

        if current_type and current_type.lower() == 'text':
            logger.info(f"列 '{table}.{column}' 类型为 TEXT，正在修改为 MEDIUMTEXT...")
            alter_sql = text(f"ALTER TABLE {table} MODIFY COLUMN `{column}` MEDIUMTEXT")
            await conn.execute(alter_sql)
            logger.info(f"成功将 '{table}.{column}' 列类型修改为 MEDIUMTEXT。")

    logger.info(f"迁移任务 '{migration_id}' 检查完成。")

async def _run_migrations(conn):
    """
    执行所有一次性的数据库架构迁移。
    """
    db_type = settings.database.type.lower()
    db_name = settings.database.name

    if db_type not in ["mysql", "postgresql"]:
        logger.warning(f"不支持为数据库类型 '{db_type}' 自动执行迁移。")
        return

    await _migrate_add_source_order(conn, db_type, db_name)
    await _migrate_add_danmaku_file_path(conn, db_type, db_name)
    await _migrate_cache_value_to_mediumtext(conn, db_type, db_name)
    await _migrate_text_to_mediumtext(conn, db_type, db_name)
    await _migrate_add_source_url_to_episode(conn, db_type, db_name)

def _log_db_connection_error(context_message: str, e: Exception):
    """Logs a standardized, detailed error message for database connection failures."""
    logger.error("="*60)
    logger.error(f"=== {context_message}失败，应用无法启动。 ===")
    logger.error(f"=== 错误类型: {type(e).__name__}")
    logger.error(f"=== 错误详情: {e}")
    logger.error("---")
    logger.error("--- 可能的原因与排查建议: ---")
    logger.error("--- 1. 数据库服务未运行: 请确认您的数据库服务正在运行。")
    logger.error(f"--- 2. 配置错误: 请检查您的配置文件或环境变量中的数据库连接信息是否正确。")
    logger.error(f"---    - 主机 (Host): {settings.database.host}")
    logger.error(f"---    - 端口 (Port): {settings.database.port}")
    logger.error(f"---    - 用户 (User): {settings.database.user}")
    logger.error("--- 3. 网络问题: 如果应用和数据库在不同的容器或机器上，请检查它们之间的网络连接和防火墙设置。")
    logger.error("--- 4. 权限问题: 确认提供的用户有权限从应用所在的IP地址连接，并有创建数据库的权限。")
    logger.error("="*60)

async def create_db_engine_and_session(app: FastAPI):
    """创建数据库引擎和会话工厂，并存储在 app.state 中"""
    try:
        db_url = _get_db_url()
        db_type = settings.database.type.lower()
        engine_args = {
            "echo": False,
            "pool_recycle": 3600,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 30
        }
        # 移除时区设置，让数据库使用其默认时区

        engine = create_async_engine(db_url, **engine_args)
        app.state.db_engine = engine
        app.state.db_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        logger.info("数据库引擎和会话工厂创建成功。")
    except Exception as e:
        # 修正：调用标准化的错误日志函数，并提供更精确的上下文
        _log_db_connection_error(f"连接目标数据库 '{settings.database.name}'", e)
        raise

async def _create_db_if_not_exists():
    """如果数据库不存在，则使用 SQLAlchemy 引擎创建它。"""
    db_type = settings.database.type.lower()
    db_name = settings.database.name

    if db_type == "mysql":
        server_url = _get_db_url(include_db_name=False)
        check_sql = text(f"SHOW DATABASES LIKE '{db_name}'")
        create_sql = text(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    elif db_type == "postgresql":
        # 对于PostgreSQL，连接到默认的 'postgres' 数据库来执行创建操作
        server_url = _get_db_url(for_server=True)
        check_sql = text(f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'")
        create_sql = text(f'CREATE DATABASE "{db_name}"')
    else:
        logger.warning(f"不支持为数据库类型 '{db_type}' 自动创建数据库。请确保数据库已手动创建。")
        return

    # 设置隔离级别以允许 DDL 语句
    engine_args = {
        "echo": False,
        "isolation_level": "AUTOCOMMIT"
    }
    # 移除时区设置

    engine = create_async_engine(server_url, **engine_args)
    try:
        async with engine.connect() as conn:
            # 检查数据库是否存在
            result = await conn.execute(check_sql)
            if result.scalar_one_or_none() is None:
                logger.info(f"数据库 '{db_name}' 不存在，正在创建...")
                await conn.execute(create_sql)
                logger.info(f"数据库 '{db_name}' 创建成功。")
            else:
                logger.info(f"数据库 '{db_name}' 已存在，跳过创建。")
    except Exception as e:
        # 修正：调用标准化的错误日志函数，并提供更精确的上下文
        _log_db_connection_error("检查或创建数据库时连接服务器", e)
        raise
    finally:
        await engine.dispose()

async def get_db_session(request: Request) -> AsyncSession:
    """依赖项：从应用状态获取数据库会话"""
    session_factory = request.app.state.db_session_factory
    async with session_factory() as session:
        yield session

async def close_db_engine(app: FastAPI):
    """关闭数据库引擎"""
    if hasattr(app.state, "db_engine"):
        await app.state.db_engine.dispose()
        logger.info("数据库引擎已关闭。")

async def create_initial_admin_user(app: FastAPI):
    """在应用启动时创建初始管理员用户（如果已配置且不存在）"""
    # 将导入移到函数内部以避免循环导入
    from . import crud
    from . import models

    admin_user = settings.admin.initial_user
    if not admin_user:
        return

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        existing_user = await crud.get_user_by_username(session, admin_user)

    if existing_user:
        logger.info(f"管理员用户 '{admin_user}' 已存在，跳过创建。")
        return

    # 用户不存在，开始创建
    admin_pass = settings.admin.initial_password
    if not admin_pass:
        # 生成一个安全的16位随机密码
        alphabet = string.ascii_letters + string.digits
        admin_pass = ''.join(secrets.choice(alphabet) for _ in range(16))
        logger.info("未提供初始管理员密码，已生成随机密码。")

    user_to_create = models.UserCreate(username=admin_user, password=admin_pass)
    async with session_factory() as session:
        await crud.create_user(session, user_to_create)

    # 打印凭据信息。
    # 注意：，
    # 以确保敏感的初始密码只输出到控制台，而不会被写入到持久化的日志文件中，从而提高安全性。     
    logger.info("\n" + "="*60)
    logger.info(f"=== 初始管理员账户已创建 (用户: {admin_user}) ".ljust(56) + "===")
    logger.info(f"=== 请使用以下随机生成的密码登录: {admin_pass} ".ljust(56) + "===")
    logger.info("="*60 + "\n")
    print("\n" + "="*60)
    print(f"=== 初始管理员账户已创建 (用户: {admin_user}) ".ljust(56) + "===")
    print(f"=== 请使用以下随机生成的密码登录: {admin_pass} ".ljust(56) + "===")
    print("="*60 + "\n")

async def init_db_tables(app: FastAPI):
    """初始化数据库和表"""
    await _create_db_if_not_exists()
    await create_db_engine_and_session(app)

    engine = app.state.db_engine
    async with engine.begin() as conn:
        # 1. 首先，确保所有基于模型的表都已创建。
        # `create_all` 会安全地跳过已存在的表。
        logger.info("正在同步数据库模型，创建新表...")
        await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库模型同步完成。")

        # 2. 然后，在已存在的表结构上运行手动迁移。
        await _run_migrations(conn)
    logger.info("数据库初始化完成。")