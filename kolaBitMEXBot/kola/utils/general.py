# -*- coding: utf-8 -*-
from decimal import Decimal, getcontext, ROUND_HALF_UP  # pour l'arrondi
import os  # for the path check
import pandas as pd
import numpy as np
import queue
import threading
import logging
import functools
from kolaBitMEXBot.kola.settings import LOGLEVELS, LOGNAME

# mlogger = logging.getLogger('{LOGNAME}.{__name__}')
# mlogger.setLevel('INFO')


def log_args(logopt=None, level="INFO"):
    """
    Decorator to log arguments of a function.  Passe the logger, level or logger name as logopt. if the level is passed will use the root logger with that level, if name passe will get the logger with that name and default to the root logger,  else use the logger passed.
Returns as decorator to log the function's arguments
    """

    def decorator(func):
        @functools.wraps(func)  # what for ?
        def wrapped_func(*args, **kwargs):
            nonlocal logopt, level
            # Voir comment récupérer le nom de la fonction qui est décorée
            # pour qu'elle soit logger
            if isinstance(logopt, logging.RootLogger):
                logger = logopt
            elif isinstance(logopt, str):
                assert logopt not in LOGLEVELS, f"utiliser level to set log level"
                logger = logging.getLogger(logopt)
            else:
                logger = logging.getLogger(__name__)
                # logger.setLevel('INFO')

            if isinstance(level, str):
                level = LOGLEVELS[level]

            # logger.setLevel(level)

            logger.log(level, f"args={args}, kwargs={kwargs}")
            return func(*args, **kwargs)

        return wrapped_func

    return decorator


# #### File functions
def confirm_path_existence_for(filename):
    """
    S'assure que le path de filename exist et sinon le crée.
    filename is './bar/foo/baz.tex' for example
    """
    # récupère juste le path
    path = "/".join(filename.split("/")[:-1])
    if len(path) != 0 and not os.path.exists(path):
        os.mkdir(path)


# #### Math functions
def in_interval(x, a, b, bornes="="):
    """
    borne can be =, a=, b=, 'strict' pour respectivement ab inclus, a,b uniquement inclus, strict
    """
    if bornes == "=":
        return a <= x and x <= b
    if bornes == "a=":
        return a <= x and x < b
    if bornes == "b=":
        return a < x and x <= b
    if bornes == "":
        return a < x and x < b


def get_precision(x):
    """
    Renvois la précision décimale avec laquelle a été écrite x
    """
    if isinstance(x, int):
        return 0

    # l'odre des chiffres est préservés
    s = pd.Series(list(str(x)))

    return int(
        sum(  # somme des conditions suivantes vraies
            # que les valeurs dont l'index est plus grand que celui du point
            s.index
            > (s.loc[s == "."].index.values[0])
        )
    )  # juste récupérer la valeur


def round_half_up(x: float, y: float) -> float:
    """
    round x to the y precision. y can be .2 for example
    !! Attention arrondis 5.665 à .01 doit être 5.67 et non 5.66
    Given a number, round it to the nearest tick. Very useful for sussing float error
       out of numbers: e.g. toNearest(401.46, 0.01) -> 401.46, whereas processing is
       normally with floats would give you 401.46000000000004.
       Use this after adding/subtracting/multiplying numbers.
    """

    assert y, f"y must be set to not divide by zero but it is {y}"
    # p = get_precision(y)
    getcontext().prec = 28  # i think it's to trigger some precision stuff
    x, y = to_decimal(x), to_decimal(y)
    return float((x / y).quantize(Decimal(1), rounding=ROUND_HALF_UP) * y)


def to_decimal(x) -> Decimal:
    """
    convert number x to Decimal. using str remove unecessary presion errors
    """
    try:
        return Decimal(str(x))
    except Exception as e:
        raise Exception(e, f"Check x, type(x)={x, type(x)}")


def round_to_d5(n: float) -> float:
    """
    round number n with precision .5 (decimal 5)
    """
    return round_half_up(n, 0.5)


def is_number(elt) -> bool:
    try:
        float(elt)
        return True
    except ValueError:
        return False


# #### Thread functions
def enumerate_threads():
    """
    file:///usr/share/doc/python3.5/html/library/threading.html
    """
    return threading.enumerate()


def current_thread():
    return threading.current_thread()


# voir https://stackoverflow.com/questions/6893968/how-to-get-the-return-value-from-a-thread-in-python
def threaded(f, daemon=False):
    def wrapped_f(q, *args, **kwargs):
        "Cette fonction appelle la fonction décorée et met le résultat dans une queue."
        ret = f(*args, **kwargs)
        q.put(ret)

    def wrap(*args, **kwargs):
        "Cette fonction est celle retourné par le décorateur.  Elle démare le thread et retour l'objet du thread avec la queue"
        q = queue.Queue()

        t = threading.Thread(target=wrapped_f, args=(q,) + args, kwargs=kwargs)
        t.daemon = daemon
        t.start()
        t.result_queue = q
        return t

    return wrap


