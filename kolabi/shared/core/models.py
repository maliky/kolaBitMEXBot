from dataclasses import dataclass
from typing import Optional

from kolabi.shared.core.runtime_types import OrderID, OrderQty, Price, Symbol


@dataclass
class OrderAck:
    """Simple acknowledgement for order operations."""
    order_id: OrderID | str
    status: str
    price: Optional[Price | float] = None
    orig_qty: Optional[OrderQty | float] = None
    executed_qty: Optional[OrderQty | float] = None
    side: Optional[str] = None
    client_order_id: Optional[str] = None
    reason: Optional[str] = None

@dataclass
class Position:
    """Simplified position information."""
    symbol: Symbol | str
    qty: OrderQty | float
    entry_price: Optional[Price | float] = None
