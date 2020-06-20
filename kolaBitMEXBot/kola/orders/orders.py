# -*- coding: utf-8 -*-
"""To place orders."""
from time import sleep
import logging

from kolaBitMEXBot.kola.settings import API_ERROR_INTERVAL
from kolaBitMEXBot.kola.utils.general import round_sprice, trim_dic
from kolaBitMEXBot.kola.utils.pricefunc import setdef_stopPrice
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.utils.exceptions import InvalidOrdStatus
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION

# from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.settings import LOGNAME
from kolaBitMEXBot.kola.bargain import Bargain

mlogger = logging.getLogger("")
mlogger.name = f"{LOGNAME}.{__name__}"
mlogger.setLevel("INFO")
# logging = logging.get
# mlogger = get_logger(name=f"{LOGNAME}.{__name__}")


def place_at_market(brg, orderQty, side, **opts):
    """Place a (market) order."""
    opts.update({"ordType": "Market"})
    mlogger.info(f"{brg}, orderQty:{orderQty}, side={side}, opts={opts}")
    return brg.bto.place(orderQty, side=side, asBulk=True, **opts)


def place(brg, side, orderQty, price, **opts):
    """
    Place a limit order.

    - brg a Bargain,
    - side,
    - orderQty in contract,
    - price
    """
    # by default ordType='Limit'
    price = round_sprice(price)
    mlogger.debug(
        f"brg={brg}, orderQty:{orderQty}, side={side}, opts={opts}, price={price}"
    )
    return brg.bto.place(orderQty=orderQty, side=side, price=price, asBulk=True, **opts)


def place_stop(brg, side, orderqty, stoppx, **opts):
    """Place a Stop, if side buy stopx must be > market price."""
    ordType = "Stop"
    opts.update({"stopPx": stoppx, "ordType": ordType})
    refPrice = get_execPrice(brg, side, typeprice=opts)

    # print(f"side={side}, refPrice={refPrice}, stoppx={stoppx}, ordType={ordType}")
    tv, eval_price = is_valid_order(brg, side, refPrice, stoppx, ordType, debug=True)
    if not tv:
        # usualy fast move when getting here
        pDelta = PRICE_PRECISION[brg.symbol] * 2
        override_price = refPrice + pDelta if side == "buy" else refPrice - pDelta

        # gère les cas de changement brusque avec LastPrice généralement.
        msg = (
            f"{now()}: Stop invalid. {side}Stop@{stoppx} when refPrice={refPrice}."
            f" eval_price={eval_price}, opts {opts}. new stop -> {override_price}."
        )
        mlogger.warning(msg)
        print(msg)
        opts["stopPx"] = override_price

    mlogger.debug(f'Placing {orderqty} "{side}" Stop@stopPx {stoppx}, opts={opts}')
    return brg.bto.place(orderqty, side=side, asBulk=True, **opts)


def place_SL(brg, side, orderqty, stoppx, price, **opts):
    """Place a Stop Limit, if side buy stopx must be > price."""
    ordType = "StopLimit"
    opts.update({"stopPx": stoppx, "ordType": ordType, "price": price})

    assert is_valid_order(
        brg, side, price, stoppx, ordType
    ), f"'{side}' SL invalide. stop price={stoppx} quand prix est {price}."

    mlogger.debug(
        f"Placing {orderqty} {side} Stop Limit ({price}) @ stopPx={stoppx}, opts={opts}"
    )
    return brg.bto.place(orderqty, side=side, asBulk=True, **opts)


def place_MIT(brg, side, orderqty, stoppx, **opts):
    """Place a Market if Touched.  if side buy stopx must be < market price"""
    ordType = "MarketIfTouched"
    opts.update({"stopPx": stoppx, "ordType": ordType})
    refPrice = get_execPrice(brg, side, typeprice=opts)
    assert is_valid_order(
        brg, side, refPrice, stoppx, ordType
    ), f"{side} MIT invalide. stop price={stoppx} quand prix est {refPrice}."
    mlogger.debug(
        f"Placing {orderqty} {side} Market if Touched @ stopPx={stoppx}, opts={opts}"
    )
    return brg.bto.place(orderqty, side=side, asBulk=True, **opts)


def place_LIT(brg, side, orderqty, stoppx, price, **opts):
    """Place a limit if touched. if side buy stopx must be < price"""
    ordType = "LimitIfTouched"
    opts.update({"stopPx": stoppx, "ordType": ordType, "price": price})
    assert is_valid_order(
        brg, side, price, stoppx, ordType
    ), f"'{side}' LIT invalide. stop price={stoppx} quand prix est {price}."
    mlogger.debug(
        f"Placing {orderqty} {side} Limit ({price}) if Touched @stopPx ({stoppx}),"
        f" opts={opts}"
    )

    return brg.bto.place(orderqty, side=side, asBulk=True, **opts)


# ### alias ####


def buy(brg, orderQty, price):
    """Place a buy (limit) order."""
    return place(brg, "buy", orderQty, price=price)


def buy_at_market(brg, orderQty):
    """Place a buy (market) order.
    Params:
  - brg a Bargain,
  - orderQty in contract"""
    return place_at_market(brg, orderQty, "buy")


