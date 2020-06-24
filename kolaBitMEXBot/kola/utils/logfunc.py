# -*- coding: utf-8 -*-
import logging
from logging import Logger, _nameToLevel

from kolaBitMEXBot.kola.utils.general import confirm_path_existence_for
from kolaBitMEXBot.kola.utils.datefunc import now
from kolaBitMEXBot.kola.settings import LOGLEVELS, LOGFMT, LOGNAME, MAINLOGLEVEL
import time


def setup_logging():
    logging.basicConfig(level=MAINLOGLEVEL, format=LOGFMT)
    # Suppress overly verbose logs from libraries that aren't helpful
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logging.getLogger()


def remove_unset_handlers(logger):
    """
    logger --
    Returns: the logger with all unset handlers removed
    """
    # stores the handlers in a list because accessing them directly was not removing them all
    hToRemove = [h for h in logger.handlers if not h.level]
    for h in hToRemove:
        logger.removeHandler(h)
    return logger


def handlers_of_type(logger, typehandler, trim=2):
    """ return the list of handers of same time for a logger. raise exception if more that trim """
    assert isinstance(trim, int)
    assert trim >= 0

    handlersType = [h for h in logger.handlers if type(h) == typehandler and h.level]

    assert len(handlersType) < trim, f"To many {handlersType}"
    # sort handlers by level, lowest first

    return sorted(handlersType, key=lambda h: h.level)


def add_uniq_type_handler(
    logger, handler, typehandler, level=None, fmt=None, prio="lower"
):
    """ logger, the looger to modify
    handler, the handler to add
    typehandler, the typeof the handler that should be added
    the level=None, the level of the handler
    fmt=None, the format of the handler
    prio=lower, lower level handler replace upper, None don't add, other raise exception if duplicate,
    higher stay in place if lower is assigned"""
    if level:
        _level = LOGLEVELS[level] if isinstance(level, str) else level
        handler.setLevel(_level)
    else:
        assert handler.level > 0, f"Handler Level not set {handler}"

    assert (
        type(handler) == typehandler
    ), f"handler={handler, type(handler)} is not of correct type {typehandler} "

    existingHandlers = handlers_of_type(logger, typehandler)
    if fmt:
        handler.setFormatter(fmt)

    if len(existingHandlers) > 1:
        raise Exception(
            f"several handler of same {typehandler} existe alerady {[(f, type(f)) for f in existingHandlers]}"
        )
    elif len(existingHandlers) == 1:
        existingHandler = existingHandlers[0]

        if not prio:
            # logger.debug(f'{logger} not adding {handler} because one of type exists already.')
            pass
        elif (prio == "lower" and existingHandler.level > _level) or (
            prio == "higher" and existingHandler.level < _level
        ):
            logger.removeHandler(existingHandler)
            logger.addHandler(handler)
            logger.warning(f"{handler} overriding {existingHandler}")
        else:
            # logger.debug(f'{logger} not adding {handler} because of level clash'
            # f' existing={existingHandler.level}, new={_level}')
            pass
    else:
        logger.addHandler(handler)

    return logger


def add_uniq_filehandler(logger, fh, level=None, fmt=None, prio=None):
    return add_uniq_type_handler(logger, fh, logging.FileHandler, level, fmt, prio)


def add_uniq_streamhandler(logger, sh, level=None, fmt=None, prio=None):
    return add_uniq_type_handler(logger, sh, logging.StreamHandler, level, fmt, prio)


def get_logfunc(logger_, level_="info"):
    """Return the logging function at level_ for logger_."""

    assert (
        level_.upper() in _nameToLevel
    ), f"level_, _nameToLevel={level_, _nameToLevel}"

    assert isinstance(
        logger_, Logger
    ), f"logger_, type(logger_)={logger_, type(logger_)}"

    return getattr(logger_, level_.lower())


def throttled_log(cpt_, logFunc_, msg_, one_in_: int = 10):
    """Log a message with logger after one_in_ cpt_() calls.

    cpt_ must be an int"""
    assert one_in_ >= 1 and isinstance(one_in_, int), f"one_in={one_in_, type(one_in_)}"
    
    if (cpt_ % one_in_) == 0:
        logFunc_(msg_)

    return None


def get_logger(
    logger=None,
    name=None,
    sLL="WARNING",
    fLL=None,
    logFile=f"default",
    fmt_=None,
    prio="lower",
):
    if name:
        logger = logging.getLogger(name)
        logger.setLevel(sLL)
    else:
        logger = logging.getLogger(LOGNAME)
    return logger


# I don't get the logging
def get_logger2(
    logger=None,
    name=None,
    sLL=None,
    fLL=None,
    logFile=f"default",
    fmt_=None,
    prio="lower",
):
    """
    Keyword Arguments:
    name    -- (default None), logger name, the logger with that name, else use __name__
ignore the rest
    sLL     -- (default 'INFO'), stream log level
    fLL     -- (default None), file log level
    logFile -- (default f'default'), if default log to log{now}.org
    fmt_    -- (default None), log format
priotype -- unique, lower, upper

    Returns: The logger
    """
    if name:
        if logger:
            # I juste want to update its attributes
            logger.name = name
            # logger = logging.getLogger(name)
        else:
            logger = logging.getLogger(name)
    elif logger is None:
        logger, logger.name = logging.getLogger(), LOGNAME

    # removing what can be add by default with level 0
    logger = remove_unset_handlers(logger)

    sLL = "INFO" if sLL is None else sLL
    fmt = logging.Formatter(fmt_) if fmt_ else logging.Formatter(LOGFMT)
    fmt.converter = time.gmtime  # setting logtimezone
    logger = add_uniq_streamhandler(
        logger, logging.StreamHandler(), sLL, fmt, prio="lower"
    )

    # adding file handler for debug logs
    # need to check that not fh exists already
    if logFile == "default":
        logFile == f'log{now("60s").strftime("%Hh%M")}.org'

    if logFile:
        confirm_path_existence_for(logFile)
        fh = logging.FileHandler(logFile, mode="w", encoding="utf-8")
        fLL = fLL if fLL else sLL
        logger = add_uniq_filehandler(logger, fh, fLL, fmt)

    return logger


def removeHandlers(logger):
    """ remove all handlers of a logger """
    handlers = logger.handlers
    for h in handlers:
        logger.removeHandler(h)
    return logger


def get_level_name(logger_):
    """Return the name of the logger_ level."""
    return logging.getLevelName(logger_.level)
