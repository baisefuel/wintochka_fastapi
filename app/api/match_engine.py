from sqlalchemy import select, or_
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.order import Order, Side, Trade
from app.models.user import UserBalance
from sqlalchemy.exc import DBAPIError
from app.schemas.openapi_schemas import OrderStatus
from app.core.config import settings 
from app.crud.balance import (
    BalanceError,
    _check_and_delete_balance
)

from uuid import UUID
from datetime import datetime, timezone
from typing import List, Tuple, Union, Optional
import asyncio
import random
import logging

api_logger = logging.getLogger("api")
DEFAULT_QUOTE_ASSET = settings.quote_asset

BalanceDict = dict[Tuple[UUID, str], UserBalance]


async def _get_and_lock_balances_canonical(
    session: AsyncSession, 
    taker_user_uuid: UUID, 
    maker_user_uuid: UUID, 
    base_asset: str, 
    quote_asset: str
) -> Tuple[BalanceDict, List[UserBalance]]:

    keys_to_lock = [
        (taker_user_uuid, base_asset),
        (taker_user_uuid, quote_asset),
        (maker_user_uuid, base_asset),
        (maker_user_uuid, quote_asset),
    ]
    
    canonical_keys = sorted(keys_to_lock, key=lambda x: (x[1], x[0])) 
    
    conditions = []
    for user_uuid, ticker in canonical_keys:
        conditions.append((UserBalance.user_uuid == user_uuid) & (UserBalance.ticker == ticker))

    query = (
        select(UserBalance)
        .where(or_(*conditions))
        .order_by(UserBalance.ticker.asc(), UserBalance.user_uuid.asc())
        .with_for_update()
    )
    
    api_logger.debug(
        f'Canonical lock requested for 4 balances. Taker: {taker_user_uuid}, Maker: {maker_user_uuid}'
    )
    
    result = await session.exec(query)
    locked_balances = result.scalars().all()

    balances_map: BalanceDict = {}
    new_balances = []

    for b in locked_balances:
        balances_map[(b.user_uuid, b.ticker)] = b
    
    for user_uuid, ticker in keys_to_lock:
        key = (user_uuid, ticker)
        if key not in balances_map:
            balance = UserBalance(user_uuid=user_uuid, ticker=ticker, available=0, reserved=0)
            session.add(balance)
            balances_map[key] = balance
            new_balances.append(balance)
            
    api_logger.debug(
        f'Canonical lock acquired. Locked {len(locked_balances)} existing, added {len(new_balances)} new balances.'
    )
    
    return balances_map, new_balances


async def async_reserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return

    user_id_str = str(user_uuid)
    
    api_logger.debug(f'Balance lock requested for reserve. User: {user_uuid}, Ticker: {ticker}.')
    balance: Optional[UserBalance] = (await session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == user_uuid, 
            UserBalance.ticker == ticker
        ).with_for_update() 
    )).scalars().first()

    if balance is None:
        balance = UserBalance(user_uuid=user_uuid, ticker=ticker, available=0, reserved=0)
        session.add(balance)
        await session.flush()
    
    if balance.available < amount:
        error_msg = f"Insufficient available {ticker}. Need {amount}, have {balance.available}"
        raise BalanceError(error_msg)
        
    balance.available -= amount
    balance.reserved += amount
    session.add(balance)
    
    api_logger.info(
        f'Asset reserved successfully. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. **New State: Available={balance.available}, Reserved={balance.reserved}**'
    )