def sell(brg, orderQty, price):
    """Place a sell (limit) order.
    Params:
  - brg a Bargain,
  - orderQty in contract,
  - price"""
    return place(brg, "sell", orderQty, price=price)


def sell_at_market(brg, orderQty):
    """Place a sell (market) order.
    Params:
  - brg a Bargain,
  - orderQty in contract"""
    return place_at_market(brg, "sell", orderQty)


def sell_stop(brg, orderQty, stopPx, price):
    """Place a sell (limit) order,"""
    return place_stop(brg, "sell", orderQty, stopPx, price=price)


def buy_stop(brg, orderQty, stopPx, price):
    """Place a buy (limit) order"""
    return place_stop(brg, "buy", orderQty, stopPx, price=price)


# brg is a Bargain
def amend_orderQty(brg, order, newQty):
    """
    Amend the orderQty of an order.

    Params:
    - brg <Bargain Object>:,
    - order <str>: one existing bitmex order,
    - newQty <int>: orderQty in contracts.
    """
    mlogger.info("Amending orderQty: % s -> % s" % (order["orderQty"], newQty))
    return brg.bto.amend(order, orderQty=newQty)


def amend_price(brg, orderID, newPrice):
    """
    Amend the price of an order.
    Params:
    - brg <Bargain Object>:,
    - order <str>: one existing bitmex order,
    -newPrice <int>
    """
    #    mlogger.debug(f'orderID={orderID}, newStopPx={newStopPx}, opts={opts}')
    newPrice = round_sprice(newPrice)
    try:
        order = {"orderID": orderID}  # format à vérifier pour harmonisation
        return brg.bto.amend(order, price=newPrice)
    except Exception as e:
        mlogger.exception(e)
        # le staop est probablement déjà déclenché.
        return {"orderID": orderID, "clOrdID": orderID, "error": "Amending error"}


def amend_prices(
    brg, orderid, newprice, which_, side=None, absdelta=None, text="", **kwargs
):
    """
    Amend the limit price of an order with a stop price too.
    ammend the stop price if it possible. using the absdelta
    Params:
    - brg <Bargain Object>:
    - orderID <str>: one existing bitmex order,
    - newprice <int>: new limit price.
    """
    #    mlogger.debug(f'orderID={orderID}, newStopPx={newStopPx}, opts={opts}')
    # devrait être fait en deux fois via chronos
    newOrder = {"orderID": orderid}
    absdelta = PRICE_PRECISION[brg.symbol] if absdelta is None else absdelta

    which = which_.replace("amend", "")  # I get a standard order
    assert which != "Market", "Market orders cannot be amended."
    newStopPx = {}

    if which in ["Stop", "MarketIfTouched"]:
        newPrice = {"stopPx": newprice}
    elif which == "Limit":
        newPrice = {"price": newprice}
    elif which in ["StopLimit", "LimitIfTouched"]:
        newPrice = {"price": newprice}
        assert side, f'Il faut renseigner "side" pour pouvoir amender "{which}"'
        newStopPx = {
            "stopPx": setdef_stopPrice(
                entryPrice=newprice, side=side, ordtype=which, absdelta=absdelta
            )
        }

    newPrices = [newPrice, newStopPx]
    mlogger.info(f"Amending {newOrder} and {newPrices}")

    try:
        amendedPx = brg.bto.amend(newOrder, **newPrice)
    except InvalidOrdStatus:
        # the stop is probably already fired. Reply with a cancel
        return None

    mlogger.debug(f"1 Success! AmendedPx={trim_dic(amendedPx, trimid=12)}")
    if newStopPx:
        try:
            text += f"{now(), newPrice, newStopPx}"
            brg.bto.amend(newOrder, **newStopPx, text=text)
        except Exception:
            mlogger.exception(
                f"*Amending stopPrice failed* {orderid, newStopPx}"
                " Probably already triggered!"
            )

    return amendedPx


def amend_stop_price(brg, orderID, newStopPx):
    """Amend the stop price of an order.
    Params:
  - brg <Bargain Object>:
- orderID <str>: one existing bitmex order,
  -newStopPx <int>: new stop price."""
    #    mlogger.debug(f'orderID={orderID}, newStopPx={newStopPx}, opts={opts}')
    newStopPx = round_sprice(newStopPx)
    try:
        order = {"orderID": orderID}  # format à vérifier pour harmonisation
        return brg.bto.amend(order, stopPx=newStopPx)
    except Exception as e:
        raise (e)


def amend_trailstop(brg: Bargain, order: str, newPegOffsetValue: float):
    """Amend la pegOffsetValue.

    (le delta de la sécurité (hook, crochet) par rapport au prix du marché).
    Params:
    - brg <Bargain Object>:,
    - order <str>: one existing bitmex order, peut se résumé à l'essentiel {'orderID'...}
    - pegOffsetValue <int>: le delta, si >0 trigger price au dessus,
    (donc orderQty doit être <0 ou side sell).
    """
    mlogger.info(f"order={order}, newPegOffsetValue={newPegOffsetValue}")
    newPegOffsetValue = round_sprice(newPegOffsetValue)
    return brg.bto.amend(
        order, pegOffsetValue=newPegOffsetValue, pegPriceType="TrailingStopPeg"
    )


