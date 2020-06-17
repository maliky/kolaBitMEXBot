# -*- coding: utf-8 -*-
"""Hooked Order"""
from itertools import product
from time import sleep

from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.general import trim_dic
from kolaBitMEXBot.kola.orders.ordercond import OrderConditionned
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
        logger=None,
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

        # for the symbol need some change to hook on other symbols
        OrderConditionned.__init__(
            self,
            send_queue=send_queue,
            order=order,
            cond=cond,
            valid_queue=valid_queue,
            logger=logger,
            nameT=nameT,
            timeout=timeout,
            symbol=symbol,
        )

        self.init_cond_frame = self.condition.cond_frame.copy()

        self.logger = get_logger(logger, sLL="INFO", name=__name__)

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

        # update relative_values
        # self.logger.debug(f"Before update {self.__repr__(False)}")
        # relative_values = self.get_relative_values()
        # self.logger.debug(f"After {self.__repr__(False)} relative_values={relative_values}")

        execValidation = {}
        while not self.stop:

            _has_been_hooked = self.hasbeen_hooked()
            # relative condition updated
            # and if hooked start_time for timeout too
            if _has_been_hooked:

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

    def hasbeen_hooked(self):
        """
        Update self.is_hooked.

        If new hooked update conditions relative to new price and time
        else return only hook
        """
        self.logger.debug(
            f"is_hooked={self.is_hooked} and condition,\n "
            f"{self.condition.__repr__(short=False)}, "
            f"cond is hooked ? {self.condition.is_hooked()}, "
            f"hookedSrcID={self.condition.hookedSrcID}"
        )

        if self.is_hooked:
            return True

        if self.condition.is_hooked():
            self.logger.info(f"self.condition={self.condition}")
            self.is_hooked = True
            self.startTime = now()
            self.logger.debug(f"* *Before update* * {self.__repr__(False)}")
            self.update_cond_with_relative_values()
            self.logger.info(
                f"{self} is hooking with ID '{self.condition.hookedSrcID}'\n"
                f"_Newly Updated conditions_:\n "
                f"- from:\nself.init_cond={self.init_cond_frame}\n"
                f"- to:\n{self.condition.__repr__(False)}"
            )

        return None

    def get_current_price(self):
        """Renvoi un prix de la condition."""
        # ajouter le price type
        return self.condition.get_current_price()

    def update_price_with_relatie_values(self):
        """Change l'ordre pour qu'il soit mise à jour par chronos."""
        old_order = self.order.copy()

        old_price = self.order.get("price", None)
        old_stopPx = self.order.get("stopPx", None)

        high = self.get_relative("price", "<")
        low = self.get_relative("price", ">")

        new_price = (high + low) / 2

        self.order["price"] = new_price

        if old_stopPx is not None:
            assert old_price is not None, f"self={self}"
            price_delta = old_price - old_stopPx
            self.order["stopPx"] = new_price - price_delta

        self.logger.info(f"old_order={old_order}, new_order={self.order}")

    def update_cond_with_relative_values(self):
        for op_, genre_ in product(["<", ">"], ["price", "temps"]):
            new_value = self.get_relative(genre_, op_)
            self.condition.update_cond(genre_, op_, new_value)

        return None

    def get_relative(self, genre_, op_):
        """Renvoie les nouvelles valeurs relative au prix et temps courant."""
        relative_values = self.get_relative_values()

        if genre_ == "price":
            cPrice = self.get_current_price()
            return cPrice + relative_values[genre_][op_]

        elif genre_ == "temps":
            cTime = now()
            return cTime + relative_values[genre_][op_]

    def get_relative_values(self):
        """Relativise les valeurs de la condition."""
        # get timeDelta
        _lowT, _highT = self.condition.get_relative_lh_temps()

        # get priceDelta
        _lowP, _highP, self.priceType = self.condition.get_relative_lh_price()
        self.relative_values = {
            "temps": {">": _lowT, "<": _highT},
            "price": {">": _lowP, "<": _highP},
        }
        return self.relative_values

    def conditions_remplies(self):
        """Check that conditions to start the ordrer."""
        cond_validated = self.condition.is_(True)
        not_market_order = self.order["ordType"] != "Market"

        return cond_validated or not_market_order
