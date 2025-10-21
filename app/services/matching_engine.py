from sqlmodel import Session, select
from app.models.order import Order, Side
from app.models.trade import Trade


class MatchingEngine:

    def __init__(self, db: Session):
        self.db = db

    def process_order(self, order: Order) -> list[Trade]:

        trades: list[Trade] = []

        opposite_side = Side.BUY if order.side == Side.SELL else Side.SELL

        query = select(Order).where(
            Order.ticker == order.ticker,
            Order.side == opposite_side,
            Order.filled < Order.quantity,
        )

        if order.side == Side.BUY:
            query = query.order_by(Order.price.asc(), Order.created_at.asc())
        else:
            query = query.order_by(Order.price.desc(), Order.created_at.asc())

        candidates = self.db.exec(query).all()

        for counter in candidates:
            if (order.side == Side.BUY and order.price < counter.price) or \
               (order.side == Side.SELL and order.price > counter.price):
                continue

            quantity = min(order.remaining, counter.remaining)
            if quantity <= 0:
                continue

            trade_price = counter.price

            trade = Trade(
                ticker=order.ticker,
                price=trade_price,
                quantity=quantity,
                buy_order_id=order.id if order.side == Side.BUY else counter.id,
                sell_order_id=order.id if order.side == Side.SELL else counter.id,
            )
            trades.append(trade)

            order.filled += quantity
            counter.filled += quantity

            self.db.add(trade)
            self.db.add(order)
            self.db.add(counter)
            self.db.commit()

            if order.remaining <= 0:
                break

        return trades
