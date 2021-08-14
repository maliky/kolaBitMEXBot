# -*- coding: utf-8 -*-
import argparse
import numpy as np
import re

from kolaBot.kola.utils.logfunc import get_logger
from kolaBot.kola.settings import LOGNAME
from kolaBot.kola.utils.constantes import PRICE_PRECISION
from kolaBot.kola.utils.pricefunc import get_prices, get_prix_decl
from kolaBot.kola.utils.orderfunc import (
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

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--tps_run",
        "-t",
        type=int,
        nargs=2,
        default=[-1, 800],
        help=(
            f"le temps en minute à partir du moment du lancement, doit être un tuple, "
            "indique la plage horaire pour laquelle l'ordre est valide."
        ),
    )
    parser.add_argument(
        "--name", "-m", default="NaDef", help=("Nom de l'ordre dans logs internes")
    )
    parser.add_argument(
        "--tOut",
        "-O",
        type=int,
        default=None,
        help=(
            "Temps d'attente de vérification in minutes de la validation de l'order.  "
            "Pour les limites order peut être très long. (default le temps du run) "
            "(default temps d'attente théorique d'un ordre ie, durée du run / nb orders)"
        ),
    )
    parser.add_argument(
        "--drPause",
        "-p",
        type=int,
        default=None,
        help=(
            f"Indique une durée de pause lorsqu'un ordre fini. C'est approximatif.  "
            "Attend au moins 10 secondes puis rnd.exp min"
        ),
    )
    parser.add_argument(
        "--prix",
        "-x",
        type=float,
        default=[-1, 1],
        nargs=2,
        help=(
            f"Une fourchette de prix dans laquelle executer le ou les essais.  "
            "Si le prix sort de la fourchette rien faire.  La fourchette de prix peut être "
            "exprimer en %% du prix actuellent, en différentiel ou en valeur absolu."
        ),
    )
    parser.add_argument(
        "--quantity",
        "-q",
        type=int,
        default=75,
        help=(
            f"Quantité de l'ordre.  Doit toujours être positive ici.  Mais peut être exprimé "
            "en %% de la balance dispo ou en valeur Absolue"
        ),
    )
    parser.add_argument(
        "--tailPrice",
        "-T",
        type=float,
        default=2,
        help=(
            f"C'est l'epaisseur de la queue (tail) qui suit le prix;  en %%, valeur absolue ou "
            "prix de départ."
        ),
    )
    parser.add_argument(
        "--logLevel",
        "-l",
        type=str,
        default="INFO",
        help=(f"le niveau pour le log."),
    )
    parser.add_argument(
        "--symbol",
        "-S",
        type=str,
        default="XBTUSD",  # define the market to listent too, help=( f"Market to listen too. could be XBTM20 XBTU20 ADAU20 BCHM20 ETHUSD LTCM20 (default={symbol_def})")
    )
    parser.add_argument(
        "--updatePause",
        "-U",
        type=int,
        default=10,
        help=(
            f"Le temps  (moyen) nentre deux update de la tail (en s)."
        ),
    )
    parser.add_argument(
        "--logPause",
        "-F",
        type=int,
        default=60,
        help=(
            f"Le temps (moyen) entre deux logs de l'évolution des prix."
        ),
    )
    parser.add_argument(
        "--argFile",
        "-A",
        type=str,
        default=None,
        help=(
            f"Indique le nom d'un fichier contenant multiples orders to run in one go."
        ),
    )
    parser.add_argument(
        "--oDelta",
        "-d",
        type=int,
        default=PRICE_PRECISION["XBTUSD"],
        help=("Différence entre le prix de l'ordre et le prix déclencheur de l'ordre."),
    )
    parser.add_argument(
        "--tDelta",
        "-s",
        type=int,
        default=PRICE_PRECISION["XBTUSD"],
        help=(
            f"Difference between trigger price and price for tail orders."
        ),
    )
    parser.add_argument(
        "--nbEssais",
        "-n",
        type=int,
        default=3,
        help=(f"nombre d'essaies à effectuer."),
    )
    parser.add_argument(
        "--oType",
        "-o",
        type=str,
        default="M",
        help=(
            f"Declenchement price type. One of one of fairPrice, markPrice, lastPrice, "
            "respectivement pour le déclenchement des stop fairPrice,  markePrice (markPrice) et"
            " lastPrice."
        ),
    )
    parser.add_argument(
        "--tType",
        "-y",
        type=str,
        default="Si-",
        help=(
            f"One of fairPrice, markPrice, lastPrice, respectivement pour le déclenchement "
            "des stop fairPrice,  markePrice (markPrice) et lastPrice.  "
            "Du moins au plus volatille."
        ),
    )
    parser.add_argument(
        "--side",
        "-c",
        type=str,
        default="buy",
        help=(f"Côte de l'ordre."),
    )
    parser.add_argument(
        "--aType",
        "-a",
        type=str,
        default="p%%q%%t%%",
        help=(
            f"Défini comment interpréter les arguments --prix (p), --quantity (q), --tailPrice (t). "
            "Suivant les lettres ajouter A, D ou %% pour en valeur Absolue, Differentiel ou "
            "pourcentage.  Exemple:  p%%qAt%% pour prix en pourcentage, quantité Absolue et "
            "t%% en pourcentage."
        ),
    )
    parser.add_argument(
        "--logFile",
        "-f",
        type=str,
        default="./log.org",
        help=(f"Le fichier de sorti log."),
    )
    parser.add_argument(
        "--liveRun",
        "-L",
        action="store_true",
        help=("Si présent effectue un live run sinon sur tests"),
    )
    parser.add_argument(
        "--dummy",
        "-D",
        action="store_true",
        help=("Si présent utilise un dummy bitmex"),
    )
    parser.add_argument(
        "--Hook",
        "-H",
        type=str,
        help=(
            "Define the name of the order to hook to and the status to wait for. "
            "F for filled, N for new, C for cancel and P for partial... formed as name_F."
        ),
    )

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
    prix, _q, _tp, atype, brg, otype, ttype, recompute=True, side=None, symbol=None
):
    """
    Définie les valeurs pour le prix, la quantité, la taille.

    Se base sur atype (argtype) et prix type (tRefPrice tail).
    Si recompute is False, ne recalcule pas les pourcentages
    -tptype: tail price type
    - symbol: XBTUSD ou ADAU20 for exemple. used to set rounding
    """
    # renvois le prix de référence pour la queue selon le tptype et side
    optype, ordtype, execinst = otype
    tptype, tordtype, texecinst = ttype

    tailPrixRef = brg.prices(tptype, side)

    # renvois le prix de référence pour l'ordre principale selon optype et side
    ordPrixRef = (
        brg.prices(optype, side) if optype else brg.prices("market_maker", side)
    )

    # on gère le passage des prix en pourcentage
    # c'est bon de rester approximatif et de ne pas forcer des prix entier du genre 8800
    try:
        tailRefPrices = get_prices(tailPrixRef, prix, atype, symbol)
        ordPrices = get_prices(ordPrixRef, prix, atype, symbol)
    except Exception as e:
        mlogger.exception(
            f"Exception {e} >>>> atype={atype}, tptype={tptype} and tailPrixRef={tailPrixRef} and ordPrixRef={ordPrixRef}, optype={optype}, symbol={symbol}"
        )
        raise e

    # mlogger.info(
    #     f" >>>> atype={atype}, tptype={tptype} and"
    #     f" tailPrixRef={tailPrixRef} and ordPrixRef={ordPrixRef}, optype={optype}"
    #     f" tailRefPrices={tailRefPrices}, ordPrices={ordPrices}, prix={prix}"
    #     f" symbol={symbol}."
    # )

    prixPrevuOrd = get_prix_decl(ordPrices, side, ordtype)
    prixPrevuTail = get_prix_decl(tailRefPrices, side, ordtype)

    # des options pour le tail
    if "tA" in atype:
        # le tail est en valeur absolue, prix ou doit être le tail / au prixPrevuTail
        assert (
            prixPrevuTail != 1
        ), f"tailRefPrices={tailRefPrices}, side={side}, atype={atype}."

        tp = abs((_tp / prixPrevuTail - 1) * 100)
    elif "tD" in atype:
        # c'est qu'on l'a donné en différentielle par rapport au prix en val abs.
        assert (
            prixPrevuTail != 0
        ), f"tailRefPrices={tailRefPrices}, side={side}, atype={atype}."

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

    markPrice, lastPrice, fairPrice: Used by stop and if-touched orders
    to determine the triggering price.
    Use only one. By default, 'markPrice' is used.
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
