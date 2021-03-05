# -*- coding: utf-8; mode: Python; blacken-line-length: 83; -*-
"""Script qui gère une paire d'ordre et leur relance."""
from queue import Queue
from time import sleep
import logging
import sys
import os
import threading
import time
from typing import Optional, Union, Set
import numpy as np
import pandas as pd
from pprint import pprint

from kolaBitMEXBot.kola.bargain import Bargain
from kolaBitMEXBot.kola.chronos import Chronos
from kolaBitMEXBot.kola.dummy_bitmex import DummyBitMEX
from kolaBitMEXBot.kola.orders.hookorder import HookOrder
from kolaBitMEXBot.kola.orders.ordercond import OrderConditionned
from kolaBitMEXBot.kola.orders.trailstop import TrailStop
from kolaBitMEXBot.kola.settings import (
    SYMBOL,
    HTTP_SIMPLE_RATE_LIMITE,
    LOGNAME,
    LOGFMT,
    ordStatusTrans,
)
from kolaBitMEXBot.kola.utils.argfunc import (
    set_order_args,
    price_type_trad,
    get_args,
)
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION
from kolaBitMEXBot.kola.utils.conditions import cVraieTpsDeA, cVraiePrixDeA, cHook
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.utils.logfunc import get_logger, setup_logging
from kolaBitMEXBot.kola.utils.orderfunc import create_order
import kolaBitMEXBot.kola.utils.exceptions as ke

# harmonization de la timezone pour le script. Que ce passe-t-il au niveau du système?
# os.environ["TZ"] = "GMT"
os.environ["TZ"] = "UTC"
time.tzset()


