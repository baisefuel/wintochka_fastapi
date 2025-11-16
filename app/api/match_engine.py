from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.order import Order, Side, Trade
from app.models.user import UserBalance
from app.schemas.openapi_schemas import OrderStatus
from app.core.config import settings 
from app.crud.balance import (
    BalanceError,
    _check_and_delete_balance
)

from uuid import UUID
from datetime import datetime
from typing import List, Tuple, Union
from sqlalchemy.sql import func as sa_func
import logging

api_logger = logging.getLogger("api")
DEFAULT_QUOTE_ASSET = settings.quote_asset


async def _get_balance_model(session: AsyncSession, user_uuid: UUID, ticker: str) -> UserBalance:
    
    balance: Union[UserBalance, None] = (await session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == user_uuid, 
            UserBalance.ticker == ticker
        ).with_for_update() 
    )).scalars().first()
    
    if balance is None:
        balance = UserBalance(user_uuid=user_uuid, ticker=ticker, available=0, reserved=0)
        session.add(balance)
        await session.flush() 
        
    return balance


async def async_reserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return

    user_id_str = str(user_uuid)
    
    try:
        balance = await _get_balance_model(session, user_uuid, ticker)
        
        if balance.available < amount:
            error_msg = f"Insufficient available {ticker}. Need {amount}, have {balance.available}"
            api_logger.warning(
                f'Balance reserve failed. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}, Detail: {error_msg}'
            )
            raise BalanceError(error_msg)
            
        balance.available -= amount
        balance.reserved += amount
        session.add(balance)
        
        api_logger.info(
            f'Asset reserved successfully. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}'
        )
    except BalanceError:
        raise
    except Exception as e:
        api_logger.error(f'DB error during asset reservation for user {user_id_str}', exc_info=e)
        raise


async def async_unreserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return
    
    user_id_str = str(user_uuid)

    try:
        balance = await _get_balance_model(session, user_uuid, ticker)
        reserved_amount = balance.reserved
        
        if reserved_amount < amount:
            error_msg = f"Insufficient reserved {ticker}. Need {amount}, have {reserved_amount}"
            api_logger.error(
                f'Asset unreserve failed: reserved amount mismatch. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}, Detail: {error_msg}'
            )
            raise BalanceError(error_msg)
            
        balance.reserved -= amount
        balance.available += amount
        session.add(balance)
        await _check_and_delete_balance(session, balance) 
        api_logger.info(
            f'Asset unreserved successfully. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}'
        )
    except BalanceError:
        raise
    except Exception as e:
        api_logger.error(f'DB error during asset unreservation for user {user_id_str}', exc_info=e)
        raise


