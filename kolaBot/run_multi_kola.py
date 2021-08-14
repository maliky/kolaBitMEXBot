# -*- coding: utf-8 -*-
"""Lauch the program to run several order couple (main order and tail)."""
import argparse
import sys

from kolaBot.kola.utils.logfunc import get_logger, setup_logging
from kolaBot.kola.bitmex_api.dummy import DummyBitMEX
import kolaBot.kola.utils.exceptions as ke
from kolaBot.kola.settings import LOGFMT, LOGNAME
from kolaBot.multi_kola import MarketAuditeur, go_multi


rlogger = setup_logging()


class argsO:
    def __init__(
        self,
        argfile="./morders.tsv",
        loglevel="INFO",
        logfile=f"./Logs/test.org",
        liverun=False,
        updatepause=10,
        logpause=600,
        dummy=False,
        **kwargs,
    ):
        """Simulate the args from command line"""
        self.argFile = argfile
        self.logLevel = loglevel
        self.logFile = logfile
        self.liveRun = liverun
        self.dummy = False if liverun else dummy
        self.updatePause = updatepause
        self.logPause = logpause

    def __repr__(self, long=False):
        """Represent the program."""
        # should find the commande to get all attributes of object
        obj = {
            "argFile": self.argFile,
            "logLevel": self.logLevel,
            "logFile": self.logFile,
            "logPause": self.logPause,
            "liveRun": self.liveRun,
            "dummy": self.dummy,
            "updatePause": self.updatePause,
        }
        return f"argsO {obj}"


def main_prg():
    """Load arguments and run main program."""
    cmdArgs = get_cmd_args()
    defaultArgs = argsO()
    logger = get_logger(
        logger=rlogger,
        name=LOGNAME,
        sLL=cmdArgs.logLevel,
        logFile=defaultArgs.logFile,
        fmt_=LOGFMT,
    )

    dbo = DummyBitMEX(up=0, logger=logger) if cmdArgs.dummy else None

    tma = MarketAuditeur(
        live=cmdArgs.liveRun, dbo=dbo, logger=logger, symbol=cmdArgs.symbol
    )
    tma.start_server()

    try:
        go_multi(
            tma,
            arg_file=cmdArgs.morders,
            logpause=defaultArgs.logPause,
            updatepause=defaultArgs.updatePause,
        )
    except ke.wsException:
        rlogger.exception("Erreur dans la socket... Quelque chose se prépare.")
        # les thread sont-ils alive ?


def get_cmd_args():
    """Parse the function's arguments."""
    # default
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--morders",
        "-m",
        type=str,
        default=f"./Orders/xbt_test.tsv",
        help=(
            f"Path to the 'tsv' file containing the market orders. usually one per symbol."
        ),
    )
    parser.add_argument(
        "--symbol",
        "-S",
        type=str,
        default="XBTUSD",
        help=(
            f"Market to listen too. could be XBTM20 XBTU20 ADAU20 BCHM20 ETHUSD LTCM20"
        ),
    )

    parser.add_argument(
        "--logLevel", "-l", type=str, default="INFO", help=("Le log level")
    )
    parser.add_argument(
        "--liveRun", action="store_true", help=("Si présent utilise live bitmex !")
    )
    parser.add_argument(
        "--dummy",
        "-D",
        action="store_true",
        help=("Si présent utilise un dummy bitmex"),
    )

    return parser.parse_args()


if __name__ == "__main__":
    main_prg()
    sys.exit()
