# -*- coding: utf-8 -*-
"""Lauch the program to run several order couple (main order and tail)."""
import argparse
from sys import exit

from kolaBitMEXBot.kola.utils.logfunc import get_logger, setup_logging
from kolaBitMEXBot.kola.dummy_bitmex import DummyBitMEX
import kolaBitMEXBot.kola.utils.exceptions as ke
from kolaBitMEXBot.kola.settings import LOGNAME, LOGFMT
from kolaBitMEXBot.kola.settings import LOGLEVELS, LOGFMT, LOGNAME, MAINLOGLEVEL
from kolaBitMEXBot.multi_kola import MarketAuditeur, go_multi


rlogger = setup_logging()


class argsO:
    def __init__(
        self,
        argfile="./morders.tsv",
        loglevel="INFO",
        logfile=f"./Logs/log.log",
        liverun=False,
        updatepause=10,
        logpause=600,
        dummy=False,
        **kwargs,
    ):
        """Simulate the args from command line """
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
            "argFile": self.argfile,
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

    tma = MarketAuditeur(live=cmdArgs.liveRun, dbo=dbo, logger=logger)
    tma.start_server()

    try:
        go_multi(
            tma,
            arg_file=defaultArgs.argFile,
            logpause=defaultArgs.logPause,
            updatepause=defaultArgs.updatePause,
        )
    except ke.wsException:
        rlogger.exception("Erreur dans la socket... Quelque chose se prépare.")
        # les thread sont-ils alive ?


def get_cmd_args():
    """Parse the function's arguments."""
    description = """Lance les ordres du fichier morders."""

    # default
    logLevel_def = "INFO"
    logLevel_help = "Le log level"
    dummy_help = "Si présent utilise un dummy bitmex"
    liveRun_help = "Si présent utilise live bitmex !"

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--logLevel", "-l", type=str, default=logLevel_def, help=logLevel_help
    )
    parser.add_argument("--liveRun", action="store_true", help=liveRun_help)
    parser.add_argument("--dummy", "-D", action="store_true", help=dummy_help)

    return parser.parse_args()


if __name__ == "__main__":
    main_prg()
    exit()
