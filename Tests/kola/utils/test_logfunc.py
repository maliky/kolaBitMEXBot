# -*- coding: utf-8 -*-
"""Test suite for kola.utils.logfunc.py."""
from Tests.hypo_strategies import st_loggers, st_logger_levels
from kolaBitMEXBot.kola.utils.logfunc import get_logfunc, throttled_log
from hypothesis import given
from hypothesis.strategies import integers, text

@given(
    logger_=st_loggers(), level_=st_logger_levels()
)
def test_get_logfunc(logger_, level_):
    get_logfunc(logger_, level_) 
    pass


@given(
    cpt_=integers(min_value=0, max_value=1000),
    logger_=st_loggers(),
    level_=st_logger_levels(),
    msg_=text(min_size=0, max_size=10),
    one_in_=integers(min_value=1, max_value=1000),
)
def test_throttled_log(cpt_, logger_, level_, msg_, one_in_):

    logfunc = get_logfunc(logger_, level_)
    throttled_log(cpt_, logfunc, msg_, one_in_)
    
    pass
