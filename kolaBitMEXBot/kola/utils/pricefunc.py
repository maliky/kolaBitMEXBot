# -*- coding: utf-8 -*-
from typing import Union, Literal, Tuple
import logging

from kolaBitMEXBot.kola.utils.general import round_sprice
from kolaBitMEXBot.kola.kolatypes import priceT, sideT, bipriceT, ordTypeT


def set_new_price(base: priceT, per: float, symbol="XBTUSD") -> priceT:
    """Crée un prix à partir d'un prix de base et d'un pourcentage."""
    logging.info(f"Setting a new price with a percentage < 1% {per}. Are you sure?")

    def _round(x):
        """Set default symbol"""
        return round_sprice(x, symbol)

    return _round(base * (1 + per / 100))


def get_prices(refPrice: priceT, prix: bipriceT, atype, symbol=None) -> bipriceT:
    """Renvois les prix formatés selon atype."""
    assert any([p in atype for p in ["p%", "pA", "pD"]]), f"atype={atype}"

    def _round(x):
        """Set default symbol"""
        return round_sprice(x, symbol)

    if "p%" in atype:
        # prix inf et prix sup
        newPrix = set_new_price(refPrice, prix[0], symbol), set_new_price(refPrice, prix[1], symbol)
    elif "pA" in atype:
        # prix en absolue
        newPrix = prix[0], prix[1]
    elif "pD" in atype:
        # prix en différentiel par rapport au prix de référence
        newPrix = _round(refPrice + prix[0]), _round(refPrice + prix[1])
        logging.debug(f"refPrice={refPrice}, prix={prix}, newPrix={newPrix}, symbol={symbol}")

    return newPrix


def setdef_stopPrice(entryPrice: priceT, side: sideT, ordtype: ordTypeT, absdelta: float = .5) -> priceT:
    """
    set de default trigger price for a given entry_price

    - side: the side of the order
    - absdelta: the default (xbt) delta between entryPrice and trigger price.

    The returned trigger price may fall cause immediat triggering if entryPrice and
    absdelta are not set correctly.
    """
    # stpPx < price si side sell
    assert absdelta > 0, f"mais {absdelta}."
    # assert ordtype in ['MarketIfTouched', 'LimitIfTouched', 'Limit'], f"but ordtype={ordtype}"

    logging.info(f'>>>> entryPrice={entryPrice}, ordtype={ordtype}, absdelta={absdelta}')
    T = {"buy": 1, "sell": -1}
    return entryPrice + T[side] * absdelta
    


def get_prix_decl(prices: Tuple[priceT, priceT], side: sideT, ordtype:ordTypeT) -> priceT:
    """
    Return the price to be reach first if markets mooves towards the prices
    
    For limit and touche limit, buy should be the biggest price
    For stop and stop limit buy should be the smallest price
    We suppose that prices are both on the same side of market price.
    Other ways we would get immediat entry.
    """
    assert prices[0] < prices[1], f"prices={prices} should be an ordered paire."
    assert side in ['buy', 'sell'], f"side={side}"
    
    if ordtype in ['Limit', 'MarketIfTouched', 'LimitIfTouched']:
        return prices[1] if side == "buy" else prices[0]
    if ordtype in ['Stop', 'StopLimit']:
        return prices[1] if side == "sell" else prices[0]

    raise Exception(f'ordtype={ordtype} not recognized.')
