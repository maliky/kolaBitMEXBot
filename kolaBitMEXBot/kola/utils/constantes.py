# -*- coding: utf-8 -*-
"""Constantes"""

# some default cols
EXECOLS = [
    "orderID",
    "clOrdID",
    "side",
    "orderQty",
    "price",
    "stopPx",
    "execType",
    "ordType",
    "execInst",
    "ordStatus",
    "triggered",
    "transactTime",
]

EXECOLS_L = EXECOLS + ["lastQty", "lastPx", "lastMkt", "commission"]

# to get price in bargain.price
SETTLEMENTPRICES = {"XBTUSD": ".BXBT", "ADAM20": ".BADAXBT30M"}

# used to round price before passing orders
PRICE_PRECISION = {"XBTUSD": 0.5, "ADAM20": 1e-8}

# used in condition
PRICELISTDFT = [
    "indexPrice",
    "markPrice",
    "lastPrice",
    "lastMidPrice",
    "bidPrice",
    "askPrice",
]