async def async_unreserve_asset(session: AsyncSession, user_uuid: UUID, ticker: str, amount: int):
    if amount <= 0: return
    
    user_id_str = str(user_uuid)
    
    api_logger.debug(f'Balance lock requested for unreserve. User: {user_uuid}, Ticker: {ticker}.')
    balance: Optional[UserBalance] = (await session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == user_uuid, 
            UserBalance.ticker == ticker
        ).with_for_update() 
    )).scalars().first()
    
    if not balance:
        error_msg = f"Balance record not found for unreserve: {ticker}"
        api_logger.error(error_msg)
        raise BalanceError(error_msg)

    reserved_amount = balance.reserved
    
    api_logger.debug(
        f'Attempting unreserve. User: {user_id_str}, Ticker: {ticker}, Amount: {amount}. Current Reserved: {reserved_amount}'
    )
    
    if reserved_amount < amount:
        error_msg = f"Insufficient reserved {ticker}. Need {amount}, have {reserved_amount}"
        api_logger.critical(
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


def _calculate_balance_deltas(
    taker_order: Order, 
    maker_order: Order, 
    trade_qty: int, 
    trade_price: int, 
    quote_asset: str
) -> dict[Tuple[UUID, str], dict[str, int]]:
    base_asset = taker_order.ticker    
    cost = trade_qty * trade_price
    changes = {}

    def get_or_init_changes(user_uuid: UUID, ticker: str):
        key = (user_uuid, ticker)
        if key not in changes:
            changes[key] = {'available_delta': 0, 'reserved_delta': 0} 
        return changes[key]

    base_taker = get_or_init_changes(taker_order.user_uuid, taker_order.ticker)
    quote_taker = get_or_init_changes(taker_order.user_uuid, quote_asset)

    api_logger.debug(f'Calculating deltas. Qty: {trade_qty}, Price: {trade_price}, Cost: {cost}')

    if taker_order.side == Side.BUY:
        
        base_taker['available_delta'] += trade_qty
        
        if taker_order.price is not None:
            quote_taker['reserved_delta'] -= cost
            quote_taker['available_delta'] += cost
            quote_taker['available_delta'] -= cost
            
            api_logger.debug(f'Taker (BUY/Limit) - Reserved: -{cost} {quote_asset}, Available: 0 {quote_asset}')
            
        else:
            quote_taker['available_delta'] -= cost 
            api_logger.debug(f'Taker (BUY/Market) - Reserved: 0, Available: -{cost} {quote_asset}')
            
    else:
        
        if taker_order.price is not None:
            base_taker['reserved_delta'] -= trade_qty
            base_taker['available_delta'] += trade_qty
            base_taker['available_delta'] -= trade_qty
            
            api_logger.debug(f'Taker (SELL/Limit) - Reserved: -{trade_qty} {base_asset}, Available: 0 {base_asset}')
            
        else:
            base_taker['available_delta'] -= trade_qty
            api_logger.debug(f'Taker (SELL/Market) - Reserved: 0, Available: -{trade_qty} {base_asset}')
            
        quote_taker['available_delta'] += cost
        

    base_maker = get_or_init_changes(maker_order.user_uuid, maker_order.ticker)
    quote_maker = get_or_init_changes(maker_order.user_uuid, quote_asset)

    
    if maker_order.side == Side.BUY:
        
        base_maker['available_delta'] += trade_qty
        
        quote_maker['reserved_delta'] -= cost
        quote_maker['available_delta'] += cost
        quote_maker['available_delta'] -= cost
        
        api_logger.debug(f'Maker (BUY) - Reserved: -{cost} {quote_asset}, Available: 0 {quote_asset}')
            
    else:
        
        base_maker['reserved_delta'] -= trade_qty
        base_maker['available_delta'] += trade_qty
        base_maker['available_delta'] -= trade_qty

        quote_maker['available_delta'] += cost
        
        api_logger.debug(f'Maker (SELL) - Reserved: -{trade_qty} {base_asset}, Available: 0 {base_asset}')
        
    return changes

async def async_execute_trade(session: AsyncSession,
                              taker_order: Order, 
                              maker_order: Order, 
                              trade_qty: int, 
                              trade_price: int) -> Trade:
    
    base_asset = taker_order.ticker
    quote_asset = DEFAULT_QUOTE_ASSET
    
    taker_id_str = str(taker_order.id)
    maker_id_str = str(maker_order.id)
    
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            api_logger.info(
                f'--- Trade execution started --- Taker ID: {taker_id_str}, Maker ID: {maker_id_str}, Qty: {trade_qty}, Price: {trade_price}. Attempt: {attempt + 1}'
            )
            
            balances, new_balances = await _get_and_lock_balances_canonical(
                session, 
                taker_order.user_uuid, 
                maker_order.user_uuid, 
                base_asset, 
                quote_asset
            )
            
            if new_balances:
                await session.flush()
            
            changes = _calculate_balance_deltas(
                taker_order, maker_order, trade_qty, trade_price, quote_asset
            )
                        
            for key, delta in changes.items():
                balance = balances[key]
                
                new_reserved = balance.reserved + delta['reserved_delta']
                new_available = balance.available + delta['available_delta']
                
                if new_available < 0:
                     error_msg = f"Insufficient available {balance.ticker} after trade calculation. Need to debit, but result is negative: {new_available}"
                     api_logger.critical(error_msg)
                     raise BalanceError(error_msg)
                
                if new_reserved < 0:
                     error_msg = f"Reserved balance became negative after trade calculation for {balance.ticker}. Value: {new_reserved}"
                     api_logger.critical(error_msg)
                     raise BalanceError(error_msg)
                
                balance.reserved = new_reserved
                balance.available = new_available
                
                session.add(balance)
                await _check_and_delete_balance(session, balance)
            
            trade = Trade(
                order_id=taker_order.id,
                timestamp=datetime.now(timezone.utc),
                ticker=base_asset,
                quantity=trade_qty,
                price=trade_price,
            )
            session.add(trade)
            
            api_logger.info(
                f'Trade executed successfully. Taker ID: {taker_id_str}, Maker ID: {maker_id_str}'
            )
            
            return trade

        except DBAPIError as e:
            if "deadlock detected" in str(e).lower() and attempt < max_retries - 1:
                await session.rollback() 
                
                wait_time = random.uniform(0.01, 0.1) * (2 ** attempt)
                api_logger.warning(
                    f'DEADLOCK DETECTED in async_execute_trade. Retrying in {wait_time:.4f}s... Attempt: {attempt + 1}'
                )
                await asyncio.sleep(wait_time)
                continue
            else:
                api_logger.error(
                    f'Critical DB error during trade execution for Taker {taker_id_str}', exc_info=e
                )
                raise
        
        except BalanceError:
            raise 

    raise Exception(f"Failed to execute trade for Taker {taker_id_str} after {max_retries} attempts due to deadlocks.")


async def async_try_to_match_order(session: AsyncSession, new_order: Order) -> Tuple[List[Trade], bool]:
    
    new_order_id_str = str(new_order.id)
    user_id_str = str(new_order.user_uuid)
    
    order_type = 'LIMIT' if new_order.price is not None else 'MARKET'
    price_info = f'Price: {new_order.price}' if new_order.price is not None else 'Price: N/A'
    
    if new_order.price is not None and new_order.status == OrderStatus.NEW:
        if new_order.side == Side.BUY:
            asset = DEFAULT_QUOTE_ASSET
            amount = new_order.qty * new_order.price
        else:
            asset = new_order.ticker
            amount = new_order.qty
                
        await async_reserve_asset(session, new_order.user_uuid, asset, amount)
        api_logger.debug(f'Asset reserved: {amount} {asset} for new Limit Order {new_order.id}.')
        
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

        api_logger.debug(
            f'Match executed. Taker: {new_order_id_str} ({remaining_qty} rem.), Maker: {maker_order.id} ({maker_remaining_qty} rem.). Trade Qty: {trade_qty}, Price: {trade_price}'
        )

        trade = await async_execute_trade(session, new_order, maker_order, trade_qty, trade_price) 
        trades.append(trade)

        new_order.filled += trade_qty
        remaining_qty -= trade_qty
        maker_order.filled += trade_qty
        
        if maker_order.filled >= maker_order.qty:
            maker_order.status = OrderStatus.EXECUTED
            api_logger.debug(f'Maker Order {maker_order.id} fully executed (status: EXECUTED).')
        elif maker_order.filled > 0:
            maker_order.status = OrderStatus.PARTIALLY_EXECUTED
            
        session.add(maker_order)
        session.add(new_order)
        
    
    if new_order.filled >= new_order.qty:
        new_order.status = OrderStatus.EXECUTED
        api_logger.info(f'FINISH MATCHING: Order {new_order_id_str} fully executed. **Status: EXECUTED**. Trades: {len(trades)}')
        return trades, False
    
    if new_order.price is not None:
        
        new_order.status = OrderStatus.PARTIALLY_EXECUTED if new_order.filled > 0 else OrderStatus.NEW
        final_status = new_order.status.value
        
        api_logger.info(
            f'FINISH MATCHING: Limit Order {new_order_id_str} completed matching. **Status: {final_status}**. Trades: {len(trades)}, Remaining Qty: {remaining_qty}'
        )
        
        return trades, True
    
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