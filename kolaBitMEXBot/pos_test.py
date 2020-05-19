# -*- coding: utf-8 -*-
"""Get the position."""
from time import sleep
from kola.bargain import Bargain
from kola.utils.orderfunc import get_logger
import argparse

# import logging


def run(live_: bool = False, **kwargs):
    """Run."""
    brg = Bargain(live=live_)
    bto = brg.bto
    ws = bto.ws
    print(brg)
    sleep(2)
    print(f"ws={ws}")
    print(f"position={ws.position(brg.symbol)}")


def get_args():
    """Parse the function's arguments."""
    description = """Un fichier pour executer des commandes simple sur bitmex."""
    parser = argparse.ArgumentParser(description=description)

    # default
    logLevel_def = "INFO"
    logLevel_help = "Le log level"

    parser.add_argument(
        "--logLevel", "-l", type=str, default=logLevel_def, help=logLevel_help
    )

    liveRun_help = "Si présent utilise live bitmex !"
    parser.add_argument("--liveRun", action="store_true", help=liveRun_help)

    return parser.parse_args()


def main_prg():
    """Parse argument and run main program."""
    args = get_args()

    fmt = (
        "%(asctime)s-%(levelname)s-%(filename)s@%(lineno)s(%(threadName)s): %(message)s"
    )
    rlogger = get_logger(name=__name__, sLL=args.logLevel, fmt_=fmt)
    run(live_=args.liveRun)


if __name__ == "__main__":
    main_prg()
