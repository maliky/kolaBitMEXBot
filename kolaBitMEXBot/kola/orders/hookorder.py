# -*- coding: utf-8 -*-
"""Hooked Order"""
from itertools import product
from time import sleep
from pandas import DataFrame

from kolaBitMEXBot.kola.utils.general import trim_dic
from kolaBitMEXBot.kola.orders.ordercond import OrderConditionned
from kolaBitMEXBot.kola.orders.condition import Condition
from kolaBitMEXBot.kola.utils.datefunc import now


class HookOrder(OrderConditionned):
    """Hooked orders."""

    def __init__(
        self,
        hSrc,
        hStatus,
        send_queue,
        order,
        cond,
        valid_queue,
        nameT,
        timeout,
        brg,
        logLevel_="INFO",
        excludeIDs_=None,
        symbol="XBTUSD",
    ):
        """
        Init a Hook ordre object.

        It's an OrderConditionned with an additionnal hooked condition.
        - hSrc : abbrevattion used to identify the clOrdID.
        - hStatus : the hook OrdType that the clOrdID must satisfy.
        """
        # Save the clOrdID of the src order to which this whas hooked
        self.hSrc = hSrc
        self.hStatus = hStatus
        self.is_hooked: bool = False
        self.excludeIDs = excludeIDs_
        self.symbol = symbol
        self.relative_values = {}
        self.brg = brg
        # for the symbol need some change to hook on other symbols
        OrderConditionned.__init__(
            self,
            send_queue=send_queue,
            order=order,
            cond=cond,
            valid_queue=valid_queue,
            logName_=__name__,
            logLevel_=self.logLevel,
            nameT=nameT,
            timeout=timeout,
            symbol=symbol,
        )

        _cond: Condition = self.condition
        self.init_cond_frame: DataFrame = _cond.cond_frame.copy()

        # self.logger = get_logger(logger, sLL="INFO", name=__name__)

    def __repr__(self, short=True):
        """Repr self."""
        rep = f"----HookOrder: {self.oclid[:10]} if {self.hStatus}@{self.hSrc}."
        relative_cond = "Set" if len(self.relative_values) else "not Set"
        if not short:
            rep += "\n" + f"----Détails: {self.order}"
            rep += "\n" + f"----Relative conditions {relative_cond}"
            rep += "\n" + f"----{self.condition.__repr__(short)}"

        return rep

    def run(self):
        """Tourne jusqu'à ce qu'au stop ou jusqu'à realisation de la condition."""
        self.logger.info(f"#### Starting {self}")

        execValidation = {}
        while not self.stop:

            _has_been_hooked = self.hasbeen_hooked()
            # relative condition updated
            # and if hooked start_time for timeout too
            if _has_been_hooked:

                self.logger.debug(f"We have reseted startTime to {self.startTime}")

                out = self.condition.is_(True) or self.order["ordType"] != "Market"

                reason_out = (
                    "Is not market Order"
                    if self.order["ordType"] != "Market"
                    else "Conditions are True"
                )

                # self.conditions_remplies()
                self.logger.debug(f"{reason_out} so out is {out} for {self}")

                if out and not self.timed_out():
                    # Envoi l'ordre à chronos qui gère le suivi de la bonne execution

                    self.logger.info(f"Déclenchement {self}, {out}.")
                    # prix doit être updated avoit envois.
                    execValidation = self.send_order()

                    # On garde le clOrdID to which self was hooked
                    self.hookSrcID = self.condition.hookedSrcID
                    break

            # sleep(2+randint(5))  # mitigate rate limite
            sleep(1.05)

        reason = self.explain()

        msg = f'"{reason}" & 1st execValidation={trim_dic(execValidation, trimid=12)}.'
        # at order leave do not close the order if finished.
        self.finalise(close=False, reason=msg)

        # on renvois les informations sur cet ordre pour les chained orders
        return execValidation

    # @throttle(pause=5)
    def hasbeen_hooked(self):
        """
        Update self.is_hooked.

        If newly hooked update conditions relative to new price and time,
        else return self.is_hooked
        """

        if self.is_hooked:
            return True

        if self.condition.is_hooked():
            self.logger.info(f"*is hooked*: {self.__repr__(False)}")

            self.is_hooked = True
            self.startTime = now()

            self.logger.debug("*Updating cond and order*")
            self.update_prices_with_relative_values()
            self.update_cond_with_relative_values()

            self.logger.info(
                f"*After update* {self} is hooking with ID '{self.condition.hookedSrcID}'\n"
                f"_Newly Updated conditions_:\n"
                f"- from:\nself.init_cond={self.init_cond_frame}\n"
                f"- to:\n{self.condition.__repr__(False)}"
            )
            return True

        return False

    def get_current_price(self):
        """Renvoi un prix de la condition."""
        current_price = self.condition.get_current_price()
        return current_price

    def update_prices_with_relative_values(self):
        """Change l'ordre pour qu'il soit mise à jour par chronos."""
        _old_order = self.order.copy()
        _side = _old_order["side"]
        _old_price = self.order.get("price", None)
        _old_stopPx = self.order.get("stopPx", None)

        cond_h_price = self.get_new_cond_values("price", "<")
        cond_l_price = self.get_new_cond_values("price", ">")

        decl_price = {"buy": cond_l_price, "sell": cond_h_price}[_side]

        if _old_price is not None:
            self.order["price"] = decl_price

        if _old_stopPx is not None:
            if _old_price is not None:
                price_delta = _old_price - _old_stopPx
                self.order["stopPx"] = decl_price - price_delta
            else:
                self.order["stopPx"] = decl_price

        self.logger.info(f"*updated order*\nold={_old_order},\nnew={self.order}")

        return None

    def update_cond_with_relative_values(self):
        """Met à jour les valeurs des conditions."""
        for genre_, op_ in product(["price", "temps"], ["<", ">"]):
            new_value = self.get_new_cond_values(genre_, op_)
            self.logger.info(
                f"Updating cond `{genre_}`, op={op_}, new_value={new_value}"
            )
            self.condition = self.condition.update_cond(genre_, op_, new_value)

        return None

    def get_new_cond_values(self, genre_, op_):
        """Return an initial condition updated to current price or time value."""
        _lowD, _highD, _initV = self.condition.get_relative_low_high(genre_)
        relative_val = {">": _lowD, "<": _highD}
        current_val = {"temps": now(), "price": self.get_current_price()}

        self.logger.info(
            f"~'{genre_, op_}'~ init={_initV}, rel_val={relative_val[op_]},"
            f" current_val={current_val[genre_]}"
        )

        return current_val[genre_] + relative_val[op_]

    def conditions_remplies(self):
        """Check that conditions to start the ordrer."""
        cond_validated = self.condition.is_(True)
        not_market_order = self.order["ordType"] != "Market"

        return cond_validated or not_market_order
