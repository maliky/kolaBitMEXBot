# -*- coding: utf-8 -*-
from base64 import b64encode
from uuid import uuid4
from kolaBitMEXBot.kola.utils.pricefunc import get_prix_decl, setdef_stopPrice
from kolaBitMEXBot.kola.utils.general import opt_add_to_, contains, log_args
from kolaBitMEXBot.kola.settings import LOGNAME, ORDERID_PREFIX
import re
from kolaBitMEXBot.kola.utils.logfunc import get_logger

mlogger = get_logger(name=f"{LOGNAME}.{__name__}")

# import logging
# mlogger = logging.getLogger('')
# mlogger.setLevel('INFO')


def toggle_order(order):
    """
    prendre un order (un dict) qui a une action et renvois ce dict avec l'action toggled 
    """
    toggle_order = order.copy()
    # will pop action toggle it and put it back in dict
    if "side" in toggle_order.keys():
        toggle_order["side"] = toggle_sides(toggle_order["side"])
    else:
        toggle_order["action"] = toggle_sides(toggle_order["action"])

    return toggle_order


def toggle_sides(chaine):
    """
    renvois la chaine ou les buy et sell ont été échangés
    """
    if "buy" in chaine:
        return chaine.replace("buy", "sell")
    elif "sell" in chaine:
        return chaine.replace("sell", "buy")
    else:
        raise Exception("Il n'y a rien à toggeler dans %s" % chaine)


def is_valid_stop(side, price, stopPx):
    """
    On sell orders, the order will trigger if the triggering price is lower than the stopPx. On buys, higher.
    Params:
    - side,
    - price: price for the new limit order that will be place,
    - stopPx: stop price that will trigger the limit order.  
    """
    if side == "buy" and price > stopPx:
        raise Exception("will buy at price higer than max of market")
    elif side == "sell" and price < stopPx:
        raise Exception("will sell at price lower than min of market")
    else:
        return True


def newClID(prefix=ORDERID_PREFIX, abbv_=""):
    """
    Génère un nouvel identifiant avec un prefix 'mlk_' par défaut.
    
    Ajout l'abbreviation to facilitate hook.
    les _ sont dans les prefix et abbv_ (eg Bl1-P).
    """
    return prefix + abbv_ + b64encode(uuid4().bytes).decode("utf8").rstrip("=\n")


def get_abbv_from_ID(oClOrdID_: str):
    """Identify dans oClOrdID_ ce qui ressemble à une abbrevation de hook.
    
    le préfix a été étendu avec nomT-PO ou nomT-SO
    """
    return oClOrdID_.split(ORDERID_PREFIX)[-1].split("-O")[0]


# @log_args()
def create_order(
    side, _q, opType, ordtype, execinst, prices=None, absdelta=2, text=None
):
    """
    Crée un 'side' ordre de type ordtype et de volume '_q'.  
    Ajoute les options 'execinst'.
    Si ordtype is stopLimit ou LimitIfTouched, absdelta détermine l'écart entre
    le prix d'entrée sur le marché et le stopPrice.
    """
    _q = int(_q)
    if _q < 30:
        raise Exception("pb de Balance et donc de quantité")

    if prices is None and ordtype != "Market":
        raise Exception(f"prices is None et ordtype ={ordtype}, quels prix donner ?")

    # création de l'ordre principal
    order = {"side": side, "orderQty": _q}

    if ordtype == "Limit":
        order["price"] = get_prix_decl(prices, side)
    elif ordtype in ["Stop", "MarketIfTouched"]:
        order["stopPx"] = get_prix_decl(prices, side)
    elif ordtype in ["StopLimit", "LimitIfTouched"]:
        _price = get_prix_decl(prices, side)
        order["price"] = _price
        order["stopPx"] = setdef_stopPrice(_price, side, absdelta)
    else:
        # cad ou ordType == 'Market'
        # par défault le prix sera celui du marché lorsque la condition sera validé
        pass

    order["ordType"] = ordtype

    # on traduit le nom lastMidPrice en un nom de prix reconnu par Bitmex.
    # lastMidPrice nous sert pour définir correctement le stop price (il me semble)
    opType = "LastPrice" if opType == "lastMidPrice" else opType
    order["execInst"] = opt_add_to_(
        opType, execinst
    )  # ': 'ReduceOnly',  #'ParticipateDoNotInitiate',
    order["text"] = text
    return order


