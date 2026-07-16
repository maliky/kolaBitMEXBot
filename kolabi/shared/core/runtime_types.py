"""Canonical runtime scalar, enum, payload, and protocol types.

Purpose: define typed boundaries and shared runtime vocabulary across bot,
shared adapters, and legacy runtime migration layers.
Inputs: none (type declarations only).
Outputs: exported `NewType`s, enums, typed dicts, dataclasses, and protocols.
Side effects: none.
Important types: scalar families (`Price`, `OrderQty`, IDs, times), command and
event enums, algebraic bot commands, `RuntimeEvent`, `OrderDict`.
Role: pure logic (type contract module).
Transitional: yes, includes compatibility aliases while legacy surfaces migrate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping, NewType, Protocol, TypedDict

if TYPE_CHECKING:
    from kolabi.shared.core.models import OrderAck

DecimalLike = Decimal | int | float | str

Symbol = NewType("Symbol", str)
ClOrdID = NewType("ClOrdID", str)
OrderID = NewType("OrderID", str)
ExecID = NewType("ExecID", str)
PairID = NewType("PairID", str)

Price = NewType("Price", Decimal)
TriggerPrice = NewType("TriggerPrice", Decimal)
LimitPrice = NewType("LimitPrice", Decimal)
StopPrice = NewType("StopPrice", Decimal)
PriceOffset = NewType("PriceOffset", Decimal)
TickSize = NewType("TickSize", Decimal)

OrderQty = NewType("OrderQty", Decimal)
FilledQty = NewType("FilledQty", Decimal)
RemainingQty = NewType("RemainingQty", Decimal)
ContractSize = NewType("ContractSize", Decimal)
MinQty = NewType("MinQty", Decimal)

ExchangeTime = NewType("ExchangeTime", datetime)
DecisionTime = NewType("DecisionTime", datetime)
SubmissionTime = NewType("SubmissionTime", datetime)

ClientOrderId = ClOrdID
ExchangeOrderId = OrderID
Quantity = OrderQty | FilledQty | RemainingQty | ContractSize | MinQty | Decimal | float | int
PriceLike = Price | TriggerPrice | LimitPrice | StopPrice | PriceOffset | Decimal | float


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert runtime numeric input to Decimal at the pure-logic boundary."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def decimal_to_float(value: DecimalLike) -> float:
    """Convert Decimal-backed runtime math to float at the exchange boundary."""
    return float(to_decimal(value))


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderRole(StrEnum):
    HEAD = "head"
    TAIL = "tail"
    HOOK = "hook"
    AMEND = "amend"
    CANCEL = "cancel"


class OrderStatus(StrEnum):
    NEW = "New"
    FILLED = "Filled"
    CANCELED = "Canceled"
    TRIGGERED = "Triggered"
    REPLACED = "Replaced"
    PARTIALLY_FILLED = "PartiallyFilled"


class TriggerKind(StrEnum):
    PRICE_BAND = "price_band"
    STOP = "stop"
    TIME_WINDOW = "time_window"
    HOOK = "hook"
    MANUAL = "manual"


class AmendReason(StrEnum):
    PRICE_REQUOTE = "price_requote"
    PARTIAL_FILL_REBALANCE = "partial_fill_rebalance"
    TRAILING_UPDATE = "trailing_update"
    RISK_REDUCTION = "risk_reduction"
    OPERATOR_REQUEST = "operator_request"


class ExchangeName(StrEnum):
    KRAKEN = "kraken"
    BITMEX = "bitmex"
    BINANCE = "binance"


class EnvironmentName(StrEnum):
    DEMO = "demo"
    TESTNET = "testnet"
    LIVE = "live"


class ExecType(StrEnum):
    NEW = "New"
    TRADE = "Trade"
    CANCELED = "Canceled"
    REPLACED = "Replaced"
    TRIGGERED = "TriggeredOrActivatedBySystem"


class RuntimeEventKind(StrEnum):
    ORDER_REQUESTED = "order_requested"
    ORDER_ACK = "order_ack"
    ORDER_VALIDATED = "order_validated"
    PRICE_TICK = "price_tick"
    TIMER = "timer"
    ERROR = "error"


class RuntimeCommandKind(StrEnum):
    PLACE = "place"
    AMEND = "amend"
    CANCEL = "cancel"
    VALIDATE = "validate"
    NOOP = "noop"


class OrderDict(TypedDict, total=False):
    pair_name: str
    side: str
    action: str
    orderQty: Quantity
    quantity: Quantity
    price: PriceLike
    stopPx: StopPrice | Decimal | float
    stopPrice: StopPrice | Decimal | float
    ordType: str
    execInst: str
    clOrdID: str
    orderID: str
    newPrice: LimitPrice | StopPrice | Decimal | float
    newQty: Quantity
    text: str | None
    oDelta: PriceOffset | Decimal | float
    cumQty: FilledQty | Decimal | float
    executedQty: FilledQty | Decimal | float
    filledQty: FilledQty | Decimal | float


class NewOrderRequest(TypedDict, total=False):
    side: str
    ordType: str
    orderQty: Quantity
    quantity: Quantity
    price: PriceLike
    stopPx: StopPrice | Decimal | float
    execInst: str
    clOrdID: str
    text: str | None
    oDelta: PriceOffset | Decimal | float


class AmendOrderRequest(TypedDict, total=False):
    ordType: str
    side: str
    orderID: str
    newPrice: LimitPrice | StopPrice | Decimal | float
    newQty: Quantity
    text: str | None
    oDelta: PriceOffset | Decimal | float


class CancelOrderRequest(TypedDict):
    ordType: str
    clOrdID: str


OrderRequest = NewOrderRequest | AmendOrderRequest | CancelOrderRequest


@dataclass(frozen=True)
class PlaceOrderCommandRequest:
    pair_name: str
    side: str
    ordType: str
    orderQty: Quantity | None = None
    price: PriceLike | None = None
    stopPx: StopPrice | Decimal | float | None = None
    execInst: str | None = None
    clOrdID: str | None = None
    text: str | None = None
    oDelta: PriceOffset | Decimal | float | None = None


@dataclass(frozen=True)
class AmendOrderCommandRequest:
    pair_name: str
    side: str
    ordType: str
    orderID: str
    clOrdID: str | None = None
    newPrice: LimitPrice | StopPrice | Decimal | float | None = None
    newQty: Quantity | None = None
    text: str | None = None
    oDelta: PriceOffset | Decimal | float | None = None


@dataclass(frozen=True)
class CancelOrderCommandRequest:
    pair_name: str
    clOrdID: str
    ordType: str = "cancel"


CommandRequestRecord = (
    PlaceOrderCommandRequest | AmendOrderCommandRequest | CancelOrderCommandRequest
)


class OrderLoad(TypedDict):
    sender: object
    timeOut: object
    symbol: str
    order: OrderDict


class BrokerReply(TypedDict, total=False):
    orderID: OrderID | str
    clOrdID: ClOrdID | str
    ordStatus: str
    execType: str
    side: str
    error: object
    transactTime: ExchangeTime | str
    price: Price | float
    stopPx: StopPrice | float
    orderQty: Quantity
    cumQty: Quantity
    executedQty: Quantity
    filledQty: Quantity


class SubmissionAck(TypedDict, total=False):
    orderID: OrderID | str
    clOrdID: ClOrdID | str
    ordStatus: str
    execType: str
    error: object


class ExecutionUpdate(TypedDict, total=False):
    orderID: OrderID | str
    clOrdID: ClOrdID | str
    ordStatus: str
    execType: str
    cumQty: Quantity
    executedQty: Quantity
    filledQty: Quantity
    transactTime: ExchangeTime | str


class PositionUpdate(TypedDict, total=False):
    symbol: str
    size: ContractSize | float
    entry_price: Price | float
    updated_at: ExchangeTime | str


class HeadCommandPayload(TypedDict):
    role: str
    command: RuntimeCommandKind
    request: NewOrderRequest | AmendOrderRequest | CancelOrderRequest


class TailCommandPayload(TypedDict):
    role: str
    command: RuntimeCommandKind
    request: NewOrderRequest | AmendOrderRequest | CancelOrderRequest


class ValidationCondition(TypedDict):
    exectype: str
    orderstatus: str


class ValidationLoad(TypedDict):
    brokerReply: BrokerReply | bool | None
    exgLoad: OrderLoad
    execValidation: BrokerReply | bool


@dataclass(frozen=True)
class PublicBookRecord:
    symbol: str
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    spread: float | None
    imbalance: float | None
    avg_bid: float | None
    avg_ask: float | None
    recorded_at: str | None
    source_timestamp: str | None


@dataclass(frozen=True)
class PublicIndicatorRecord:
    symbol: str
    name: str
    value: float
    recorded_at: str | None


@dataclass(frozen=True)
class PrivateOrderRecord:
    symbol: str
    status: str
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    reason: str | None = None
    is_cancel: bool | None = None
    side: str | None = None
    order_type: str | None = None
    price: float | None = None
    stop_price: float | None = None
    quantity: float | None = None
    filled_quantity: float | None = None
    source_timestamp: str | None = None
    local_timestamp: str | None = None
    local_id: int | None = None
    exchange: str | None = None
    market_type: str | None = None


@dataclass(frozen=True)
class PrivateFillRecord:
    symbol: str
    exchange: str | None = None
    market_type: str | None = None


@dataclass(frozen=True)
class PrivatePositionRecord:
    symbol: str
    size: float | None
    entry_price: float | None
    exchange: str | None = None
    market_type: str | None = None


class CryptoApiLike(Protocol):
    dummy: bool
    dummyID: str

    def exec_orders(self) -> Iterable[BrokerReply]: ...


class BargainLike(Protocol):
    """Legacy broker-facing negotiation protocol kept for compatibility.

    Why this name:
    - `Bargain` keeps the strategic metaphor: negotiation with the market.
    - This protocol describes the older broker-facing surface
      (price read, balance, execution lookup, position/open-order access).
    - The active Chronos/Isis/Horus path does not call this protocol directly;
      live execution goes through `DragonSong`, `CommandExecutor`, and
      `ExchangePort`.
    - It is intentionally structural: any object exposing this surface can be
      used without explicit inheritance.
    """

    symbol: str
    crypto_api: CryptoApiLike

    def prices(self, price_type: str | None = None, side: str | None = None) -> float: ...
    def get_balance(self, ref: str | None = None) -> float: ...
    def minimum_order_quantity(self, symbol: str | None = None) -> float: ...
    def execution(self, *args: object, **kwargs: object) -> object: ...
    def get_exec_clID_with_(self, *args: object, **kwargs: object) -> Iterable[str]: ...
    def get_open_orders(self, *args: object, **kwargs: object) -> object: ...
    def get_position(self, *args: object, **kwargs: object) -> object: ...
    def order_reached_status(self, *args: object, **kwargs: object) -> bool: ...


@dataclass(frozen=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    at: datetime
    symbol: Symbol
    order: OrderDict | None = None
    reply: BrokerReply | None = None
    note: str | None = None


@dataclass(frozen=True)
class PlaceHeadCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    request: PlaceOrderCommandRequest
    role: OrderRole = OrderRole.HEAD
    reason: str = OrderRole.HEAD.value
    legacy_order: OrderDict | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))


@dataclass(frozen=True)
class PlaceTailCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    request: PlaceOrderCommandRequest
    role: OrderRole = OrderRole.TAIL
    reason: str = OrderRole.TAIL.value
    legacy_order: OrderDict | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))


@dataclass(frozen=True)
class AmendTailCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    request: AmendOrderCommandRequest
    role: OrderRole = OrderRole.TAIL
    reason: str = OrderRole.TAIL.value
    legacy_order: OrderDict | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))


@dataclass(frozen=True)
class AmendHeadCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    request: AmendOrderCommandRequest
    role: OrderRole = OrderRole.HEAD
    reason: str = OrderRole.HEAD.value
    legacy_order: OrderDict | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))


@dataclass(frozen=True)
class CancelCommand:
    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    request: CancelOrderCommandRequest
    role: OrderRole = OrderRole.CANCEL
    reason: str = OrderRole.CANCEL.value
    legacy_order: OrderDict | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))


DragonSong = PlaceHeadCommand | PlaceTailCommand | AmendHeadCommand | AmendTailCommand | CancelCommand


@dataclass(frozen=True)
class VerifyHeadVisibilityCommand:
    """Low-priority REST query used only to enrich head-order visibility."""

    kind: RuntimeCommandKind
    symbol: Symbol
    pair_name: str
    attempt_index: int
    request: PlaceOrderCommandRequest
    exchange_order_id: str | None = None
    exchange: str = ""
    market_type: str = "futures"


@dataclass(frozen=True)
class HeadVisibilityResult:
    """One-shot REST visibility evidence; never a pair-lifecycle event."""

    pair_name: str
    attempt_index: int
    checked_at: datetime
    visible: bool
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    status: str | None = None
    price: PriceLike | None = None
    quantity: Quantity | None = None
    error: str | None = None


OgunCommand = DragonSong | VerifyHeadVisibilityCommand


@dataclass(frozen=True)
class RuntimeCommand:
    """Legacy permissive command carrier kept only for non-bot transitional code."""

    kind: RuntimeCommandKind
    symbol: Symbol
    request: CommandRequestRecord | None = None
    pair_name: str | None = None
    role: OrderRole | None = None
    legacy_order: OrderDict | None = None
    order: OrderDict | None = None
    reason: str | None = None
    exchange: str = ""
    market_type: str = "futures"

    def __post_init__(self) -> None:
        if self.legacy_order is not None and not isinstance(self.legacy_order, MappingProxyType):
            object.__setattr__(self, "legacy_order", MappingProxyType(dict(self.legacy_order)))
        if self.order is not None and not isinstance(self.order, MappingProxyType):
            object.__setattr__(self, "order", MappingProxyType(dict(self.order)))


@dataclass(frozen=True)
class RuntimeState:
    """Minimal runtime shell state for orchestration layers.

    Vocabulary choice:
    - Keep metaphorical orchestration terms at the boundary (`Chronos`,
      `Bargain`, `head`, `tail`).
    - Keep strategy lifecycle enums in `kolabi.bot.domain`.
    - `OrderStatus` remains the raw exchange order status family.
    """

    symbol: Symbol
    active_order_id: ClientOrderId | None = None
    active_exchange_order_id: ExchangeOrderId | None = None
    status: OrderStatus | None = None
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class ExchangePort(Protocol):
    """Abstract async exchange boundary consumed by OgunExecutor."""

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck: ...
    async def place_tail(self, command: PlaceTailCommand) -> OrderAck: ...
    async def amend_head(self, command: AmendHeadCommand) -> OrderAck: ...
    async def amend_tail(self, command: AmendTailCommand) -> OrderAck: ...
    async def cancel(self, command: CancelCommand) -> OrderAck: ...
    async def verify_head_visibility(
        self,
        command: VerifyHeadVisibilityCommand,
    ) -> HeadVisibilityResult: ...
