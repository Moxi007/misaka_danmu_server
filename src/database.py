import secrets
import string
import logging
from fastapi import FastAPI, Request
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from .config import settings
from .orm_models import Base

# 使用模块级日志记录器
logger = logging.getLogger(__name__)

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
            sources_res = await conn.execute(text(f"SELECT id FROM anime_sources WHERE anime_id = {anime_id} ORDER BY id"))
            sources_ids = sources_res.scalars().all()
            for i, source_id in enumerate(sources_ids):
                order = i + 1
                await conn.execute(text(f"UPDATE anime_sources SET source_order = {order} WHERE id = {source_id}"))
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
    db_type = settings.database.type.lower()
    if db_type == "mysql":
        db_url = URL.create(
            drivername="mysql+aiomysql",
            username=settings.database.user,
            password=settings.database.password,
            host=settings.database.host,
            port=settings.database.port,
            database=settings.database.name,
            query={"charset": "utf8mb4"},
        )
    elif db_type == "postgresql":
        db_url = URL.create(
            drivername="postgresql+asyncpg",
            username=settings.database.user,
            password=settings.database.password,
            host=settings.database.host,
            port=settings.database.port,
            database=settings.database.name,
        )
    else:
        raise ValueError(f"不支持的数据库类型: '{db_type}'。请使用 'mysql' 或 'postgresql'。")
    try:
        engine = create_async_engine(
            db_url,
            echo=False,
            pool_recycle=3600,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30
        )
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
        # 创建一个不带数据库名称的连接URL
        server_url = URL.create(
            drivername="mysql+aiomysql",
            username=settings.database.user,
            password=settings.database.password,
            host=settings.database.host,
            port=settings.database.port,
            query={"charset": "utf8mb4"},
        )
        check_sql = text(f"SHOW DATABASES LIKE '{db_name}'")
        create_sql = text(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    elif db_type == "postgresql":
        # 对于PostgreSQL，连接到默认的 'postgres' 数据库来执行创建操作
        server_url = URL.create(
            drivername="postgresql+asyncpg",
            username=settings.database.user,
            password=settings.database.password,
            host=settings.database.host,
            port=settings.database.port,
            database="postgres",
        )
        check_sql = text(f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'")
        create_sql = text(f'CREATE DATABASE "{db_name}"')
    else:
        logger.warning(f"不支持为数据库类型 '{db_type}' 自动创建数据库。请确保数据库已手动创建。")
        return

    # 设置隔离级别以允许 DDL 语句
    engine = create_async_engine(server_url, echo=False, isolation_level="AUTOCOMMIT")
    
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