#  -*- coding: utf-8 -*-
"""BitMEX API Connector."""
from __future__ import absolute_import
from time import sleep, time

from json import dumps
from numpy import random
import datetime as dt
import requests as rq
import pandas as pd

from kolaBitMEXBot.kola.connexion.auth import APIKeyAuthWithExpires
from kolaBitMEXBot.kola.connexion.custom_ws_thread import BitMEXWebsocket
from kolaBitMEXBot.kola.utils.general import round_sprice, trim_output
from kolaBitMEXBot.kola.settings import (
    HTTP_SIMPLE_RATE_LIMITE,
    HTTP_BULK_RATE_LIMITE,
    ORDERID_PREFIX,
)
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION
from kolaBitMEXBot.kola.utils.orderfunc import newClID, split_ids, get_abbv_from_ID
from kolaBitMEXBot.kola.utils.datefunc import now, multiply_time_unit, TC
from kolaBitMEXBot.kola.utils.logfunc import get_logger
import kolaBitMEXBot.kola.utils.exceptions as ke

# https://www.bitmex.com/api/explorer/


class BitMEX(object):
    """BitMEX API Connector."""

    def __init__(
        self,
        base_url=None,
        symbol=None,
        apiKey=None,
        apiSecret=None,
        orderIDPrefix=ORDERID_PREFIX,
        shouldWSAuth=True,
        postOnly=False,
        timeout=8,
        logger=None,
    ):
        """Init connector."""
        self.dummy = False  # to flag this as not dummy
        self.logger = get_logger(logger, name=__name__, sLL="DEBUG")
        self.base_url = base_url
        self.symbol = symbol
        self.prec = PRICE_PRECISION[symbol]
        self.postOnly = postOnly

        if apiKey is None:
            raise Exception("Please set an API key and Secret.")

        self.apiKey = apiKey
        self.apiSecret = apiSecret
        if len(orderIDPrefix) > 13:
            raise ValueError(
                "settings.ORDERID_PREFIX must be at most 13 characters long!"
            )
        self.orderIDPrefix = orderIDPrefix
        self.retries = 0  # initialize counter

        # Prepare HTTPS session
        self.session = rq.Session()
        # These headers are always sent
        self.session.headers.update(
            {"user-agent": "idev-"}
        )  # peut tester avec un autre nom (liquidbot)
        self.session.headers.update({"content-type": "application/json"})
        self.session.headers.update({"accept": "application/json"})

        # Create websocket for streaming data
        ws = BitMEXWebsocket(self.apiKey, self.apiSecret, logger=self.logger, symbol=symbol)
        self.ws = ws
        self.logger.debug(f"ws={ws}")
        self.ws.connect(base_url, symbol, shouldAuth=shouldWSAuth)
        self.timeout = timeout
        self.logger.info(f"Fini init {self}")

    def __repr__(self):
        """Representation of BitMex Object."""
        rep = f"BitMEX object {self.symbol}: url={self.base_url}"
        return rep

    def __del__(self):
        """Close socket."""
        self.exit()

    def _curl_bitmex(
        self,
        path,
        query=None,
        postdict=None,
        timeout=None,
        verb=None,
        rethrow_errors=True,
        max_retries=None,
    ):
        """Send a request to BitMEX Servers."""
        # Handle URL
        url = self.base_url + path

        if timeout is None:
            timeout = self.timeout

        # Default to POST if data is attached, GET otherwise
        if not verb:
            verb = "POST" if postdict else "GET"

        # By default don't retry POST or PUT.
        # Retrying GET/DELETE is okay because they are idempotent.
        # In the future we could allow retrying PUT, so long as 'leavesQty' is not used
        # or you could change the clOrdID (set {"clOrdID": "new", "origClOrdID": "old"})
        # so that an amend can't erroneously be applied twice.
        if max_retries is None:
            #  if verb in ['POST', 'PUT'] else 12  # il faut faire attention à l'idempotence
            max_retries = 30

        # Auth: API Key/Secret
        auth = APIKeyAuthWithExpires(self.apiKey, self.apiSecret)

        def exit_or_throw(e, reponse=None, load=None):
            """ Gère la sortie en cas d'erreur socket ou de request"""
            if rethrow_errors:
                error = response.json().get("error", {}) if response is not None else {}
                self.logger.error(f"msg={error}, load={load}")

                if load["verb"] == "GET":
                    self.retry(**load)

                raise ke.InvalidOrder(e, load, error)

        # Make the request
        response = None

        if verb in ["POST", "PUT"]:  # don't want to log GET
            self.logger.info(
                f'sending {verb} to {url}: {dumps(postdict or query or "no postdict nor query.")}'
            )
        load = None
        try:
            # for logging exception
            load = {
                "path": path,
                "query": query,
                "postdict": postdict,
                "timeout": timeout,
                "verb": verb,
                "rethrow_errors": rethrow_errors,
                "max_retries": max_retries,
            }

            req = rq.Request(verb, url, json=postdict, auth=auth, params=query)
            prepped = self.session.prepare_request(req)
            self.logger.debug(f"req={req}, prepped={prepped}")
            response = self.session.send(prepped, timeout=timeout)

            # Make non-200s throw
            response.raise_for_status()

        except rq.exceptions.HTTPError as e:
            if response is None:
                raise exit_or_throw(e, "response is None", load)

            # 400
            elif response.status_code == 400:
                error = response.json().get("error", {})
                # exemple de response.json()
                # {'error': {'message': 'Invalid ordStatus', 'name': 'HTTPError'}}
                message = error.get("message", "No message").lower()
                self.logger.warning(f'Error message="{message}", load={load}')

                # Duplicate clOrdID: that's fine, probably a deploy,
                # go get the order(s) and return it
                # This request has expired can happend when server is overloaded
                if (
                    "duplicate clordid" in message
                    or "this request has expired" in message
                ):
                    return self.retry(**load)

                if "insufficient available balance" in message:
                    raise ke.InsufficientBalance(e, load, message)

                if "invalid ordstatus" in message:
                    # probablement un changement trop brusque dans un amend on ignore l'ordre
                    # problème car attente de valeur retour
                    self.logger.info(
                        f"Raising an InvalidOrdStatus exception message={message}"
                    )
                    raise ke.InvalidOrdStatus(e, load, message)

                if "invalid orderid" in message:
                    # probablement un changement trop brusque dans un amend on ignore l'ordre
                    self.logger.info("Raising an InvalidOrderID exception")
                    raise ke.InvalidOrderID(e, load, message)

                if "invalid orderqty" in message:
                    # Une quantité trop petite probablement.  On envois à Chronos
                    # pour vérifier
                    self.logger.info("Raising an InvalidOrderID exception")
                    raise ke.InvalidOrderQty(e, load, message)

                # other 400 error raise
                raise ke.InvalidOrder(e, load, message)

            # 401 - Auth error. This is fatal.
            elif response.status_code == 401:
                self.logger.error(
                    "API Key or Secret incorrect, please check and restart."
                )
                self.logger.error("Error: " + response.text)
                if postdict:
                    self.logger.error(postdict)
                    self.exit()

            # 404, can be thrown if order canceled or does not exist.
            elif response.status_code == 404:
                if verb == "DELETE":
                    self.logger.error(
                        f"Order not found. postdict = {postdict} and load={load}"
                    )
                    # self.logger.error("Order not found: %s" % postdict["orderID"])
                    return None
                exit_or_throw(e, response, load)

            # 429, ratelimit; cancel orders & wait until X-Ratelimit-Reset
            elif response.status_code == 429:
                self.logger.error(
                    f"Ratelimited on current request. "
                    "Sleeping, then trying again. "
                    f"Request: {url} \n {dumps(postdict)}"
                )

                # Figure out how long we need to wait.
                ratelimit_reset = response.headers["X-Ratelimit-Reset"]
                to_sleep = int(ratelimit_reset) - int(time())
                reset_str = dt.datetime.fromtimestamp(int(ratelimit_reset)).strftime(
                    "%X"
                )

                # We're ratelimited, and we may be waiting for a long time. Cancel orders.
                self.logger.warning("Canceling all known orders in the meantime.")
                self.cancel([o["orderID"] for o in self.open_orders()])

                self.logger.error(
                    f"Your ratelimit will reset at {reset_str}."
                    f"Sleeping for {to_sleep} seconds."
                )

                sleep(to_sleep)
                return self.retry(**load)  # ne passe pas are exit_or_throw

            # 502 Server Error: Bad Gateway
            elif response.status_code in [502, 503]:
                load = {
                    "path": path,
                    "query": query,
                    "postdict": postdict,
                    "timeout": timeout,
                    "verb": verb,
                    "rethrow_errors": rethrow_errors,
                    "max_retries": max_retries,
                    "exception": e,
                }

                return self.retry(**load)

            # in other status_code cases
            exit_or_throw(e, "Unhandled Error", load)
        except (rq.exceptions.Timeout, rq.exceptions.ConnectionError):
            # Timeout, re-run this request
            load = {
                "path": path,
                "query": query,
                "postdict": postdict,
                "timeout": timeout,
                "verb": verb,
                "rethrow_errors": rethrow_errors,
                "max_retries": max_retries,
            }

            return self.retry(**load)

        # Reset retry counter on success
        self.retries = 0

        return response.json()

    def amend(self, order, **kwargs):
        """
        Amend an order.
        
        - bto <BitMex Object>:,
        - orderID <str>: one existing bitmex orderID,
        - stopPx <int>: new stop price,
        - qty? <int>: new quantity
        """
        # ## Quel est la différence entre amending a limit order and a stoploss ?
        # dans le limit c'est le price que l'on amende dans le stop loss c'est le stopPx
        #  clOrdID = order.get('clOrdID', None)
        newOrder = {"orderID": order["orderID"]}

        def is_pos(key, kwargs):
            return (key in kwargs) and (kwargs[key] > 0)

        assert is_pos("price", kwargs) or is_pos(
            "stopPx", kwargs
        ), f"Price must be positive. kwargs={kwargs}"

        newOrder.update(kwargs)

        def is_same_signe(k, basedic, refdic):
            return (k in basedic) and ((basedic[k] * refdic[k]) > 0)

        if is_same_signe("orderQty", kwargs, order):
            newOrder["orderQty"] = kwargs["orderQty"]
            # remove the iceberg (because m'empeche d'avoir le market maker fee)
            # crypt_qty = abs(int(random.random()*(kwargs['orderQty'])))
            # crypt_qty = 30 if crypt_qty < 30 else crypt_qty
            #            newOrder['displayQty'] = crypt_qty

        elif "orderQty" in kwargs:
            # c'est que la quantité est renseignée mais de signe différent avec l'ancienne.
            raise Exception(
                "La Modification va changer le side de l'ordre et"
                + " donc le clore immediatement."
            )
        if "text" in kwargs:
            newOrder["text"] = kwargs["text"]

        # invalide orderStatus
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(path="order", postdict=newOrder, verb="PUT")

    def authentication_required(fn):
        """Annotation for methods that require auth."""

        def wrapped(self, *args, **kwargs):
            if not (self.apiKey):
                #                msg = "You must be authenticated to use this method"
                raise
            else:
                self.__doc__ = fn.__doc__
                return fn(self, *args, **kwargs)

        return wrapped

    @authentication_required
    def amend_bulk_orders(self, orders):
        """Amend multiple orders."""
        # Note rethrow; if this fails, we want to catch it and re-tick
        sleep(HTTP_BULK_RATE_LIMITE)
        return self._curl_bitmex(
            path="order/bulk",
            postdict={"orders": orders},
            verb="PUT",
            rethrow_errors=True,
        )

    @authentication_required
    def cancel(self, orderID):
        """Cancel an existing order. (or List or  dict from split_ids)"""
        path = "order"

        if isinstance(orderID, dict):
            clIDList = orderID.get("clIDList")
            oIDList = orderID.get("oIDList")
            ret = {}

            if clIDList:
                postdict = {"clOrdID": clIDList}
                sleep(HTTP_SIMPLE_RATE_LIMITE)
                ret["clID"]: self._curl_bitmex(
                    path=path, postdict=postdict, verb="DELETE"
                )

            if oIDList:
                postdict = {"orderID": oIDList}
                sleep(HTTP_SIMPLE_RATE_LIMITE)
                ret["oID"] = self._curl_bitmex(
                    path=path, postdict=postdict, verb="DELETE"
                )

            return ret

        if isinstance(orderID, list):
            return self.cancel(split_ids(orderID))

        elif orderID.startswith(ORDERID_PREFIX):
            postdict = {"clOrdID": orderID}
        else:
            postdict = {"orderID": orderID}

        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(path=path, postdict=postdict, verb="DELETE")

    @authentication_required
    def delta(self):
        """Get delta."""
        return self.position(self.symbol)["homeNotional"]

    def exit(self):
        """Close websocket."""
        self.ws.exit()

    @authentication_required
    @trim_output()
    def margin(self, currency="XBt"):
        """Get avalaible margin."""
        path = "user/margin"
        query = {"currency": currency}
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(path=path, query=query, verb="GET")

    @authentication_required
    @trim_output()
    def funds(self):
        """Get your current balance."""
        # check abonnement à "wallet"
        return self.ws.funds()

    @authentication_required
    def http_open_orders(self):
        """Get 10 open orders via HTTP. Used on close to ensure we catch them all."""
        path = "order"
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        orders = self._curl_bitmex(
            path=path,
            query={
                "filter": dumps(
                    {"ordStatus.isTerminated": False, "symbol": self.symbol}
                ),
                # 'filter': dumps({'open':True})  ## recommandation de l'API
                "count": 10,
            },
            verb="GET",
        )
        # Only return orders that start with our clOrdID prefix.
        return [o for o in orders if str(o["clOrdID"]).startswith(self.orderIDPrefix)]

    @trim_output()
    def instrument(self, symbol):
        """Get an instrument's details."""
        return self.ws.get_instrument(symbol)

    @trim_output()
    def instruments(self, filtre=None):
        """Get http instruments ?. What for filter ?."""
        query = {"filter": dumps(filtre)} if filtre else {}
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(path="instrument", query=query, verb="GET")

    @authentication_required
    @trim_output()
    def isolate_margin(self, symbol, leverage, rethrow_errors=False):
        """Set the leverage on an isolated margin position. Not sure about that."""
        path = "position/leverage"
        postdict = {"symbol": symbol, "leverage": leverage}
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(
            path=path, postdict=postdict, verb="POST", rethrow_errors=rethrow_errors
        )

    def market_depth(self, symbol):
        """Get market depth / orderbook."""
        try:
            return self.ws.market_depth(symbol)
        except Exception:
            self.logger.exception(f"market_depth")

    @authentication_required
    def open_orders(self):
        """Get open orders."""
        try:
            return self.ws.open_orders(self.orderIDPrefix)
        except Exception:
            self.logger.exception(f"open_orders")

    @authentication_required
    def exec_orders(self, exectype=None):
        """
        Get return content of the execution table filtered by exectype.

        (def None). exectype can be Trade, New, Replaced, Canceled
        """
        try:
            orders = self.ws.exec_orders(self.orderIDPrefix)
        except Exception:
            self.logger.exception(f"exec_orders")
        return [o for o in orders if o["execType"] == exectype] if exectype else orders

    @authentication_required
    def place(self, orderQty, side=None, clOrdID=None, asBulk=False, **opts):
        """
        Place a buy or sell a uniq order as a bulk order if asBulk=True.

        Returns what _curl_bitmex_returns
        mendatory is orderQty
        """
        try:
            postdict = self.create_order(side, orderQty, clOrdID, **opts)
        except Exception as e:
            self.logger.error(
                f'Check Exception "{e}" with '
                f"args={side, orderQty, clOrdID} and opts={opts}."
            )

            raise (e)
        # self.logger.warning(f"postdict={postdict}")
        if asBulk:
            sleep(HTTP_BULK_RATE_LIMITE)
            return self._curl_bitmex(
                path="order/bulk", postdict={"orders": [postdict]}, verb="POST"
            )
        else:
            sleep(HTTP_SIMPLE_RATE_LIMITE)
            retVal = self._curl_bitmex(path="order", postdict=postdict, verb="POST")
            # self.logger.info(f"retVal = {retVal}")
            return retVal

    @authentication_required
    def create_order(self, side, orderQty, clOrdID=None, **opts):
        """
        Create an postdic for an order, using orderQty in contracts.
        
        Quantities are always positives
        """
        # Generate a unique clOrdID with our prefix so we can identify it.
        if clOrdID is None:
            clOrdID = newClID()

        postdict = {"symbol": self.symbol, "clOrdID": clOrdID}

        # setting the orderQty
        if side:
            orderQty = -orderQty if "sell" == side.lower() else orderQty
        orderQty = round(orderQty)

        # we handle number of contracts not crypto
        postdict.update(
            {
                "orderQty": orderQty,  # on suppose orderQty >=1 et on ne veux pas afficher 0
                # 'displayQty': int(random.random() * (abs(orderQty) - 1)) + 1  # crypt_qty
            }
        )

        self.checking_positive_value(opts, "price", "stopPx")

        # adding building arguments
        # opts = {k:v for (k,v) in opts.items() if v is not None}
        # postdict.update(opts)
        for k, v in opts.items():
            if v is not None:
                postdict[k] = v

        return postdict

    @authentication_required
    def create_bulk_orders(self, orders):
        """Create multiple orders. chaque ordre est un tuble (side, orderQty), orders est une listede tuple"""
        oes = []
        for order in orders:
            o = self.create_order(*order)
            if self.postOnly:
                o["execInst"] = "ParticipateDoNotInitiate"
                oes.append(o)
        sleep(HTTP_BULK_RATE_LIMITE)
        return self._curl_bitmex(
            path="order/bulk", postdict={"orders": oes}, verb="POST"
        )

    @authentication_required
    def position(self, symbol):
        """Get your open position."""
        try:
            return self.ws.position(symbol)
        except Exception:
            self.logger.exception(f"position")

    def recent_trades(self):
        try:
            return self.ws.recent_trades()
        except Exception:
            self.logger.exception(f"recent_trades")

    def set_leverage(self, symbol, leverage):
        """making a name more meaningfull for me"""
        return self.isolate_margin(symbol, leverage)

    def ticker_data(self, symbol=None):
        """Get ticker data."""
        if symbol is None:
            symbol = self.symbol
        try:
            return self.ws.get_ticker(symbol)
        except Exception:
            self.logger.exception(f"ticker_data")

    @authentication_required
    def withdraw(self, amount, fee, address):
        path = "user/requestWithdrawal"
        postdict = {"amount": amount, "fee": fee, "currency": "XBt", "address": address}
        sleep(HTTP_SIMPLE_RATE_LIMITE)
        return self._curl_bitmex(
            path=path, postdict=postdict, verb="POST", max_retries=0
        )

    def checking_positive_value(self, dico, *keys):
        """check if the dico[key] are positive, else raise an exception."""
        positiveValues = [dico[k] > 0 for k in keys if dico.get(k, False)]
        assert all(positiveValues), f"All Prices {keys} must be positive in {dico}"

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
        exception=None,
    ):
        """Renvois la réponse du borker après un retry
        Relance l'ordre un certain nombre de fois, un après avoir attendu quelques secondes.
        Nécessite les même argurments que _curl_bitmex et dans le même ordre"""
        self.retries += 1

        if self.retries >= max_retries:
            raise ke.MaxRetries(
                e=exception, load=postdict, extra=f"{path}: Max retries hit"
            )

        tps1 = random.uniform(1, 2)
        tps2 = random.uniform(4, 10) if self.retries > 3 else 0
        tps3 = random.uniform(20, 80) if self.retries > 6 else 0
        self.logger.warning(
            f"retry {verb} {path} {self.retries}/{max_retries}: "
            f"Pause de {round_sprice(tps1+tps2+tps3, self.symbol)} sec:"
            f' postdict={dumps(postdict or "")}'
        )
        # going to sleep
        sleep(tps1) if fast else sleep(round_sprice(tps1 + tps2 + tps3, self.symbol))

        return self._curl_bitmex(
            path, query, postdict, timeout, verb, rethrow_errors, max_retries
        )

    def change_orderId(self, postdict):
        """Change l'id d'un ordre pour que le retry ne pose pas de problème."""

        if "orders" in postdict:  # in case of bulk orders
            postdict["orders"] = [self.change_orderId(o) for o in postdict["orders"]]
        else:
            oID = postdict["orderID"]
            # on récupère l'abbrevation de l'ancien si possible
            nID = newClID(abbv_=get_abbv_from_ID(oID))

            postdict.update({"orderID": nID})

        self.logger.warning(f"Changed old ID {oID} --> new ID {nID}")

        return postdict

    def get_bucketed_trades(
        self,
        startDate=None,
        endDate=None,
        binSize="1m",
        extra_filter=None,
        count=100,
        columns=None,
        reverse=True,
    ):
        """Returns historical data bucketed by binSize
        -  columns: timestamp, symbol,  open,  high,  low,  close,  trades,  volume,  vwap,\
        lastSize,  turnover,  homeNotional,  foreignNotional,
        - count
        - option for bin = [1m,5m,1h,1d].
        endDate and startDate format 2016-12-27T11:00Z (isoformat)"""
        if endDate is None:
            endDate = now().isoformat()
        if startDate is None:
            units = multiply_time_unit(24, TC[binSize])
            startDate = (
                pd.Timestamp(dt.datetime.now() - pd.Timedelta(units))
                .round(TC[binSize])
                .isoformat()
            )

        path = "trade/bucketed"
        query = {
            "symbol": self.symbol,
            "binSize": binSize,
            "count": count,
            "startDate": startDate,
            "endDate": endDate,
            "reverse": reverse,
        }

        if columns is not None:
            query["columns"] = columns
        if extra_filter is not None:
            query["filter"] = extra_filter

        verb = "GET"

        if count < 750:
            sleep(HTTP_SIMPLE_RATE_LIMITE)
            trades = self._curl_bitmex(path, query=query, verb=verb)
        else:
            # paginer avec start et end date
            sleep(HTTP_SIMPLE_RATE_LIMITE)
            trades = self._curl_bitmex(path, query=query, verb=verb)
        return trades
