# -*- coding: utf-8 -*-
from kolaBitMEXBot.kola.orders.condition import Condition
from kolaBitMEXBot.kola.utils.datefunc import tpsDans
from pandas import Timestamp, Timedelta


def cVraieTpsDiffDe(brg, x):
    """Condition Vraie pour un temps de > now +x."""
    return Condition(brg, ("temps", ">", tpsDans(x)))


def cVraieTpsDe(brg, tpsDeb):
    """Condition Vraie pour un temps de > tpsDeb."""
    return Condition(brg, ("temps", ">", tpsDeb))


def cVraieTpsDiffA(brg, x):
    """Condition Vraie un temps jusqu'à < now +x."""
    return Condition(brg, ("temps", "<", tpsDans(x)))


def cVraieTpsA(brg, tpsFin):
    """Condition Vraie un temps jusqu'à < tpsFin."""
    return Condition(brg, ("temps", "<", tpsFin))


def cVraieTpsDiffDeA(brg, x, y):
    """Condition vraie pour un temps entre maintenant +x min et maintenant + y min."""
    tpsDeb = tpsDans(x)
    tpsFin = tpsDans(y)
    if tpsDeb > tpsFin:
        raise Exception("tps départ(%s) > tps d'arrivé (%s)" % (tpsDeb, tpsFin))
    return Condition(brg, (("temps", ">", tpsDeb), ("temps", "<", tpsFin)))


def cVraieTpsDeA(brg, tpsDeb, tpsFin, logger=None):
    """Condition vraie pour un temps compris de tpsDeb A tpsFin."""
    if tpsDeb > tpsFin:
        raise Exception("tps départ(%s) > tps d'arrivé (%s)" % (tpsDeb, tpsFin))
    return Condition(
        brg, (("temps", ">", tpsDeb), ("temps", "<", tpsFin)), logger=logger
    )


def cVraiePrixDiffDeA(brg, price_type, x, y):
    """
    Condition vraie pour un prix entre market +x  et market + y.

    x et y peuvent être <0
    """
    basePrice = brg.prices(price_type)
    prixInf = basePrice + x
    prixSup = basePrice + y
    if prixInf > prixSup:
        raise Exception("prix départ(%s) > prix d'arrivé (%s)" % (prixInf, prixSup))
    return Condition(brg, ((price_type, "<", prixSup), (price_type, ">", prixInf)))


def cVraiePrixDiffSup(brg, price_type, x):
    """Condition vraie pour un prix supérieur à market +x. x peut être négatif."""
    basePrice = brg.prices(price_type)
    prixInf = basePrice + x
    return Condition(brg, (price_type, ">", prixInf))


def cVraiePrixDiffInf(brg, price_type, x):
    """Condition vraie pour un prix inférieur à market + x. x peut être négatif."""
    basePrice = brg.prices(price_type)
    prixSup = basePrice + x
    return Condition(brg, (price_type, "<", prixSup))


def cVraiePrixDeA(brg, price_type, prixInf, prixSup, logger=None):
    """Condition vraie pour un prix compris entre x usd et y usd."""

    assert prixInf < prixSup, f"prix départ({prixInf}) > prix d'arrivé ({prixSup})"
    # logging.warning(f'Setting Price type={price_type}, inf {prixInf}, sup {prixSup}')
    return Condition(
        brg, ((price_type, "<", prixSup), (price_type, ">", prixInf)), logger=None
    )


def cVraiePrixSup(brg, price_type, prixInf):
    """Condition vraie pour un prix supérieur à prixInf."""
    return Condition(brg, (price_type, ">", prixInf))


def cVraiePrixInf(brg, price_type, prixSup):
    """Condition vraie pour un prix inférieur à prixSup."""
    return Condition(brg, (price_type, "<", prixSup))


def cVraieToujours(brg):
    """Condition toujours vraie."""
    return cVraieTpsDe(brg, Timestamp.now() - Timedelta(1, unit="s"))


def cHook(brg, hSrc, srcStatus="Filled", exclIDs=[]):
    """
    Créer la condtion Hook.
    
    le genre hook,
    hSrcva servir à récupérer le clOrdID au moment de l'éval
    status est l'état cherché pour le hook
    """
    _cond = Condition(brg, ("hook", hSrc, srcStatus))
    # ajoute la liste des ids à exclure
    _cond = _cond.set_excludeClIDs(exclIDs)
    _cond.excludeIDs = exclIDs
    return _cond
