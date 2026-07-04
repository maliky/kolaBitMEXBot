"""Runtime command translation helpers.

Purpose: translate legacy order dict payloads into typed runtime commands and
derive pure validation/timeout metadata.
Inputs: `OrderDict` payloads and `RuntimeCommand` instances.
Outputs: normalised commands, role payloads, and validation hints.
Side effects: none.
Important types: `RuntimeCommand`, `RuntimeCommandKind`, `OrderDict`,
`HeadCommandPayload`, `TailCommandPayload`.
Role: transitional compatibility adapter.
Transitional: yes, still bridges legacy order function signatures.
"""
from __future__ import annotations

from decimal import Decimal
from typing import cast

from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    CancelOrderCommandRequest,
    CommandRequestRecord,
    DragonSong,
    HeadCommandPayload,
    OrderDict,
    OrderRole,
    PlaceOrderCommandRequest,
    RuntimeCommand,
    RuntimeCommandKind,
    Symbol,
    TailCommandPayload,
    ValidationCondition,
)


def runtime_command_from_order(
    *,
    symbol: str,
    order: OrderDict,
    reason: str | None = None,
) -> RuntimeCommand:
    normalized = cast(OrderDict, dict(order))
    ord_type = str(normalized.get("ordType", ""))
    request: CommandRequestRecord
    role: OrderRole
    if ord_type == "cancel":
        kind = RuntimeCommandKind.CANCEL
        command_reason = reason or OrderRole.CANCEL.value
        request = CancelOrderCommandRequest(
            pair_name=str(normalized.get("pair_name", "")),
            clOrdID=str(normalized["clOrdID"]),
        )
        role = OrderRole.CANCEL
    elif ord_type.startswith("amend"):
        kind = RuntimeCommandKind.AMEND
        command_reason = reason or OrderRole.AMEND.value
        request = AmendOrderCommandRequest(
            pair_name=str(normalized.get("pair_name", "")),
            side=str(normalized["side"]),
            ordType=ord_type,
            orderID=str(normalized["orderID"]),
            clOrdID=str(normalized.get("clOrdID", "")) or None,
            newPrice=_maybe_float_price(normalized.get("newPrice")),
            newQty=normalized.get("newQty"),
            text=str(normalized.get("text", "")) or None,
            oDelta=normalized.get("oDelta"),
        )
        role = OrderRole.AMEND
    else:
        kind = RuntimeCommandKind.PLACE
        command_reason = reason or OrderRole.HEAD.value
        request = PlaceOrderCommandRequest(
            pair_name=str(normalized.get("pair_name", "")),
            side=str(normalized["side"]),
            ordType=ord_type,
            orderQty=normalized.get("orderQty", normalized.get("quantity")),
            price=_maybe_float_price(normalized.get("price")),
            stopPx=_maybe_float_price(normalized.get("stopPx")),
            execInst=str(normalized.get("execInst", "")) or None,
            clOrdID=str(normalized.get("clOrdID", "")) or None,
            text=str(normalized.get("text", "")) or None,
            oDelta=normalized.get("oDelta"),
        )
        role = OrderRole.HEAD if command_reason == OrderRole.HEAD.value else OrderRole.TAIL
    return RuntimeCommand(
        kind=kind,
        symbol=Symbol(symbol),
        request=request,
        pair_name=str(normalized.get("pair_name", "")) or None,
        role=role,
        legacy_order=normalized,
        order=normalized,
        reason=command_reason,
    )


def timeout_override_minutes_for(command: RuntimeCommand | DragonSong) -> int | None:
    ord_type = command_order_type(command)
    if ord_type == "Market":
        return 5
    if ord_type == "cancel":
        return 1
    return None


def validation_conditions_for(
    command: RuntimeCommand | DragonSong,
    *,
    trailstop_sender: bool = False,
) -> tuple[ValidationCondition, ...]:
    ord_type = command_order_type(command)
    if ord_type.startswith("amend"):
        return ({"exectype": "Replaced", "orderstatus": "New"},)
    if ord_type == "cancel":
        return ({"exectype": "Canceled", "orderstatus": "Canceled"},)
    if ord_type in {"Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"} and trailstop_sender:
        return ({"exectype": "New", "orderstatus": "New"},)
    return ({"exectype": "Trade", "orderstatus": "Filled"},)


