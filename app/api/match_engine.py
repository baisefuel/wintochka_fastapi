from sqlalchemy import select, or_, update as sa_update
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.order import Order, Side, Trade 
from app.models.user import UserBalance
from sqlalchemy.exc import DBAPIError
from app.schemas.openapi_schemas import OrderStatus
from app.core.config import settings 
from app.crud.balance import BalanceError, _check_and_delete_balance 

from uuid import UUID
from datetime import datetime, timezone
from typing import List, Tuple, Union, Optional
import asyncio
import random
import logging

api_logger = logging.getLogger("api")
DEFAULT_QUOTE_ASSET = settings.quote_asset

MAX_RETRIES = 5

async def async_reserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: 
        return

    stmt = (
        sa_update(UserBalance)
        .where(
            UserBalance.user_uuid == user_uuid,
            UserBalance.ticker == ticker,
            UserBalance.available >= amount  
        )
        .values(
            available=UserBalance.available - amount,
            reserved=UserBalance.reserved + amount
        )
    )

    result = await session.execute(stmt)

    if result.rowcount == 0:
        current_balance: Optional[UserBalance] = (await session.exec(
            select(UserBalance).where(
                UserBalance.user_uuid == user_uuid, 
                UserBalance.ticker == ticker
            )
        )).scalars().first()
        
        available = current_balance.available if current_balance else 0
        error_msg = f"Insufficient available {ticker}. Need {amount}, have {available}."
        
        api_logger.critical(f"Asset reservation failed. User: {user_uuid}, Ticker: {ticker}. Detail: {error_msg}")
        raise BalanceError(error_msg)
        
    api_logger.info(
        f'Asset reserved successfully. User: {user_uuid}, Ticker: {ticker}, Amount: {amount}.'
    )


async def async_unreserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: 
        return
    
    stmt = (
        sa_update(UserBalance)
        .where(
            UserBalance.user_uuid == user_uuid,
            UserBalance.ticker == ticker,
            UserBalance.reserved >= amount
        )
        .values(
            reserved=UserBalance.reserved - amount,
            available=UserBalance.available + amount
        )
    )
    
    result = await session.execute(stmt)

    if result.rowcount == 0:
        error_msg = f"Unreserve failed: Balance record not found or insufficient reserved {ticker}."
        api_logger.critical(f"Asset unreserve failed. User: {user_uuid}, Ticker: {ticker}. Detail: {error_msg}")
        raise BalanceError(error_msg)
        
    updated_balance: Optional[UserBalance] = (await session.exec(
        select(UserBalance).where(UserBalance.user_uuid == user_uuid, UserBalance.ticker == ticker)
    )).scalars().first()
    
    if updated_balance:
        await _check_and_delete_balance(session, updated_balance) 

    api_logger.info(
        f'Asset unreserved successfully. User: {user_uuid}, Ticker: {ticker}, Amount: {amount}.'
    )


async def async_execute_trade(session: AsyncSession,
                              taker_order: Order, 
                              maker_order: Order, 
                              trade_qty: int, 
                              trade_price: int) -> Trade:
    
    base_asset = taker_order.ticker
    quote_asset = DEFAULT_QUOTE_ASSET
    cost = trade_qty * trade_price
    
    taker_id = taker_order.user_uuid
    maker_id = maker_order.user_uuid
    
    if taker_order.side == Side.BUY:
        buyer_order, seller_order = taker_order, maker_order
    else:
        buyer_order, seller_order = maker_order, taker_order
        
    buyer_id = buyer_order.user_uuid
    seller_id = seller_order.user_uuid


    is_buyer_taker = (buyer_order.id == taker_order.id)
    balance_field_buyer = UserBalance.available if is_buyer_taker else UserBalance.reserved
    
    stmt_debit_quote = (
        sa_update(UserBalance)
        .where(
            UserBalance.user_uuid == buyer_id, 
            UserBalance.ticker == quote_asset,
            balance_field_buyer >= cost 
        )
        .values({
            balance_field_buyer.key: balance_field_buyer - cost
        })
    )
    result = await session.execute(stmt_debit_quote)
    if result.rowcount == 0:
        api_logger.critical(f"Balance check failed: Buyer {buyer_id} insufficient {quote_asset} (Debit Field={balance_field_buyer.key}). Need {cost}.")
        raise BalanceError(f"Buyer {buyer_id} insufficient {quote_asset} for trade.")

    stmt_credit_base = (
        sa_update(UserBalance)
        .where(UserBalance.user_uuid == buyer_id, UserBalance.ticker == base_asset)
        .values(available=UserBalance.available + trade_qty)
    )
    await session.execute(stmt_credit_base)
    

    is_seller_taker = (seller_order.id == taker_order.id)
    balance_field_seller = UserBalance.available if is_seller_taker else UserBalance.reserved
    
    stmt_debit_base = (
        sa_update(UserBalance)
        .where(
            UserBalance.user_uuid == seller_id, 
            UserBalance.ticker == base_asset,
            balance_field_seller >= trade_qty
        )
        .values({
            balance_field_seller.key: balance_field_seller - trade_qty
        })
    )
    result = await session.execute(stmt_debit_base)
    if result.rowcount == 0:
        api_logger.critical(f"Balance check failed: Seller {seller_id} insufficient {base_asset} (Debit Field={balance_field_seller.key}). Need {trade_qty}.")
        raise BalanceError(f"Seller {seller_id} insufficient {base_asset} for trade.")
    
    stmt_credit_quote = (
        sa_update(UserBalance)
        .where(UserBalance.user_uuid == seller_id, UserBalance.ticker == quote_asset)
        .values(available=UserBalance.available + cost)
    )
    await session.execute(stmt_credit_quote)
    
    trade = Trade(
        order_id=taker_order.id,
        timestamp=datetime.now(timezone.utc),
        ticker=base_asset,
        quantity=trade_qty,
        price=trade_price,
    )
    session.add(trade)
    
    api_logger.info(
        f'Trade executed successfully. Taker Order ID: {taker_order.id}, Maker Order ID: {maker_order.id}'
    )
    
    return trade