async def async_execute_trade(session: AsyncSession,
                              taker_order: Order, 
                              maker_order: Order, 
                              trade_qty: int, 
                              trade_price: int) -> Trade:
    
    base_asset = taker_order.ticker
    quote_asset = DEFAULT_QUOTE_ASSET
    cost = trade_qty * trade_price
    
    taker_id_str = str(taker_order.id)
    maker_id_str = str(maker_order.id)
    
    if taker_order.side == Side.BUY:
        reserved_to_unreserve = trade_qty * taker_order.price
        await async_unreserve_asset(session, taker_order.user_uuid, quote_asset, reserved_to_unreserve)
        
        balance_base = await _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        session.add(balance_base) 
        
        balance_quote = await _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available -= cost
        session.add(balance_quote)
        await _check_and_delete_balance(session, balance_quote)   
    else:    
        reserved_to_unreserve = trade_qty
        await async_unreserve_asset(session, taker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = await _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        session.add(balance_base)
        await _check_and_delete_balance(session, balance_base)
        
        balance_quote = await _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available += cost
        session.add(balance_quote)

    
    if maker_order.side == Side.BUY:
        balance_base = await _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        session.add(balance_base) 
        
        reserved_to_unreserve = trade_qty * maker_order.price
        await async_unreserve_asset(session, maker_order.user_uuid, quote_asset, reserved_to_unreserve)
        
        balance_quote = await _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available -= cost 
        session.add(balance_quote)
        await _check_and_delete_balance(session, balance_quote)
    else: 
        reserved_to_unreserve = trade_qty
        await async_unreserve_asset(session, maker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = await _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        session.add(balance_base)
        await _check_and_delete_balance(session, balance_base)
        
        balance_quote = await _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available += cost
        session.add(balance_quote)

    
    trade = Trade(
        order_id=taker_order.id,
        timestamp=datetime.utcnow(),
        ticker=base_asset,
        quantity=trade_qty,
        price=trade_price,
    )
    session.add(trade)
    
    api_logger.info(
        f'Trade executed successfully. Ticker: {base_asset}, Qty: {trade_qty}, Price: {trade_price}, Taker ID: {taker_id_str}, Maker ID: {maker_id_str}'
    )
    
    return trade


async def async_try_to_match_order(session: AsyncSession, new_order: Order) -> Tuple[List[Trade], bool]:
    
    new_order_id_str = str(new_order.id)
    
    api_logger.info(f'Starting match attempt for order {new_order_id_str}')
    
    is_buy = new_order.side == Side.BUY
    opposite_side = Side.SELL if is_buy else Side.BUY
    
    query = select(Order).where(
        Order.ticker == new_order.ticker,
        Order.side == opposite_side,
        Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED]),
    ).order_by(
        Order.price.asc() if is_buy else Order.price.desc(),
        Order.timestamp.asc() 
    ).with_for_update()
    
    if new_order.price is not None:
        if is_buy:
            query = query.where(Order.price <= new_order.price)
        else:
            query = query.where(Order.price >= new_order.price)
            
    counter_orders = (await session.exec(query)).scalars().all()
    
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

        trade = await async_execute_trade(session, new_order, maker_order, trade_qty, trade_price) 
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
        api_logger.info(f'Order {new_order_id_str} fully executed. Trades: {len(trades)}')
        return trades, False
    
    if new_order.price is not None:
        new_order.status = OrderStatus.PARTIALLY_EXECUTED if new_order.filled > 0 else OrderStatus.NEW
        api_logger.info(f'Order {new_order_id_str} partially matched or remains new. Trades: {len(trades)}')
        return trades, True
    else:
        new_order.status = OrderStatus.EXECUTED
        api_logger.info(f'Market Order {new_order_id_str} executed with available liquidity. Trades: {len(trades)}')
        return trades, False


async def async_cancel_order_and_unreserve(session: AsyncSession, order: Order): 
    order_id_str = str(order.id)
    user_uuid_str = str(order.user_uuid)
    
    if order.status in [OrderStatus.EXECUTED, OrderStatus.CANCELLED]:
        error_msg = f"Order {order_id_str} is already {order.status.value}."
        api_logger.warning(
            f'Cancellation attempt failed. Order ID: {order_id_str}, User ID: {user_uuid_str}, Detail: {error_msg}'
        )
        raise BalanceError(error_msg)
        
    remaining_qty = order.qty - order.filled
    
    if order.side == Side.BUY:
        asset_to_unreserve = DEFAULT_QUOTE_ASSET
        amount_to_unreserve = remaining_qty * (order.price or 0)
    else:
        asset_to_unreserve = order.ticker
        amount_to_unreserve = remaining_qty
        
    try:
        await async_unreserve_asset(session, order.user_uuid, asset_to_unreserve, amount_to_unreserve)
    except BalanceError as e:
        api_logger.critical(
            f'Failed to unreserve funds for order {order_id_str}. Data inconsistency suspected!',
            exc_info=e
        )
        raise
        
    order.status = OrderStatus.CANCELLED
    session.add(order)
    
    api_logger.info(
        f'Order successfully cancelled and funds unreserved. Order ID: {order_id_str}, User ID: {user_uuid_str}'
    )