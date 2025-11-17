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
from datetime import datetime, timezone
from typing import List, Tuple, Union
from sqlalchemy.sql import func as sa_func
import logging

api_logger = logging.getLogger("api")
DEFAULT_QUOTE_ASSET = settings.quote_asset


async def _get_balance_model(session: AsyncSession, user_uuid: UUID, ticker: str) -> UserBalance:
    
    api_logger.debug(f'Balance lock requested. User: {user_uuid}, Ticker: {ticker}.') 
    
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
        
        api_logger.debug(
            f'Attempting reserve. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. Current: Available={balance.available}, Reserved={balance.reserved}'
        )
        
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
            f'Asset reserved successfully. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. **New State: Available={balance.available}, Reserved={balance.reserved}**'
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
        
        api_logger.debug(
            f'Attempting unreserve. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. Current Reserved: {reserved_amount}'
        )
        
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
            f'Asset unreserved successfully. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. **New State: Available={balance.available}, Reserved={balance.reserved}**'
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

    api_logger.info(
        f'--- Trade execution started --- Taker ID: {taker_id_str}, Maker ID: {maker_id_str}, Base: {base_asset}, Quote: {quote_asset}, Qty: {trade_qty}, Price: {trade_price}, Cost: {cost}'
    )
    

    if taker_order.side == Side.BUY:
        reserved_to_unreserve = cost
        
        if taker_order.price is not None:
            api_logger.debug(f'Taker (BUY/Limit) unreserving: {reserved_to_unreserve} {quote_asset}')
            await async_unreserve_asset(session, taker_order.user_uuid, quote_asset, reserved_to_unreserve)
        else:
             api_logger.debug(f'Taker (BUY/Market) skips unreserving: no funds reserved.')
             
        balance_base = await _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        session.add(balance_base) 
        
        balance_quote = await _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available -= cost
        session.add(balance_quote)
        await _check_and_delete_balance(session, balance_quote) 
        
        api_logger.debug(f'Taker {taker_id_str} (BUY) processed: Got {trade_qty} {base_asset}, Paid {cost} {quote_asset}.')
        
    else:
        reserved_to_unreserve = trade_qty
        api_logger.debug(f'Taker (SELL) unreserving: {reserved_to_unreserve} {base_asset}')
        await async_unreserve_asset(session, taker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = await _get_balance_model(session, taker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        session.add(balance_base)
        await _check_and_delete_balance(session, balance_base)
        
        balance_quote = await _get_balance_model(session, taker_order.user_uuid, quote_asset)
        balance_quote.available += cost
        session.add(balance_quote)
        
        api_logger.debug(f'Taker {taker_id_str} (SELL) processed: Paid {trade_qty} {base_asset}, Got {cost} {quote_asset}.')

    if maker_order.side == Side.BUY:
        balance_base = await _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available += trade_qty
        session.add(balance_base) 
        
        reserved_to_unreserve = cost
        api_logger.debug(f'Maker (BUY) unreserving: {reserved_to_unreserve} {quote_asset}')
        await async_unreserve_asset(session, maker_order.user_uuid, quote_asset, reserved_to_unreserve)
        
        balance_quote = await _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available -= cost 
        session.add(balance_quote)
        await _check_and_delete_balance(session, balance_quote)
        
        api_logger.debug(f'Maker {maker_id_str} (BUY) processed: Got {trade_qty} {base_asset}, Paid {cost} {quote_asset}.')

    else:
        reserved_to_unreserve = trade_qty
        api_logger.debug(f'Maker (SELL) unreserving: {reserved_to_unreserve} {base_asset}')
        await async_unreserve_asset(session, maker_order.user_uuid, base_asset, reserved_to_unreserve)

        balance_base = await _get_balance_model(session, maker_order.user_uuid, base_asset)
        balance_base.available -= trade_qty
        session.add(balance_base)
        await _check_and_delete_balance(session, balance_base)
        
        balance_quote = await _get_balance_model(session, maker_order.user_uuid, quote_asset)
        balance_quote.available += cost
        session.add(balance_quote)

        api_logger.debug(f'Maker {maker_id_str} (SELL) processed: Paid {trade_qty} {base_asset}, Got {cost} {quote_asset}.')

    trade = Trade(
        order_id=taker_order.id,
        timestamp=datetime.now(timezone.utc),
        ticker=base_asset,
        quantity=trade_qty,
        price=trade_price,
    )
    session.add(trade)
    
    api_logger.info(
        f'Trade executed successfully. Ticker: {base_asset}, Qty: {trade_qty}, Price: {trade_price}, **Cost: {cost}**, Taker ID: {taker_id_str}, Maker ID: {maker_id_str}'
    )
    
    return trade


async def async_try_to_match_order(session: AsyncSession, new_order: Order) -> Tuple[List[Trade], bool]:
    
    new_order_id_str = str(new_order.id)
    user_id_str = str(new_order.user_uuid)
    
    order_type = 'LIMIT' if new_order.price is not None else 'MARKET'
    price_info = f'Price: {new_order.price}' if new_order.price is not None else 'Price: N/A'
    
    api_logger.info(f'Starting match attempt for **{order_type}** order {new_order_id_str} (User: {user_id_str}). Side: {new_order.side.value}, Qty: {new_order.qty}, {price_info}')    
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
    
    api_logger.debug(f'{len(counter_orders)} potential counter orders found for {new_order_id_str}.')
    
    remaining_qty = new_order.qty - new_order.filled
    trades = []
    
    for maker_order in counter_orders:
        if remaining_qty <= 0: break
            
        maker_remaining_qty = maker_order.qty - maker_order.filled
        if maker_remaining_qty <= 0: continue
            
        trade_qty = min(remaining_qty, maker_remaining_qty)
        
        trade_price = maker_order.price
        
        if trade_price is None: 
             api_logger.warning(f'Skipping Maker {maker_order.id} as price is None. Likely corrupted data in order book.')
             continue

        api_logger.debug(
            f'Match found. Taker: {new_order_id_str} ({remaining_qty} rem.), Maker: {maker_order.id} ({maker_remaining_qty} rem.). Trade Qty: {trade_qty}, Price: {trade_price}'
        )

        trade = await async_execute_trade(session, new_order, maker_order, trade_qty, trade_price) 
        trades.append(trade)

        new_order.filled += trade_qty
        remaining_qty -= trade_qty
        maker_order.filled += trade_qty
        
        if maker_order.filled >= maker_order.qty:
            maker_order.status = OrderStatus.EXECUTED
            api_logger.info(f'Maker Order {maker_order.id} fully executed.')
        elif maker_order.filled > 0:
            maker_order.status = OrderStatus.PARTIALLY_EXECUTED
            api_logger.debug(f'Maker Order {maker_order.id} partially executed.')
        session.add(maker_order)

    if new_order.filled >= new_order.qty:
        new_order.status = OrderStatus.EXECUTED
        api_logger.info(f'Order {new_order_id_str} fully executed. **Status: EXECUTED**. Trades: {len(trades)}')
        return trades, False
    
    if new_order.price is not None:
        new_order.status = OrderStatus.PARTIALLY_EXECUTED if new_order.filled > 0 else OrderStatus.NEW
        api_logger.info(f'Limit Order {new_order_id_str} completed matching. **Status: {new_order.status.value}**. Trades: {len(trades)}, Remaining Qty: {remaining_qty}')
        return trades, True
    else:
        new_order.status = OrderStatus.EXECUTED
        api_logger.info(f'Market Order {new_order_id_str} finished execution (status: EXECUTED). **Executed Qty: {new_order.filled}/{new_order.qty}**. Trades: {len(trades)}')
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
        
        if order.price is None:
             api_logger.info(f'Cancellation: Market BUY Order {order_id_str} has no reserved funds (amount_to_unreserve=0), skipping unreserve call.')
             
    else:
        asset_to_unreserve = order.ticker
        amount_to_unreserve = remaining_qty
        
    try:
        if amount_to_unreserve > 0:
            await async_unreserve_asset(session, order.user_uuid, asset_to_unreserve, amount_to_unreserve)
    except BalanceError as e:
        api_logger.critical(
            f'Failed to unreserve funds for order {order_id_str}. Data inconsistency suspected! Expected unreserve: {amount_to_unreserve} {asset_to_unreserve}',
            exc_info=e
        )
        raise
        
    order.status = OrderStatus.CANCELLED
    session.add(order)
    
    api_logger.info(
        f'Order successfully cancelled. Order ID: {order_id_str}, User ID: {user_uuid_str}. **Unreserved {amount_to_unreserve} {asset_to_unreserve}**'
    )