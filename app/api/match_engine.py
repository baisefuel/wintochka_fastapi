from sqlmodel import Session, select
from app.models.order import Order, Side, Trade
from app.models.user import UserBalance
from app.schemas.openapi_schemas import OrderStatus
from app.core.config import settings
from uuid import UUID
from datetime import datetime
from typing import List, Tuple

DEFAULT_QUOTE_ASSET = settings.quote_asset

class BalanceError(Exception):
    pass


def _get_balance_model(session: Session, user_uuid: UUID, ticker: str) -> UserBalance:
    balance = session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == user_uuid, 
            UserBalance.ticker == ticker
        )
    ).first()
    
    if balance is None:
        balance = UserBalance(user_uuid=user_uuid, ticker=ticker, available=0, reserved=0)
        session.add(balance)
        session.commit()
        session.refresh(balance)
        
    return balance


def reserve_asset(session: Session, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return

    balance = _get_balance_model(session, user_uuid, ticker)
    
    if balance.available < amount:
        raise BalanceError(f"Insufficient available {ticker}. Need {amount}, have {balance.available}")
        
    balance.available -= amount
    balance.reserved += amount
    session.add(balance)


def unreserve_asset(session: Session, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return

    balance = _get_balance_model(session, user_uuid, ticker)
    
    if balance.reserved < amount:
        raise BalanceError(f"Insufficient reserved {ticker}. Need {amount}, have {balance.reserved}")
        
    balance.reserved -= amount
    balance.available += amount
    session.add(balance)


def execute_trade(session: Session, 
                  taker_order: Order, 
                  maker_order: Order, 
                  trade_qty: int, 
                  trade_price: int) -> Trade:
    
    base_asset = taker_order.ticker
    quote_asset = DEFAULT_QUOTE_ASSET
    cost = trade_qty * trade_price

    
    if taker_order.side == Side.BUY:
        reserved_to_unreserve = trade_qty * taker_order.price
        unreserve_asset(session, taker_order.user_uuid, quote_asset, reserved_to_unreserve)
        
        balance_base = _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        
        balance_quote = _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available -= cost
        
    else:
        reserved_to_unreserve = trade_qty
        unreserve_asset(session, taker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        
        balance_quote = _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available += cost

    
    if maker_order.side == Side.BUY:
        reserved_to_unreserve = trade_qty * maker_order.price
        unreserve_asset(session, maker_order.user_uuid, quote_asset, reserved_to_unreserve)
        
        balance_base = _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        
        balance_quote = _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available -= cost 
        
    else:
        reserved_to_unreserve = trade_qty
        unreserve_asset(session, maker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        
        balance_quote = _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available += cost

    trade = Trade(
        order_id=taker_order.id,
        timestamp=datetime.utcnow(),
        ticker=base_asset,
        quantity=trade_qty,
        price=trade_price,
    )
    session.add(trade)
    return trade


def try_to_match_order(session: Session, new_order: Order) -> Tuple[List[Trade], bool]:
    
    is_buy = new_order.side == Side.BUY
    
    opposite_side = Side.SELL if is_buy else Side.BUY
    
    query = select(Order).where(
        Order.ticker == new_order.ticker,
        Order.side == opposite_side,
        Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED]),
    ).order_by(
        Order.price.asc() if is_buy else Order.price.desc(),
        Order.timestamp.asc() 
    )
    
    if new_order.price is not None:
        if is_buy:
            query = query.where(Order.price <= new_order.price)
        else:
            query = query.where(Order.price >= new_order.price)
            
    
    counter_orders = session.exec(query).all()
    
    remaining_qty = new_order.qty - new_order.filled
    trades = []
    
    for maker_order in counter_orders:
        if remaining_qty <= 0: break
            
        maker_remaining_qty = maker_order.qty - maker_order.filled
        if maker_remaining_qty <= 0: continue
            
        trade_qty = min(remaining_qty, maker_remaining_qty)
        
        trade_price = maker_order.price
        if trade_price is None: 
            continue

        trade = execute_trade(session, new_order, maker_order, trade_qty, trade_price)
        trades.append(trade)

        new_order.filled += trade_qty
        remaining_qty -= trade_qty
        maker_order.filled += trade_qty
        
        if maker_order.filled >= maker_order.qty:
            maker_order.status = OrderStatus.EXECUTED
        elif maker_order.filled > 0:
            maker_order.status = OrderStatus.PARTIALLY_EXECUTED
        session.add(maker_order)

    if new_order.filled >= new_order.qty:
        new_order.status = OrderStatus.EXECUTED
        return trades, False
    
    if new_order.price is not None:
        new_order.status = OrderStatus.PARTIALLY_EXECUTED if new_order.filled > 0 else OrderStatus.NEW
        return trades, True
    else:
        new_order.status = OrderStatus.EXECUTED
        return trades, False


def cancel_order_and_unreserve(session: Session, order: Order):
    
    if order.status in [OrderStatus.EXECUTED, OrderStatus.CANCELLED]:
        raise BalanceError("Order is already executed or cancelled.")
        
    remaining_qty = order.qty - order.filled
    
    if order.side == Side.BUY:
        asset_to_unreserve = DEFAULT_QUOTE_ASSET
        amount_to_unreserve = remaining_qty * order.price
    else:
        asset_to_unreserve = order.ticker
        amount_to_unreserve = remaining_qty
        
    try:
        unreserve_asset(session, order.user_uuid, asset_to_unreserve, amount_to_unreserve)
    except BalanceError as e:
        print(f"Error unreserving funds for order {order.id}: {e}")

    order.status = OrderStatus.CANCELLED
    session.add(order)