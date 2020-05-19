# -*- coding: utf-8 -*-
import argparse
import numpy as np
import re

from kolaBitMEXBot.kola.utils.logfunc import get_logger
from kolaBitMEXBot.kola.settings import LOGNAME
from kolaBitMEXBot.kola.utils.pricefunc import get_prices, get_prix_decl
from kolaBitMEXBot.kola.utils.orderfunc import (
    set_order_type,
    set_exec_instructions,
    set_price_type,
    is_valid_order_options,
)

mlogger = get_logger(name=f"{LOGNAME}.{__name__}")


def get_args():
    """
    Parse the function's arguments
    """
    description = """
    Un bot pour faire du trading de cryptomonnaie.
    """

    # default
    name_def = "NaDef"
    aType_def = "p%%q%%t%%"
    argFile_def = None
    dr_pause_def = None
    logFile_def = "./log.org"
    logLevel_def = "INFO"
    logPause_def = 60
    nbEssai_def = 3
    oType_def = "M"
    prix_def = [-1, 1]
    quantity_def = 75
    side_def = "buy"
    tOut_def = None
    tType_def = "Si-"
    tailPrice_def = 2
    tps_run_def = [-1, 800]
    updatePause_def = 10
    sDelta_def = 2
    hook_def = ""
    symbol_def = "XBTUSD"  # define the market to listent too

    name_help = f"Nom de l'ordre dans logs internes"
    symbol_help = f"Market to listen too. could be XBTM20 XBTU20 ADAM20 BCHM20 ETHUSD LTCM20 (default={symbol_def})"
    sDelta_help = (
        f"Différence entre le prix de l'ordre et le prix déclencheur de l'ordre."
        "  Utilisé pour les ordres de StopLimit et LimitIfTouched (default={sDelta_def})"
    )
    tps_run_help = f"le temps en minute à partir du moment du lancement, doit être un tuple, indique la plage horaire pour laquelle l'ordre est valide. (default={tps_run_def})"
    tOut_help = f"Temps d'attente de vérification in minutes de la validation de l'order.  Pour les limites order peut être très long. (default le temps du run) (default temps d'attente théorique d'un ordre ie, durée du run / nb orders)"
    prix_help = f"Une fourchette de prix dans laquelle executer le ou les essais.  Si le prix sort de la fourchette rien faire.  La fourchette de prix peut être exprimer en %% du prix actuellent, en différentiel ou en valeur absolu. (default={prix_def})"
    nbEssai_help = f"nombre d'essaies à effectuer (default={nbEssai_def})"
    side_help = f"côte de l'ordre (default={side_def})"
    quantity_help = f"quantité de l'ordre.  Doit toujours être positive ici.  Mais peut être exprimier en %% de la balance dispo ou en valeur Absolue (default={quantity_def})"
    tailPrice_help = f"C'est l'epaisseur de la queue (tail) qui suit le pri;  en %%, valeur absolu ou prix de départ (default={tailPrice_def})"
    aType_help = f"définie comment interpréter les arguments --prix (p), --quantity (q), --tailPrice (t). Suivant les lettres ajouter A, D ou %% pour en valeur Absolue, Differentiel ou pourcentage.  Exemple:  p%%qAt%% pour prix en pourcentage, quantité Absolue et t%% en pourcentage (default={aType_def})"
    oType_help = f"Declenchement price type. One of one of indexPrice, markPrice, lastPrice, respectivement pour le déclenchement des stop indexPrice,  markePrice (markPrice) et lastPrice (default ={oType_def})"
    logLevel_help = f"le niveau pour le log (default={logLevel_def})"
    logFile_help = f"Le fichier de sorti log. (default {logFile_def})"
    liveRun_help = "Si présent effectue un live run sinon sur tests"
    dummy_help = "Si présent utilise un dummy bitmex"
    updatePause_help = f"Le temps  (moyen) nentre deux update de la tail (en s), (default={updatePause_def})"
    logPause_help = f"Le temps (moyen) entre deux logs de l'évolution des prix. (default={logPause_def})"
    argFile_help = f"Indique le nom d'un fichier contenant multiples orders to run in one go. (default {argFile_def})"
    tType_help = f"one of indexPrice, markPrice, lastPrice, respectivement pour le déclenchement des stop indexPrice,  markePrice (markPrice) et lastPrice.  Du moins au plus volatille. Default (default {tType_def})"
    dr_pause_help = f"Indique une durée de pause lorsqu'un ordre fini. C'est approximatif.  attend au moins 10 secondes puis rnd.exp (default={dr_pause_def}) min"
    hook_help = f"define the name of the order to hook to and the status to wait for. F for filled, N for new, C for cancel and P for partial... formed as name_F (default={hook_def})"

    parser = argparse.ArgumentParser(description=description)

    parser.add_argument(
        "--tps_run", "-t", type=int, nargs=2, default=tps_run_def, help=tps_run_help
    )
    parser.add_argument("--name", "-m", default=name_def, help=name_help)
    parser.add_argument("--tOut", "-O", type=int, default=tOut_def, help=tOut_help)
    parser.add_argument(
        "--drPause", "-p", type=int, default=dr_pause_def, help=dr_pause_help
    )
    parser.add_argument(
        "--prix", "-x", type=float, default=prix_def, nargs=2, help=prix_help
    )
    parser.add_argument(
        "--quantity", "-q", type=int, default=quantity_def, help=quantity_help
    )
    parser.add_argument(
        "--tailPrice", "-T", type=float, default=tailPrice_def, help=tailPrice_help
    )
    parser.add_argument(
        "--logLevel", "-l", type=str, default=logLevel_def, help=logLevel_help
    )
    parser.add_argument(
        "--symbole", "-S", type=str, default=symbol_def, help=symbol_help
    )
    parser.add_argument(
        "--updatePause", "-U", type=int, default=updatePause_def, help=updatePause_help
    )
    parser.add_argument(
        "--logPause", "-F", type=int, default=logPause_def, help=logPause_help
    )
    parser.add_argument(
        "--argFile", "-A", type=str, default=argFile_def, help=argFile_help
    )
    parser.add_argument(
        "--sDelta", "-s", type=int, default=sDelta_def, help=sDelta_help
    )
    parser.add_argument(
        "--nbEssais", "-n", type=int, default=nbEssai_def, help=nbEssai_help
    )
    parser.add_argument("--oType", "-o", type=str, default=oType_def, help=oType_help)
    parser.add_argument("--tType", "-y", type=str, default=tType_def, help=tType_help)
    parser.add_argument("--side", "-c", type=str, default=side_def, help=side_help)
    parser.add_argument("--aType", "-a", type=str, default=aType_def, help=aType_help)
    parser.add_argument(
        "--logFile", "-f", type=str, default=logFile_def, help=logFile_help
    )
    parser.add_argument("--liveRun", "-L", action="store_true", help=liveRun_help)
    parser.add_argument("--dummy", "-D", action="store_true", help=dummy_help)
    parser.add_argument("--Hook", "-H", type=str, help=hook_help)

    return parser.parse_args()


