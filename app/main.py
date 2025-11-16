from fastapi import FastAPI
from contextlib import asynccontextmanager
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.core.logging_config import setup_logging
from app.core.config import settings
from app.core.db import async_engine 
from app.api import routes_public, routes_admin, routes_trade
from app.models.user import User as UserModel, UserRole
from app.models.instrument import Instrument as InstrumentModel
from uuid import uuid4
setup_logging() 

import logging
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncSession(async_engine) as session:
        
        admin_check = await session.exec(
            select(UserModel).where(UserModel.role == UserRole.ADMIN)
        )
        if not admin_check.first():
            admin_key = f"key-{uuid4()}" 
            admin = UserModel(
                name="Admin User", 
                role=UserRole.ADMIN, 
                api_key=admin_key, 
                is_active=True
            )
            session.add(admin)
            print(f"\n--- ⚠️ TEST ADMIN CREATED ---")
            print(f"API KEY: TOKEN {admin_key}\n")
            
        instrument_check = await session.exec(
            select(InstrumentModel).where(InstrumentModel.ticker == "RUB")
        )
        if not instrument_check.first():
            instrument = InstrumentModel(name="Ruble", ticker="RUB", is_active=True)
            session.add(instrument)
            
        await session.commit()
    
    yield

app = FastAPI(
    title=settings.app_name,
    version="0.2",
    lifespan=lifespan,
)

app.include_router(routes_public.router)
app.include_router(routes_trade.router)
app.include_router(routes_admin.router)