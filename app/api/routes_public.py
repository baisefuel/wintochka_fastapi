from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from typing import List
from uuid import uuid4
from sqlalchemy import select, and_, asc, desc, func as sa_func 
from sqlmodel.ext.asyncio.session import AsyncSession
from datetime import datetime, timezone
import logging

from app.core.db import get_async_session
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

api_logger = logging.getLogger("api")

router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.post("/register",
             response_model=UserSchema,
             summary="Register",
             description="Регистрация пользователя в платформе. Обязательна для совершения сделок.",
             status_code=status.HTTP_200_OK,
)
async def register(body: NewUser, session: AsyncSession = Depends(get_async_session)):
    try:
        api_key = f"key-{uuid4()}"
        user = UserModel(name=body.name, api_key=api_key, role=UserRole.USER, is_active=True) 
        session.add(user)
        user_name = user.name
        await session.commit()
        await session.refresh(user)

        api_logger.info(
            f'User registered successfully. User ID: {user.uuid}, Name: {user_name}'
        )

        return UserSchema(id=user.uuid, name=user.name, role=user.role, api_key=user.api_key)
    
    except Exception as e:
        await session.rollback() 
        
        api_logger.error(
            f'Failed to register user: {body.name}', 
            exc_info=e
        )
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error during user registration."
        )


@router.get("/instrument", 
             response_model=List[InstrumentSchema], 
             summary="List Instruments", 
             description="Список доступных инструментов",)
async def list_instruments(session: AsyncSession = Depends(get_async_session)):
    try:
        rows = (await session.exec(select(InstrumentModel).where(InstrumentModel.is_active == True))).scalars().all()
        
        api_logger.info('Successfully retrieved instrument list.')
        
        return [InstrumentSchema(name=r.name, ticker=r.ticker) for r in rows]
    except Exception as e:
        api_logger.error('Failed to retrieve instruments.', exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving instrument list."
        )


@router.get("/orderbook/{ticker}",
            response_model=L2OrderBook, 
            summary="Get Orderbook",
            description="Текущие заявки",
)
async def get_orderbook_public(
    ticker: str = Path(..., title="Ticker"),
    limit: int = Query(10, le=25, title="Limit"),
    session: AsyncSession = Depends(get_async_session)
):
    try:
        instrument = (
            await session.exec(
                select(InstrumentModel).where(InstrumentModel.ticker == ticker)
            )
        ).scalars().first()
        
        if not instrument:
            api_logger.warning(f'Orderbook request failed: Instrument {ticker} not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found.")

        
        remaining_qty_expr = Order.qty - Order.filled
        
        price_is_not_null = Order.price.is_not(None) 
        
        bids_query = (
            select(
                Order.price.label("price"),
                sa_func.sum(remaining_qty_expr).label("qty")
            )
            .where(
                and_(
                    Order.ticker == ticker,
                    Order.side == Side.BUY,
                    Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED]), 
                    remaining_qty_expr > 0,
                    price_is_not_null
                )
            )
            .group_by(Order.price)
            .order_by(desc(Order.price))
            .limit(limit)
        )
        
        asks_query = (
            select(
                Order.price.label("price"),
                sa_func.sum(remaining_qty_expr).label("qty")
            )
            .where(
                and_(
                    Order.ticker == ticker,
                    Order.side == Side.SELL,
                    Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED]),
                    remaining_qty_expr > 0,
                    price_is_not_null
                )
            )
            .group_by(Order.price)
            .order_by(asc(Order.price))
            .limit(limit)
        )

        bids_results = (await session.exec(bids_query)).mappings().all()
        asks_results = (await session.exec(asks_query)).mappings().all()

        api_logger.info(
            f'Successfully retrieved orderbook for {ticker}. Bid levels: {len(bids_results)}, Ask levels: {len(asks_results)}'
        )
        
        bid_levels = [Level(price=r['price'], qty=r['qty']) for r in bids_results]
        ask_levels = [Level(price=r['price'], qty=r['qty']) for r in asks_results]
        
        return L2OrderBook(
            bid_levels=bid_levels,
            ask_levels=ask_levels
        )
        
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f'Failed to retrieve orderbook for {ticker}', exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving orderbook data."
        )


@router.get(
    "/transactions/{ticker}",
    response_model=List[Transaction],
    summary="Get Transaction History",
    description="История сделок",
)
async def get_transaction_history(
    ticker: str = Path(..., title="Ticker"),
    limit: int = Query(10, le=100, title="Limit"),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        instrument = (
            await session.exec(
                select(InstrumentModel).where(InstrumentModel.ticker == ticker)
            )
        ).scalars().first()
        
        if not instrument:
            api_logger.warning(f'Transaction history request failed: Instrument {ticker} not found.')
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, 
                detail=f"Instrument {ticker} not found."
            )

        rows = (await session.exec(
            select(TradeModel)
            .where(TradeModel.ticker == ticker)
            .order_by(TradeModel.timestamp.desc())
            .limit(limit)
        )).scalars().all()

        result = [
            Transaction(
                ticker=r.ticker,
                amount=int(r.quantity),
                price=int(r.price),
                timestamp=r.timestamp,
            )
            for r in rows
        ]
        
        api_logger.info(
            f'Transaction history successfully retrieved. Ticker: {ticker}, Limit: {limit}, Count: {len(result)}'
        )
        
        return result
        
    except HTTPException:
        raise
        
    except Exception as e:
        api_logger.error(
            f'CRITICAL FAILURE to retrieve transaction history. Ticker: {ticker}, Limit: {limit}.', 
            exc_info=e
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving transaction history."
        )