# -*- coding: utf-8 -*-
# a tester
from collections import OrderedDict

import numpy as np
from pandas import Series, Index, DataFrame, to_datetime, concat, Timedelta, isna

from kolaBitMEXBot.kola.utils.general import round_sprice, contains
from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION, MAX_PRICE_VARIATION
from kolaBitMEXBot.kola.kolatypes import symbT, sideT, priceT


PRICE_COLUMNS = [
    "date",
    "price",
    "refPrice",
    "stopTail",
    "refTail",
    "flexTail",
    "sOfsD",
    "fScale",
]


class PriceObj:
    """This classe keeps track of a price and element associated with this price
    L'objet prix à un sens, une tête et plusieurs queues
    par défault la tail suit les ref price"""

    def __init__(
        self,
        price: priceT,
        refPrice: priceT,
        tail_perct_init: float = 0.5,
        head: sideT = "buy",
        updatepause: float = 6,
        timeBin: int = 60,
        logger=None,
        min_flex: float = 0.2,
        symbol: symbT = "XBTUSD",
    ):
        """
        Head is the direction, need a price (market price and ref price) and
        tail_perct_init (%): default.  Le nb_enregistrement est la longeur
        de la bd des prix
        - Une tête sur la prix du marché référence et trois queues
        (tails ou tails).
        - La queue bleu (Qbleue) elle suit le prix du marché toujours à la même
        distance (même épaisseur)
        - La queue rouge, c'est le stop, elle ne bouge pas tant qu'il n'y a pas
        de bénéf.
        - La queue verte (flexTail) elle est d'épaisseur variable,
        son prix est passé au brokeur si au dessus du refPrice
        - min_flex est le pourcentage de la refTail jusqu'ou
        on peux réduire la flexTail ex. 80 de la ref tail
        - main_window_size c'est la taille de l'historique de ce prixObject,
        doit être calcule en fonction de la taille de la bin voulue
        et de la fréquence des mis à jour
        - updatepause c'est le nombre moyen de secondes entre deux mise à jour
        - timeBin (in seconds) c'est la taille de la fenêtre utilisé pour calculer
        la variation de prix.
        avec la updatepause permet d'estimer la main_window_size
        - symbol: keep track of price symbol to format and round price correctly
        """

        self.logger = get_logger(logger, sLL="INFO", name=__name__)

        self.head = head
        self.refPrice = refPrice
        self.symbol = symbol
        self.prec = PRICE_PRECISION[symbol]
        # tail percent sera appliqué au prix de référence nombre entre 0 et 100
        self.tail_perct_init = tail_perct_init
        self.timeBin = timeBin
        self.updatePause = updatepause
        # essaye d'avoir un tableau plus grand que nécessaire
        # par défaut ~30
        self.main_window_size = int(timeBin / self.updatePause * 4)

        # on défini une fonction pour mettre à jour la flexTail
        # le maxiumn de variation jamais observé pour la bin en pourcentage de variation
        # TODO: mieux définir cette fonction...
        N = 100
        self.neg_exps = neg_exps_values(max_var=MAX_PRICE_VARIATION[symbol], N=100)
        self.var_dist_hist = Series(index=range(N), data=self.neg_exps)
        self.min_flex = min_flex

        # on initialise les prix et la df qui les contiendra
        # define self.data

        self.data: DataFrame = DataFrame(
            index=Index(create_index(self.main_window_size), name="PriceObj.data"),
            columns=PRICE_COLUMNS,
        )
        self.data.loc[:, :] = DataFrame(
            self.get_current_prices(price, refPrice)
        ).T.values

        # On s'assure que la colonne date est au bon format
        self.logger.info(f"$$$$$$$$$$$$$$$${self.data}")
        self.data.date = to_datetime(self.data.date)

        self.logger.debug(f"Init df prices Object:\n{self.data.describe()}")

        # on définie l'épaisseur du stop
        self.tail_base_width = round_sprice(
            abs(self.data.refPrice.init - self.data.refTail.init), symbol
        )

    def get_current_prices(
        self,
        price,
        refPrice,
        refTail=None,
        stopTail=None,
        flexTail=None,
        sOfsD=None,
        sOfsP=None,
        fOfsD=None,
        fScale=None,
    ) -> Series:
        """Renvois les prix actuels pour remplir ou mettre à jour la df"""
        if refPrice is None:
            refPrice = self.get_refPrice()
        if refTail is None:
            refTail = self.get_refTail(refPrice)
        if stopTail is None:
            stopTail = refTail
        if sOfsD is None:
            # on pass refPrice car celui de la db is nan at this point
            sOfsD = self.get_stopTail_offset_delta(refPrice, stopTail)
        if sOfsP is None:
            sOfsP = self.get_stopTail_offset_per(refPrice, stopTail)

        if flexTail is None:
            flexTail = self.get_flexTail(refPrice, refTail)

        if fOfsD is None:
            fOfsD, fScale = self.flexTail_offset_delta(refPrice)

        current_prices = OrderedDict(
            [
                ("date", now()),
                ("price", price),
                ("refPrice", refPrice),
                ("stopTail", stopTail),
                ("refTail", refTail),
                ("flexTail", flexTail),
                ("sOfsD", sOfsD),
                ("fScale", fScale),
            ]
        )
        # # attention à l'ordre
        # current_prices = (now(), price, refPrice, stopTail, refTail, flexTail, sOfsD,
        #                   fOfsD, sOfsP, fScale)

        return Series(current_prices)

    def __repr__(self, short=3):
        """representation for the price obj. pass a number != 0 to trim the df output.
        default 3.  return only rows with price change"""
        if getattr(self, "data", None) is not None:
            fCols = set(
                [c for c in self.data.columns if contains(["price", "tail"], c.lower())]
            ) & set(self.data.columns)
            data = self.data.loc[:, self.data.columns].drop_duplicates(subset=fCols)
            initRow = self.data.tail(1)  # return df
            data = concat([data.head(3), initRow])
            rep = concat([data.head(short), initRow]) if short else f"{data}"
            return f"{rep}\n"
        else:
            return "Data not initialised yet but price, refPrice, are {price, refPrice}"

    def get_stopTail_offset_per(self, refPrice=None, stopTail=None):
        """
        Renvois l'écart (en % du refPrice) entre le refPrice et la stopTail.

        >0 si stopTail au dessus, <0 sinon
        """

        refPrice = self.get_refPrice(refPrice)
        stopOffsetDelta = self.get_stopTail_offset_delta(refPrice, stopTail)
        stopOffsetPer = stopOffsetDelta / refPrice
        return round(stopOffsetPer * 100, 2)

    def get_stopTail_offset_delta(self, refPrice=None, stopTail=None):
        """
        Renvoie l'écart (en valeur) entre le prix de référence et la stopTail actuelles.

        Return >0 si stopTail au dessus, <0 sinon. S'assure que les prix sont définis
        """
        refPrice = self.get_refPrice(refPrice)
        tail = self.get_data(nomElt="stopTail", default_ret=stopTail)
        if (tail - refPrice) == 0:
            return 0
        else:
            offset = round_sprice(tail - refPrice, self.symbol)
            return offset

    def flexTail_offset_delta(self, refPrice=None):
        """Calcul l'offset flexible qui est fonction des variation des prix."""
        _refPrice = self.get_refPrice(refPrice)
        _base_ofs = self.get_refTail(_refPrice) - _refPrice

        _scale = self.get_scale(self.get_current_variation()[0])

        flexOfs = round_sprice(_scale * _base_ofs, self.symbol)

        return round_sprice(flexOfs, self.symbol), round(_scale * 100, 4)

    def get_refTail(self, refPrice=None):
        """
        Renvoie la queue de référence.  (la bleue).

        Celle ci ne change pas d'épaisseur mais suit le prix de reférence.
        Elle sert à déclancher la mise à jour du stopTail lorsque la nouvelle Tail
        sera sortie du bois (ie assure une prise)
        """
        refPrice = self.get_refPrice(refPrice)

        sens = 1 if self.head.lower() == "buy" else -1
        epaisseur = refPrice * self.tail_perct_init / 100
        ofs = -sens * epaisseur

        refTail = round_sprice(refPrice + ofs, self.symbol)

        return refTail

    def new_current_stopTail(self):
        """Renvoie un booléen indiquant si la stop tail courante est nouvelle."""
        return self.data.stopTail.current != self.data.stopTail.previous

    def get_flexTail(self, refPrice=None, refTail=None):
        """
        Compute the green tail flexible tail thickness.

        C'est un nombre en minTail et maxTail qui dépend du dernier delta
        si le tableau est rempli.
        JE me base sur ça pour calculer l'offset
        """
        if isna(refTail):
            refTail = self.get_refTail(refPrice)

        # if self.enought_data():
        refPrice = self.get_refPrice(refPrice)
        flexOfs, scale = self.flexTail_offset_delta(refPrice)
        flexTail = refPrice + flexOfs
        return round_sprice(flexTail, self.symbol)

    def enought_data(self):
        """Check that we have enought data to compute the variation for the flex tail"""
        date_range = self.get_date_range()
        seuil = 2 * self.timeBin
        logmsg = f"date_range={date_range}({type(date_range)}) et seuil={seuil}"
        self.logger.debug(logmsg)
        try:
            if date_range > seuil:
                return True
            else:
                logmsg = f"Not enought_data. Only {date_range}s in data. Need {seuil}s"
                self.logger.info(logmsg)
                return False
        except Exception as e:
            logmsg = f"date_range={date_range}, seuil={seuil}"
            raise (e)

    def get_current_variation(self):
        """Renvoie var % des derniers et avant derniers prix moyens."""
        if getattr(self, "data", None) is None:
            return 0, 0, 0
        # on suppose que le data à les données nécessaire pour les calculs suivants
        # définit les mask avec les unité de temps Unit Time (UT)
        oneUTAgo = now() - Timedelta(self.timeBin, "s")
        twoUTAgo = now() - Timedelta(2 * self.timeBin, "s")
        currUT_mask = (self.data.date > oneUTAgo).values
        prevUT_mask = (self.data.date > twoUTAgo).values & list(not_array(currUT_mask))

        mean_curr_price = self.data.loc[currUT_mask].refPrice.mean()
        mean_prev_price = self.data.loc[prevUT_mask].refPrice.mean()

        if any(isna([mean_prev_price, mean_curr_price])):
            return 0, mean_prev_price, mean_curr_price
        else:
            return (
                (mean_curr_price - mean_prev_price) / mean_prev_price * 100,
                mean_curr_price,
                mean_prev_price,
            )

    def get_scale(self, current_var):
        """
        implémente e^-f(t)

        t=current_var, ie la variation en pourcentage des deux derniers prix binnés,
        a=min_flex et b=1.
        Cela donne une fonction qui a son image dans [0,1]
        mais avec la plus part de valeurs proches de 1.

        Rappel: si t \\in [O,1] alors f(t)=bt + (1-t)*a sera \\in [a, b] de façon lin..
        Elle tend rapidement vers 0 pour les current_var > 80% des variation historique
        """
        # rang dans un tableau de lenVal ligne donc compris entre 0 et 1
        lenVal = len(self.var_dist_hist)

        # implémente l'historique des variation
        rang = (
            len(self.var_dist_hist[self.neg_exps < -np.exp(current_var + 1)]) / lenVal
        )
        # min_flex est un % en valeur (entre 0 et 1)
        scale = (1 - rang) * self.min_flex + rang

        # logmsg = f'ov={current_var}, rang={rang}, scale={scale}'
        # self.logger.info(logmsg)

        return scale

    def is_stopTail(self, tail):
        """
        Test si tail est une stopTail.
        Pour être une stopTail, tail doit être plus loin que la stopTail précédente
        et refTail doit être plus loin que le prix de référence.
        Revoir la deuxième condition
        """
        assert self.head, f"Le dragon doit avoir une tête {self.head}"
        if "buy" in self.head:
            # vrai si la queue actuelle est au dessus de la stoptail et du prix initial
            # voir si utiliser refTail ou flexTail
            if tail > self.data.stopTail.current:
                return self.data.flexTail.current > self.data.refPrice.init
        elif "sell" in self.head:
            if tail < self.data.stopTail.current:
                return self.data.flexTail.current < self.data.refPrice.init

        return False

    def refTail_is_stopTail(self):
        """Définie une fonction si refTail est une stop Tail"""
        return self.is_stopTail(self.data.refTail.current)

    def flexTail_is_stopTail(self):
        """Définie une fonction si flex tail est une stop Tail"""
        return self.is_stopTail(self.data.flexTail.current)

    def update_to(self, price, refPrice):
        """
        Update the current prices and save the old ones in data.
        Update the stoptail if necessairy
        """
        # save the time, price and tails
        self.data.iloc[:-1] = self.data.shift(1).iloc[
            :-1
        ]  # décale toutes les lignes sauf la dernière
        # update price, refPrice, and tails
        self.data.loc["current"] = self.get_current_prices(
            price, refPrice, stopTail=self.data.stopTail.previous
        )

        # finaly we check the current tails and update stopTail if necessary
        if self.flexTail_is_stopTail():
            # update de la stopTail
            self.data.loc["previous", "stopTail"] = self.data.stopTail.current
            self.data.loc["current", "stopTail"] = self.data.flexTail.current
            logmsg = (
                f"**** New StopTail: {self.data.stopTail.previous}"
                f" -> {self.data.stopTail.current} ****"
            )
            self.logger.debug(logmsg)

        # voir dans trailstop
        # self.logger.warning(self)

    def get_date_range(self):
        """Renvoie la date range timedelta (en seconds) converte par le price data"""
        min_date, max_date, date_range = None, None, None

        if getattr(self, "data", None) is None:
            return 0

        try:
            min_date = self.data.date.min()
            max_date = self.data.date.max()
            if any(isna([min_date, max_date])):
                return 0
            else:
                date_range = max_date - min_date
                return date_range.seconds
        except Exception as e:
            logmsg = f"data={self.data}, min_date={min_date} et max_date={max_date}"
            self.logger.error(logmsg)
            raise (e)

    def get_init_date(self):
        return self.get_data(nomElt="date", temps="init")

    def get_data(self, nomElt, temps="current", default_ret=None):
        """
        Renvoie un element du data si il existe.
        Si refPrice ou date demandés, envoie des valeurs par défaut
        """
        if getattr(self, "data", None) is not None:
            elt = self.data.loc[temps].loc[nomElt]
            if not isna(elt):
                return elt

        if nomElt == "refPrice":
            return self.refPrice
        elif nomElt == "date":
            return now()
        elif nomElt in ["stopTail", "flexTail"]:
            if default_ret is None:
                self.logger.warning("Attention default_ret is none and used")
            return default_ret
        else:
            expmsg = f"Check nomElt={nomElt}, temps={temps} et data={self.data}"
            raise Exception(expmsg)

    def get_refPrice(self, refPrice=None):
        """Renvoie le prix de référence en s'assurant qu'il est définit.  """
        if isna(refPrice):
            return self.get_data("refPrice")
        else:
            return refPrice


def not_array(arr):
    """return a map of the negated elements"""

    def not_func(x):
        return not x

    return map(not_func, arr)


def create_index(core_size_: int):
    """
    Créer a str index of size core_size. with specific first and last names

    avec la dernière appelée init et les deux premières current et previous.
    """
    assert core_size_ > 1, f"core_size={core_size_}"
    idx = (
        ["current", "previous"] + [f"{i}" for i in range(2, core_size_ - 1)] + ["init"]
    )
    return idx


def neg_exps_values(max_var, N):
    """Plotter pour voir mais il s'agit de valeurs pour x => -np.exp(x)"""

    def neg_exp(x):
        return -np.exp(x)

    data = list(map(neg_exp, np.linspace(start=1, stop=max_var, num=N)))
    return data
