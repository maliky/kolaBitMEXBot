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
        brg,
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
        self.brg = brg
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

        self.logger = get_logger(logger, sLL="DEBUG", name=__name__)

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
        # relative_values = self.get_condition_values()
        # self.logger.debug(f"After {self.__repr__(False)} relative_values={relative_values}")

        execValidation = {}
        while not self.stop:

            _has_been_hooked = self.hasbeen_hooked()
            # relative condition updated
            # and if hooked start_time for timeout too
            if _has_been_hooked:
                
                self.startTime = now()  # initiate the timeout
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

    def hasbeen_hooked(self):
        """
        Update self.is_hooked.

        If newly hooked update conditions relative to new price and time,
        else return self.is_hooked
        """
        self.logger.debug(
            f" $$$ is_hooked={self.is_hooked} and condition,\n "
            f"{self.condition.__repr__(short=False)}, "
            f"cond is hooked ? {self.condition.is_hooked()}, "
            f"hookedSrcID={self.condition.hookedSrcID}"
        )

        if self.is_hooked:
            return True

        if self.condition.is_hooked():
            self.logger.info(f"*** self.condition={self.__repr__(short=False)}")
            self.is_hooked = True
            self.startTime = now()
            self.logger.debug(f"* *Before update* * {self.__repr__(False)}")
            self.update_cond_with_relative_values()
            self.logger.info(
                f"$$*$ {self} is hooking with ID '{self.condition.hookedSrcID}'\n"
                f"_Newly Updated conditions_:\n "
                f"- from:\nself.init_cond={self.init_cond_frame}\n"
                f"- to:\n{self.condition.__repr__(False)}"
            )

        return None

    def get_current_price(self):
        """Renvoi un prix de la condition."""
        current_price = self.condition.get_current_price()
        self.logger.info(f"current_price={current_price}")
        return current_price

    def update_price_with_relatie_values(self):
        """Change l'ordre pour qu'il soit mise à jour par chronos."""
        old_order = self.order.copy()

        old_price = self.order.get("price", None)
        old_stopPx = self.order.get("stopPx", None)

        high = self.get_new_cond_values("price", "<")
        low = self.get_new_cond_values("price", ">")

        new_price = (high + low) / 2

        self.order["price"] = new_price

        if old_stopPx is not None:
            assert old_price is not None, f"oldprice none but oldstop ok, self={self}"

            price_delta = old_price - old_stopPx
            self.order["stopPx"] = new_price - price_delta

        self.logger.info(f"old_order={old_order}, new_order={self.order}")

    def update_cond_with_relative_values(self):
        """Met à jour les valeurs des conditions."""
        for op_, genre_ in product(["<", ">"], ["price", "temps"]):
            new_value = self.get_new_cond_values(genre_, op_)
            self.logger.debug(f"Updating cond genre_={genre_}, op_={op_}, new_value={new_value}")
            self.condition = self.condition.update_cond(genre_, op_, new_value)

        return None

    def get_new_cond_values(self, genre_, op_):
        """Renvoie les nouvelles valeurs relatives aux prix et temps courants."""
        relative_condition_values = self.get_relative_condition_values()

        if genre_ == "price":
            relative_values, _prixCourant = relative_condition_values[genre_]
            base_value = self.get_current_price()
        elif genre_ == "temps":
            relative_values = relative_condition_values[genre_]
            base_value = now()
        else:
            raise Exception(f"genre={genre_} pas pris en compte pour le moment.")
        
        return base_value + relative_values[op_]

    def get_relative_condition_values(self):
        """renvois les valeurs de la condition."""
        _lowT, _highT = self.condition.get_relative_lh_temps()
        _lowP, _highP, prixCourant = self.condition.get_relative_lh_price()

        return {
            "temps": {">": _lowT, "<": _highT},
            "price": ({">": _lowP, "<": _highP}, prixCourant)
        }

    def conditions_remplies(self):
        """Check that conditions to start the ordrer."""
        cond_validated = self.condition.is_(True)
        not_market_order = self.order["ordType"] != "Market"

        return cond_validated or not_market_order