class MarketAuditeur:
    """Classe du Market Auditeur."""

    def __init__(self, live: bool = False, dbo=None, logger=None, symbol=SYMBOL):
        """
        Une classe  pour placer un ordre conditionné et sa trace sur bitmex.
        Place an order pair on symbol market. 

        Un auditeur de marché, c'est une connexion qui écoute les prix du marché
        - serveur chronos:  envoie les ordres au marché
        - ordre principal d'achat ou de vente conditionné par le temps ou le prix
        - ordre de trace conditionné par le prix et qui sert aussi de stop loss.
        """
        self.live: bool = live  # connexion à live bitmex or test.bitmex
        self.ocp: Optional[
            Union[OrderConditionned, HookOrder]
        ] = None  # ordre principal
        self.stop: Optional[bool] = False

        self.symbol = symbol

        self.dbo = dbo  # dummy bitmex for test

        # on garde un suivi de la balance ici pour further analysis
        self.resultats = pd.DataFrame(
            index=pd.DatetimeIndex(data=[], name="start_time")
        )
        daynum = pd.Timestamp.now().strftime("%j")
        prefix = "tma" + daynum if live else "fma" + daynum
        prefix += "-dum" if self.dbo is not None else ""

        logfile = "./Logs/multi_kola.org"
        if logger:
            self.logger = logger
        else:
            self.logger = get_logger(logger, name=__name__, sLL="INFO")

        # to cache the hooks
        self.hookedIDs: Set[str] = set()
        self.logger.info(f"Market Auditeur Initialisé: {self}")

    def __repr__(self):
        """Représentation du Market auditeur."""
        rep = f"Live={self.live}-{self.symbol}, log to {self.logger}"
        if len(self.resultats):
            rep += f"Resultats={self.resultats}"

        return rep

    def start_server(self):
        """Démarre les services."""
        # canal échange ordre, serveur dispacheur
        self.fileDattente: Queue = Queue()

        # canal échange ordre, serveur dispacheur
        self.fileDeConfirmation: Queue = Queue()

        # connexion avec Bitmex
        self.brg: Bargain = Bargain(
            live=self.live, logger=self.logger, dbo=self.dbo, symbol=self.symbol
        )
        # Serveur dispacheur d'ordre
        self.chrs: Chronos = Chronos(
            self.brg,
            self.fileDattente,
            self.fileDeConfirmation,
            logger=self.logger,
        )
        self.chrs.start()
        # Resultats financiers
        self.resultats.loc[now(), :] = (self.balance(), np.nan)

    def stop_server(self):
        """Arrête le serveur."""
        self.stop = True

    # et étudier le passé du prix pour définir les meilleurs paramètres
    # @log_args()
    def go(
        self,
        tps_run,
        prix,
        essais,
        side,
        q,
        tp,
        atype,
        oType=False,
        nameT=None,
        updatepause=None,
        logpause=None,
        dr_pause=None,
        tType=None,
        timeout=None,
        oDelta=None,
        tDelta=None,
        hook=None,
    ):
        """
        Loop over three conditions.

        - times interval in offset from now,
        - a prix interval in offset from now
        - and the number of essais.
        Will try to lauch a trailorder avec les même paramètres
        utiliser des nombres <0 si nécessaire

        - atype define if price and quantity are set in %.
        use the string %p%q to set price and/or q in %
        sinon le prix est définie en différentiel par rapport au fairPrice price
        q is in % of available margin in [0,100]
        - tp peut être en % (defaut), absolue ou différentielle
        ajouter tA ou tD and les atypes
        - le temps est en minutes
        - si présent dr_pause définie le temps d'attendre entre deux ordres
        - hook: un tuple (srcName: ordre sur lequel se hooker et s
        rcStatus, le satus que cette ordre doit avoir pour déclencher le hook
        srcName doit être un nom d'un autre ordre.   La fin de src name est -S ou
        -P pour distinguer les ordres primaire ou secondaire.
        puis _ et un lettre pour le status   eg. src-S_C
        """
        # self.logger.info(
        #     f"#### Go : tps_run={tps_run}, prix={prix}, essais={essais},"
        #     f" side={side}, q={q}, tp={tp}, atype={atype}, oType={oType},"
        #     f" tType={tType}, oDelta={oDelta}, tDelta={tDelta},
        # dr_pause={dr_pause}m,"
        #     f" timeOut={timeout} m, balance={self.balance()}"
        # )
        _info = {
            "tps_run": tps_run,
            "prix": prix,
            "essais": essais,
            "side": side,
            "q": q,
            "tp": tp,
            "atype": atype,
            "oType": oType,
            "tType": tType,
            "oDelta": oDelta,
            "tDelta": tDelta,
            "dr_pause": dr_pause,
            "timeout": f"{timeout}m",
            "balance": self.balance(),
        }
        self.logger.debug(
            f"#### Go with args :\n{_info}"
        )

        # Init. des paramètres temps pour la condition de validité de l'ocp
        self.tpsDeb = now() + pd.Timedelta(tps_run[0], unit="m")
        self.tpsFin = now() + pd.Timedelta(tps_run[1], unit="m")
        tfmt = "%Y-%m-%dT%H:%M"
        repTpsDeb = self.tpsDeb.strftime(tfmt)
        repTpsFin = self.tpsFin.strftime(tfmt)

        # #### Setting defaults ####

        # Infos sur la durée moyenne de l'essai attendue.
        if pd.isna(dr_pause):
            dr_pause = 0
            dr_moy = (self.tpsFin - self.tpsDeb) / essais
        else:
            dr_moy = pd.Timedelta(dr_pause, unit="m")

        dr_essai_theo = dr_moy + pd.Timedelta(30, unit="s")

        if pd.isna(timeout):
            timeOut = (self.tpsFin - self.tpsDeb) / essais
        else:
            timeOut = pd.Timedelta(timeout, unit="m")

        if pd.isna(oDelta):
            oDelta = PRICE_PRECISION[self.symbol]

        if pd.isna(tDelta):
            tDelta = PRICE_PRECISION[self.symbol]

        # Expanding shortcut for ref price type:
        # decl du main order.
        oType = price_type_trad(oType, side)
        opType, ordType, execInst = oType
        # decl du stop (tail Type).
        tType = price_type_trad(tType, side)
        tpType, tOrdType, tExecInst = tType

        # #### On commence la boucle qui va gérer le run.
        # i.e. la relance des ordres pendant la période de validité des conditions
        i = 0
        while i < essais and not self.stop:
            self.tpsDebEssai = now()  # on sauvegarde le temps de départ

            # Traitement des paramètres de l'ordre suivant atype
            self.logger.info(f"oType={oType}, tType={tType}")
            oPrices, _q, _tp, tailPrices = set_order_args(
                prix,
                q,
                tp,
                atype,
                self.brg,
                oType,
                tType,
                recompute=True,
                side=side,
                symbol=self.symbol,
            )

            _info = {
                "Balance": f"{self.balance()}$",
                "tps_run": (repTpsDeb, repTpsFin),
                "début": self.tpsDebEssai,
                "pause": dr_moy,
                "hook": hook,
                "oPrices": oPrices,
                "tPrice": tailPrices,
                "side": side,
                "q": _q,
                "tp": round(_tp, 4),
                "oDelta": oDelta,
                "tDelta": tDelta,
                "opType": (opType, ordType, execInst),
                "tpType": (tpType, tOrdType, tExecInst),
                "timeOut": timeOut,
            }
            self.logger.info(
                f"### Essais {i+1}/{essais}, ({nameT}):\n{_info}"
            )

            # L'order Price type (déclencheur pour Touched & stop) est déjà dans execInst
            # penser à faire un objet order pour faciliter la mise à jour et l'init
            order = create_order(
                side, _q, opType, ordType, execInst, oPrices, oDelta
            )

            # On initialise les arguments condition pour les ordres principaux
            kwargs = {
                "send_queue": self.fileDattente,
                "order": order,
                "cond": cVraieTpsDeA(self.brg, self.tpsDeb, self.tpsFin),
                "valid_queue": self.fileDeConfirmation,
                "nameT": f"{nameT}-PO",
                "timeout": timeOut,
                "symbol": self.symbol,
            }

            self.logger.debug(f"~~~~ Order = {order}")
            if hook:
                _hSrc, _status = hook.split("_")
                _hStatus = ordStatusTrans[_status]
                # hook formé du 'nom-S_F' avec code ou nom-P_C eg.
                # <nom>-<src_secondair|src_principal>_<ordTargetStatus>-<SO|PO>id..
                self.ocp = HookOrder(
                    hSrc=_hSrc,
                    hStatus=_hStatus,
                    excludeIDs_=self.hookedIDs,
                    brg=self.brg,
                    **kwargs,
                )

                # add hook conditions
                hookCond = cHook(self.brg, _hSrc, _hStatus, exclIDs=self.hookedIDs)
                self.ocp.add_condition(hookCond)
                self.ocp.condition.set_excludeClIDs(self.hookedIDs)
            else:
                self.ocp = OrderConditionned(**kwargs)

            self.logger.debug(f"~~~~ Order = {order}")

            # On ajoute la condition de prix
            # ou ["lastPrice", 'fairPrice', 'markPrice']
            self.ocp.add_condition(
                cVraiePrixDeA(self.brg, opType, oPrices[0], oPrices[1])
            )

            msg = f"### OrdrePrincipal défini ### {self.ocp.__repr__(short=False)}"
            self.logger.debug(msg)

            # on accroche un stop (tail, trail) to follow the main order
            self.oct: TrailStop = TrailStop(
                self.ocp,
                self.brg,
                pegOffset_perc=_tp,
                updatepause=updatepause,
                logLevel_="INFO",
                logpause=logpause,
                nameT=f"{nameT}-SO",
                refPrice=tpType,
                execinst=tExecInst,
                ordtype=tOrdType,
                tDelta=tDelta,
            )

            # on active le tout, à partir de la tail
            try:
                self.oct.start()
                self.oct.join()

                # il s'agit ensuite de bloquer jusqu'à execution du stoptrace.
                # cela nécessite, conditions de l'ocp et du stop validées

            except Exception as e:
                self.logger.warning(
                    "#### Exception %s. Stopping -->\n%s" % (e, self.oct.condition)
                )
                self.fin_des_essais("ERROR", close=False)
                break

            self.fin_essai(
                i,
                essais,
                close=False,
                dr_pause=dr_pause,
                dr_essai_theo=dr_essai_theo,
            )
            # avant d'essaie aussi et s'assurer que le stop a bien été exécuté
            i += 1

            if hook:
                # On sauvegarde les clOrdID  qui on déclenché le hook
                self.hookedIDs |= set([self.ocp.condition.hookedSrcID])
                self.logger.info(f">>>> Updating hookedIDs={self.hookedIDs}")

        self.fin_des_essais(essais, close=False)
        sys.exit()

    def record_new_executions(self, fout_="./Logs/executions.csv"):
        """Enregistre les executions courantes."""
        self.logger.info(f"Recording new_executions to {fout_}")
        lexec = 0

        while not lexec:
            if self is not None:
                execution = self.brg.execution()

            sleep(10)
            lexec = len(execution)

        self.logger.info(f"First_execution")
        execution.to_csv(fout_)

        while True:
            new_execution = self.brg.execution()
            new_lexec = len(new_execution)
            if new_lexec > lexec:
                new_execution.loc[lexec:new_lexec].to_csv(
                    path_or_buf=fout_, header=False, mode="a"
                )
                lexec = new_lexec
            sleep(5)

        self.brg.execution.to_csv(fout_)  # append

    def fin_essai(self, i, n, close=False, dr_pause=None, dr_essai_theo=None):
        """
        Affiche les infos de finalisation de l'esssai et close quantity close.

        On fait varie le temps d'attente entre les essais.
        Il dépend du temps de l'essai
        """
        # info sur les résultats
        if close:
            # self.brg.cancel_and_close(quantity=close)
            # pourrai avoir un pb de timed out ici
            # sleep(randint(1, 3))
            pass

        # revoir le calcul de la balance
        self.resultats.loc[now(), "balance"] = self.balance()
        res_delta = self.resultats.iloc[-1] - self.resultats.iloc[-2]
        self.resultats.loc[self.resultats.index[-1], "benef"] = res_delta.loc["balance"]

        # info sur la durée de l'essai
        self.logger.info(
            f"\n\n#### Fin de l'essai {i+1}/{n}, Résultats:\n"
            f"{self.resultats.iloc[-1,:]}"
        )

        # si timed_out, restart without pause ?
        if not self.oct.main_oc.timed_out() and i + 1 < n:
            self.pause(dr_pause, dr_essai_theo)

    def balance(self):
        """
        Renvois la balance('usd') de façon en prenant compte les ordres passés récement
        et peut-être pas encore inscrit dans la balance, xbt, usd ou None (statoshi)

        """
        return self.brg.get_balance("usd")

    def fin_des_essais(self, essais, close=False):
        """
        Action à prendre à la fin de tous les essais
        Notre multi-tentative est finie.  On ferme tout
        """
        # On devrait mettre en place une tail sur la totalité de la position engagée,
        self.logger.info(
            f"\n\n################ Fin des {essais} essais:"
            f"{self.resultats}"
            "\n################ close and cancel ################\n\n"
        )
        # self.stop = True car va arrêter tous les ordres même les autres
        # si q == 0 close all position.  q mesure la réduction de la position
        # self.brg.cancel_and_close(quantity=close)
        # self.exit()
        sys.exit()

    def pause(self, dr_pause, dr_essai_theo):
        """
        set a variying waiting time between trials.

        - dr_pause is in minutes.  Waiting at least 10s and then a number
        varying with dr_pause
        """

        _dr_pause = 10 if dr_pause is None else dr_pause * 60

        try:
            dr_essai = now() - self.tpsDebEssai
            dr_delta = dr_essai - dr_essai_theo
            if dr_delta.seconds > 0:
                pause = _dr_pause
            else:
                pause = dr_delta.seconds + _dr_pause

            # attend au moins 10 secondes puis un nb aléatoire
            # qui suit une loi exponentielle de param dr_pause
            rnd_wait = np.floor(np.random.exponential(pause))
            _dr_pause = pd.Timedelta(10 + rnd_wait, unit="s")

            self.logger.info(
                f"Temps de l'essai {dr_essai} (theo: {dr_essai_theo})."
                f"  Going to sleep for {_dr_pause}.\n"
                "****************"
            )

            sleep(_dr_pause.seconds)
        except Exception as e:
            self.logger.error(
                f"{e} with dr_pause={_dr_pause}, "
                f"dr_essai_theo={dr_essai_theo} pause={pause}"
            )


