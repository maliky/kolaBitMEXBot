# -*- coding: utf-8 -*-
from typing import Union, Literal, Tuple
import logging

from kolaBitMEXBot.kola.utils.general import round_to_d5

priceT = Union[float, int]
sideT = Literal["buy", "sell"]
bipriceT = Tuple[priceT, priceT]  # couple de price


def set_new_price(base: priceT, per: float) -> priceT:
    """Crée un prix à partir d'un prix de base et d'un pourcentage."""
    logging.info(f"Setting a new price with a percentage < 1% {per}. Are you sure?")

    return base * (1 + per / 100)


def get_prices(refPrice: priceT, prix: bipriceT, atype, symbol=None) -> bipriceT:
    """
    Renvois les prix formatés selon atype
    """
    assert any([p in atype for p in ["p%", "pA", "pD"]]), f"atype={atype}"

    if "p%" in atype:
        # prix inf et prix sup
        newPrix = set_new_price(refPrice, prix[0]), set_new_price(refPrice, prix[1])
    elif "pA" in atype:
        # prix en absolue
        newPrix = prix[0], prix[1]
    elif "pD" in atype:
        # prix en différentiel par rapport au prix de référence
        newPrix = refPrice + prix[0], refPrice + prix[1]

    if symbol is not None:
        round_func = 
        return newPrix


def setdef_stopPrice(price: priceT, side: sideT, absdelta: float = 2) -> priceT:
    """
    Soit le price voulu d'entrée (de l'ordre) sur le marché et un side.

    renvois un prix de déclenchement (stopPx) différé d'un delta dans le bon sens.
    par défaut absdelta=2 pour un déclenchement proche du prix.

    pour buy orders les stpPx > price d'entrée
    Renvois le stopPrice ou stopPx

    """
    # stpPx < price si side sell
    assert absdelta > 0, f"abs delta doit être positif strictement."

    # side = side.lower()
    T = {"buy": 1, "sell": -1}

    return price + T[side] * absdelta


def get_prix_decl(prices: Tuple[priceT, priceT], side: sideT) -> priceT:
    """
    given un tuple ordonnée de prices et un side
    renvois le prices qui sera le premier atteind en suivant le sens side
    """

    return prices[0] if side == "buy" else prices[1]
