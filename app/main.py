from fastapi import FastAPI
from contextlib import asynccontextmanager
from sqlmodel import Session, select
from app.core.config import settings
from app.core.db import create_db_and_tables, engine
from app.api import routes_public, routes_admin, routes_trade
from app.models.user import User as UserModel, UserRole
from app.models.instrument import Instrument as InstrumentModel
from uuid import uuid4


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables() 
    
    with Session(engine) as session:
        if not session.exec(select(UserModel).where(UserModel.role == UserRole.ADMIN)).first():
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
             
        if not session.exec(select(InstrumentModel).where(InstrumentModel.ticker == "MEMCOIN")).first():
             instrument = InstrumentModel(name="Meme Coin", ticker="MEMCOIN", is_active=True)
             session.add(instrument)
             
        session.commit()
    
    yield

app = FastAPI(
    title=settings.app_name,
    version="0.2",
    lifespan=lifespan,
)

app.include_router(routes_public.router)
app.include_router(routes_trade.router)
app.include_router(routes_admin.router)