# -*- coding: utf-8 -*-
"""Get the position."""
from time import sleep
from kolaBot.kola.utils.orderfunc import get_logger
from kolaBot.kola.utils.constantes import PRICE_PRECISION
from kolaBot.kola.settings import LOGFMT
from kolaBot.tests.utils import T
from kolaBot.multi_kola import MarketAuditeur

import argparse

# import logging


def run(logger_, func_, live_: bool = False, symbol_=None):
    """
    Run simple default function func_ on a market defined by symbol_.

    - func_ : can be position, buyL
    - symbol_: define the market
    """
    tma = MarketAuditeur(live=False, logger=logger_, symbol=symbol_)
    tma.start_server()
    offset, offsetPx, offsetStop = [PRICE_PRECISION[symbol_] * x for x in [4, 20, 30]]
    qty = {'XBTUSD': 40, 'ADAU20': 5000}.get(symbol_, 40)
    T(tma, qty_=qty, offsetPx_=offsetPx, offsetStop_=offsetStop)
 
    logger_.info("Waiting 2s for connection to establish...")
    sleep(2)
    logger_.info(f"ws={T.ws}")

    logger_.info(f"Running {func_}")

    try:
        if func_ == 'position':
            logger_.info(f"position={T.ws.position(T.symbol)}")
        if func_ == "buyL":
            logger_.info(f"T.buyL()={T.buyL()}")
    except Exception as ex:
        logger_.error(f"Something went wrong. Exception:\n{ex}")

    logger_.info('**** End ****')


def get_args():
    """Parse the function's arguments."""
    description = """Un fichier pour executer des commandes simple sur bitmex."""
    parser = argparse.ArgumentParser(description=description)

    # default
    symbol_dft = 'XBTUSD'
    symbol_hlp = f"Symbol to initiate the Bargain (def. {symbol_dft})."
    parser.add_argument("-S", '--Symbol', default=symbol_dft, help=symbol_hlp)

    logLevel_def = "INFO"
    logLevel_help = "Le log level"
    parser.add_argument(
        "--logLevel", "-l", type=str, default=logLevel_def, help=logLevel_help
    )
    liveRun_help = "Si présent utilise live bitmex !"
    parser.add_argument("--liveRun", action="store_true", help=liveRun_help)

    func_dft = 'position'
    func_hlp = (
        f"Fonction to apply on the market. try 'position' or 'buyL' (def. {func_dft})."
    )
    parser.add_argument("-f", '--func', default=func_dft, help=func_hlp)
    return parser.parse_args()


def main_prg():
    """Parse argument and run main program."""
    args = get_args()

    rlogger = get_logger(name=__name__, sLL=args.logLevel, fmt_=LOGFMT)
    run(logger_=rlogger, func_=args.func, live_=args.liveRun, symbol_=args.symbol)


if __name__ == "__main__":
    main_prg()
