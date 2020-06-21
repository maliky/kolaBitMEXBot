#  -*- coding: utf-8 -*-
"""A dummy class for testing my kola framework"""
from time import sleep
from json import dumps

import pandas as pd
import numpy.random as rnd

from kolaBitMEXBot.kola.utils.general import round_price
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.settings import XBTSATOSHI
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION


class DummyBitMEX:
    """Classe qui simule les réponses de BitMex sans avoir besoin d'être connecté"""

    def __init__(self, start_price=7900, up=0.1, data=None, logger=None, N=1000):
        self.logger = get_logger(logger=logger, name=__name__, sLL="INFO")
        self.dummy = True
        self.startPrice = start_price
        self.up = up
        self.N = N
        self.pg = PriceGen(startPrice=start_price, N=N, up=up, data=data)
        self.next_datum = self.pg.next  # creat a new price
        self.current_dum_price = self.next_datum()
        self.availableMargin = 1e7
        self.dummyID = "dummyIDDIDIDIDI"
        self.symbol = "XBTUSD"
        self.prec = PRICE_PRECISION["XBTUSD"]

    def __repr__(self):
        rep = f"Dummy BitMEX object (dummy ø): url=local"
        return rep

    def new_datum(self):
        """generate a new price"""
        self.current_dum_price = self.next_datum()
        self.logger.info(f"new datum= {self.current_dum_price}")

        return self.current_dum_price

    def funds(self):
        return {"availableMargin": self.availableMargin}

    def http_open_orders(self):
        pass

    def exec_orders(self):
        """Renvois un status aléatoire pour l'ordre avec dummyID"""
        # orderStatus can be New, triggered, Canceled, Filled?
        order = {"orderID": self.dummyID, "clOrdID": self.dummyID}
        order["execType"] = self.rnd_execType()
        order["ordStatus"] = self.rnd_ordStatus(order["execType"])
        order["triggered"] = self.rnd_trigStatus("execType")
        return [order]

    def margin(self):
        return {"availableMargin": self.availableMargin}

    def instrument(self, symbol=None):
        refPrice = self.current_dum_price
        MarkPrice = refPrice + round_price(rnd.normal(), self.prec)

        return {
            "multiplier": 1,
            "quoteToSettleMultiplier": 1,
            "underlyingToSettleMultiplier": 1,
            "isQuanto": False,
            "isInverse": False,
            "isQuanto": False,
            "isInverse": False,
            "MarkPrice": MarkPrice,
            "MarkPrice": MarkPrice,
            "indicativeSettlePrice": refPrice,
            "symbol": self.symbol,
            "askPrice": MarkPrice + 0.5,
            "bidPrice": MarkPrice - 1,
            "midPrice": MarkPrice - 0.25,
            "lastMidPrice": MarkPrice - 0.25,
            "LastPrice": MarkPrice - 0.25,
        }

    def cancel(self, ids):
        pass

    def _curl_bitmex(self, path, query, max_retries=None):
        price = self.new_datum()

        if max_retries is None:
            max_retries = 10

        def exit_or_throw(e, reponse=None, load=None, rethrow_errors=True):
            """ gère la sortie en cas d'erreur socket ou de request"""
            if rethrow_errors:
                error = response.json()["error"]
                self.logger.error(f"{error}, {load}")
                if load.get("verb") in ["POST", "PUT"]:
                    raise Exception((e, load))

                elif load["verb"] == "GET":
                    self.retry(**load)

        response = None

        # self.logger.debug(f'new curl price? {price}')
        if path == "trade" and query == {
            "symbol": ".BXBT",
            "count": 1,
            "columns": "price",
            "reverse": "true",
        }:
            return [{"price": price}]
        else:
            pass

    def retry(
        self,
        path,
        error=None,
        query=None,
        postdict=None,
        timeout=None,
        verb=None,
        rethrow_errors=False,
        max_retries=None,
        fast=False,
    ):
        """Renvois la réponse du broker après waiting and retrying.
        Nécessite les même argurments que _curl_bitmex et dans le même ordre"""
        self.retries += 1

        if self.retries >= max_retries:
            raise Exception(
                f'Max retries hit, raising with {path} ({dumps(postdict or "")})'
            )

        tps1 = rnd.uniform(1, 2)
        tps2 = rnd.uniform(2, 3) if self.retries > 3 else 0
        tps3 = rnd.uniform(10, 30) if self.retries > 6 else 0
        self.logger.warning(
            f"retry {verb} {path} {self.retries}/{max_retries}: "
            f"Pause de {round_price(tps1+tps2+tps3, self.prec)} sec:"
            f' postdict={dumps(postdict or "")}'
        )
        # going to sleep
        sleep(tps1) if fast else sleep(round_price(tps1 + tps2 + tps3, self.prec))

        return self._curl_bitmex(
            path, query, postdict, timeout, verb, rethrow_errors, max_retries
        )

    def position(self, symbol=None):
        data = {
            "account": "mlk",
            "symbol": self.symbol,
            "currency": self.symbol,
            "underlying": None,
            "quoteCurrency": "USD",
            "commission": None,
            "initMarginReq": None,
            "maintMarginReq": None,
            "riskLimit": None,
            "leverage": 1,
            "prevRealisedPnl": None,
            "prevClosePrice": None,
            "openingTimestamp": None,
            "currentTimestamp": None,
            "timestamp": now(),
            "avgEntryPrice": None,
            "LastPrice": self.current_dum_price,
            "currentCost": "?",
            "currentQty": 100,
            "liquidationPrice": "?",
            "breakEvenPrice": None,
        }

        return data

    def place(self, orderQty, side=None, **kwargs):
        new_datum = self.new_datum()
        self.availableMargin -= orderQty / self.current_dum_price / XBTSATOSHI
        usd_balance = (
            self.funds()["availableMargin"] * XBTSATOSHI * self.current_dum_price
        )
        self.logger.debug(
            f"Dummy placing {orderQty} with {kwargs}, balance={usd_balance}$"
        )
        execType = self.rnd_execType()
        return {
            "orderID": self.dummyID,
            "clOrdID": self.dummyID,
            "newDatum": new_datum,
            "ordStatus": self.rnd_ordStatus(execType),
            "execType": execType,
            "triggered": self.rnd_trigStatus(execType),
        }

    def amend(self, orderID, **kwargs):
        self.logger.debug(f"Dummy Ammending with {orderID, kwargs}")
        new_datum = self.new_datum()
        execType = self.rnd_execType()

        return {
            "orderID": orderID,
            "newDatum": new_datum,
            "ordStatus": self.rnd_ordStatus(execType),
        }

    def rnd_ordStatus(self, exectype):
        """Renvois un status au hasard mais avec fote chance de filled        """
        if exectype in ["Canceled", "New"]:
            return exectype
        elif exectype == "Trade":
            return rnd.choice(["Filled", "PartiallyFilled"], p=[0.8, 0.2])
        elif exectype in ["Replaced", "TriggeredOrActivatedBySystem"]:
            return "New"
        else:
            raise Exception(f"Check exectype={exectype}")

    def rnd_execType(self):
        """Renvois un status au hasard mais avec fote chance de filled        """
        return rnd.choice(
            ["New", "Trade", "Canceled", "Replaced", "TriggeredOrActivatedBySystem"],
            p=[0.6, 0.2, 0.05, 0.1, 0.05],
        )

    def rnd_trigStatus(self, exectype):
        """Renvois un status au hasard mais avec fote chance de filled        """
        return (
            "StopOrderTriggered" if exectype == "TriggeredOrActivatedBySystem" else ""
        )