# ############### temps related

# #### Formatting functions
def string_to_tuple(chaine, seps=["(", ",", ")", " "]):
    return tuple([float(x) for x in chaine if x not in seps])


def fullDict(D):
    """Renvois les elts du dictionnaire D qui ne sont pas vide."""
    return {k: v for (k, v) in D.items() if v not in ["", None, 0]}


def value_filter(D, vfilter=[], dans=True):
    """Renvois les élts de D dont les valeurs sont (in True) ou (in False) et dans vfilter
    """
    if dans:
        return {k: v for (k, v) in D.items() if v in vfilter}
    else:
        return {k: v for (k, v) in D.items() if v not in vfilter}


def kfilter(D, kfilter=[]):
    """Filter a dictionnary based on keys in liste kfilter."""
    return {k: v for (k, v) in D.items() if k in kfilter}


def set_none_def(x, y):
    """Alias for: x if y is None else y."""
    return x if y is None else y


# @log_args(level='DEBUG')
def opt_add_to_(value_=None, opt_=None):
    """
    Ajoute l'option opt to value pour que l'on ait un format de string séparé par virgules
    - value can be None will be coerced to ''
    - Avoid redoncance de opt si déjà présent dans value.
    - ajoute la virgule si value contient déjà qqchose
    """
    value = value_ if value_ else ""  # permet d'avoir un '' quand None
    opt = opt_ if opt_ else ""
    if value.find(opt) < 0:
        return f"{value},{opt}" if len(value) else f"{opt}"
    else:
        return value


def opt_pop_if_in_(val_=None, opt_=None):
    """
    Given a val remove the element of the comma separated list if val is contained in on the elements.
    """
    if not val_:
        return opt_
    opt = opt_ if opt_ else ""
    return ",".join([x for x in opt.split(",") if val_ not in x.lower()])


def contains(liste, string, op="or"):
    """Renvois le nombre d'élément de la liste qui sont contenu dans le string."""
    amap = map(lambda x: string.find(x), liste)
    arr = np.array(list(amap)) >= 0
    if op == "or":
        return sum(arr)
    elif op == "and":
        return all(arr)
    else:
        raise Exception(f"Revoir l'opérateur {op}")


def trim_dic(dic, trimlist=["", 0, None], trimid=0, droptime=False, others=None):
    """
    Trim a dictionnary to only return entries not in trimList.

    defaut ('', 0, None),
    trim id should be a number to shorten the ID, set droptime True to drop columns with time in their name. set a list of other cols to drop trim from the dictionnary
    """
    if not isinstance(dic, dict):
        # "object dic={dic} must have .item() but of type {type(dic)}"
        return dic

    assert isinstance(trimid, int), f"Entrer un nombre pour reduire les ids"

    trimDic = {k: v for k, v in dic.items() if v not in trimlist}
    if trimid:
        trimDic.update({k: v[:trimid] for k, v in dic.items() if "ID" in k and trimid})
    if droptime:
        trimDic = {k: v for k, v in trimDic.items() if "time" not in k.lower()}
    if others:
        trimDic = {k: v for k, v in trimDic.items() if k not in others}
    return trimDic


# ############### decorators ################
def trim_output(trimimage=True):
    """
    Triming the output which shoud a dict, see trim_dic
    """

    def decorator(func):
        @functools.wraps(func)  # what for ?
        def wrapped_func(*args, **kwargs):
            fImage = func(*args, **kwargs)
            return trim_dic(fImage) if trimimage else fImage

        return wrapped_func

    return decorator


# #### ?? class
class dotdict(dict):
    """
    dot.notation access to dictionary attributes
    """

    def __getattr__(self, attr):
        return self.get(attr)

    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


def sort_dic_list(diclist, key, reverse=False, default=None):
    """
    sort a list of dictionnaries in ascending orders
    dicList: a list of dictionnaryies
    key: the dic key for the sort
    reverse: (False) ascending, True, descending
    default: (None) if not 'raise', the default value to return when the dict is not found in a dict
    """
    # a terme pourrait utilise pandas dataframe ici
    if default != "raise":
        return sorted(diclist, key=lambda d: d.get(key, default), reverse=reverse)
    else:
        return sorted(diclist, key=lambda d: d[key], reverse=reverse)


def cdr(expr_: str, sKey_: str = "_") -> str:
    """Return the all but the head part of a string split with sKey."""
    parts = expr_.split(sKey_)
    if len(parts) <= 1:
        return ""
    elif len(parts) == 2:
        return parts[-1]

    return sKey_.join(parts)


def car(expr_: str, sKey_: str = "_"):
    """Return the head part of a string split with sKey."""
    parts = expr_.split(sKey_)
    return parts[0] if len(parts) > 0 else parts
