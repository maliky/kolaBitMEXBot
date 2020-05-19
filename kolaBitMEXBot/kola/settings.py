# -*- coding: utf-8 -*-
API_REST_INTERVAL = 5
API_ERROR_INTERVAL = 10
HTTP_SIMPLE_RATE_LIMITE = 1.5
HTTP_BULK_RATE_LIMITE = 0.30
TIMEOUT = 12
SYMBOL = "XBTUSD"
ORDERID_PREFIX = "foo_"

LIVE = False
POST_ONLY = False

URL = "https://www.bitmex.com/api/v1/"

# you may need to find a better way to feed your keys in the app
# but for testing purpose this should be ok
LIVE_URL = "https://www.bitmex.com/api/v1/"
LIVE_KEY = "Hlyouru6sliveis3jSkeyYJr"
LIVE_SECRET = "AOAIxand74lwsomeeMpd5dOI8VsecretM2ua273fgR0BAK3e"

TEST_URL = "https://testnet.bitmex.com/api/v1/"
TEST_KEY = "HxitestFovqkeyb3_0drZ41j"  
TEST_SECRET = "vJQygm1iPand_UPWTmffff_sometest5ydG-O-csecretF90"  

# constante
XBTSATOSHI = 10 ** -8

# for portfolio calculation
CONTRACTS = ["XBTUSD"]

# definie if the run parse commande line or takes arguments from setting file
PARSE_COMMANDE_LINE = True

# LOGS
LOGLEVELS = {
    "CRITICAL": 50,
    "ERROR": 40,
    "WARNING": 30,
    "INFO": 20,
    "DEBUG": 10,
    "NOTSET": 0,
    None: 0,
}
MAINLOGLEVEL = "DEBUG"

LOGFMT = "%(asctime)s %(threadName)s~%(levelno)s /%(filename)s@%(lineno)s@%(funcName)s/ %(message)s"

LOGNAME = "kola"

ordStatusTrans = {"N": "New", "C": "Canceled", "F": "Filled", "P": "PartiallyFilled", "T": "Triggered"}
