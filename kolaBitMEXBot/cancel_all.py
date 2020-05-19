# -*- coding: utf-8 -*-
"""Cancel and close all open order on testnet."""

from sys import exit
from tests.utils import Test
import logging
from multi_kola import MarketAuditeur


def main_prg():
    """Main program."""
    logging.getLogger("").setLevel("DEBUG")
    logging.info("Creating server")
    tma = MarketAuditeur(live=False)
    logging.info("Starting server")
    tma.start_server()
    logging.info("Running object Test server")

    T = Test(tma)
    logging.info(T.close_and_cancel())
    tma.stop_server()
    exit()


if __name__ == "__main__":
    main_prg()
