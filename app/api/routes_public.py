from fastapi import APIRouter, Depends, HTTPException, Path, Query
from typing import List
from uuid import uuid4
from sqlmodel import Session, select, and_, asc, desc
from sqlalchemy.sql import func as sa_func
from datetime import datetime
from pydantic import BaseModel, Field

from app.core.db import get_session
from app.schemas.openapi_schemas import (
    NewUser,
    User as UserSchema,
    Instrument as InstrumentSchema,
    L2OrderBook,
    Level,
    Transaction,
    OrderStatus,
)
from app.models.user import User as UserModel, UserRole
from app.models.instrument import Instrument as InstrumentModel
from app.models.order import Trade as TradeModel, Order, Side

router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.post("/register",
             response_model=UserSchema,
             summary="Register",
             description="Регистрация пользователя в платформе. Обязательна для совершения сделок.\napi_key полученный из этого метода следует передавать в другие через заголовок Authorization.\n\nНапример для api_key='key-...' знаначение будет таким:\n\nAuthorization: TOKEN key-...",
)
def register(body: NewUser, session: Session = Depends(get_session)):
    api_key = f"key-{uuid4()}"
    user = UserModel(name=body.name, api_key=api_key, role=UserRole.USER, is_active=True) 
    session.add(user)
    session.commit()
    session.refresh(user)

    return UserSchema(id=user.uuid, name=user.name, role=user.role, api_key=user.api_key)


@router.get("/instrument", 
            response_model=List[InstrumentSchema], 
            summary="List Instruments", 
            description="Список доступных инструментов",)
def list_instruments(session: Session = Depends(get_session)):
    rows = session.exec(select(InstrumentModel).where(InstrumentModel.is_active == True)).all()
    return [InstrumentSchema(name=r.name, ticker=r.ticker) for r in rows]


@router.get("/orderbook/{ticker}",
             response_model=L2OrderBook, 
             summary="Get Orderbook",
             description="Текущие заявки",
)
def get_orderbook_public(
    ticker: str = Path(..., title="Ticker"),
    limit: int = Query(10, le=25, title="Limit"),
    session: Session = Depends(get_session)
):
    remaining_qty = Order.qty - Order.filled
    
    bids_query = (
        select(
            Order.price.label("price"),
            sa_func.sum(remaining_qty).label("qty")
        )
        .where(
            and_(
                Order.ticker == ticker,
                Order.side == Side.BUY,
                Order.status == OrderStatus.NEW,
                remaining_qty > 0
            )
        )
        .group_by(Order.price)
        .order_by(desc(Order.price))
        .limit(limit)
    )
    
    asks_query = (
        select(
            Order.price.label("price"),
            sa_func.sum(remaining_qty).label("qty")
        )
        .where(
            and_(
                Order.ticker == ticker,
                Order.side == Side.SELL,
                Order.status == OrderStatus.NEW,
                remaining_qty > 0
            )
        )
        .group_by(Order.price)
        .order_by(asc(Order.price))
        .limit(limit)
    )

    bids_results = session.exec(bids_query).mappings().all()
    asks_results = session.exec(asks_query).mappings().all()

    bid_levels = [Level(price=r['price'], qty=r['qty']) for r in bids_results]
    ask_levels = [Level(price=r['price'], qty=r['qty']) for r in asks_results]
    
    return L2OrderBook(
        bid_levels=bid_levels,
        ask_levels=ask_levels
    )


@router.get(
    "/transactions/{ticker}",
    response_model=List[Transaction],
    summary="Get Transaction History",
    description="История сделок",
)
def get_transaction_history(
    ticker: str = Path(..., title="Ticker"),
    limit: int = Query(10, le=100, title="Limit"),
    session: Session = Depends(get_session),
):
    rows = session.exec(
        select(TradeModel)
        .where(TradeModel.ticker == ticker)
        .order_by(TradeModel.timestamp.desc())
        .limit(limit)
    ).all()

    result = []
    for r in rows:
        result.append(
            Transaction(
                ticker=r.ticker,
                amount=int(r.quantity),
                price=int(r.price),
                timestamp=r.timestamp,
            )
        )
    return result