async def async_try_to_match_order(session: AsyncSession, new_order: Order) -> Tuple[List[Trade], bool]:
    
    new_order_id_str = str(new_order.id)
    user_id_str = str(new_order.user_uuid)
    
    order_type = 'LIMIT' if new_order.price is not None else 'MARKET'
    price_info = f'Price: {new_order.price}' if new_order.price is not None else 'Price: N/A'
    
    api_logger.info(
        f'START MATCHING: {order_type} order {new_order_id_str} (User: {user_id_str}). Side: {new_order.side.value}, Qty: {new_order.qty}, {price_info}'
    )
    
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
    
    api_logger.debug(f'Found {len(counter_orders)} potential counter orders for {new_order_id_str}.')
    
    remaining_qty = new_order.qty - new_order.filled
    trades = []
    
    for maker_order in counter_orders:
        if remaining_qty <= 0: break
            
        maker_remaining_qty = maker_order.qty - maker_order.filled
        if maker_remaining_qty <= 0: continue
            
        trade_qty = min(remaining_qty, maker_remaining_qty)
        trade_price = maker_order.price
        
        if trade_price is None: 
            api_logger.warning(f'Skipping Maker {maker_order.id}: Price is None. Potential data corruption.')
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
        session.add(new_order)
        
    
    
    is_open = False
    
    if new_order.filled >= new_order.qty:
        new_order.status = OrderStatus.EXECUTED
        api_logger.info(f'FINISH MATCHING: Order {new_order_id_str} fully executed. **Status: EXECUTED**. Trades: {len(trades)}')
    
    elif new_order.price is not None:
        
        new_order.status = OrderStatus.PARTIALLY_EXECUTED if new_order.filled > 0 else OrderStatus.NEW
        
        if remaining_qty > 0:
            if new_order.side == Side.BUY:
                asset = DEFAULT_QUOTE_ASSET
                amount = remaining_qty * new_order.price
            else:
                asset = new_order.ticker
                amount = remaining_qty
            
            try:
                 await async_reserve_asset(session, new_order.user_uuid, asset, amount)
                 is_open = True
            except BalanceError:
                new_order.status = OrderStatus.CANCELLED
                api_logger.critical(f'Failed to reserve remaining funds for {new_order.id}. Cancelling order.')
                is_open = False
            
        final_status = new_order.status.value
        api_logger.info(
            f'FINISH MATCHING: Limit Order {new_order_id_str} completed matching. **Status: {final_status}**. Trades: {len(trades)}, Remaining Qty: {remaining_qty}'
        )
        
    else:
        if new_order.filled > 0:
            new_order.status = OrderStatus.PARTIALLY_EXECUTED 
            log_status = 'PARTIALLY EXECUTED'
        else:
            new_order.status = OrderStatus.CANCELLED
            log_status = 'CANCELLED (No match found)'
            
        api_logger.info(
            f'FINISH MATCHING: Market Order {new_order_id_str} finished execution. Status: {log_status}. Executed Qty: {new_order.filled}/{new_order.qty}. Trades: {len(trades)}'
        )

    session.add(new_order)
    return trades, is_open


async def async_execute_match_with_retry(session: AsyncSession, new_order: Order) -> Tuple[List[Trade], bool]:
    order_id_log = str(new_order.id)
    
    for attempt in range(MAX_RETRIES):
        try:
            trades, is_open = await async_try_to_match_order(session, new_order)
            await session.commit()
            
            api_logger.info(f"Order matching completed successfully after {attempt + 1} attempt(s). Order ID: {order_id_log}")
            return trades, is_open
        
        except DBAPIError as e:
            await session.rollback()
            
            if 'deadlock detected' in str(e).lower() or 'was deadlocked' in str(e).lower():
                
                if attempt < MAX_RETRIES - 1:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    api_logger.warning(
                        f"Deadlock detected for order {order_id_log} on attempt {attempt + 1}. Retrying in {delay:.2f} seconds."
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    api_logger.error(f"Deadlock persists after {MAX_RETRIES} attempts. Failing order matching for {order_id_log}.")
                    raise
            else:
                api_logger.error(f"Non-deadlock DBAPIError during matching for {order_id_log}: {e}")
                raise

        except BalanceError as e:
            await session.rollback()
            api_logger.error(f"BalanceError during matching for {order_id_log}: {e}")
            raise

        except Exception as e:
            await session.rollback()
            api_logger.error(f"Unexpected error during matching for {order_id_log}: {e}")
            raise


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
    
    asset_to_unreserve = ""
    amount_to_unreserve = 0
    
    if order.side == Side.BUY:
        asset_to_unreserve = DEFAULT_QUOTE_ASSET
        amount_to_unreserve = remaining_qty * (order.price or 0)
    else:
        asset_to_unreserve = order.ticker
        amount_to_unreserve = remaining_qty
        
    try:
        if amount_to_unreserve > 0:
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
        f'Order successfully cancelled. Order ID: {order_id_str}, User ID: {user_uuid_str}. **Unreserved {amount_to_unreserve} {asset_to_unreserve}**'
    )