def go_multi(ma: MarketAuditeur, arg_file=None, updatepause=None, logpause=None):
    """
    Charge le fichier orders arg_file, and starts market auditeur.

    - ma the interface to bitmex market
    - arg_file: path to order file
    - updatepause : time to wait between market price checking (throttle)
    - logpause: time to wait before logging prices objects
    """
    try:
        df = read_order_file(arg_file)
    except Exception as e:
        logging.error(df)
        raise (e)

    logging.info(
        f"Starting {len(df.index)} paire(s): update every={updatepause}s,"
        f" log every={logpause}s"
    )

    # _ = threading.Thread(
    #     target=ma.record_new_executions, kwargs={"fout_": "./Logs/executions.csv"}
    # )

    for idx in df.index:
        kwargs = df.loc[idx]
        kwargs.update(
            {"nameT": idx[1], "updatepause": updatepause, "logpause": logpause}
        )
        logging.debug(f" kwargs={kwargs}")

        t = threading.Thread(target=ma.go, name=idx[1], kwargs=kwargs)
        sleep(HTTP_SIMPLE_RATE_LIMITE)  # gérer le cas des erreur au départ ?
        t.start()


def read_order_file(arg_file):
    """Read the order file, parsing arguments to correct types."""
    # read the morder_file
    df = pd.read_csv(
        filepath_or_buffer=arg_file, sep="\t", comment="#", skip_blank_lines=True,
    )
    df = df.set_index([df.index, df.name]).drop(columns="name")
    df = df.apply(coerce_types, axis=1)
    return df


