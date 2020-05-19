# -*- coding: utf-8 -*-
import threading
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.datefunc import now, setdef_timedelta
from kolaBitMEXBot.kola.utils.general import (
    contains,
    log_args,
    trim_dic,
    sort_dic_list,
    opt_pop_if_in_,
)
from kolaBitMEXBot.kola.utils.pricefunc import setdef_stopPrice
from kolaBitMEXBot.kola.utils.orderfunc import get_order_from
from kolaBitMEXBot.kola.orders.trailstop import TrailStop
from kolaBitMEXBot.kola.orders.orders import (
    place,
    place_stop,
    place_at_market,
    place_SL,
    place_MIT,
    place_LIT,
    amend_prices,
    get_execPrice,
    cancel_order,
)
from time import sleep
import pickle
import kolaBitMEXBot.kola.utils.exceptions as ke
import logging

# from kolaBitMEXBot.kola.orders import orders
import pandas as pd
import queue


class Chronos(threading.Thread):
    # Cet objet s'assure que les orders reçus sont bien exécutés.
    # et il sert d'interface à plusieur thread vers la même connexion

    def __init__(self, brg, recpt_queue, valid_queue=None, logger=None, nameT="chrsT"):
        """Un thread qui tourne jsuqu'à ce que stop soit vrai.
        utilise brg pour passer les orders reçu dans la queue.
        vérifie la queue chaque freq secondes"""
        threading.Thread.__init__(self, name=nameT)
        self.brg = brg
        self.recpt_queue = recpt_queue
        self.valid_queue = valid_queue
        self.reply_queue = queue.Queue()
        self.stop = False
        self.logger = get_logger(logger, name=__name__, sLL="INFO")

        self.logger.info(f"Fini init {self}")

    def __repr__(self):
        queues = {
            "reception": self.recpt_queue,
            "validation": self.valid_queue,
            "reply": self.reply_queue,
        }
        rep = f"Chronos thread, using queues {queues}"
        return rep

    def run(self):
        """Tourne jusqu'à ce que stop soit mis en faute."""
        self.logger.info("Chronos started...")

        while not self.stop:
            self.logger.info("Chronos en écoute...")

            # on bloque le thread
            # attend ordre et oid associés qui arrive dans cette queue
            rcvLoad = self.recpt_queue.get(block=True)
            self.logger.debug(f"Chronos received load: {rcvLoad}")

            # Bientôt volume et autre KPI

            sender = rcvLoad["sender"]
            timeOut = rcvLoad["timeOut"]

            # make a deep copy of the order to avoid changing rcvLoad
            rcvOrder = pickle.loads(pickle.dumps(rcvLoad["order"]))
            ordType = rcvOrder.pop("ordType", None)
            assert ordType, f"Nous devons avoir un ordType dans le rcvOrder {rcvOrder}"

            execInst = rcvOrder.get("execInst", "")  # TriggeredOrActivatedBySystem

            try:
                # 'Limit', 'Market', 'Stop', 'MarketIfTouched',
                # 'StopLimit', 'LimitIfTouched'
                side = rcvOrder.pop("side")
                orderQty = rcvOrder.pop("orderQty")

                # pop le prix si dans le rcvOrder
                # sinon le prix du marché en fonction du side, en market_maker
                price = self.pop_price_from_(rcvOrder, side, execInst)

                # renvois un stopPx par défaut si ordType le nécessite
                stopPx = self.pop_stopPx_from_(rcvOrder, price, side, ordType)

                if ordType == "Market":
                    rcvOrder["execInst"] = opt_pop_if_in_("price", rcvOrder["execInst"])
                    timeOut = pd.Timedelta(5, unit="m")  # pourquoi ?
                    self.reply_queue.put(
                        place_at_market(self.brg, orderQty, side, **rcvOrder)
                    )

                elif ordType == "Limit":
                    rcvOrder["execInst"] = opt_pop_if_in_("price", rcvOrder["execInst"])
                    self.reply_queue.put(
                        place(self.brg, side, orderQty, price, **rcvOrder)
                    )

                elif ordType == "Stop":
                    self.reply_queue.put(
                        place_stop(self.brg, side, orderQty, stopPx, **rcvOrder)
                    )

                elif ordType == "StopLimit":
                    self.reply_queue.put(
                        place_SL(self.brg, side, orderQty, stopPx, price, **rcvOrder)
                    )

                elif ordType == "MarketIfTouched":
                    self.reply_queue.put(
                        place_MIT(self.brg, side, orderQty, stopPx, **rcvOrder)
                    )

                elif ordType == "LimitIfTouched":
                    self.reply_queue.put(
                        place_LIT(self.brg, side, orderQty, stopPx, price, **rcvOrder)
                    )

                elif ordType.startswith("amend"):
                    # One of the previous type (except Market) prefixed with 'amend'
                    orderID = rcvOrder.pop("orderID")
                    newPrice = rcvOrder.pop("newPrice")
                    text = rcvOrder.pop("text", "")
                    self.reply_queue.put(
                        amend_prices(
                            self.brg,
                            orderID,
                            newPrice,
                            ordType,
                            side,
                            absdelta=2,
                            text=text,
                        )
                    )
                elif ordType == "cancel":
                    timeOut = pd.Timedelta(1, unit="m")  # pourquoi ?
                    clOrdID = rcvOrder.pop("clOrdID")
                    self.reply_queue.put(cancel_order(self.brg, {"clOrdID": clOrdID}))
                else:
                    expmsg = f"Action type '{ordType}' pas prise en compte"
                    raise Exception(expmsg)

                # gestion des conditions de validation de l'ordre
                valconditions = [{"exectype": "Trade", "orderstatus": "Filled"}]

                if ordType in [
                    "Stop",
                    "MarketIfTouched",
                    "StopLimit",
                    "LimitIfTouched",
                ] and isinstance(sender, TrailStop):
                    valconditions = [{"exectype": "New", "orderstatus": "New"}]

                elif ordType.startswith("amend"):
                    valconditions = [{"exectype": "Replaced", "orderstatus": "New"}]

                elif ordType == "cancel":
                    # devrait toujours valider
                    valconditions = [
                        {"exectype": "Canceled", "orderstatus": "Canceled"}
                    ]

                # si pas d'exception c'est que l'ordre est bien transmit au broker
                # Reste à vérifier l'execution
                kwargs = {
                    "timeout": timeOut,
                    "rcvload": rcvLoad,
                    "waitstep": 0.1,  # temps entre les vérification (intern)
                    "valconditions": valconditions,
                }

                # on lance un thread qui tourne jusqu'à validation
                threadName = (
                    f'VT-{rcvLoad["order"]["clOrdID"].replace("mlk_", "")[:10]}'
                )
                self.logger.info(f"{threadName} check validation avec {kwargs}")
                threading.Thread(
                    target=self.wait_for_change, name=threadName, kwargs=kwargs
                ).start()

            except KeyError as k:
                self.logger.exception(f"rcvOrder={rcvOrder} et rcvLoad={rcvLoad}")
                raise k

            except (ke.InvalidOrdStatus, ke.InvalidOrderID) as e:
                if ordType.startswith("amend"):
                    self.logger.error(f"Amending failed.  No validation!")
                    self.valid_queue.put(
                        {
                            "brokerReply": False,
                            "exgLoad": rcvLoad,
                            "execValidation": False,
                        }
                    )
                else:
                    raise (e)

            except ke.InvalidOrderQty:
                self.logger.error(f"Canceling order and closing the essai.")
                self.valid_queue.put(
                    {"brokerReply": False, "exgLoad": rcvLoad, "execValidation": False}
                )

            except ke.InsufficientBalance:
                self.logger.error(f"Insufficient Balance, Closing the essai.")
                # we do so because to keep consistency with attached stop tail
                self.logger.warning(f"Replacing 80% of the rcvLoad {rcvLoad}")
                # attention chronos pourrait traiter un autre ordre que celui générant l'erreur, non ?
                sender = rcvLoad["sender"]
                overQty = sender.order.get("orderQty", 0)
                reducedQty = round(overQty * 0.8)
                if reducedQty < 31:
                    self.logger.exception(f"Canceling order.  Closing the essai?")
                    self.valid_queue.put(
                        {
                            "brokerReply": False,
                            "exgLoad": rcvLoad,
                            "execValidation": False,
                        }
                    )
                else:
                    sender.order["orderQty"] = reducedQty
                    sender.send_order()

            except ke.InvalidOrder as io:
                self.logger.error(f"Invalid order? {rcvOrder}")
                self.log_reply()
                raise io

            except Exception as e:
                # Si on arrive ici il y a probablement un gros pb de connexion
                # que faire ?
                # vraisemblablement d'un pique au niveau de bitmex avec affluence
                # if no money handle
                self.logger.exception(f"Unknown exception. RdvOrder={rcvOrder}")
                self.log_reply()
                raise e

    def log_reply(self, absMsg="No reply available"):
        """Log the reply if available"""
        reply = self.wait_for_reply(block=False)
        if reply is None:
            self.logger.error(absMsg)
        else:
            self.logger.error(f"Reply={trim_dic(reply, trimid=12)}")
            self.reply_queue.put(reply)

    # @log_args(level='DEBUG')
    def wait_for_reply(self, block=True, timeout=None):
        """Get the reply from the queue, return None if timeout reached."""
        try:
            # if block is false and nothing in queue raise queue.Empty
            reply = self.reply_queue.get(block, timeout)
            if block and timeout <= 0:
                self.logger.debug(
                    f"timeout={bool(timeout)} and block={block} while "
                    f"reply={reply, type(reply)}"
                )

            if isinstance(reply, list):
                assert len(reply) <= 1, (
                    f"Reply is too long. Que choisir ?"
                    f"Reply={trim_dic(reply, trimid=12)}"
                )
                reply = reply[0]

            return reply
        except queue.Empty:
            self.logger.error(
                f"reply={trim_dic(reply, trimid=12) if reply is not None else 'reply is None'},"
                f" timeout={timeout}, block={block}"
            )

    def get_ID_from(self, rcvOrder, idType="clOrdID"):
        """Return the ID from the rcvOrder (a dict containing an order)."""
        assert rcvOrder is not None, f"rcvOrder={rcvOrder} should not be None."
        try:
            return get_order_from(rcvOrder).get(idType)
        except Exception as e:
            self.logger.error(f"rcvOrder, idType={rcvOrder, idType}")
            raise (e)

    def wait_for_change(
        self,
        valconditions=None,
        rcvload=None,
        timeout: pd.Timedelta = None,
        waitstep: float = 1,
    ):
        """
        Boucle qui attend de recevoir dans order execution from ws.

        ordertype: orderstatus.
        - timeout doit être un pd.Timedelta, 
        - waitStep en second
        ordertype is 'ordStatus our triggered et ordstatus is
        """
        self.logger.debug(f"Thread started.")

        # defaults
        timeOut = setdef_timedelta(timeout, default=pd.Timedelta(60, unit="m"))

        clOrdID = (
            self.brg.bto.dummyID
            if self.brg.bto.dummy
            else self.get_ID_from(rcvload, "clOrdID")
        )

        startTime = now()

        def update_timeleft(timeout=timeOut, starttime=startTime, now_=None):
            """
            Update time left
            
            -now_: is sometime set by default.
            """
            _now = now_ if now_ else now()
            # convert to seconds +/-
            _timeleft = (timeout + starttime - _now).delta / 10e8

            return _timeleft if _timeleft > 0 else 0

        timeLeft = update_timeleft()

        while timeLeft > 0 and not self.is_changed_(clOrdID, valconditions):
            sleep(waitstep)
            timeLeft = update_timeleft()
            if timeLeft % 298 == 0:
                # logging every 4:58
                self.logger.info(f"timeLeft={timeLeft}s. Still waiting...")
                sleep(1)  # avoid too much logging and throwtlle the system

        # block until next reply
        reply = self.wait_for_reply(timeout=timeLeft)

        # problème avec les reply None

        if rcvload["order"]["ordType"] == "cancel":
            self.logger.info(f"ordType is cancel. rcvload={rcvload}")
            replyID = rcvload["order"]["clOrdID"]
        else:
            # mais si ce n'est pas la bonne reply ?
            replyID = (
                rcvload["order"]["clOrdID"]
                if reply is None
                else self.get_ID_from(reply, "clOrdID")
            )

        seenReplyIDs = {}

        while replyID != clOrdID and timeLeft:
            # saving ID already seen to avoid clutering logs
            if replyID is not None:
                seenReplyIDs[replyID] = seenReplyIDs.get(replyID, 0) + 1

            if seenReplyIDs.get(replyID, 0) < 2:
                self.logger.debug(
                    f"*No match ID* for clOrdID={clOrdID}"
                    f" in {trim_dic(reply, trimid=12)}!"
                )
            if reply:
                # to be sure not to put Nones back in the loop
                self.reply_queue.put(reply)

            try:
                # can get None here, not good... if timeout..
                reply = self.wait_for_reply(timeout=timeLeft)
                replyID = (
                    clOrdID if reply is None else self.get_ID_from(reply, "clOrdID")
                )
            except Exception as e:
                self.logger.error(
                    f"reply={reply}, timeLeft={timeLeft}, replyID={replyID}"
                    f"clOrdID={clOrdID}"
                )
                raise (e)

            timeLeft = update_timeleft()

            self.logger.debug(
                f"Getting out of the loop. seenReplyIds={seenReplyIDs},"
                f" timeLeft={timeLeft}, reply={reply}."
            )

        # ici il y a en fait le cas des reply error d'ammending
        validation = (
            reply
            if timeLeft and reply is not None and not reply.get("error", False)
            else False
        )

        self.logger.info(
            f"Attendu {timeOut - pd.Timedelta(timeLeft, unit='s')}."
            f"Validation is {bool(validation)}."
        )

        self.valid_queue.put(
            {"brokerReply": reply, "exgLoad": rcvload, "execValidation": validation}
        )

    def is_changed_(
        self, ID, valconditions=[{"exectype": "New", "orderstatus": "New"}]
    ):
        """
        Return test if order with ID is exec or canceled and compare to val.

        handles a list of validation conditions.
        """
        statusType = {}
        for dic in valconditions:
            exectype, orderstatus = dic["exectype"], dic["orderstatus"]
            statusType[f"is_{exectype}-{orderstatus}"] = self.ID_type_status_exec(
                ID, exectype, orderstatus
            )

        # by default we handle cancel orders
        statusType["is_canceled"] = self.ID_type_status_exec(ID, "Canceled", "Canceled")

        # self.logger.debug(f'ID={ID[:10]}, statusType={statusType}')

        return any(statusType.values())

    #    @log_args(LOGNAME)
    def ID_type_status_exec(self, ID, exectype="New", orderstatus="New"):
        """
        Renvois un order avec ID et dont le status type est status.

        Défault status Filled et statustype 'ordStatus, exectype default New
        """
        # self.logger.debug(f'ID={ID}, status={status}, statustype={statustype}')
        # on récupère les orders de type voulu
        execOrders = [
            o for o in self.brg.bto.exec_orders() if o["execType"] == exectype
        ]
        ordWstatus = [
            o
            for o in execOrders
            if o["ordStatus"] == orderstatus and o["ordStatus"] != "PartiallyFilled"
        ]

        # logOrders = [trim_dic(o, trimid=12) for o in ordWstatus]
        # self.logger.debug(f'logOrders={logOrders}')

        # on gère le cas test avec dummy brg
        ID = self.brg.bto.dummyID if self.brg.bto.dummy else ID

        # On filtre les orders par ID
        oidWstatus = [o for o in ordWstatus if ID in [o["orderID"], o["clOrdID"]]]

        # logOrders = [trim_dic(o, trimid=12, droptime=True) for o in self.brg.bto.exec_orders()]
        # self.logger.debug(f'logOrders={logOrders}, ID={ID}, orderstatus={orderstatus}, exectype={exectype}')
        def latest(ordList):
            """ given a list of orders return the one with latest transtime"""
            assert isinstance(ordList, list), f"{ordList} should be a list of orders"
            return sort_dic_list(ordList, "transactTime")

        if len(oidWstatus) == 0:
            return {}
        elif len(oidWstatus) == 1:
            return oidWstatus[0]
        else:
            self.logger.warning(
                f"{len(oidWstatus)} previous orders with ID={ID}. Returning latest only"
            )
            try:
                return latest(oidWstatus)
            except TypeError:
                self.logger.exception(f"Returning -1 of oidWstatus={oidWstatus},")
                return oidWstatus[-1]

    def pop_price_from_(self, rcvOrder, side, execInst):
        """
        Get price from rcvOrder.

        Renvois le prix si renseigné, 
        sinon le prix marché de type execInst, 
        sinon lastMidprice
        """
        return rcvOrder.pop(
            "price", get_execPrice(self.brg, side, {"execInst": execInst})
        )

    def pop_stopPx_from_(self, rcvOrder, price, side, ordtype, absdelta=2):
        """
        Pop the stopPx from the rcvOrder.  

        Use class method to facilitate eventual logging.
        if stopPx not in rcvOrder, 
        set de default stopPx based on price side, ordType and absdelta
        """
        stopPx = rcvOrder.pop("stopPx", None)
        # probably not necessary as stop should be set
        absdelta = rcvOrder.pop("sDelta", 2)
        if stopPx is None and contains(["Stop", "Touched"], ordtype):
            stopPx = setdef_stopPrice(price, side, absdelta)

        return stopPx