def command_order_type(command: RuntimeCommand | DragonSong) -> str:
    if command.request is not None:
        return command.request.ordType
    return str((command.order or {}).get("ordType", ""))


def command_payload_for_role(
    command: RuntimeCommand,
    *,
    role: OrderRole,
) -> HeadCommandPayload | TailCommandPayload:
    if command.request is not None:
        request = _request_dict_from_record(command.request)
    else:
        order = cast(OrderDict, dict(command.order or {}))
        if command.kind == RuntimeCommandKind.CANCEL:
            request = {
                "ordType": "cancel",
                "clOrdID": str(order["clOrdID"]),
            }
        elif command.kind == RuntimeCommandKind.AMEND:
            amend_request: dict[str, object] = {
                "ordType": str(order["ordType"]),
                "side": str(order["side"]),
                "orderID": str(order["orderID"]),
                "text": str(order.get("text", "")),
            }
            if "newPrice" in order:
                amend_request["newPrice"] = _as_float_price(order["newPrice"])
            if "newQty" in order:
                amend_request["newQty"] = order["newQty"]
            if "oDelta" in order:
                amend_request["oDelta"] = order["oDelta"]
            request = amend_request
        else:
            request = _new_order_request_from(command)
    payload = {
        "role": role.value,
        "command": command.kind,
        "request": request,
    }
    if role == OrderRole.TAIL:
        return cast(TailCommandPayload, payload)
    return cast(HeadCommandPayload, payload)


def _new_order_request_from(command: RuntimeCommand) -> dict[str, object]:
    order = cast(OrderDict, dict(command.order or {}))
    request: dict[str, object] = {
        "ordType": str(order["ordType"]),
        "side": str(order["side"]),
    }
    if "orderQty" in order:
        request["orderQty"] = order["orderQty"]
    if "quantity" in order:
        request["quantity"] = order["quantity"]
    for key in ("price", "stopPx", "execInst", "clOrdID", "text", "oDelta"):
        if key in order:
            request[key] = order[key]
    return request


def _request_dict_from_record(
    request: PlaceOrderCommandRequest | AmendOrderCommandRequest | CancelOrderCommandRequest,
) -> dict[str, object]:
    if isinstance(request, CancelOrderCommandRequest):
        return {"ordType": request.ordType, "clOrdID": request.clOrdID}
    if isinstance(request, AmendOrderCommandRequest):
        payload: dict[str, object] = {
            "ordType": request.ordType,
            "side": request.side,
            "orderID": request.orderID,
            "text": request.text or "",
        }
        if request.newPrice is not None:
            payload["newPrice"] = _as_float_price(request.newPrice)
        if request.newQty is not None:
            payload["newQty"] = request.newQty
        if request.clOrdID is not None:
            payload["clOrdID"] = request.clOrdID
        if request.oDelta is not None:
            payload["oDelta"] = request.oDelta
        return payload
    payload = {
        "ordType": request.ordType,
        "side": request.side,
    }
    if request.orderQty is not None:
        payload["orderQty"] = request.orderQty
    if request.price is not None:
        payload["price"] = _as_float_price(request.price)
    if request.stopPx is not None:
        payload["stopPx"] = _as_float_price(request.stopPx)
    if request.execInst is not None:
        payload["execInst"] = request.execInst
    if request.clOrdID is not None:
        payload["clOrdID"] = request.clOrdID
    if request.text is not None:
        payload["text"] = request.text
    if request.oDelta is not None:
        payload["oDelta"] = request.oDelta
    return payload


def _as_float_price(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str)):
        return float(Decimal(str(value)))
    raise TypeError(f"Unsupported price value type: {type(value)!r}")


def _maybe_float_price(value: object | None) -> float | None:
    if value is None:
        return None
    return _as_float_price(value)


__all__ = [
    "command_payload_for_role",
    "runtime_command_from_order",
    "timeout_override_minutes_for",
    "validation_conditions_for",
]
