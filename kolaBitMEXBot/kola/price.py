# -*- coding: utf-8 -*-
# a tester
from kola.utils.general import round_to_d5
from kola.utils.logfunc import get_logger
from kola.utils.datefunc import now
from kola.utils.general import contains
import numpy as np
import pandas as pd
from collections import OrderedDict


class PriceObj:
    """This classe keeps track of a price and element associated with this price
    L'objet prix à un sens, une tête et plusieurs queues
    par défault la tail suit les ref price"""

    def __init__(
        self,
        price,
        refPrice,
        tail_perct_init=0.5,
        head="buy",
        updatepause=6,
        timeBin=60,
        logger=None,
        max_var=2.6,
        min_flex=0.2,
    ):
        """head is the direction, need a price (market price and ref price) and
        tail_perct_init (%): default.  Le nb_enregistrement est la longeur de la bd des prix \n
        - Une tête sur la prix du marché référence et trois queues (tails ou tails).\n
        - La queue bleu (Qbleue) elle suit le prix du marché toujours à la même distance (même épaisseur)\n
        - La queue rouge, c'est le stop, elle ne bouge pas tant qu'il n'y a pas de bénéf.
        - La queue verte (flexTail) elle est d'épaisseur variable, son prix est passé au brokeur si au dessus du refPrice
        - min_flex est le pourcentage de la refTail jusqu'ou on peux réduire la flexTail ex. 80 de la ref tail
        - main_window_size c'est la taille de l'historique de ce prixObject, doit être calcule en fonction de la taille de la bin voulue et de la fréquence des mis à jour
        - updatepause c'est le nombre moyen de secondes entre deux mise à jour
        - timeBin c'est la taille de la fenêtre utilisé pour calculer la variation de prix.
        avec la updatepause permet d'estimer la main_window_size
        - max_var en quoi? et min_flex determine la flexibilité des queue.  max_var est une statistique décrivant la variation nécessaire pour que la queue se réduise à min_flex.  Elle est issue d'obeservation du marché voir getting_data.ipyng.  la var à un très long mais fine queue. 90% < .22
        """
        self.logger = get_logger(logger, sLL="INFO", name=__name__)

        self.head = head
        self.refPrice = refPrice

        self.tail_perct_init = tail_perct_init  # tail percent sera appliqué au prix de référence nombre entre 0 et 100
        self.timeBin = timeBin
        self.updatePause = updatepause
        # essaye d'avoir un tableau plus grand que nécessaire
        # par défaut ~30
        self.main_window_size = int(timeBin / self.updatePause * 4)

        # on défini une fonction pour mettre à jour la flexTail
        # le maxiumn de variation jamais observé pour la bin en pourcentage de variation
        # TODO: mieux définir cette fonction...
        self.max_var = max_var
        N = 100
        self.neg_exps = self.__neg_exps(self.max_var, N)
        self.var_dist_hist = pd.Series(index=range(N), data=self.neg_exps)
        self.min_flex = min_flex

        # on initialise les prix et la df qui les contiendra
        # define self.data
        self.data = None
        self.data = self.__init_price_df(price, refPrice)

        # on définie l'épaisseur du stop
        self.tail_base_width = round_to_d5(
            abs(self.data.refPrice.init - self.data.refTail.init)
        )

    def __init_price_df(self, price, refPrice):
        """Initialise la df qui contiendra l'historique des prix"""

        def create_index():
            """
            Créer une df de taille n (n ligne).

            avec la dernière appelée init et les deux premières current et previous.
            """
            ind = list(range(self.main_window_size))
            ind[0], ind[1], ind[-1] = "current", "previous", "init"
            return ind

        index = pd.Index(create_index(), name="PriceObj.data")
        columns = [
            "date",
            "price",
            "refPrice",
            "stopTail",
            "refTail",
            "flexTail",
            "sOfsD",
            "fScale",
        ]
        # self.data = pd.DataFrame(index=index, columns=columns)

        current_prices = self.get_current_prices(price, refPrice)
        data = pd.DataFrame(index=index, columns=columns)
        data.loc[:, :] = current_prices.values
        # On s'assure que la colonne date est au bon format pour faciliter les calculs ultérieurs
        data.date = pd.to_datetime(data.date)
        self.logger.debug(f"Init df prices Object:\n{data.describe()}")
        return data

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
    ):
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

        return pd.Series(current_prices)

    def __neg_exps(self, max_var, N):
        """Plotter pour voir mais il s'agit de valeurs pour x => -np.exp(x)"""
        data = list(
            map(lambda x: -np.exp(x), np.linspace(start=1, stop=max_var, num=N))
        )
        return data

    def __repr__(self, short=3):
        """representation for the price obj. pass a number != 0 to trim the df output.
        default 3.  return only rows with price change"""
        if self.data is not None:
            fCols = set(
                [c for c in self.data.columns if contains(["price", "tail"], c.lower())]
            ) & set(self.data.columns)
            data = self.data.loc[:, self.data.columns].drop_duplicates(subset=fCols)
            initRow = self.data.tail(1)  # return df
            data = pd.concat([data.head(3), initRow])
            rep = pd.concat([data.head(short), initRow]) if short else f"{data}"
            return f"{rep}\n"
        else:
            return "Data not initialised yet but price, refPrice, are {price, refPrice}"

    def get_stopTail_offset_per(self, refPrice=None, stopTail=None):
        """Renvois l'écart (en % du refPrice) entre le refPrice et la stopTail.  >0 si stopTail au dessus, <0 sinon"""

        refPrice = self.get_refPrice(refPrice)
        stopOffsetDelta = self.get_stopTail_offset_delta(refPrice, stopTail)
        stopOffsetPer = stopOffsetDelta / refPrice
        return round(stopOffsetPer * 100, 2)

    def get_stopTail_offset_delta(self, refPrice=None, stopTail=None):
        """Renvois l'écart (en valeur) entre le prix de référence actuelle et la stopTail actuelle.  >0 si stopTail au dessus, <0 sinon.
        S'assure que les prix sont définis"""
        refPrice = self.get_refPrice(refPrice)
        tail = self.get_data(nomElt="stopTail", default_ret=stopTail)
        offset = round_to_d5(tail - refPrice)
        return offset

    def flexTail_offset_delta(self, refPrice=None):
        """Calcul l'offset flexible qui est fonction des variation des prix."""
        refPrice = self.get_refPrice(refPrice)
        refTail = self.get_refTail(refPrice)
        base_ofs = round(refTail - refPrice, 2)
        current_var, currP, prevP = self.get_current_variation()
        scale = self.get_scale(current_var)
        flexOfs = round(scale * base_ofs, 2)
        # logmsg = (f'Offset_delta: prevP={round(prevP, 2)}, currP={round(currP, 2)},'
        #           f' var={round(current_var,2)}, scale={scale}, base_ofs={base_ofs},'
        #           f' flexOfs={flexOfs}, refPrice={refPrice}')

        # self.logger.debug(logmsg)
        return round_to_d5(flexOfs), round(scale * 100, 2)

    def get_refTail(self, refPrice=None):
        """Renvois la queue de référence.  (la bleue).  Celle ci ne change pas d'épaisseur mais suit le prix de reférence dans ses variations.
        Elle sert à déclancher la mise à jour du stopTail lorsque la nouvelle Tail sera sortie du bois (ie assure une prise)"""
        refPrice = self.get_refPrice(refPrice)

        sens = 1 if self.head.lower() == "buy" else -1
        epaisseur = refPrice * self.tail_perct_init / 100
        ofs = -sens * epaisseur

        refTail = round_to_d5(refPrice + ofs)

        # logmsg = f'refPrice={refPrice}, refOfs={round(ofs,2)}, sens={sens}, direction head={self.head} --> refTail={refTail}'
        # self.logger.debug(logmsg)

        return refTail

    def new_current_stopTail(self):
        """Renvois un booléen indiquant si la stop tail courante est nouvelle."""
        return self.data.stopTail.current != self.data.stopTail.previous

    def get_flexTail(self, refPrice=None, refTail=None):
        """Renvois le prix de la queue Verte, la flexible.
        C'est un nombre en minTail et maxTail qui dépend du dernier delta
        si le tableau est rempli.
        JE me base sur ça pour calculer l'offset
        """
        if pd.isna(refTail):
            refTail = self.get_refTail(refPrice)

        # if self.enought_data():
        if True:
            refPrice = self.get_refPrice(refPrice)
            flexOfs, scale = self.flexTail_offset_delta()
            flexTail = refPrice + flexOfs

            return round_to_d5(flexTail)
        else:
            return refTail

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
        """Renvois le % de var des prix moyen pour la dernière et avant dernière timeBin."""
        if self.data is None:
            return 0, 0, 0
        # on suppose que le data à les données nécessaire pour les calculs suivants
        # définit les mask avec les unité de temps Unit Time (UT)
        oneUTAgo = now() - pd.Timedelta(self.timeBin, "s")
        twoUTAgo = now() - pd.Timedelta(2 * self.timeBin, "s")
        currUT_mask = (self.data.date > oneUTAgo).values
        prevUT_mask = (self.data.date > twoUTAgo).values & list(not_array(currUT_mask))

        mean_curr_price = self.data.loc[currUT_mask].refPrice.mean()
        mean_prev_price = self.data.loc[prevUT_mask].refPrice.mean()

        if any(pd.isna([mean_prev_price, mean_curr_price])):
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
        Cela donne une fonction qui a son image dans [0,1] mais avec la plus part de valeurs proches de 1.

        Rappel: si t \\in [O,1] alors f(t)=bt + (1-t)*a sera \\in [a, b] de façon linéaire.
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
        """ Test si tail est une stopTail.
        Pour être une stopTail, tail doit être plus loin que la stopTail précédente et refTail doit être plus loin que le prix de référence.
        Revoir la deuxième condition  """
        assert self.head, f"Le dragon doit avoir une tête {self.head}"
        if "buy" in self.head:
            # c'est vrai si la queue actuelle est au dessus de la stoptail et du prix initial
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
        """Renvois la date range timedelta (en seconds) converte par le price data"""
        min_date, max_date, date_range = None, None, None

        if self.data is None:
            return 0

        try:
            min_date = self.data.date.min()
            max_date = self.data.date.max()
            if any(pd.isna([min_date, max_date])):
                return 0
            else:
                date_range = max_date - min_date
                return date_range.seconds
        except Exception as e:
            logmsg = f"data={self.data}, min_date={min_date} et max_date={max_date} et date_range"
            self.logger.error(logmsg)
            raise (e)

    def get_init_date(self):
        return self.get_data(nomElt="date", temps="init")

    def get_data(self, nomElt, temps="current", default_ret=None):
        """ Renvois un element du data si il existe.
        Si refPrice ou date demandé, envois des valeurs par défaut si n'a rien trouvé dans data
        """
        if self.data is not None:
            elt = self.data.loc[temps].loc[nomElt]
            if not pd.isna(elt):
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
        """Renvois le prix de référence en s'assurant qu'il est définit.  """
        if pd.isna(refPrice):
            return self.get_data("refPrice")
        else:
            return refPrice


def not_array(arr):
    """return a map of the negated elements"""

    def not_func(x):
        return not x

    return map(not_func, arr)
