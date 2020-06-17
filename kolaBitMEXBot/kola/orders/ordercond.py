# -*- coding: utf-8 -*-
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.orderfunc import (
    newClID,
    toggle_order,
    get_order_from,
    remove_execInst,
)
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.utils.general import trim_dic
from kolaBitMEXBot.kola.utils.datefunc import setdef_timedelta
from time import sleep
from threading import Thread
import pandas as pd


class OrderConditionned(Thread):
    # Un thread qui court pour un temps donné.
    # Il lance un ordre prédéfini si la condition associée est validée.
    def __init__(
        self,
        send_queue,
        order,
        cond,
        valid_queue=None,
        logger=None,
        nameT=None,
        timeout=None,
        symbol="XBTUSD",
    ):
        """
        Une queue, pour passer les ordres, un ordre à passer si la condition est validée.
        - l'ordre (order) peut être stopé prématurement en mettant stop=True
        - order est un dict avec keys: side, orderQty..
        - un timeout pendant lequel l'ordre (dont l'évaluation de sa condition) est actif
        def 2 jours
        - hook : nom du hook, ou de l'abbrevation qui sert de hook.
        - sLL= debug level
        -symbol: symbol for this order, def. XBTUSD
        """
        Thread.__init__(self, name=nameT)

        self.logger = get_logger(logger, sLL="INFO", name=__name__)
        self.symbol = symbol
        self.send_queue = send_queue
        self.valid_queue = valid_queue
        self._canceled = False
        self.condition = cond
        self.stop = False
        self.orderIDPrefix = "mlk_"
        self.order = order  # a dict ex. {'side': 'buy', 'orderQty': 100, 'options'...}
        self.oclid = newClID(abbv_=nameT)
        self.order["clOrdID"] = self.oclid

        # default for timeOut (2 days)
        # could add a timecond eg cVraieTpsDiffA(timeOut.seconds or delta)
        self.timeOut = setdef_timedelta(timeout, pd.Timedelta(2, unit="D"))
        self.startTime = now()

        self.logger.debug(f"#### Init fini for {self.__repr__(short=False)}")

    def __repr__(self, short=True, suffix="----"):
        """Représenation."""
        rep = f"{suffix}OrderCond: {self.oclid[:10]}"
        if not short:
            rep += "\n" + f"----Détails: {self.order}"
            rep += "\n" + f"----{self.condition.__repr__(short)}"
        return rep

    def elapsed_time(self):
        """Return the elapsed time in seconds."""
        return now() - self.startTime

    def timed_out(self):
        """
        Say if ordre timed_out or not.

        Deux raisons peuvent le time-out.
        1) Il y a une condition de temps qui ne peut plus être vraie,
        2) Le paramètre timeOut de l'orderCondition est arrivé à expiration
        """
        return (self.elapsed_time() >= self.timeOut) or self.condition.timed_out()

    def canceled(self):
        return self._canceled
    
    def add_condition(self, condition):
        """Ajoute une ou des conditions à la condition existante."""
        self.condition.add_condition(condition)

    def get_price_conds(self):
        """Retoure une des conditions prix."""
        self.condition.get_price_conds()

    def run(self):
        """Run until truth exit condition."""
        self.logger.info(f"#### Starting {self.__repr__(False)}")

        execValidation = {}
        while not self.stop and not self.timed_out():

            if self.condition.is_(True) or (self.order["ordType"] != "Market"):
                # Envoi l'ordre à chronos qui gère le suivi de la bonne execution
                condition_sortie = (
                    self.condition
                    if self.condition.is_(True)
                    else f'Immediately placing a {self.order["ordType"]} Order'
                )
                self.logger.info(f"Déclenchement {self} '{condition_sortie}'")
                execValidation = self.send_order()
                if isinstance(execValidation, dict):
                    if execValidation.get('ordStatus', False) == 'Canceled' :
                        self._canceled = True
                break

            # sleep(2+randint(5))  # mitigate rate limite
            sleep(1.05)

        reason = self.explain()

        msg = f'"{reason}" & 1st execValidation={trim_dic(execValidation, trimid=12)}.'
        # at order leave do not close the order if finished.

        self.finalise(close=False, reason=msg)

        # on renvois les informations sur cet ordre pour les chained orders
        return execValidation

    def explain(self):
        """Find reason."""
        reason = ""
        if self.stop:
            reason = "Stop received"
        elif self.condition.is_(True):
            reason = f"{self.condition}"
        elif self.timed_out():
            reason = f"Timed_out: timeout={self.timeOut} while elapsed_time={self.elapsed_time()}"
            rep = self.cancel_order()
            reason += f"\n---- Détails: {rep}"
        elif self.order["ordType"] in ["Limit", "Stop"]:
            # ? pourquoi ce test
            reason = "Self is Limit or Stop order"
        elif self._canceled:
            reason = "Self has been canceled on exec time"

        return reason

    def cancel_order(self):
        """Envois une demande pour annuler l'ordre en cours via chronos."""
        load, _order = self.get_load()
        _order["ordType"] = "cancel"
        self.send_queue.put(load)
        return self.wait_for_broker_reply()

    def get_load(self, order=None):
        """Set the default load pour this order."""
        # un identifiant pour le suivi
        assert self.symbol is not None, f"order={order}"
        load = {"sender": self, "timeOut": self.timeOut, "symbol": self.symbol}
        _order = order if order else self.order
        load["order"] = _order
        return load, _order

    def send_order(self, order=None):
        """
        Passe l'ordre au serveur chronos chargé de le faire executer.

        l'ordre et d'en vérifier la validation.
        """
        load, _order = self.get_load(order)

        ordType = _order.get("ordType", None)
        assert ordType, f"Should have an order Type here but order={order}"

        # check the execInst:
        execInst = _order.get("execInst", None)
        if execInst:
            _order["execInst"] = remove_execInst(execInst, "lastMidPrice")

        self.logger.debug(f"Envoi à Chronos du load={load}")
        self.send_queue.put(load)

        return self.wait_for_broker_reply()

    def wait_for_broker_reply(self):
        """
        Wait for the borker reply.

        Should only get a validated orders but if we get error we could cancel."""
        self.logger.debug(f"{self} waiting for validation")

        while True:
            rcvLoad = self.valid_queue.get(block=True)
            execValidation = rcvLoad["execValidation"]
            try:
                order = get_order_from(rcvLoad["exgLoad"])
            except Exception as e:
                self.logger.error(f"{self}, rcvLoad={rcvLoad}")
                raise (e)

            if self.oclid == order["clOrdID"]:
                self.logger.debug(
                    f"_Validation {bool(execValidation)}_ for order={order},"
                )
                # Normalement execValidation est une reply
                return execValidation
            else:
                # received something that's not for us.  replace in queue
                self.valid_queue.put(rcvLoad)
                sleep(0.1)

    def finalise(self, close=False, reason=None):
        """Finalise somme values depending on reason."""
        reason = f"{self}" if reason is None else f"{reason}"
        # the closing order.  Will reduce only.. Attention si execInst dans order
        if close:
            closing_order = {
                "side": toggle_order(self.order),
                "execInst": "Close",
                "ordType": "Market",
                "ordQty": None,
            }
            self.send_order({"order": closing_order})
            reason += " ... with order closing."

        if self.canceled():
            reason += ">>>> IS CANCELED <<<<"
            
        self.logger.info(f"with {reason}")

