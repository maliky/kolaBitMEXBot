# -*- coding: utf-8 -*-
"""Types for bitmex Kola."""
from typing import Literal

sideT = Literal['buy', 'sell']
symbT = Literal['XBTUSD']
# majuscules importantes
priceTT = Literal['LastPrice', 'IndexPrice', 'MarkPrice']

# see exec_ordesr in custom_bitmex
execTypeT = Literal['Trade', 'New', 'Replaced', 'Canceled']

ordStatusT = Literal['New', 'Canceled', 'Filled', 'PartiallyFilled']
ordStatusL = ['New', 'Canceled', 'Filled', 'PartiallyFilled', "Triggered"]

ordTypeT = Literal['Stop', 'MarketIfTouched', 'StopLimit', 'LimitIfTouched']
ordTypeL = ['Stop', 'MarketIfTouched', 'StopLimit', 'LimitIfTouched']
