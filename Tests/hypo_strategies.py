# -*- coding: utf-8 -*-
"""hypothesis tests strategies de kolaBitMEXBot."""
from hypothesis.strategies import composite, integers, sampled_from, characters
import logging


def get_level_names(all_=False):
    _level_names = set(logging._nameToLevel.keys()) - {"NOTSET", "WARN"}
    if all_:
        return list(_level_names) + [lev.lower() for lev in _level_names]
    else:
        return list(_level_names)


@composite
def st_logger_levels(draw):
    return draw(sampled_from(get_level_names()))


@composite
def st_loggers(draw):
    """Return a random leveled Logger"""
    _name_size = draw(integers(min_value=0, max_value=10))
    _name = [draw(characters(whitelist_categories="L")) for i in range(_name_size)]

    logger = logging.getLogger("".join(_name))

    _level = draw(sampled_from(get_level_names()))
    logger.setLevel(_level)

    return logger
