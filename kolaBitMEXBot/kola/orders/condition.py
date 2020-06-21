# -*- coding: utf-8 -*-
"""Conditions pour les ordres."""
from numpy import array
from pandas import DataFrame, concat, Timestamp, Series
from typing import Set, Optional, Union, List, Dict

from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.bargain import Bargain
from kolaBitMEXBot.kola.kolatypes import ordStatusL
from kolaBitMEXBot.kola.orders.orders import get_execPrice
from kolaBitMEXBot.kola.utils.constantes import PRICELIST_DFT


class Condition:
    """
    Définie la condition d'exécution et d'évaluation d'un ordre.

    # Extension: Ajouter des options extra pour le prix, IndexPrice price, ect...
    # pour que l'on puisse choisir sur quoi s'applique la condition
    # c'est ici que l'évaluation ou l'appel à des indicateur aura lieu.
    # voir donc côté volume bin ect..
    # faire une condtion toujours vrai, ou toujorus fause
    """

    def __init__(self, brg: Bargain, cond, logger=None):
        """
        Initialise cond_frame et des hookedSrcID to exclude from evaluation.

        - brg: Une prise we
        - cond: un tuple (type, 'op', value).
        Si type is usualy temps (then val is Timestamp) ou prix (val float)

        There can only be one price condition (for now)
        """
        self.brg: Bargain = brg

        self.logger = get_logger(logger, sLL="DEBUG", name=__name__)

        # Une liste de mots clef pour le prix
        # Fairprice = market price et LastPrice ~= midPrice
        self.price_list: List[str] = PRICELIST_DFT

        # to cached hooked ID, il ne doit y en avoir qu'un
        self.hookedSrcID: str = ""
        self.excludeIDs: Set[str] = set()

        # store les conditions dans un df avec les cols (genre, op, value)
        self.cond_frame: DataFrame = self.forme_conditions(cond)
        # market price when condition is initialised.  use to compute relative p.
        self.init_prices = self.get_default_prices()
        self.init_time = now()

    def __repr__(self, short=True):
        """Représentation pour la condition."""
        if self.cond_frame is None:
            return "No Condition yet"

        if self.cond_frame is not None:
            conditions_evaluees = self.cond_frame
            tValues = self.evalue_les_conditions()
            try:
                conditions_evaluees.loc[:, "test"] = tValues
            except ValueError as ex:
                msg = (
                    f"{ex}: tValues={tValues} while"
                    f" conditions_evaluees={conditions_evaluees}"
                )
                self.logger.error(msg)
                conditions_evaluees = "<<<< Exceptionnal Error >>>>"

        ret = f"----Cond: ({self.brg.symbol}) _truthValue is {all(tValues)}_ at {now()}"
        if not short:
            prices = self.get_default_prices()
            ret += f"\n--------Détails: refPrices={prices},\n{conditions_evaluees}"
            if sum(self.cond_frame.genre == "hook"):
                ret += f"\n--------Hook to Exclude: {self.get_excludeIDs()}"
        return ret

    def get_default_prices(self) -> Dict[str, float]:
        """
        Ask the bargainer for the prices in pricelist.

        PRICELIST_DFT = {"Index", "Mark", "Last", "lastMid", "bid", "ask"}price
        """
        return {f"{p}": self.brg.prices(p) for p in self.price_list}

    def timed_out(self):
        """Regarde si il y a une condition de temps qui ne peut plus être vraie."""
        mask = (self.cond_frame.genre == "temps") & (self.cond_frame.op == "<")
        temp_cond = self.cond_frame.loc[mask]
        if temp_cond.empty:
            return False
        else:
            # then I have temporal condition potentialy false for ever
            try:
                return not all(temp_cond.apply(axis=1, func=self.evalue_une_condition))
            except Exception:
                raise

    def get_excludeIDs(self):
        self.logger.debug(f"self.excludeIDs={self.excludeIDs}")
        return self.excludeIDs

    def set_excludeClIDs(self, exclIDs=[]):
        """Add the sequence exclIDs to self."""
        setattr(self, "excludeIDs", set(exclIDs))
        self.logger.debug(f">>>>Excluding IDs {self.excludeIDs}")
        return self

    def add_condition(self, condition):
        """Ajoute une ou des conditions à la condition existante."""
        # non-attention concatenation axis not aligned
        self.cond_frame = concat(
            [self.cond_frame, condition.cond_frame], axis=0, sort=True
        )
        self.cond_frame = self.cond_frame.reset_index(drop=True)

    def forme_conditions(self, cond_) -> DataFrame:
        """Vérifie la formation des conditions et les retournent bien formatées."""
        try:
            # si chacune des longueurs des cond est 3 je suppose que c'est bon
            cond_formatee: DataFrame = DataFrame(
                data=list(cond_), columns=["genre", "op", "value"]
            )
        except ValueError:
            # on a probablement un itérable mais avec un pb de shape.
            # (une seul cond)
            _cond = array(cond_)
            _cond.shape = (1, 3)
            cond_formatee: DataFrame = DataFrame(
                data=list(_cond), columns=["genre", "op", "value"]
            )
        except Exception as e:
            self.logger.error(f"{e} un pb dans le format de la condition {[_cond]}")
            raise e

        # On vérifie le fond de la forme
        if all(cond_formatee.apply(axis=1, func=self.verifie_forme)):
            cond_formatee = cond_formatee.reset_index(drop=True)
            return cond_formatee
        else:
            raise Exception(f"Une condition {cond_formatee} n'est pas correcte.")

    def set_price_val(self, price_ref, val):
        """Vérifie que l'on a une cond. prix et la renvoie si tel est le cas."""
        mask = self.cond_frame.genre == price_ref
        if any(mask):
            mid = self.cond_frame.loc[mask].index
            self.cond_frame.loc[mid, "value"] = val

    def verifie_forme(self, cond):
        """Vérifie qu'une tuple défini bien une condition."""
        genre_ok = op_ok = value_ok = False
        # opérateur
        op_ok = cond.op in ["<", ">", "==", "!="]

        # genre
        if cond.genre in self.price_list:
            genre_ok = True
            try:
                cond.value = float(cond.value)
            except Exception:
                raise Exception("la valeur %s n''est pas du bon type" % cond.value)

            value_ok = isinstance(cond.value, (int, float))

        if cond.genre in ["temps"]:
            genre_ok = True
            value_ok = isinstance(cond.value, (Timestamp))

        if cond.genre == "hook":
            # l'op doit être le clOrdID et value le status cherché
            genre_ok = True
            op_ok = True  # override
            value_ok = isinstance(cond.op, str) and (cond.value in ordStatusL)

        return genre_ok and op_ok and value_ok

    def evalue_les_conditions(
        self, cond_frame_: Optional[Union[DataFrame, Series]] = None
    ):
        """
        Evalue les conditions de cond_frame_.

        Renvois le vecteur de résultats.
        Si cond is None alors l'évaluation sera vraie.
        """
        test = False
        cond_frame = self.cond_frame if cond_frame_ is None else cond_frame_
        try:
            test = cond_frame.apply(axis=1, func=self.evalue_une_condition)
        except TypeError:
            # evalue_une_condition() got an unexpected keyword argument 'axis?
            # c'est qu'il n'y a qu'une condition
            test = self.evalue_une_condition(cond_frame)
        except AttributeError as ae:
            if "NoneType" in ae.__repr__():
                # On est en initialisation passons après un warning.
                self.logger.warning(f"Ignoring exception {ae}. Returning 'Init'")
                return "Init"
        except Exception:
            raise Exception(f"cond={cond_frame}")

        return test

    def evalue_une_condition(self, cond):
        """
        Évalue une condition en fonction du genre.

        Les plus simple sont prix et temps.
        """
        if cond.genre in self.price_list:
            current_price = self.brg.prices(cond.genre)
            return self.evalue(current_price, cond.op, cond.value)

        elif cond.genre in ["temps"]:
            return self.evalue(now(), cond.op, cond.value)
        elif cond.genre == "hook":
            return self.evalue_un_hook(cond)

    def is_(self, t_value):
        """
        Vérifie avec les données de la prise web si la condition has t_value.

        Si t_value is false, retourne inverse le test.
        """
        ret = None
        try:
            ret = all(self.evalue_les_conditions())
        except TypeError:
            ret = True

        return ret if t_value else not ret

    def evalue(self, a, op: str, b) -> bool:
        """Retourne <bool> a op b où op est un operateur de type string."""
        assert op in ["<", ">"]
        return bool(a < b) if op == "<" else bool(a > b)

    def is_hooked(self):
        """
        Say if condition has hooks and if their conditions are validated.

        If so, we say that the condition is hooked.
        """
        has_hook = self.get_conds('hook')
        if len(has_hook):
            return self.evalue_les_conditions(has_hook).all()

        return False

    def evalue_un_hook(self, cond_):
        """
        Evalue a hook.

        Cherche dans les ordres executés un clOrdID correspondand à cond_.op
        (ie l'abbrevation de la srcKey).
        Vérifie ensuite que ces ordres ont le status cond_.value
        (eg. Filled, Triggered, Canceled)
        """
        clOrdIDs = [
            clID
            for clID in self.brg.get_exec_clID_with_(srcKey_=cond_.op)
            if clID not in self.excludeIDs
        ]

        self.logger.debug(
            f"~~~~ self.brg.get_exec_(srcKey_=cond_.op)="
            f"{self.brg.get_exec_clID_with_(srcKey_=cond_.op, debug_=False)}"
        )

        if len(clOrdIDs):
            tv = self.brg.order_reached_status(clOrdIDs[-1], cond_.value)
            if tv and (clOrdIDs[-1] != self.hookedSrcID):
                self.logger.info(f"We hook to {clOrdIDs[-1]}")
                self.hookedSrcID = clOrdIDs[-1]

            return tv

        return False

    def get_low_high(self, _genre):
        """get low high value for _genre condition."""
        mask = {
            "temps": self.get_conds("temps").values,
            "price": self.get_conds("price"),
        }[_genre]

        assert sum(mask) == 2, f"mask={mask}, self.cond_frame={self.cond_frame}"
        sorted_cond = self.cond_frame.loc[mask, "value"].sort_values()

        return sorted_cond.values

    def get_relative_low_high(self, _genre):
        """Return the initial difference for genre condition"""
        assert _genre in ["temps", "price"], f"{_genre} is not implemented."
        low, high = self.get_low_high(_genre)
        initVal = {
            "temps": self.init_time,
            "price": self.init_prices[self.get_price_type()],
        }[_genre]

        return low - initVal, high - initVal, initVal

    def get_conditions(self, genre_):
        """Return condition for genre"""
        if "price" in genre_.lower():
            return self.get_conds("price")
        elif genre_ == "temps":
            return self.get_conds("temps")

    def get_conds(self, genre_, pricetype_="*"):
        """Renvoie le mask pour les conditions de temps."""
        if "price" in genre_.lower():
            return self._get_price_conds(pricetype_)

        return self.cond_frame.loc[self.cond_frame.genre == genre_].sort_values("value")

    def _get_price_conds(self, pricetype_="*"):
        """Renvoie le mask pour les conditions de prix."""
        prices = self.cond_frame.genre.isin(self.price_list).sort_values("value")

        if pricetype_ == "*":
            return prices
        else:
            assert pricetype_ in prices.genre
            return prices.loc[prices.genre == pricetype_]

    def get_current_price(self, pricetype_=None):
        """
        Renvoie un prix courant. Devrait gérer les prix de diff type. ??

        Normalement renvoie le prix associé à la condition prix.
        Si plusieurs condition prix, nécessite de préciser le pricetype_
        """
        _pricetype = self.get_price_type() if pricetype_ is None else pricetype_
        prices = self.get_price_conds(_pricetype).values

        currentSellPx = get_execPrice(self.brg, "sell", _pricetype, _forceLive=True)
        currentBuyPx = get_execPrice(self.brg, "buy", _pricetype, _forceLive=True)

        useSellPrice = currentSellPx <= max(prices.values) if len(prices) else True

        return currentSellPx if useSellPrice else currentBuyPx

    def get_price_type(self):
        """
        Renvoie le type de prix de la condition prix.

        Si cette fonction est appelé alors il ne doit y avoir qu'une
        condition prix (ou qu'un genre).
        """
        mask = self.get_price_conds()
        _genre = self.cond_frame.loc[mask, "genre"]

        assert len(_genre.unique()) <= 1, f"self.cond_frame={self.cond_frame}"

        if len(_genre):
            return _genre.iloc[0]

    def update_cond(self, genre_, op_, value_):
        """"
        Met à jour une condition.
        - genre_ le genre de la condition 'temps, hook, IndexPrice, LastPrice...
        - op: l'opérateur de la condition
        - value_: la nouvelle valeur
        """
        mask = self.get_conds(genre_).values & (self.cond_frame.op == op_).values

        def msg_debug(_msg_):
            return (
                f">>>>>>>>>>>>>>>> genre={genre_}, op={op_}, value_={value_}\n"
                f"{_msg_}.\n{genre_, op_},\n {self.cond_frame}.\n mask={mask}."
            )

        assert sum(mask) <= 1, msg_debug("Update several conditions at once")

        if sum(mask) == 0:
            self.logger.warning(msg_debug("Trying to update condition but none found"))
            return self

        self.cond_frame.loc[mask, "value"] = value_

        return self
