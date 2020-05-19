Kola BitMEX Bot
===============

Presentation
------------

KolaBot is a program to handle concurrently several pairs of orders in
the BitMEX exchange. A pair is a main order (amoung those allowed by
BitMEX) and an opposite order (again among allowed) that acts as a stop.
Each order enter the book based on conditions you set in
`morders.tsv <https://github.com/maliky/kolaBitMEXBot/blob/master/kolaBitMEXBot/morders.tsv>`__.

Main conditions
~~~~~~~~~~~~~~~

-  time condition: an order activate if the time enters the [dateA,
   dateB] range
-  price condition: an order activate the market enters the [priceA,
   priceB] range

other conditions
~~~~~~~~~~~~~~~~

-  a timeout: an order will cancel after timeout minutes. Note this
   enable speed conditions (eg. activate only if price rise by 60ø in 1
   minute, else cancel and restart)
-  a repeat #: an order will repeat # times if canceled or filled
-  a waitting time: if canceled or filled the order pair will wait
   before repeating (min wait ~1 minute)
-  a hook condition: (new not fully stable yet), an order will activate
   if a hooked order reach a specified status (filled, partialy filled,
   canceled)

Units
~~~~~

-  time units: they are generaly in minutes and for some command line
   params, in seconds.
-  price units can be:

   -  relative to market price (index, mark or last Price)

      -  in %, of the activating price (eg. if price move by 5% from
         order activation)
      -  in differential of the activating price (eg if price move by
         -80ø from order activation)

   -  absolute, (eg. if price reach 3500ø)

Options specific to the stop order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By defaut kolaBitMEXBot runs an opening order (main) and a close order
(secondary or tail or trail). If your main is a buy, the secondary will
be a close and vice-versa. It is possible to run only one of the two
order by setting automatic cancelation condition on the not wanted
order.

Here's a simple example: A buy at market (main order) with a stop at
market (secondary order). You just need to set the price differential
between the main order and the tail order. Like you want a tail at:

-  100ø below the activating price (relative in differential),
-  or at 2% below of the activating price (relative in %),
-  or at 2500ø what ever the activating price (absolute)

Example scenarios
~~~~~~~~~~~~~~~~~

-  you may want to buy 10% of your available balance if the index price
   goes down by 2% in 2 hours but this should only true for the next 24
   hours.

-  you may want to place a simple buy limit if the price goes up by 100
   USD and then automatically set a (trailing) stop at this higher price
   - 20 USD. This trail stop actually acts both as a stop order and a
   trail stop. If the market moves against you, it stays in place but if
   the market move in your favor it will only move once the price will
   be 20 or higher above the entry price (in this example). Also you can
   set in the code some flexibility to the trail. Meaning that if the
   price rise quickly the trail price will close down on the order up to
   20% of the initial 20 USD delta you set. This is because usualy chart
   increase are followed buy sharp reverse and I think it's wise to go
   out of the market to pick it up automatically later.

-  you may want to use a sell (short) each time the price hits 20000 USD
   with a buy at stop 21000 USD and if the stop trigger wait 20 minutes
   before resetting the condition.

All this and more is feasible with this bot. I do not recommend using it
to do trading below 1 order per minute unless you have special API
arrangement with BitMEX and in that case you should have an improved
bot. :o). The reason is that if you have several orders even If I use
the websockets, I make REST call that increase with the number of order
pairs to track.

Hooks: Update <2020-05-05 mar.>
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You can now use hooks or what was once called chained order in BitMEX. A
hook condition is a condition that is true if the hooked order reach the
state defined in the condition. The states can be Filled (F), Triggered
(T), Canceled (C) or PartiallyFilled (P) Since orders are always passed
in pairs. The hook condition can apply to the Principal order (the one
to enter the book) or the Secondary order (the one to exit). The syntax
to define the hook condition is as follow \`<name>-[P|S]-[F|C|T|P]\`

-  Examples:

   -  src1-P-F, will hook when the principal order of the pair named
      src1 will be filled
   -  src1-P-C, will hook when the principal order of the pair named
      src1 will cancel
   -  foo-S-P, will hook when the principal order of the pair named foo
      will partially fill …

#. Why hooks ?

   Hooks can be useful especially with trail orders. Hook price and time
   settings are relative to the moment their are started. So you can
   have an order that enter the book when a trailing stop is filled.
   This could be useful to catch reversal in momentum.

Installation
------------

Download or clone the repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Git Project URL: https://github.com/maliky/kolaBitMEXBot

.. code:: bash

   git clone https://github.com/maliky/kolaBitMEXBot.git
   cd kolaBitMexBot

Install dependencies
~~~~~~~~~~~~~~~~~~~~

This will create a virtualenv and install packages required by the
program. You need to \`pip3\` and \`python3.8\` installed on your
system. *note To install mutliple python on your system, check
\`pyenv`.*

.. code:: bash

   virtualenv --python=</path/to/python3>  .
   source ./bin/activate
   pip install -r requirements.txt

   # run main programmes
   python -m  kolaBitMEXBot.run_multi_kola -h
   python -m  kolaBitMEXBot.multi_kola -h

pip install
~~~~~~~~~~~

