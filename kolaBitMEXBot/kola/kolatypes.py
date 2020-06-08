# -*- coding: utf-8 -*-
"""Types for bitmex Kola."""
from typing import Literal, Union, Tuple

sideT = Literal["buy", "sell"]
symbT = Literal["ADAM20", "XBTUSD"]
# majuscules importantes
priceTT = Literal["LastPrice", "IndexPrice", "MarkPrice"]
priceT = Union[float, int]
sideT = Literal["buy", "sell"]
bipriceT = Tuple[priceT, priceT]  # couple de price

# see exec_ordesr in custom_bitmex
execTypeT = Literal["Trade", "New", "Replaced", "Canceled"]

ordStatusT = Literal["New", "Canceled", "Filled", "PartiallyFilled"]
ordStatusL = ["New", "Canceled", "Filled", "PartiallyFilled", "Triggered"]

ordTypeT = Literal["Limit", "Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"]
ordTypeL = ["Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"]
