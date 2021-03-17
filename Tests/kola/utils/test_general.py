# -*- coding: utf-8 -*-
"""Test du module kola.utils.general"""
from kolaBot.kola.utils.general import get_precision
from hypothesis import given, strategies as st
from numpy.random import randint


def test_get_precision():
    """Teste la fonction qui renvoie la précision de x."""
    exemples = {
        0: [randint(0, int(1e9)), 10e9, 10 / 10, 123e0, 0.0, 0.00],
        1: [0.000000008e8, 0.50, 101 / 10, 123e-1],
        2: [0.01, 0.000000008e7, 0.050, 101 / 100, 123e-2, 2e-2],
        7: [20e-8],
        8: ["20e-8", 2e-8, 2e-08, 21e-08, 2.1e-07, 2.01e-06,],
        # 2.0e-7, 2.00e-6 
        10: [1234567809e-10],
    }
    # for k in range(3,8,1):
    #     exemples[k] = exemples.get(k, []) + [float(f"2e-{k}")]
        
    for res, list_tests in exemples.items():
        for val in list_tests:
            assert res == get_precision(val)

