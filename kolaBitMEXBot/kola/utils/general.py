# -*- coding: utf-8 -*-
from decimal import Decimal, getcontext, ROUND_HALF_UP  # pour l'arrondi
import os  # for the path check
import pandas as pd
import numpy as np
import queue
import threading
import logging
import functools
from kolaBitMEXBot.kola.utils.constantes import PRICE_PRECISION
from kolaBitMEXBot.kola.settings import LOGLEVELS


def log_exception(logopt_=None, level_="ERROR"):
    """
    Decorator to log the function signature if exception is catched.

    - logopt: a logger object, a level or logger name.
    if level, will use the root logger with that level,
    if name get the logger with that name,  default to the root logger, 
    else use the logger passed.
    
    Return decorator
    """
    def set_logger(logopt_, level_):
        """Set get a logger and a level."""
        if isinstance(logopt_, logging.RootLogger):
            _logger = logopt_
        elif isinstance(logopt_, str):
            assert logopt_ not in LOGLEVELS, f"Use a level not {logopt_}"
            _logger = logging.getLogger(logopt)
        else:
            _logger = logging.getLogger(__name__)

        if isinstance(level_, str):
            _level = LOGLEVELS[level]

        return _logger, _level

    def set_log_message(ex_, func_, args_=None, kwargs_=None, res=None):
        """Set the log message."""
        # get / set the logger 
        msg = f"Catching exception >>>> {ex_}.\n"
        msg += f"{func_.__qualname__} Signature: args={args_}, kwargs={kwargs_}."
        if res is not None:
            msg += f" >>>> res={res}"
            
        return msg
        
    def decorator(func):
        @functools.wraps(func)  # what for ?

        def wrapped_func(*args, **kwargs):
            nonlocal logopt_, level_

            try:
                res = func(*args, **kwargs)
                return res
            except Exception as ex:
                logger, level = set_logger(logopt_, level_)
                msg = set_log_message(ex, func, args, kwargs, res)
                logger.log(level, msg)
                raise(ex)

        return wrapped_func

    return decorator


def log_args(logopt=None, level="INFO"):
    """
    Decorator to log arguments of a function.

    Passe the logger, level or logger name as logopt.
    if the level is passed will use the root logger with that level,
    if name passe will get the logger with that name and default
    to the root logger,  else use the logger passed.
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
    Interval check.  x in [a,b]

    - Bornes can be =, a=, b=, 'strict'
    pour respectivement ab inclus, a,b uniquement inclus, strict.
    """
    if bornes == "=":
        return a <= x and x <= b
    if bornes == "a=":
        return a <= x and x < b
    if bornes == "b=":
        return a < x and x <= b
    if bornes == "":
        return a < x and x < b



@log_exception()
def get_precision(x):
    """Renvois la précision décimale avec laquelle a été écrite x."""
    if isinstance(x, int):
        return 0

    # l'ordre des chiffres est préservés
    s = pd.Series(list(str(x)))

    # case of scientific notation
    if sum(s.isin(["e", "-"])) == 2:
        main_prec = get_precision(float("".join(s.iloc[:-4])))
        sprec_ = int("".join(s.iloc[-2:]))
        return int(main_prec + sprec_)

    # somme des conditions suivantes vraies
    # que les valeurs dont l'index est plus grand que celui du point
    # juste récupérer la valeur
    if not len(s.loc[s == "."].index):
        logger.debug(f"Getting_precision for s={s}. but something is wrong...")
                
    return int(sum(s.index > (s.loc[s == "."].index.values[0])))


def round_price(price: float, precision_=0.5) -> float:
    """Set defaut to round an XBT price. see round_half_up"""
    assert precision_ is not None
    # assert price != 0, f"price={price}, precision_={precision_}"
    return round_half_up(price, precision_)


def round_sprice(x, symbol_=None):
    """
    Renvois une fonction qui arrondie à l'unité de la précision passée.

    Par exemple: x=234.7 -> arrondira au dixème
    x=.00005 -> au millionnième
    """
    # assert x != 0, f"symbol_={symbol_}, x={x}"
    sprecision = PRICE_PRECISION.get(symbol_, 10**-get_precision(x))
    return round_price(x, sprecision)


def round_half_up(x: float, precision_: float) -> float:
    """
    Round x to the precision_.

    Given a number, round it to the nearest tick.
    - precision_ can be .2
    !! Attention arrondis 5.665 à .01 doit être 5.67 et non 5.66
    """
    assert precision_, f"y must be set but it is {precision_}"
    # p = get_precision(y)
    getcontext().prec = 28  # set precision context
    prec_ = to_decimal(precision_)
    arrondi = (to_decimal(x) / prec_).quantize(Decimal(1), rounding=ROUND_HALF_UP) * prec_

    # si precision_ = .5 -> 1 si 1e-7 -> 7
    round_precision = get_precision(precision_)
    x_ = float(round(arrondi, round_precision) )
    #logging.debug(f">>>> x_={x_} >>>> arrondi={arrondi}, round_precision={round_precision}.")
    return x_


def to_decimal(x: float) -> Decimal:
    """
    Convert float x to Decimal.

    using str remove unecessary precision errors.
    """
    try:
        return Decimal(str(x))
    except Exception as e:
        raise Exception(e, f"Check x, type(x)={x, type(x)}")


def round_to_d5(n: float) -> float:
    """Round number n with precision .5 (decimal 5)"""
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
