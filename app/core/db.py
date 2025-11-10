from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings
from typing import Generator

DATABASE_URL = settings.database_url
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True) 

def create_db_and_tables():
    from app.models.user import User, UserBalance
    from app.models.instrument import Instrument
    from app.models.order import Order, Trade
    
    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session