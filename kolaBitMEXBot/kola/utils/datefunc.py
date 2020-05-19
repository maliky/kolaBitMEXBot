# -*- coding: utf-8 -*-
import pandas as pd
import datetime as dt

TC = {"1m": "60s", "5m": "300s", "1h": "1H", "1d": "1D"}

def multiply_time_unit(factor, unit):
    """
    Takes a pandas timedelta unit et multiply it by factor
    """
    return str(factor * int(unit[:-1])) + unit[-1]


# #### setting argument before run


def elapsed_time_since(datetime):
    """
    Renvois une chaine représentant le temps écoulé depuis datetime (datetime.datetime) jusqu'à maintenant
    """
    elapsed_td = dt.datetime.now() - datetime
    days = elapsed_td.days
    hours = int(elapsed_td.seconds / 3600)
    minutes = int((elapsed_td.seconds - hours * 3600) / 60)
    seconds = elapsed_td.seconds - (hours * 3600 + minutes * 60)
    return "{0:2d} j. {1:2d}:{2:2d}:{3:2d}".format(days, hours, minutes, seconds)


def remaining_time_to(datetime):
    """
    Renvois une chaine représentant le temps restant de maintenant jusqu'à datetime (datetime.datetime)
    """
    remaining_dr = datetime - dt.datetime.now()
    days = remaining_dr.days
    hours = int(remaining_dr.seconds / 3600)
    minutes = int((remaining_dr.seconds - hours * 3600) / 60)
    seconds = remaining_dr.seconds - (hours * 3600 + minutes * 60)
    return "{0:2d} j. {1:2d}:{2:2d}:{3:2d}".format(days, hours, minutes, seconds)


def hier():
    """
    Renvois  un tps avec l'heure d'hier
    """
    return now("ms") - pd.Timedelta(1, unit="D")


def now(ru="s"):
    """
    Renvois simplement un tps avec l'heure de l'appelle arrondie par défaut à la seconde
    """
    return pd.Timestamp.now().round(ru)


def tpsDans(x):
    """
    retourne le temps de l'appel + x minutes
    """
    return tpsOffset(x)


def tpsIlya(x):
    """
    retourne le temps de l'appel - x minutes
    """
    return tpsOffset(x, signe="-")


def tpsOffset(x, signe="+"):
    """
    retourne le temps de l'appel +- x minutes
    """
    if signe == "-":
        return (now() - pd.Timedelta(x, unit="m")).round("s")
    if signe == "+":
        return (now() + pd.Timedelta(x, unit="m")).round("s")


def setdef_timedelta(td, default=None):
    """
    Return td in pd.Timedelat format or default if td is None
    """
    if td is None:
        return default if default else pd.Timedelta(60, unit="m")

    if isinstance(td, pd.Timedelta):
        return td
    else:
        try:
            # suppose they are minutes
            return pd.Timedelta(td, unit="m")
        except Exception as e:
            raise (e)