If you just want to use kolaBitMEXBot, you can install the module
directly with \`pip\`

.. code:: bash

   pip install kolaBitMEX

but I would recommand doing it as a pip editable module with:

.. code:: bash

   # build package with the setup.py
   python setup.py sdist bdist_wheel; twine check dist/*
   # if you used virtualenv wheel and twine will have been installed

   # install the package from local source
   pip install -e . 

   # run progams ... edit them... have fun
   run_multi_kola -h
   multi_kola -h

Edit `settings.py <https://github.com/maliky/kolaBitMEXBot/blob/master/kolaBitMEXBot/kola/settings.py>`__ with your BitMEX keys
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Write your orders in the `morder.tsv <https://github.com/maliky/kolaBitMEXBot/blob/master/kolaBitMEXBot/morders.tsv>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test your orders on testnet.BitMEX.com
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   python -m kolaBitMEXBot.run_multi_kola -l INFO > testlog.org

Check the testlog.org file

Satified? Run it live!
~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   python run_multi_kola.py -l INFO --live

Extension TODO
--------------

.. _make-chained-or-hooked-orders-2020-05-05-mar.:

DONE make chained (or hooked) orders <2020-05-05 mar.>
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

That is, you can an orders starting based on the state of one or more
other orders.

Extend dummy bargain to have a personnal test net
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Write hyptothesis tests
~~~~~~~~~~~~~~~~~~~~~~~

FAQ
---

Why is it free ?
~~~~~~~~~~~~~~~~

#. Because I had fun making it.
#. Also because the gift you may give me having fun using this code,
   will be infinitely more valuable for me, if its free.:o)

381b5ygUaK3CpHSKH2kKYCYKGMUbH4ruiw (BTC only)

Did I loose money with that bot ?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

No, but this is a personnal investissement. I spend a gift of 0.5 XBT to
test it live, and during the development phase which I started in
September 2017 I used most of it. The reasons are simple: I didn't know
much about trading and I'm not a professional coder. Also, testnet is
not as good as real market to make real tests. I'm confident that this
bot is a great tool to help anyone willing to gain money. 🥂

Warning !
~~~~~~~~~

Check the code! I'm not an professional programmer and if I made many
tests along this 2 years journey on building kola BitMEX bot I did not
consistently use a test framework yet. THEREFORE there are obviously
many improvements to be made.

That been published, THIS CODE IS LICENCE FREE. No Gnu, no MIT, no
GitHub©, no what so ever regulation from my part. Although, I may be
obliged by some legal contracts I'm not consciously aware off because I
did reused codes notably `BitMEX's API
connectors <https://github.com/BitMEX/api-connectors>`__ and an
uncountable number of functions from python's imported libraries
(pandas, request, numpy, datetime…). I hope their authors don't really
care. We are born free, let's stay so. So, your are free to do what you
want with this code BUT… do check it and understand it.

File Index
----------

Core program files
~~~~~~~~~~~~~~~~~~

.. code:: bash

   kolaBitMEXBot
   ├── cancel_all.py  ->  cancel and close all order on testnet
   ├── kola
   │   ├── bargain.py  ->  handle connections to markets
   │   ├── chronos.py  ->  handle timeouts and thread of active orders
   │   ├── connexion
   │   │   ├── auth.py  ->  authentification to bitMEX
   │   │   ├── custom_ws_thread.py  ->  websocket API
   │   │   └── __init__.py
   │   ├── custom_bitmex.py
   │   ├── dummy_bitmex.py
   │   ├── __init__.py
   │   ├── orders
   │   │   ├── condition.py  ->  hold condition object to activate orders
   │   │   ├── hookorder.py  ->  orders that can hook to other orders
   │   │   ├── __init__.py
   │   │   ├── ordercond.py  ->  basic order with condition. other orders inherit it
   │   │   ├── orders.py  ->  functions to places limit, stop, limit if touched ...
   │   │   └── trailstop.py  ->  orders that follow price variation and update 
   │   ├── price.py  ->  object to follow the different prices indexes
   │   ├── settings.py  ->  setting files (where your keys may be)
   │   ├── secrets.py  ->  where API keys could be
   │   ├── types.py  ->  (new) types to start typing the programm
   │   └── utils
   │       ├── argfunc.py  ->  handle command line arguments
   │       ├── conditions.py  ->  function to set conditions
   │       ├── constantes.py  ->  constants
   │       ├── datefunc.py  ->  function to handle dates
   │       ├── exceptions.py  ->  customized exceptions
   │       ├── general.py  ->  generic utils
   │       ├── __init__.py
   │       ├── logfunc.py  ->  log function
   │       ├── orderfunc.py  ->  utils to set or check orders
   │       └── pricefunc.py  ->  utils to set or get prices
   ├── morders.tsv  ->  where you set your orders
   ├── multi_kola.py  ->  handle the (multiple runs) of one pair of orders 
   ├── pos_test.py  ->  (depreciated...)
   ├── run_multi_kola.py  ->  handle multiple pairs of orders (parse morders.tsv)
   └── tests
       └── utils.py

   5 directories, 33 files

Setup and annexes program files
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.python-version
   pyenv local python-version, should be >=3.8
.dir-locals.el
   a versatile IDE config file (emacs :))
.gitignore
   files that git should ignore
setup.cfg
   config file for flake, mypy
LICENSE.txt
   a permissive license
README.rst
   this README
requirements.txt
   set of required modules
setup.py
   package file for python
