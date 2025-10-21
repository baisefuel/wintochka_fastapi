from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select
from typing import List
from app.models.order import Order, Side
from app.models.trade import Trade
from app.core.db import get_session
from app.services.matching_engine import MatchingEngine

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.post("/", response_model=Order, status_code=status.HTTP_201_CREATED)
def create_order(order: Order, db: Session = Depends(get_session)):
    db.add(order)
    db.commit()
    db.refresh(order)

    engine = MatchingEngine(db)
    trades = engine.process_order(order)

    return order


@router.get("/", response_model=List[Order])
def list_orders(db: Session = Depends(get_session)):
    orders = db.exec(select(Order)).all()
    return orders


@router.get("/book/{ticker}")
def get_orderbook(ticker: str, db: Session = Depends(get_session)):
    buys = db.exec(
        select(Order).where(Order.ticker == ticker, Order.side == Side.BUY)
    ).all()
    sells = db.exec(
        select(Order).where(Order.ticker == ticker, Order.side == Side.SELL)
    ).all()

    return {
        "ticker": ticker,
        "bids": sorted([{"price": o.price, "quantity": o.remaining} for o in buys],
                       key=lambda x: x["price"], reverse=True),
        "asks": sorted([{"price": o.price, "quantity": o.remaining} for o in sells],
                       key=lambda x: x["price"]),
    }
