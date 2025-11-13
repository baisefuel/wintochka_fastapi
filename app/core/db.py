from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings
from typing import AsyncGenerator

ASYNC_DATABASE_URL = settings.async_database_url

async_engine = create_async_engine(
    ASYNC_DATABASE_URL, 
    echo=False, 
    pool_pre_ping=True
) 

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(async_engine) as session:
        yield session