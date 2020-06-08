# -*- coding: utf-8 -*-
from time import sleep
from urllib.parse import urlparse
import decimal
import json
import ssl
import sys
import threading
import websocket
from websocket import create_connection

from kolaBitMEXBot.kola.connexion.auth import generate_nonce, generate_signature
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.general import round_half_up, trim_dic
from kolaBitMEXBot.kola.settings import SYMBOL, ORDERID_PREFIX, TEST_URL
import kolaBitMEXBot.kola.utils.exceptions as ke

# Connects to BitMEX websocket for streaming realtime data or dummy data
# The Marketmaker still interacts with this as if it were a REST Endpoint, but now it can get
# much more realtime data without heavily polling the API.
#
# The Websocket offers a bunch of data as raw properties right on the object.
# On connect, it synchronously asks for a push of all this data then returns.
# Right after, the MM can start using its data. It will be updated in realtime, so the MM can
# poll as often as it wants.


class BitMEXWebsocket:

    # Don't grow a table larger than this amount. Helps cap memory usage.
    MAX_TABLE_LEN = 1000

    def __init__(self, apiKey, apiSecret, logger=None, symbol=None):
        self.__reset()
        self.apiKey = apiKey
        self.apiSecret = apiSecret
        self.logger = get_logger(logger, name=__name__, sLL="INFO")
        self.retries = 1
        # not goog practice as it will be only for one symbol can't do ...arbitrage
        self.wsURL = None  # will contain the wsURL after first connection
        self.symbol = symbol
        self.logger.debug(f"Init {self}")

    def __repr__(self):
        return f"symbol={self.symbol} BitMexWebSocket Object (wsURL={self.wsURL}) with apiKey={self.apiKey}, logger={self.logger}"

    def __del__(self):
        self.exit()
        
    def single_connection(self, endpoint=TEST_URL, symbol=SYMBOL, shouldAuth=True):
        """Erreur pas de __subscribe"""
        self.subscriptions = self.__subscribe(symbol, shouldAuth)
        self.logger.info(f"Creating a connexion")
        self.wsURL = get_wsURL(self.subscriptions, endpoint)
        self.ws = create_connection(self.wsURL, header=self.__get_auth())
        self.logger.info(self.ws.recv())
        return self.ws

    def connect(self, endpoint="", symbol=SYMBOL, shouldAuth=True):
        """Connect to the websocket and initialize data stores."""

        self.symbol = symbol
        self.shouldAuth = shouldAuth

        # We can subscribe right in the connection querystring, so let's build that.
        # Subscribe to all pertinent endpoints
        subscriptions = [f"{sub}:{symbol}" for sub in ["quote", "trade"]]
        subscriptions += ["instrument"]  # We want all of them
        if self.shouldAuth:
            subscriptions += [f"{sub}:{symbol}" for sub in ["order", "execution"]]
            subscriptions += ["margin", "position"]

        # Get WS URL and connect.  Serais mieux de faire avec request
        urlParts = list(urlparse(endpoint))
        urlParts[0] = urlParts[0].replace("http", "ws")
        urlParts[2] = "/realtime?subscribe=" + ",".join(subscriptions)
        #        wsURL = urlparse(urlParts)
        wsURL = "{0}://{1}{2}".format(*urlParts)
        self.__connect(wsURL)
        self.wsURL = wsURL

        # Connected. Wait for partials
        self.__wait_for_symbol(symbol)
        if self.shouldAuth:
            self.__wait_for_account()

    #
    # Data methods
    #
    def get_instrument(self, symbol=SYMBOL):
        instruments = self.data["instrument"]
        matchingInstruments = [i for i in instruments if i["symbol"] == symbol]
        if len(matchingInstruments) == 0:
            raise Exception("Unable to find instrument or index with symbol: " + symbol)
        instrument = matchingInstruments[0]
        # Turn the 'tickSize' into 'tickLog' for use in rounding
        # http://stackoverflow.com/a/6190291/832202
        instrument["tickLog"] = (
            decimal.Decimal(str(instrument["tickSize"])).as_tuple().exponent * -1
        )
        return instrument

    def get_ticker(self, symbol=SYMBOL):
        """Return a ticker object. Generated from instrument."""

        instrument = self.get_instrument(symbol)

        # If this is an index, we have to get the data from the last trade.
        if instrument["symbol"][0] == ".":
            ticker = {}
            ticker["mid"] = ticker["buy"] = ticker["sell"] = ticker[
                "last"
            ] = instrument["markPrice"]
        # Normal instrument
        else:
            bid = instrument["bidPrice"] or instrument["lastPrice"]
            ask = instrument["askPrice"] or instrument["lastPrice"]
            ticker = {
                "last": instrument["lastPrice"],
                "buy": bid,
                "sell": ask,
                "mid": (bid + ask) / 2,
            }

        # The instrument has a tickSize. Use it to round values.
        return {
            k: round_half_up(float(v or 0), instrument["tickSize"])
            for k, v in ticker.items()
        }

    def funds(self):
        return self.data["margin"][0]

    def market_depth(self, symbol=SYMBOL):
        raise NotImplementedError(
            "orderBook is not subscribed; use askPrice and bidPrice on instrument"
        )
        # return self.data['orderBook25'][0]

    def open_orders(self, clOrdIDPrefix=ORDERID_PREFIX):
        orders = self.data["order"]
        # Filter to only open orders (leavesQty > 0) and those that we actually placed
        return [
            o
            for o in orders
            if str(o["clOrdID"]).startswith(clOrdIDPrefix) and o["leavesQty"] > 0
        ]

    def exec_orders(self, clOrdIDPrefix=ORDERID_PREFIX):
        """
        Renvois tous mes ordres qui sont dans la table.
        
        Renvois True si l'ordre a été exécuté.
        see https://www.onixs.biz/fix-dictionary/5.0.SP2/msgType_D_68.html
        for order explanation
        """
        orders = self.data["execution"]
        # Filter to only open orders (leavesQty > 0) and those that we actually placed
        return [o for o in orders if str(o["clOrdID"]).startswith(clOrdIDPrefix)]

    def position(self, symbol_=None):
        """Get the position for symbol."""
        _symbol = self.symbol if symbol_ is None else symbol_
        positions = self.data["position"]
        pos = [p for p in positions if p["symbol"] == _symbol]
        if len(pos) == 0:
            # No position found; stub it
            return {
                "avgCostPrice": 0,
                "avgEntryPrice": 0,
                "currentQty": 0,
                "symbol": _symbol,
            }
        return pos[0]

    def recent_trades(self):
        return self.data["trade"]

    #
    # Lifecycle methods
    #
    def error(self, err):
        self._error = err
        self.exit()

    def exit(self):
        self.exited = True
        self.ws.close()

    #
    # Private methods
    #
    def __connect(self, wsURL):
        """Connect to the websocket in a thread."""
        ssl_defaults = ssl.get_default_verify_paths()
        sslopt_ca_certs = {"ca_certs": ssl_defaults.cafile}
        self.ws = websocket.WebSocketApp(
            wsURL,
            on_message=self.__on_message,
            on_close=self.__on_close,
            on_error=self.__on_error,
            header=self.__get_auth(),
        )

        self.wst = threading.Thread(
            target=lambda: self.ws.run_forever(sslopt=sslopt_ca_certs)
        )
        self.wst.daemon = True
        self.wst.start()

        # Wait for connect before continuing
        conn_timeout = 5
        while (
            (not self.ws.sock or not self.ws.sock.connected)
            and conn_timeout
            and not self._error
        ):
            sleep(1)
            conn_timeout -= 1

        if not conn_timeout or self._error:
            self.logger.warning(
                f"Ending the connexion because not conn_timeout={not conn_timeout} or self._error={self._error}"
            )
            self.exit()
            sys.exit(1)

    def __reconnect(self):
        self.logger.warning(f"Reconnecting...")
        self.__connect(self.wsURL)
        self.__wait_for_symbol(self.symbol)
        if self.shouldAuth:
            self.__wait_for_account()

    def __get_auth(self):
        """Return auth headers. Will use API Keys if present in settings."""
        if self.shouldAuth is False:
            return []

        # To auth to the WS using an API key, we generate a signature of a nonce and
        # the WS API endpoint.
        nonce = generate_nonce()
        return [
            "api-nonce: " + str(nonce),
            "api-signature: "
            + generate_signature(self.apiSecret, "GET", "/realtime", nonce, ""),
            "api-key:" + self.apiKey,
        ]

    def __wait_for_account(self):
        """On subscribe, this data will come down. Wait for it."""
        # Wait for the keys to show up from the ws
        while not {"margin", "position", "order"} <= set(self.data):
            self.logger.debug(f"len data = {len(set(self.data))}")
            sleep(0.1)

    def __wait_for_symbol(self, symbol=SYMBOL):
        """On subscribe, this data will come down. Wait for it."""
        while not {"instrument", "trade", "quote"} <= set(self.data):
            sleep(0.1)

    def __send_command(self, command, args):
        """Send a raw command."""
        self.ws.send(json.dumps({"op": command, "args": args or []}))

    def __on_message(self, message):
        """Handler for parsing WS messages."""
        message = json.loads(message)
        # self.logger.debug(json.dumps(message))  # interesting but dict too much
        # table = message["table"] if "table" in message else None
        # action = message["action"] if "action" in message else None

        # je ne comprends pas la dif avec.. il n'y en a pas
        table = message.get("table", None)
        action = message.get("action", None)
        try:
            if "subscribe" in message:
                if ~message["success"]:
                    pass
                else:
                    self.error(
                        f"Unable to subscribe to {message['request']['args'][0]}."
                        f" Error: \"{message['error']}\" Please check and restart."
                    )
            elif "status" in message:

                if message["status"] == 400:
                    self.error(message["error"])

                if message["status"] == 401:
                    self.error("API Key incorrect, please check and restart.")

            elif action:

                if table not in self.data:
                    self.data[table] = []

                if table not in self.keys:
                    self.keys[table] = []

                # There are four possible actions from the WS:
                # 'partial' - full table image
                # 'insert'  - new row
                # 'update'  - update row
                # 'delete'  - delete row
                if action == "partial":
                    self.logger.debug(f"{table}: partial")
                    self.data[table] += message["data"]
                    # typesK = list(message.get('types', {}).keys())
                    # filterD = message.get('filter', None)
                    # self.logger.debug(f"{table}: partial, typeK={typeK},
                    # ... filterD={filterD}")
                    # self.data[table] += message['data']

                    # Keys are communicated on partials to let you know how
                    # to uniquely identify
                    # an item. We use it for updates.
                    self.keys[table] = message["keys"]

                elif action == "insert":

                    mdata = message["data"]

                    if table == "execution":
                        _stars = "*"
                        self.logger.debug(
                            f"{_stars}{table}: inserting{_stars}"
                            f" {[trim_dic(d, trimid=12) for d in mdata]}"
                        )

                    self.data[table] += mdata

                    # Limit the max length of the table to avoid excessive
                    # memory usage.
                    # Don't trim orders because we'll lose valuable state if we do.
                    if (
                        table not in ["order", "orderBookL2"]
                        and len(self.data[table]) > BitMEXWebsocket.MAX_TABLE_LEN
                    ):
                        self.data[table] = self.data[table][
                            (BitMEXWebsocket.MAX_TABLE_LEN // 2) :
                        ]

                elif action == "update":

                    # filtre data based on symbol
                    # message['data'] = [elt for elt in message.get('data', [])
                    #                    if elt.get('symbol', '') == self.symbol]

                    # if len(message['data']):
                    #     self.logger.debug(f'{table}: updating {message["data"]}')

                    # Locate the item in the collection and update it.
                    for updateData in message["data"]:
                        item = findItemByKeys(
                            self.keys[table], self.data[table], updateData
                        )
                        if not item:

                            continue  # No item found to update.
                        # Could happen before push
                        # dans la version brute de ce programm il y a ci-dessus
                        # un return

                        # Log executions
                        if table == "order":
                            is_canceled = (
                                "ordStatus" in updateData
                                and updateData["ordStatus"] == "Canceled"
                            )
                            if "cumQty" in updateData and not is_canceled:
                                contExecuted = updateData["cumQty"] - item["cumQty"]
                                if contExecuted > 0:
                                    instrument = self.get_instrument(item["symbol"])
                                    iside, isymbol = item["side"], item["symbol"]
                                    itick, iprice = instrument["tickLog"], item["price"]
                                    self.logger.info(
                                        f"Confirme Execution: {iside} {contExecuted} 1$ Contracts at the current {isymbol} price of {iprice}$ and {itick}"
                                    )
                                    raise Exception(f"{item}")

                        # Update this item.
                        item.update(updateData)

                        # Remove canceled / filled orders
                        if table == "order" and item["leavesQty"] <= 0:
                            self.data[table].remove(item)

                elif action == "delete":
                    self.logger.debug("%s: deleting %s" % (table, message["data"]))
                    # Locate the item in the collection and remove it.
                    for deleteData in message["data"]:
                        item = findItemByKeys(
                            self.keys[table], self.data[table], deleteData
                        )
                        self.data[table].remove(item)
                else:
                    raise Exception("Unknown action: %s" % action)
        except Exception:
            pass

    def __on_open(self):
        self.logger.debug("Websocket Opened.")

    def __on_close(self):
        self.logger.info("ws is closed!")
        self.exit()

    def __on_error(self, error):
        self.logger.error(f"WS Error, retries={self.retries}: error='{error}'. Pause")
        sleep(60 * self.retries)
        self.retries *= 2
        # faire un système de gestion des erreur low level
        if error == "Connection is already closed":
            self.logger.error(f'error="{error}", reconnecting...')
            self.__reconnect()
        elif not self.exited:
            self.error(error)
            raise ke.wsException(error)

    def __reset(self):
        self.data = {}
        self.keys = {}
        self.exited = False
        self._error = None


def findItemByKeys(keys, table, matchData):
    """parcours les items de la table.
Pour chaque item vérifie que tous les éléments clefs sont les même que ceux de match Data.
Sinon, si l'un des éléments clefs est !=, ne renvois pas l'item"""
    for item in table:
        matched = True
        for key in keys:
            if item[key] != matchData[key]:
                matched = False
        if matched:
            return item


def get_wsURL(subscriptions, endpoint=TEST_URL):
    # Get WS URL and connect. Serais mieux de faire avec request
    urlParts = list(urlparse(endpoint))
    urlParts[0] = urlParts[0].replace("http", "ws")
    urlParts[2] = "/realtime?subscribe=" + ",".join(subscriptions)
    #        wsURL = urlparse(urlParts)
    wsURL = "{0}://{1}{2}".format(*urlParts)
    return wsURL
