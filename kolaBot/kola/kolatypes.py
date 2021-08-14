# -*- coding: utf-8 -*-
"""Types for bitmex Kola."""
from typing import Literal, Union, Tuple

sideT = Literal["buy", "sell"]
symbT = Literal["ADAU20", "XBTUSD"]
# majuscules importantes
priceTT = Literal["lastPrice", "fairPrice", "markPrice"]
priceT = float
bipriceT = Tuple[priceT, priceT]  # couple de price

# see exec_ordesr in bitmex_api.custom_api
execTypeT = Literal["Trade", "New", "Replaced", "Canceled"]

ordStatusT = Literal["New", "Canceled", "Filled", "PartiallyFilled"]
ordStatusL = ["New", "Canceled", "Filled", "PartiallyFilled", "Triggered"]

ordTypeT = Literal["Limit", "Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"]
ordTypeL = ["Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"]