def check_args(func):
    """
    Check the arguments
    """

    def new_func(*args):
        assert len(args) > 1
        assert all([isinstance(g, (int, float, str)) for g in args])
        args = [int(x) for x in args]
        return func(*args)

    return new_func


# @log_args(logopt=__name__)
def set_order_args(
    prix, _q, _tp, atype, brg, optype, tptype, recompute=True, side=None
):
    """
    définie les valeurs pour le prix, la quantité, la taille, en se basant sur atype (argtype) et prix type (tRefPrice tail).
    Si recompute is False, ne recalcule pas les pourcentages
    
    """
    # renvois le prix de référence pour la queue selon le tptype et side
    tailPrixRef = brg.prices(tptype, side)

    # renvois le prix de référence pour l'ordre principale selon optype et side
    ordPrixRef = (
        brg.prices(optype, side) if optype else brg.prices("market_maker", side)
    )

    # on gère le passage des prix en pourcentage
    # c'est bon de rester approximatif et de ne pas forcer des prix entier du genre 8800
    try:
        tailRefPrices = get_prices(tailPrixRef, prix, atype)
        ordPrices = get_prices(ordPrixRef, prix, atype)
    except Exception as e:
        mlogger.exception(
            f"Exception >>>> atype={atype} and tailPrixRef={tailPrixRef} and ordPrixRef={ordPrixRef}, optype={optype}"
        )
        raise (e)

    prixPrevuOrd = get_prix_decl(ordPrices, side)
    prixPrevuTail = get_prix_decl(tailRefPrices, side)

    # des options pour le tail
    if "tA" in atype:
        # le tail est en valeur absolue, prix ou doit être le tail / au prixPrevuTail
        tp = abs((_tp / prixPrevuTail - 1) * 100)
    elif "tD" in atype:
        # c'est qu'on l'a donné en différentielle par rapport au prix en val abs.
        tp = (_tp / prixPrevuTail) * 100
    else:
        # c'est que l'on à donné un pourcentage. tp = tp
        tp = _tp

    # si le quantité est donnée en pourcentage de la balance
    # on doit passer la quantité en contrats ou  (usd)
    # on calcul q approximativement par rapport au prix de déclenchement prévu
    if "q%" in atype and recompute and _q < 100:
        # on diminue toujours notre balance de 5%
        satoshi_balance = round(brg.get_balance(prixPrevuOrd) * 0.95, 2)
        mlogger.debug(f"q={_q} Avant, balance = {satoshi_balance}")
        q = int(np.floor(brg.get_balance("usd") * _q / 100))
        mlogger.debug(f"q={q} 1$ contracts Après")
    elif "qA" in atype and recompute:
        q = _q
    else:
        mlogger.exception(f"atype={atype}, _q={_q}, prixPrevuOrd={prixPrevuOrd}")
        raise Exception("Pb setting the quantity")

    # tailRefPrices is juste pour info car on les retrouve avec tp
    return ordPrices, int(q), float(tp), tailRefPrices


def price_type_trad(exType_, side=None):
    """
    Transforme an order shorthand (exType_) in orders options.

    MarkPrice, LastPrice, IndexPrice: Used by stop and if-touched orders
    to determine the triggering price. 
    Use only one. By default, 'MarkPrice' is used. 
    Also used for Pegged orders to define the value of 'LastPeg'.  
    It is bitMex fair price.

    Returns priceType, ordType et execInst.
    """
    execInst = ""
    priceType = None
    decrypt = r"(?P<majs>[A-Z]+)?(?P<mins>[a-z]+)?(?P<extra>.+)?"
    matchDict = re.search(decrypt, exType_).groupdict()
    # mlogger.debug(f'MatchDict={matchDict}')

    ordType = set_order_type(matchDict["majs"], exType_)
    priceType = set_price_type(matchDict["mins"], side)
    execInst = set_exec_instructions(matchDict["extra"], execInst, ordType, priceType)

    assert is_valid_order_options(
        ordType, priceType, execInst
    ), f"Check ordType={ordType}, priceType={priceType}, execInst='{execInst}'"

    return priceType, ordType, execInst
