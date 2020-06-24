# -*- coding: utf-8 -*-
"""Tools to bargain."""
from pandas import Timedelta, DataFrame
from numpy.random import randint
from typing import Optional, Set, Dict, List

from kolaBitMEXBot.kola.kolatypes import ordStatusT
from kolaBitMEXBot.kola.custom_bitmex import BitMEX
from kolaBitMEXBot.kola.secrets import LIVE_KEY, LIVE_SECRET, TEST_KEY, TEST_SECRET
from kolaBitMEXBot.kola.settings import (
    LIVE_URL,
    TEST_URL,
    SYMBOL,
    ORDERID_PREFIX,
    TIMEOUT,
    POST_ONLY,
    XBTSATOSHI,
    CONTRACTS,
)
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.utils.general import round_sprice, is_number, trim_dic, cdr, car
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.constantes import (
    EXECOLS,
    SETTLEMENTPRICES,
    PRICE_PRECISION,
    INSTRUMENT_PRICES,
)


# MULTIPLIER À CORRIGER
# Trouver un moyen d'annuler juste les ordre passé dans ce bargain (prefix ?)
# voir comment mettre ça en fils de Thread, probablement en faisant hériter Bargain
# ! important avoir un bargain pour un broker car, throtteling prices


class Bargain:
    """
    A class to group my function for trading in Bitmex.

    (C'est mon dragon à plusieurs queues)
    """

    def __init__(
        self,
        postOnly=POST_ONLY,
        live=False,
        symbol=SYMBOL,
        orderIDPrefix=ORDERID_PREFIX,
        timeout=TIMEOUT,
        logger=None,
        dbo=None,
    ):
        """Initialisation dbo is a dummy bitMEX object used for testing."""
        self.logger = get_logger(logger, name=__name__, sLL="INFO")

        self.symbol = symbol
        self.precision = PRICE_PRECISION[symbol]
        self.last_check_time = now() - Timedelta(
            2, unit="D"
        )  # on s'assure d'être dans le passé.
        self.cached_refPrices = None

        self.live = live
        if self.live and dbo is None:
            baseUrl, apiKey, apiSecret = LIVE_URL, LIVE_KEY, LIVE_SECRET
        else:
            baseUrl, apiKey, apiSecret = TEST_URL, TEST_KEY, TEST_SECRET

        if dbo:
            self.bto = dbo
            self.dbo = dbo
        else:
            # si on ne passe pas l'argument dummy BitMEX object
            # On en crée un réel
            self.bto = BitMEX(
                base_url=baseUrl,
                symbol=self.symbol,
                apiKey=apiKey,
                apiSecret=apiSecret,
                orderIDPrefix=orderIDPrefix,
                postOnly=postOnly,
                timeout=timeout,
                logger=self.logger,
            )
            self.dbo = None

        self.logger.info(f"Fini init {self}")

    def __repr__(self, short=True):
        """Représente the Bargain object."""
        bto = self.dbo if self.dbo else self.bto
        rep = f"----BRG-live({self.live}) using {bto}"
        if not short:
            rep += f"logger={self.logger}\n"

        return rep

    def get_delta(self):
        """Calculate currency delta for portfolio."""
        # à creuser
        portfolio = self.get_portfolio()
        IndexPrice_delta = 0
        mark_delta = 0
        for self.symbol in portfolio:
            item = portfolio[self.symbol]
            if item["futureType"] == "Quanto":
                IndexPrice_delta += (
                    item["currentQty"] * item["multiplier"] * item["IndexPrice"]
                )
                mark_delta += (
                    item["currentQty"] * item["multiplier"] * item["MarkPrice"]
                )
            elif item["futureType"] == "Inverse":
                IndexPrice_delta += (item["multiplier"] / item["IndexPrice"]) * item[
                    "currentQty"
                ]
                mark_delta += (item["multiplier"] / item["MarkPrice"]) * item[
                    "currentQty"
                ]
            elif item["futureType"] == "Linear":
                IndexPrice_delta += item["multiplier"] * item["currentQty"]
                mark_delta += item["multiplier"] * item["currentQty"]
        basis_delta = mark_delta - IndexPrice_delta
        delta = {
            "IndexPrice": IndexPrice_delta,
            "mark_price": mark_delta,
            "basis": basis_delta,
        }
        return delta

    def get_balance(self, atPrice="satoshi"):
        """
        Calcule la balance en satoshi (def), usd ou xbt atPrice.

        Si atPrice is None, use market buy sell or mid price
        Voir aussi abonnement "wallet"
        """
        satoshi_balance = self.bto.margin()["availableMargin"] * self.get_leverage()
        xbt_balance = satoshi_balance * XBTSATOSHI

        if isinstance(atPrice, str):
            atPrice = atPrice.lower()
            if "usd" in atPrice:
                # renvois la balance en USD
                if "buy" in atPrice:
                    current_price = self.prices("market", "buy")
                elif "sell" in atPrice:
                    current_price = self.prices("market", "sell")
                else:
                    current_price = self.prices("midPrice")
                return round_sprice(xbt_balance * current_price, self.symbol)

            elif "xbt" in atPrice:
                return xbt_balance

        elif is_number(atPrice):
            # atPrice should be float
            return round_sprice(xbt_balance * atPrice, self.symbol)

        elif atPrice is not None:
            raise Exception("atPrice should be str number or None")

        return satoshi_balance

    def get_multiplier(self):
        """Get multiplier."""
        # check market_maker
        instrument = self.bto.instrument(self.symbol)

        if instrument["underlyingToSettleMultiplier"] is None:
            multiplier = float(instrument["multiplier"]) / float(
                instrument["quoteToSettleMultiplier"]
            )
        else:
            multiplier = float(instrument["multiplier"]) / float(
                instrument["underlyingToSettleMultiplier"]
            )
        return multiplier

    def get_leverage(self):
        """Get leverage for symbol."""
        return self.bto.position(self.symbol)["leverage"]

    def get_open_orders(self, summary=True):
        """Get the open orders."""
        orders = self.bto.http_open_orders()

        if summary:
            open_order = []
            keys = ["clOrdID", "orderID", "orderQty", "price", "side", "displayQty"]
            if self.dbo is None:
                for order in orders:
                    open_order.append({k: order[k] for k in keys})
            return open_order

        return orders

    # Cost or gain of position
    def get_position(self, summary: bool = True, trimempty: bool = True):
        """
        Get current position for SYMBOL.

        - summary(True), if False show all
        """
        position = self.bto.position(self.symbol)
        if summary:
            keys = [
                "account",
                "symbol",
                "currency",
                "underlying",
                "quoteCurrency",
                "commission",
                "initMarginReq",
                "maintMarginReq",
                "riskLimit",
                "leverage",
                "prevRealisedPnl",
                "prevClosePrice",
                "openingTimestamp",
                "currentTimestamp",
                "timestamp",
                "avgEntryPrice",
                "LastPrice",
                "currentCost",
                "currentQty",
                "liquidationPrice",
                "breakEvenPrice",
            ]
            return {k: position[k] for k in keys}
        else:
            return trim_dic(position) if trimempty else position

    def get_portfolio(self):
        """Get portfolio."""
        contracts = CONTRACTS
        portfolio = {}
        for self.symbol in contracts:
            position = self.bto.position(symbol=self.symbol)
            instrument = self.bto.instrument(symbol=self.symbol)

            if instrument["isQuanto"]:
                future_type = "Quanto"
            elif instrument["isInverse"]:
                future_type = "Inverse"
            elif not instrument["isQuanto"] and not instrument["isInverse"]:
                future_type = "Linear"
            else:
                raise NotImplementedError(
                    "Unknown future type; not quanto or inverse: %s"
                    % instrument["symbol"]
                )

            if instrument["underlyingToSettleMultiplier"] is None:
                multiplier = float(instrument["multiplier"]) / float(
                    instrument["quoteToSettleMultiplier"]
                )
            else:
                multiplier = float(instrument["multiplier"]) / float(
                    instrument["underlyingToSettleMultiplier"]
                )

            portfolio[self.symbol] = {
                "currentQty": float(position["currentQty"]),
                "futureType": future_type,
                "multiplier": multiplier,
                "MarkPrice": float(instrument["MarkPrice"]),
                "IndexPrice": float(instrument["indicativeSettlePrice"]),
            }

        return portfolio

    def has_open_orders(self):
        """Check if there is an open order."""
        return self.get_open_orders() != []

    def order_open_is(self, ordID, t_value=True):
        """Check if order with ordID is open."""
        orders = self.bto.open_orders()
        if t_value:
            ret = any([order["orderID"] == ordID for order in orders])
        else:
            ret = any([order["orderID"] == ordID for order in orders])

        return ret

    def exec_orders(self):
        """Get the exectuted orders."""
        oexec = self.bto.exec_orders()
        return oexec  # filtrer oexec

    def execution(self, clOrdID_: Optional[str] = None) -> DataFrame:
        """
        Display the execution table.

        Si clOrdID_ is None, Renvois tous mes ordres executés.
        Sinon renvois seulement les lignes clOrdID_ dans l'ordre
        transactTime ascendant.
        """
        try:
            df = DataFrame(self.bto.ws.data["execution"])
        except ValueError as ve:
            # pb avec la table execution qui ne renvois pas des object tous de la même taille
            # va filtrer / trier
            execution = self.bto.ws.data["execution"]
            EXEC: Dict[int, List] = {}
            for elt in execution:
                EXEC.get(len(elt), []).append(elt)

            self.logger.exception(
                f"Exception {ve}: We have {len(EXEC)} diff execution shape returning the longuest of {list(map(len, EXEC.values()))}"
            )
            df = DataFrame(EXEC.get(max(EXEC.keys()), []))
            self.logger.warning(f"Returning only {df}")

        if len(df):
            df = df.sort_values("transactTime")

        if clOrdID_ is not None:
            mask = df.loc[:, "clOrdID"] == clOrdID_
            return df.loc[mask, :]

        return df

    def get_srcKey(self, clOrdID_):
        """
        Reconstruit à partir de clOrdID, la srcKey qui l'identifierai.

        la hooKey est formé du nom de l'ordre (nameT) d'un - d'une lettre S ou P
        identifiant la source primaire ou secondaire.
        Il y a ensuite un _ et d'une lettre C, F, T pour identifier le status visé.

        exemple Src-S_C  - cherche le clOrd de nom Src-SO pour cancel.
        On récupère et renvois la lettre maitresse.
        """
        # clOrd without prefix
        _clOrdID = cdr(clOrdID_, ORDERID_PREFIX)
        nom = car(_clOrdID, "-")  # srcname-S ou srcname-P ou ''
        _typeOrd = cdr(_clOrdID, "-")  # -OrdtargetStatus-SO|PO
        typeOrd = _typeOrd[0] if _typeOrd else ""

        _srcKey = f"{nom}-{typeOrd}"

        return _srcKey

    def get_exec_clID_with_(self, srcKey_, debug_=False):
        """
        Récupère les clOrdID associés à srcKey_ si ils existent.

        la srcKey ne contient pas le status que doit avoir exec
        Si en trouve plusieurs IDS, les renvois dans l'ordre ascendant
        des transactTimes.
        """
        # Ordres exécutés et ordonnés dans l'ordre ascendant
        exec_clOrdID = (
            self.execution().loc[:, "clOrdID"] if len(self.execution()) else []
        )
        if debug_:
            return self.execution().loc[:, EXECOLS] if len(self.execution()) else []

        seenIDs: Set[str] = set()
        clOrdIDs = []

        for clID in exec_clOrdID:
            if (clID not in seenIDs) and (self.get_srcKey(clID) == srcKey_):
                clOrdIDs.append(clID)
                seenIDs |= set([clID])

        return clOrdIDs

    def get_exec_with_(self, srcKey_, minTransacTime, debug_=False):
        """
        Récupère les clOrdID associés à srcKey_ si ils existent.

        ajoute une condition de temps
        """
        _execution = self.execution()
        # Ordres exécutés et ordonnés dans l'ordre ascendant
        exec_orders = (
            _execution.loc[:, ["clOrdID", "transactTime"]] if len(_execution) else []
        )
        if debug_:
            return _execution.loc[:, EXECOLS] if len(_execution) else []

        seenIDs: Set[str] = set()
        clOrdIDs = []

        if len(exec_orders):
            for i, clID in enumerate(exec_orders["clOrdID"]):
                clID_not_seen = clID not in seenIDs
                possible_srID = self.get_srcKey(clID) == srcKey_
                in_last_minute = exec_orders.iloc[i].loc["transactTime"] > (
                    now() - Timedelta("30s")
                )
                if clID_not_seen and possible_srID and in_last_minute:
                    clOrdIDs.append(clID)
                    seenIDs |= set([clID])

        return clOrdIDs

    def order_reached_status(self, clOrdID_: str, ordStatus_: ordStatusT) -> bool:
        """
        Test si l'ordre clOrdID_ à atteint ordStatus.

        Regarde dans data['execution'] et vérifie le ordStatus
        de la dernière execution du clOrdID_.
        """
        execOrders: DataFrame = self.execution(clOrdID_)
        if "triggered" in ordStatus_.lower():
            return bool(execOrders.iloc[-1].triggered == "StopOrderTriggered")

        return bool(execOrders.iloc[-1].ordStatus == ordStatus_)

    def order_had_status(self, clOrdID_: str, ordStatus_: ordStatusT) -> bool:
        """Test si l'ordre clOrdID_ à eu le ordStatus dans data['execution']."""
        ordExec = self.execution(clOrdID_)
        mask = ordExec.loc[:, "ordStatus"] == ordStatus_

        return bool(sum(mask))

    def recent_trades(self):
        """Les trades récents?."""
        return self.bto.recent_trades()

    def get_most_recent_settlement_price(self):
        """Query the market for the last settlement price of symbol."""
        path = "trade"
        query = {
            "symbol": SETTLEMENTPRICES.get(self.symbol, self.symbol),
            "count": 1,
            "columns": "price",
            "reverse": "true",
        }
        # self.logger.debug(f'Got new price from curl {self.cached_refPrices}')
        # not sure here about the markPrice for the IndexPrice
        rep = self.bto._curl_bitmex(path, query)[0]
        self.logger.debug(f"asking: {path}, {query}. *Réponse: {rep}*")

        return rep["price"]

    def prices(
        self, typeprice=None, side="buy", symbol_=None, force_live: bool = False
    ):
        """
        Show summary of current prices.

        typeprice can be 'delta', 'IndexPrice', 'market', 'askPrice',

        'midPrice', 'ref_delta','market_maker', 'lastMidPrice' or None
        (for all instrument prices)
        - symbol if not user bargain symbol but maybe could want other price
        - force_live: should we force getting live price ? (def false)
        """
        _symbol = self.symbol if symbol_ is None else symbol_

        prices = {
            k: v for (k, v) in self.bto.instrument(_symbol).items() if "rice" in k
        }
        # prices.keys = 'maxPrice', 'prevClosePrice', 'prevPrice24h', 'highPrice',
        # 'LastPrice', 'LastPriceProtected', 'bidPrice', 'midPrice', 'askPrice',
        # 'impactBidPrice', 'impactMidPrice', 'impactAskPrice', 'MarkPrice',
        # 'MarkPrice', 'indicativeSettlePrice', 'lowPrice',
        # execInst, MarkPrice, LastPrice, IndexPrice

        ret = None
        typeprice = "" if typeprice is None else typeprice

        try:
            if typeprice == "delta":
                ret = prices["askPrice"] - prices["bidPrice"]

            elif typeprice.lower() == "indexprice":
                # S'assurer qu'il n'y a pas deux appels consécutif à moins de x seconde
                # minisytème de cache  jusquà 11s avant nouvel appel au broker
                # can be force is force_live = True

                timeLaps = now() - self.last_check_time
                msg = (
                    f"Checking cached price"
                    f" timeLaps={timeLaps}, now={now()},"
                    f" last_check_time={self.last_check_time}"
                    f" self.cached_refPrices={self.cached_refPrices}"
                )

                if (
                    self.cached_refPrices is None
                    or timeLaps > Timedelta(randint(2, 8), unit="s")
                    or self.bto.dummy
                    or force_live
                ):
                    cached_refPrices = self.get_most_recent_settlement_price()
                    self.last_check_time = now()

                    self.cached_refPrices = cached_refPrices
                    msg += f">>>> New Cached_refPrices={cached_refPrices} <<<<."

                self.logger.debug(msg)

                ret = self.cached_refPrices

            elif typeprice.lower() == "lastprice":
                # askPrice > bidPrice
                ret = prices["askPrice"] if side == "buy" else prices["bidPrice"]
            elif typeprice == "market_maker":
                ret = prices["bidPrice"] if side == "buy" else prices["askPrice"]
            elif typeprice.lower() == "lastmidprice":
                ret = self.prices("midPrice")  # this is close to the last price
            elif typeprice.lower() in ["market", "market_price", "markprice"]:
                # fairePrice is the marketPrice dans XBT check markMethod
                ret = prices["markPrice"]
            elif typeprice == "ref_delta":
                ret = self.prices("midPrice") - self.prices("indexPrice")
            elif typeprice:
                ret = prices[self.camelCase_price(typeprice)]
        except Exception as e:
            self.logger.error(f"prices={prices}, e={e}")
            raise (e)

        return prices if not ret else round_sprice(ret, self.symbol)

    def camelCase_price(self, priceName):
        """
        CamelCase the price name so it matches the instrument keys.

        keys:
        prices= [f"{suf}Price" for suf in ['max', 'prevClose', 'prev', 'high', 'low',
        'last', 'bid', 'mid', 'ask', 'impactBid', 'impactMid', 'impactAsk',
        'fair', 'mark', 'indicativeSettle']] + [lastPriceProtected]
        """

        assert priceName.lower() in [
            p.lower() for p in INSTRUMENT_PRICES
        ], f"priceName={priceName} and INSTRUMENT_PRICES={INSTRUMENT_PRICES}"

        if priceName.lower() == "lastpriceprotected":
            return "lastPriceProtected"

        _name = priceName.lower().split("price")[0]
        if "impact" in _name:
            assert "prev" not in _name, "Gérer le cas où les deux sont dans le nom."
            _name = self.capitalize_last(_name, "impact")
        elif "prev" in _name:
            _name = self.capitalize_last(_name, "prev")

        return f"{_name}Price"

    def capitalize_last(self, _name, first):
        """Split the name with first and capitalize last part."""
        _split = _name.split("first")
        return f"{first}{_split[-1].capitalize()}" if len(_split) > 1 else _name

    def set_leverage(self, leverage):
        return self.bto.isolate_margin(self.symbol, leverage)

    def cancel_and_close(self, quantity=None):
        """
        Cancel and close all postions at market price.

        may close only position weighted by quantity
        """
        if quantity:
            self.cancel_all_orders()

        return self.close_position(quantity)

    def close_position(self, quantity=None):
        """close position for symbole SYMBOL at market price"""
        qty = self.get_position().get("currentQty", 0)
        # on s'assure de diminuer une position, envisager de le faire en %
        quantity = 0 if quantity is None or abs(quantity) > abs(qty) else quantity

        if qty < 0:
            qty += abs(quantity)
        elif qty > 0:
            qty -= abs(quantity)

        qty = -qty

        if qty != 0:
            return self.bto.place(qty, execInst="Close")
        else:
            self.logger.warning("Probably no position to close.")

    def cancel_all_orders(self):
        """cancel all open orders of Bargain brg\n -param: Bargain\n -return: True if ok, false otherways"""
        # should be done in bulk
        oes = self.get_open_orders()
        ids = [oe["orderID"] for oe in oes]
        if ids != []:
            self.bto.cancel(ids)
            return True
        return False