class PriceGen:
    """Classe to generate dummy price data.
    We use a dataframe so we can plot in advance the price
    et on peut aussi prévoir en détail l'évolution des prix """

    def __init__(self, startPrice=7900, up=0, N=20, data=None):
        extreme = [5] * 2 + [9]
        medium = [2] * 3 + [3] * 2
        zero = [0] * 10
        self.distrib = extreme + medium + zero
        self.distrib += list(map(lambda x: x * -(1 - up), self.distrib))

        self.N = N
        self.startPrice = startPrice
        self.up = up
        if data is None:
            self._rndData = rnd.choice(self.distrib, size=(N,))
            self._tmp = pd.Series(data=self._rndData)
            self.data = self._tmp.cumsum() + startPrice
        else:
            self.data = data

        self.get_price = self._generateur()

    def __repr__(self):
        repr = f"distrib={self.distrib}"
        return repr

    def _generateur(self):
        for i in range(len(self.data)):
            yield (i, self.data.iloc[i])

    def next(self):
        i, dprice = next(self.get_price)
        return dprice

    def plot(self):
        return self.data.plot()

    def inverse_data(self):
        inverse_data = -self.data
        max_data = self.data.max()
        min_data = self.data.min()
        self.data = inverse_data + max_data + min_data

    def update_data(self, distrib=None):
        if distrib is not None:
            self.distrib = distrib
            _rndData = rnd.choice(self.distrib, size=(self.N,))
            _tmp = pd.Series(data=_rndData)
            self.data = _tmp.cumsum() + self.startPrice


# For fun but not used here
def price_gen(startPrice=7900, up=0.5):
    extreme = [4] * 2 + [7]
    medium = [1] * 4 + [2]
    zero = [0] * 6
    distrib = extreme + medium + zero
    distrib += list(map(lambda x: x * -(1 - up), distrib))

    x = startPrice

    while True:
        next_step = rnd.choice(distrib)
        if up:
            x += next_step
        else:
            x -= next_step
            yield x
