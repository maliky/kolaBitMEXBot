# -*- coding: utf-8 -*-
"""Trail stops"""
from kolaBitMEXBot.kola.orders.condition import Condition
from kolaBitMEXBot.kola.orders.ordercond import OrderConditionned
from kolaBitMEXBot.kola.utils.pricefunc import setdef_stopPrice
from kolaBitMEXBot.kola.settings import API_ERROR_INTERVAL
from kolaBitMEXBot.kola.price import PriceObj
from kolaBitMEXBot.kola.utils.orderfunc import (
    get_logger,
    toggle_sides,
    get_order_from,
    opt_add_to_,
)
import numpy.random as rnd
from time import sleep


class TrailStop(OrderConditionned):
    """
    Object TrailStop.

    Permet de faire un ordre avec trace, et lancement conditionné
    ance un order conditionné simple puis lorsqu'éxécuté lance sa trace, 
    tail ou tail, met à jour la condition pour la trace
    """

    def __init__(
        self,
        main_oc,
        brg,
        pegOffset_perc=0.5,
        updatepause=None,
        logger=None,
        logpause=60,
        nameT=None,
        refPrice=None,
        execinst=None,
        ordtype=None,
    ):
        """Une queue, pour passer les ordres, un ordre à passer si la cond validée.
        
        - l'ordre peut être stopé prématurement en mettant stop=True
        - l'ordre doit être un buy ou sell at market
        - pegOffset_perc in [0, 100] % du prix de ref pour la trace
        - updatepause, c'est le temps moyen à attendre entre deux mise-à-jour
        """
        self.logger = get_logger(logger, sLL="INFO", name=__name__)

        self.main_oc = main_oc
        self.brg = brg
        self.pegOffset_perc = pegOffset_perc
        self.ordType = ordtype

        # trouver une façon de définir ça on the fly basé la volatilité
        self.timeBin = 361

        # le temps en seconde à attendre (approx.) avant de logger les prix
        # def. log les prix toutes les minutes
        self.logPause = 60 if logpause is None else logpause
        self.updatePause = 0.2 if updatepause is None else updatepause
        assert (
            self.logPause >= self.updatePause
        ), f"Trying to log ({self.logPause}) more often than update ({self.updatePause})"

        # Initialisation de la condition de trace
        self.mPrice_type = "LastPrice"

        # markPRICE IS THE FAIRE PRICE != marketPrice (~lastprice)
        # refPrice contient 'IndexPrice', 'MarkPrice' ou 'LastPrice'
        if not refPrice or refPrice == "lastMidPrice":
            self.refPrice_type = "LastPrice"
        else:
            self.refPrice_type = refPrice

        # On définie la vente ou l'achat en fonction du prix.
        # La trace est aussi un ordre Conditionné mais de type Stop.
        # On l'initialise.
        self.execInst = opt_add_to_(execinst, self.refPrice_type)

        self.side = toggle_sides(self.main_oc.order["side"])
        self.init_order = {
            "orderQty": self.main_oc.order["orderQty"],
            "side": self.side,
            "execInst": self.execInst,
            "ordType": self.ordType,
        }

        # Création d'une condtion stop pour le suivi du stop,
        # même si le déclenchement se fera automatiquement sur le marché
        if "sell" in self.side:
            self.op = "<"
            args = (self.refPrice_type, "<", -1)
        else:
            self.op = ">"
            args = (self.refPrice_type, ">", 1e9)

        self.tailStop_triggerCond = Condition(self.brg, args, logger=self.logger)

        OrderConditionned.__init__(
            self,
            # s'assurer que si l'op n'est plus, la queue reste
            send_queue=self.main_oc.send_queue,
            order=self.init_order,
            cond=self.main_oc.condition,
            valid_queue=self.main_oc.valid_queue,
            logger=self.logger,
            nameT=nameT,
        )

    def __repr__(self, tracing=False, short=True, cond=False):
        ret = f"----TrailStop"
        ret += f"{OrderConditionned.__repr__(self, short=short, suffix=' of ')}"
        if cond:
            ret += f"\n----TailTrigger{self.tailStop_triggerCond}"
        if tracing:
            ret += f"\n----Tracing: {self.main_oc}"
        return ret

    def run(self):
        """Tourne jusqu'à un stop explicite où la condition se réalise."""
        self.logger.info(f"#### Starting {self}")

        try:
            # on s'assure que le main order est bien démarré
            self.main_oc.start()

        except RuntimeError as re:
            # Il se peut qu'il soit déjà démarré
            # cas d'un rattachement à un ordre existant
            if "can only" in re.__repr__():
                self.logger.exception(f"{self.main_oc} already started... continue")
                pass

        # On bloque jusqu'à la fin de l'exécution du main_oc.
        # note: il est peut être déjà fini
        self.logger.info(f"Waiting for {self} to effectively join{self.main_oc}")
        try:
            self.main_oc.join()
        except Exception:
            self.logger.exception(f"Joined, {self.main_oc} was already done")
            # Ici main_oc doit avoir été correctement executé ou canceled (check Chronos)

        if self.main_oc.timed_out():
            self.logger.info(
                f">>>> {self.main_oc} Timed out. This cancels the tail <<<<"
            )
        else:
            # on met en place la condition pour la tail et on lance l'update
            self.logger.info(f"#### Placing stop {self}")
            reply = self.place_tailstop()
            orderRef = get_order_from(reply)
            sleep(0.12)  # laisser le temps de se mettre à jour?

            self.logger.info(f"Tail{self.main_oc} en place! Reply: {orderRef}")

            i = 0
            while not self.stop:
                # on met à jour la condition de tail_oc
                # si la mise à jour (amend) échoue, newOrder sera False
                newOrder = self.amend_stop_price(reply["orderID"]) if reply else reply
                # Si condition sortir de la boucle.
                if not newOrder or self.tailStop_triggerCond.is_(True):

                    # Chronos vérifie l'execution
                    logmsg = (
                        f"Fin de Trace: newOrder ({newOrder}) is False "
                        f"_ou_\n{self.tailStop_triggerCond.__repr__(short=False)}"
                    )
                    self.logger.info(logmsg)
                    break

                if self.PO.new_current_stopTail():
                    reply = newOrder

                # On attend un nombre variable de seconds
                # ne pas descendre sous 6s sinon risque d'être rejeté par le broker
                pause = self.updatePause + rnd.random() * 4 - 2
                pause = pause if pause > 0 else 0
                sleep(pause)

                i += 1
                # si int(60), log environs toutes les i * logPause
                if i % int(self.logPause / self.updatePause) == 0:
                    self.logger.info(f"{self.__repr__(cond=True)}\n{self.PO}")

            # On sort de la boucle si stop et true ou condition met
            # et finalise (..méthode héritée)
            self.finalise(False)

    def place_tailstop(self):
        """
        Appelé une fois l'ordre principal déclanché.
        
        On enclenche alors une trace par rapport à un prix de référence.
        On crée pour cela un objet prix qui suivra l'évolution via le broker.
        On envois l'ordre (le stop) au broker et on le met à jour ensuite
        un tailstop c'est :
        - ordType: un parmi 'Stop', 'StopLimit', 'MarketIfTouched', 'LimitIfTouched',
        - orderQty
        - stopPx
        - execInst: parmi ParticipateDoNotInitiate; MarkPrice, LastPrice, IndexPrice; ReduceOnly; Close.

        Renvois le résultat de OC.send_order
        """
        mPrice = self.brg.prices(self.mPrice_type, self.side)
        refPrice = self.brg.prices(self.refPrice_type, self.side)

        # l'offset du prix au départ est nulle
        self.PO = PriceObj(
            mPrice,
            refPrice,
            tail_perct_init=self.set_tail_strategy(),
            head=toggle_sides(self.side),
            updatepause=self.updatePause,
            timeBin=self.timeBin,
            logger=self.logger,
        )

        # on met à jour la condition car déjà crée dans OCT
        self.tailStop_triggerCond = Condition(
            self.brg, (self.refPrice_type, self.op, self.PO.data.stopTail.current)
        )

        # Nous avons juste besoin de mettre à jour le stopPx que nous ne connaissions pas
        # au moment de l'initialisation
        if "Limit" in self.order["ordType"]:
            # on passe aussi un prix
            self.order["price"] = self.PO.data.stopTail.current
            self.order["stopPx"] = setdef_stopPrice(
                self.order["price"], self.order["side"], absdelta=1
            )
        else:
            self.order["stopPx"] = self.PO.data.stopTail.current

        self.logger.info(f"Sending {self.__repr__(short=False, cond=True)}\n{self.PO}")

        return self.send_order()

    def amend_stop_price(self, orderRefID):
        """
        Mise à jour du prix pour la condition de déclenchement (ie la trace).
        nécessite l'ID de l'ordre à changer (orderRefID)
        """
        # maj des tails en fonction des stratégies
        # self.PO.tail_perct = self.PO.set_tail_strategy()

        # maj des prix
        self.PO.update_to(
            price=self.brg.prices(self.mPrice_type, self.side),  # mPrice
            refPrice=self.brg.prices(self.refPrice_type, self.side),
        )  # refPrice

        # maj de la condition
        # self.logger.debug(f'theoretical stopPx={self.refPrice_type, self.PO.data.flexTail.current}')
        self.tailStop_triggerCond.set_price_val(
            self.refPrice_type, self.PO.data.stopTail.current
        )
        # on ne met à jour le StopPx que s'il y a un changement
        try:
            if self.PO.new_current_stopTail():
                # il y a un amend (modif) à faire on envois l'ordre à chronos
                self.order["orderID"] = orderRefID
                self.order["newPrice"] = self.PO.data.stopTail.current
                self.order["ordType"] = self.amend_order_type(self.ordType)

                self.logger.info(
                    f"*** *New stopTail: {self.PO.data.stopTail.previous}"
                    f" --> {self.PO.data.stopTail.current}* ***\n"
                    f"Order to amend is {self.order}\n{self.PO}"
                )

                return self.send_order()
            else:
                # sinon pas de changement, on continue de suivre orderRefID
                return {"orderID": orderRefID}

        except Exception as e:
            # doit être géré par chronos
            # 'message': 'Invalid ordStatus
            # requests.exceptions.HTTPError

            self.logger.error(f"amend_stop_price raise {e.__repr__()}, type:{type(e)}")
            if "Service Unavailable" in e.__repr__():
                sleep(API_ERROR_INTERVAL)
                return True
            elif "Invalid ordStatus" in e.__repr__():
                # c'est probablement que l'on veut modifier un stop
                # qui s'est déclanché et qui n'existe plus.
                # on doit sortir de la boucle
                return False
            else:
                raise (e)

    def amend_order_type(self, ordertype):
        """Add amend prefix to ordtype if not already there"""
        return ordertype if ordertype.startswith("amend") else f"amend{ordertype}"

    def set_tail_strategy(self):
        """Définie la longueur de la trace en pourcentage.
        Utilise pour cela la position des prix du marché (midPrice)
        par rapport à ceux de référecence (indexPrice).  L'hypothèse est
        que le marché tends vers le prix de ref.  Aussi il s'agit de détecter
        les contre courant.  Nous suivant le prix de réf (indexPrice) donc lui
        a tendance à aller vers le marché
        1) Si je suis à l'achat mais que le indexPrice est > au marché alors on
        allonge la trace du pourcentage la différence |midprice - indexPrice| par
        rapport au indexPrice. Sinon en garde le pourcentage d'initialisation
        2) A contrario si je suis en vente mais que le indexPrice est < au marché
        alors on alonge la trace du pourcentage la différence |midprice - indexPrice|
        par rapport au maché.  Sinon status quo."""

        return self.pegOffset_perc
