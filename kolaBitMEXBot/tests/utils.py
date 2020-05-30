# -*- coding: utf-8 -*-
"""Test utilities"""
# file:///usr/share/doc/python3.5/html/library/unittest.html?highlight=test#module-unittest
import logging
from typing import Literal
from pandas import DataFrame
from time import sleep
from collections import OrderedDict

from kolaBitMEXBot.multi_kola import MarketAuditeur

from kolaBitMEXBot.kola.orders.orders import (
    place,
    place_at_market,
    place_stop,
    place_SL,
    place_MIT,
    place_LIT,
)
from kolaBitMEXBot.kola.kolatypes import sideT, symbT, priceTT
from kolaBitMEXBot.kola.utils.general import trim_dic
from kolaBitMEXBot.kola.utils.pricefunc import setdef_stopPrice
from kolaBitMEXBot.kola.utils.orderfunc import toggle_order, split_ids
from kolaBitMEXBot.kola.utils.constantes import EXECOLS
from kolaBitMEXBot.kola.bargain import Bargain
from kolaBitMEXBot.kola.custom_bitmex import BitMEX
from kolaBitMEXBot.kola.connexion.custom_ws_thread import BitMEXWebsocket


class Test:
    """Classe qui regroupe des outils pour les tests sur Bitmex."""

    def __init__(self, tma, pricetype="LastPrice"):
        """
        Regroupe quelques fonction pour faciliter les tests depuis ipython.
        
        from kolaBitMEXBot.kola.test.utils_test import Test; T = Test()
        """
        self.qty: int = 40
        self.offset: float = 2
        self.offsetPx: float = 10
        self.offsetStop: float = 20
        self.tma = tma
        self.brg: Bargain = tma.brg
        self.bto: BitMEX = tma.brg.bto
        self.ws: BitMEXWebsocket = tma.brg.bto.ws
        self.Ods = OrderedDict()  # a dictionnary for oders passed
        self.logger = logging.getLogger(__name__)
        self.S: symbT = self.bto.symbol
        self.priceType: priceTT = pricetype

    def placeM(self, side: sideT, qty=None, trim_: bool = True, **opts) -> None:
        """Place side at market."""
        _qty = qty if qty else self.qty
        reply = place_at_market(self.brg, _qty, side, **opts)
        oPlace = [trim_dic(o) for o in reply][0] if trim_ else reply
        self.Ods[f'{oPlace["orderID"]}'] = oPlace

        return None

    def placeL(
        self, side: sideT, offset_=None, qty=None, trim_: bool = True, **opts
    ) -> None:
        """Place Limite on side with offset."""
        _qty = qty if qty else self.qty
        _offset = self.set_offset(side, offset_, offsettype="Limit")
        _price = self.brg.prices(self.priceType) + _offset

        reply = place(self.brg, side, _qty, price=_price, **opts)

        oPlace = [trim_dic(o) for o in reply][0] if trim_ else reply
        self.Ods[f'{oPlace["orderID"]}'] = oPlace
        return None

    def placeS(self, side: sideT, offset, qty=None, trim_: bool = True, **opts) -> None:
        """Place stop at side with offset."""
        _qty = qty if qty else self.qty
        opts["execInst"] = opts.get("execInst", self.priceType)
        reply = place_stop(
            self.brg, side, _qty, self.brg.prices(self.priceType) + offset, **opts
        )
        self.OplaceS = [trim_dic(o) for o in reply] if trim_ else reply
        return None

    def placeSL(
        self,
        side: sideT,
        stopoffset=None,
        priceoffset=None,
        qty=None,
        trim_: bool = True,
        **opts,
    ):
        """Place stop at side with offset."""
        _qty = qty if qty else self.qty

        offsetStop = self.set_offset(side, stopoffset, offsettype="Stop")
        offsetPx = self.set_offset(side, priceoffset, offsettype="Limit")

        stopPx = self.brg.prices(self.priceType) + offsetStop
        price = stopPx + offsetPx
        opts["execInst"] = opts.get("execInst", self.priceType)
        reply = place_SL(self.brg, side, _qty, stopPx, price, **opts)
        self.OplaceSL = [trim_dic(o) for o in reply] if trim_ else reply
        return self.OplaceSL

    def placeMIT(self, side: sideT, stopoffset, qty=None, trim_: bool = True, **opts):
        """Place stop at side with offset."""
        _qty = qty if qty else self.qty

        offsetStop = self.set_offset(side, stopoffset, offsettype="Limit")
        stopPx = self.brg.prices(self.priceType) + offsetStop
        opts["execInst"] = opts.get("execInst", self.priceType)
        reply = place_MIT(self.brg, side, _qty, stopPx, **opts)
        self.OplaceMIT = [trim_dic(o) for o in reply] if trim_ else reply
        return self.OplaceMIT

    def placeLIT(
        self,
        side: sideT,
        stopoffset=None,
        priceoffset=None,
        qty=None,
        trim_: bool = True,
        **opts,
    ):
        """Place stop at side with offset."""
        _qty = qty if qty else self.qty

        offsetStop = self.set_offset(side, stopoffset, offsettype="Stop")
        offsetPx = self.set_offset(side, priceoffset, offsettype="Limit")

        stopPx = self.brg.prices(self.priceType) + offsetStop
        price = stopPx + offsetPx
        opts["execInst"] = opts.get("execInst", self.priceType)
        reply = place_LIT(self.brg, side, _qty, stopPx, price, **opts)

        self.OplaceLIT = [trim_dic(o) for o in reply] if trim_ else reply
        return self.OplaceLIT

    def buyM(self, qty=None, **opts):
        """Buy at Market."""
        self.ObuyM = self.placeM("buy", qty)
        return self.ObuyM

    def sellM(self, qty=None, **opts):
        """Sell at Market."""
        self.OsellM = self.placeM("sell", qty)
        return self.OsellM

    def buyL(self, offset=None, qty=None, **opts):
        """Buy Limit with offset from current price."""
        self.ObuyL = self.placeL("buy", offset, qty)
        return self.ObuyL

    def sellL(self, offset=None, qty=None, **opts):
        """Sell limit with offset from current price."""
        offset = -1 if offset is None else offset
        self.OsellL = self.placeL("sell", offset, qty)
        return self.OsellL

    def buyS(self, offset=None, qty=None, **opts):
        """Buy Stop with offset."""
        offset = +2 if offset is None else offset
        self.ObuyS = self.placeS("buy", offset, qty)
        return self.ObuyS

    def sellS(self, offset=None, qty=None, **opts):
        """Sell Stop with offset."""
        offset = -2 if offset is None else offset
        self.OsellS = self.placeS("sell", offset, qty)
        return self.OsellS

    # # Other function ##
    def get_execO(self, full=False):
        """Return exec orders."""
        if full:
            data = [trim_dic(o) for o in self.bto.open_orders()]
            self.execO = DataFrame(data)
        else:
            self.execO = DataFrame([self.get_Os(o) for o in self.bto.exec_orders()])
        return self.execO

    def get_openO(self, full=False):
        """Return open orders."""
        if full:
            self.openO = DataFrame([trim_dic(o) for o in self.bto.open_orders()])
        else:
            self.openO = DataFrame([self.get_Os(o) for o in self.bto.open_orders()])
        return self.openO

    def get_Os(self, o):
        """Return orders simplified."""
        self.o = (
            simp(o.get("execID", "")),
            simp(o["orderID"]),
            simp(o["clOrdID"]),
            o["transactTime"],
            o["ordType"],
            o["ordStatus"],
        )

        return self.o

    def close_positions(self):
        """Close position for symbole SYMBOL at market price."""
        qty = self.brg.get_position()["currentQty"]
        if qty:
            return self.placeM("buy", -qty) if qty < 0 else self.placeM("sell", qty)
        else:
            return True

    def close_and_cancel(self):
        """Cancel and close all postions at market price."""
        self.cancel_all()
        return self.close_positions()

    def cancel_all(self) -> bool:
        """
        Cancel all open orders of Bargain brg.

        return: Boolean
        """
        orders = self.brg.get_open_orders()
        ids = split_ids([order["orderID"] for order in orders])
        clIDList = ids.get("clIDList")
        oIDList = ids.get("oIDList")
        if clIDList:
            self.brg.bto.cancel(clIDList)
            sleep(1)
        if oIDList:
            self.brg.bto.cancel(oIDList)
            sleep(1)

        return True

    def amend_prices(
        self, which, orderid: str, newprice: float, absdelta=10, closer: bool = False
    ):
        """
        Amend the stop price of an order.
        
        - brg <Bargain Object>:
        - orderID <str>: one existing bitmex order,
        -newStopPx <int>: new stop price.
        """
        #    logger.debug(f'orderID={orderID}, newStopPx={newStopPx}, opts={opts}')
        assert orderid or self.Ods, f"Quel order amender?"

        _oid = orderid if orderid else list(self.Ods.keys())[-1]
        try:
            _o = self.Ods[_oid]
        except Exception:
            self.logger.exception(f"_oid={_oid} and self.Ods.keys={self.Ods.keys()}")

        assert which and not _o["ordType"] == "StopLimit", f"whichpirice ? to amennd?"

        if newprice is None:
            _side = toggle_order(_o["side"]) if closer else _o["side"]
            newPrice = setdef_stopPrice(_o["price"], _side, absdelta=absdelta)

        order = {"orderID": _oid}
        if "Limit" in _o["ordType"]:
            newPrice = {"price": newPrice}
        elif "Stop" in _o["ordType"]:
            newPrice = {"stopPx": newPrice}
        else:
            raise Exception('Amending {_o["ordType"]} to implement!')

        self.Ods[f"amemded-{_oid}"] = self.brg.bto.amend(order, **newPrice)
        return self.Ods[f"amemded-{_oid}"]

    def last_oid(self):
        """Dernier orderID."""
        return list(self.Ods.keys())[-1]

    def set_offset(self, side: sideT, absoffset=None, offsettype="Stop"):
        """
        Renvois offset pour un side.
        
        si absoffset n'est pas renseigné renvois self.offset.
        Si sell offset négatif
        """
        offset = absoffset if absoffset else self.offsetPx
        assert offset > 0, f"offset={offset} (ou absoffset) doit être >0 ici"

        T = {"buy": 1, "sell": -1, "Stop": 1, "Limit": -1}
        return T[side] * T[offsettype] * offset

    # #### access table from ws  ####
    def order(self):
        """Display the order table."""
        return DataFrame(self.ws.data["order"])

    def execution(self, trim_: bool = True) -> DataFrame:
        """Display the execution table."""
        _df = DataFrame(self.ws.data["execution"])
        return _df.loc[:, EXECOLS] if trim_ else _df

    def margin(self):
        """Display the maring table."""
        return DataFrame(self.ws.data["margin"])

    def position(self):
        """Display the position table."""
        return DataFrame(self.ws.data["position"])

    def instrument(self):
        """Display the instrument table."""
        return DataFrame(self.ws.data["instrument"])

    def quote(self):
        """Display the quote table."""
        return DataFrame(self.ws.data["quote"])

    def trade(self):
        """Display the trade table."""
        return DataFrame(self.ws.data["trade"])


# #### General close cancel ####
# # utilities ################


def simp(id):
    return id[:12]


logging.getLogger("").setLevel("INFO")
tma = MarketAuditeur(live=False)
tma.start_server()
T = Test(tma)