def place_with_contigency():
    # pour les ordres liés entre eux
    pass


def cancel_all_orders(brg):
    """cancel all open orders of Bargain brg
    param: Bargain
    return: Boolean"""

    # should be done in bulk
    orders = brg.get_open_orders()
    ids = [order["orderID"] for order in orders]
    if ids != []:
        brg.bto.cancel(ids)
    return True


def cancel_order(brg: Bargain, order):
    """Cancel the Bargain order."""
    while True:
        try:
            return brg.bto.cancel(get_anID(order))
        except ValueError:
            sleep(API_ERROR_INTERVAL)
        else:
            break


def get_anID(order):
    oid = order.get("orderID", None)
    if oid is None:
        oid = order.get("clOrdID", None)
    if oid is None:
        raise Exception(f"Trying to get an ID from {order} but None")

    return oid


def cancel_all_with_condition(brg, key, value):
    if key is None or value is None:
        mlogger.warning("key or value are not passed in cancel with condition")
    else:
        orders = brg.get_open_orders(False)
        ids = [order["orderID"] for order in orders if order[key] == value]
        if ids:
            brg.bto.cancel(ids)
        else:
            mlogger.warning("No orders to cancel.")


def cancel_all_buy(brg):
    cond = ("side", "buy")
    return cancel_all_with_condition(brg, *cond)


def cancel_all_sell(brg):
    cond = ("side", "sell")
    return cancel_all_with_condition(brg, *cond)


def cancel_all_ordType(brg, ordType):
    """Main ordType are: 'Limit', 'Stop', 'Market', 'StopMarket'"""
    cond = ("ordType", ordType)
    return cancel_all_with_condition(brg, *cond)


def close_and_cancel(brg):
    """cancel and close all postions at market price"""
    cancel_all_orders(brg)
    return close_position_at_market(brg)


def close_position_at_market(brg):
    """close position for symbole SYMBOL at market price"""
    position = brg.get_position()
    qty = position["currentQty"]
    if qty < 0:
        return buy_at_market(brg, -qty)
    elif qty > 0:
        return sell_at_market(brg, qty)
    else:
        mlogger.warning("Probably no position to close.")

    return None


def ask(brg, q=30, margin=0):
    """sell_order at askprice q contracts"""

    askprice = brg.prices("askPrice")
    sell_order_infos = brg.bto.sell_order(q, askprice + margin)
    return q, askprice, sell_order_infos["clOrdID"]


def bid(brg, q=30, margin=0):
    """buy_order at bidprice q contracts"""

    bidprice = brg.bto.instrument(brg.SYMBOL)["bidPrice"]
    buy_order_infos = brg.bto.buy_order(q, bidprice - margin)
    return q, bidprice, buy_order_infos["clOrdID"]


# @log_args()
def is_valid_order(
    brg, side, price_=None, stoppx=None, ordertype="Stop", opts=None, debug=False
):
    """
    Check that the side price is correct for a stop.

    - if price is None, get current market price (LastPrice),
    - ordertype is stop or touched
    """
    assert side in ["buy", "sell"], f"side={side} n'est pas pris en compte"

    if "Limit" in ordertype and not price_:
        raise Exception(f'Un "{ordertype}" doit avoir un price.')
    elif ordertype not in ["Limit", "Market"] and not stoppx:
        raise Exception(f"Un ordre {ordertype} doit avoir un stopPx")

    recent_price = get_execPrice(brg, side)
    price = price_ if price_ else recent_price

    stopAbove, stopBelow = stoppx >= price, stoppx < price

    tv = stopAbove if side == "buy" else stopBelow

    return tv, recent_price if debug else tv


def is_newPrice_valide(brg, side, newPrice):
    """Teste la validité du prix par rapport au side."""
    if side == "buy":
        return newPrice < brg.prices("market", side)
    elif side == "sell":
        return newPrice > brg.prices("market", side)


def get_execPrice(brg, side, typeprice=None, deftypeprice="LastPrice", symbol=None):
    """
    Return the current market price defined by the typeprice (def. lastMidPrice).

    typeprice can be a dictionary with execInst.
    Will check for the price type in execInst.
    - symbol: the symbol to get the price for
    """
    if typeprice is None:
        # 'lastMidPrice'  # == markPrice ?
        assert deftypeprice is not None
        typePrice = deftypeprice
    elif isinstance(typeprice, dict):
        typePrices = [
            p for p in typeprice.get("execInst", "").split(",") if "Price" in p
        ]
        typePrice = typePrices[0] if typePrices else deftypeprice
    elif isinstance(typeprice, str):
        typePrice = typeprice
    else:
        msg = f"typeprice={typeprice} Non pris en charge."
        raise Exception(msg)

    try:
        return brg.prices(typeprice=typePrice, side=side, symbol_=symbol)
    except AttributeError as ex:
        raise (ex)