def get_order_from(rcvLoad):
    """
    Find an order in the received load and return it, if the load is empty juste return it like that
    """

    if not rcvLoad:
        # we handle the case of empty loads
        return rcvLoad

    # cas de [{'order': ...}]
    ret = False
    if isinstance(rcvLoad, list):
        if len(rcvLoad) == 1:
            ret = rcvLoad[0]
        elif len(rcvLoad) > 1:
            mlogger.warning("On retourne le 1er elt de {recvLoad}.")
            ret = rcvLoad[0]

    elif isinstance(rcvLoad, dict):
        ret = (
            rcvLoad.get("order", False) or rcvLoad.get("orders", [False])[0] or rcvLoad
        )

    if not ret:
        raise Exception(f'Problème dans le format du rcvLoad "{rcvLoad}" so ret={ret}')

    return ret


# @log_args(logopt=__name__)
def set_order_type(ordkey, _extype):
    """
    Given the ordkey L, M, S, T, SL or TL, renvois le type d'ordre OrdType to be used par passer les ordres
    """
    # order type translation
    OT = {
        "L": "Limit",
        "M": "Market",
        "S": "Stop",
        "MT": "MarketIfTouched",
        "SL": "StopLimit",
        "LT": "LimitIfTouched",
    }

    ordType = OT.get(ordkey, None)

    if ordType is None:
        raise Exception(f"ordkey={ordkey}, _extype={_extype}")

    return ordType


def set_exec_instructions(extrakey, execinst, ordtype, pricetype):
    """
    Renvois execInst correctement formaté et valide avec les ordtype
    
    """

    execInst = ""
    if ordtype and contains(["Stop", "Touched"], ordtype):
        # pour Stop, StopLimit, MarketIfTouched, LimiteIfTouched. ordre avec stopPx
        _priceType = re.sub(r"(ask|bid)", "Last", pricetype)
        execInst = opt_add_to_(execInst, _priceType)

    if extrakey is None:
        return execinst

    if ("!" in extrakey) and ("Market" in ordtype):
        raise Exception(f"ExcInst {extrakey, ordtype} incompatibles")

    execInst = execinst
    if "!" in extrakey:
        execInst = opt_add_to_(execInst, "ParticipateDoNotInitiate")

    if "-" in extrakey:
        execInst = opt_add_to_(execInst, "ReduceOnly")

    return execInst


def set_price_type(pricekey, side):
    """
    avec la pricekey i, l ou m renvois le type de prix pour le suivi du déclenchement de la peg.
    Attention dans condition,  j'utilise ask et bid mais pour exec if faut LastPrice qui ont des stopPx.
priceType can be None if exist but not in PT
    
    """
    if pricekey is None:
        # defaultPriceType = 'bidPrice' if side is None or side == 'buy' else 'askPrice'
        defaultPriceType = "lastMidPrice"
    else:
        defaultPriceType = None

    PT = {
        "i": "IndexPrice",
        "l": "lastMidPrice",
        "m": "MarkPrice",
        "market": "MarketPrice",
        "indexPrice": "IndexPrice",
    }
    priceType = PT.get(pricekey, defaultPriceType)

    if priceType is None:
        raise Exception(f"priceType={priceType} is not in PT={PT}")

    return priceType


def is_valid_order_options(ordtype, pricetype, execinst=""):
    """
    Check the validity of the options.

    Si le type est market ou limit, il faut que le prix soit le prix du marché.
    car l'ordre va être passé immédiatement.
    """
    if ordtype in ["Market", "Limit"] and pricetype not in [
        "LastPrice",
        "bidPrice",
        "askPrice",
        "lastMidPrice",
    ]:
        return False
        #        raise Exception(f"ordtype={ordtype} and pricetype={pricetype} incompatible")
    return True


def remove_execInst(cslist, val):
    """Remove from the list of comma separated option the val."""
    _cslist = cslist.split(",")
    if val in _cslist:
        _cslist.remove(val)
        return ",".join(_cslist)
    return cslist


def split_ids(idlist):
    """
    Given a list of ids, returns two list one with oid and the other with clorid based on ORDERID_PREFIX 
    """
    clIDList = []
    oIDList = []
    for anID in idlist:
        if anID.startswith(ORDERID_PREFIX):
            clIDList.append(anID)
        else:
            oIDList.append(anID)

    return {"clIDList": clIDList, "oIDList": oIDList}