def coerce_types(s):
    """
    Coerce the df multi_orders types to what the go function expect.

    - tps_run: s.tps_run
    - essais: "int"
    - dr_pause: "float"
    - timeout: "int",
    - side: str
    - prix: s.prix,
    - q: "int", 
    - tp: "float", 
    - atype: str
    - oType: str
    - oDelta: "float",
    - tDelta: "float",
    - tType: str
    - hook: str
    """

    def handle_tuple(tpl, atype=None):
        """Change tuple elements to float except if they are - or +."""
        el1, el2 = tpl.strip(" ").split(" ")

        if atype is not None:
            if "p%" in atype:
                el1 = -90 if el1 == "-" else float(el1)
                el2 = 90 if el2 == "+" else float(el2)
            if "pD" in atype:
                el1 = float(el2) * 10 if el1 == "-" else float(el1)
                el2 = float(el1) * 10 if el2 == "+" else float(el2)
            if "pA" in atype:
                if el1 == "-":
                    # pas mal d'assertion à faire ici,
                    # pb de précision, d'arrondi
                    el1 = int(float(el2) / 10)
                else:
                    el1 = float(el1)

                if el2 == "+":
                    el2 = int(float(el1) * 10)
                else:
                    el2 = float(el2)
        return float(el1), float(el2)

    def coerce_to(ctype, elt):
        if pd.isna(elt):
            return None
        elif ctype == "int":
            return int(elt)
        elif ctype == "float":
            return float(elt)
        else:
            raise Exception("check coercing")

    return {
        "tps_run": handle_tuple(s.tps_run),
        "essais": coerce_to("int", s.essais),
        "dr_pause": coerce_to("float", s.pause),
        "timeout": coerce_to("int", s.tOut),
        "side": s.side.strip(),
        "prix": handle_tuple(s.prix, s.atype.strip()),
        "q": coerce_to("int", s.q),
        "tp": coerce_to("float", s.tp),
        "atype": s.atype.strip(),
        "oType": s.oType.strip(),
        "oDelta": coerce_to("float", s.oDelta),
        "tDelta": coerce_to("float", s.tDelta),
        "tType": s.tType.strip(),
        "hook": "" if pd.isna(s.hook) else s.hook.strip(),
    }


def main_prg():
    """Parse args and run main program."""
    args = get_args()

    kwargs = {
        "tps_run": args.tps_run,
        "prix": args.prix,
        "essais": args.nbEssais,
        "side": args.side,
        "q": args.quantity,
        "tp": args.tailPrice,
        "atype": args.aType,
        "oType": args.oType,
        "updatepause": args.updatePause,
        "logpause": args.logPause,
        "dr_pause": args.drPause,
        "tType": args.tType,
        "timeOut": args.tOut,
        "oDelta": args.oDelta,
        "tDelta": args.tDelta,
        "arg_file": args.argFile,
        "name": args.name,
        "hook": args.Hook,
    }

    rlogger = setup_logging()
    rlogger = get_logger(
        name=LOGNAME, sLL=args.logLevel, logFile=args.logFile, fmt_=LOGFMT
    )
    dbo = DummyBitMEX(up=0, logger=rlogger) if args.dummy else None
    tma = MarketAuditeur(
        live=args.liveRun, dbo=dbo, logger=rlogger, symbol=args.symbol
    )
    tma.start_server()

    try:
        if args.argFile is None:
            # on démarre un truc simple
            _ = kwargs.pop("arg_file")
            t = threading.Thread(target=tma.go, name=args.name, kwargs=kwargs)
            t.start()
        else:
            go_multi(
                tma,
                arg_file=args.argFile,
                logpause=args.logPause,
                updatepause=args.updatePause,
            )
    except ke.wsException:
        rlogger.exception("Erreur dans la socket... Quelque chose se prépare.")
        # les thread sont-ils alive ?


if __name__ == "__main__":
    main_prg()
    sys.exit()
