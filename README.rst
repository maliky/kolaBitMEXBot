Kola BitMEX Bot
===============

Presentation
------------

KolaBot is a program to handle concurrently several pairs of orders in
the BitMEX exchange. Each pair is made of an order to enter the market
and one to exit. You can choose cancel the entrance or exit by using
orders that will cancel automatically as they enter the book.

Use the configuration file *morders.tsv*, you can set different pairs
and the conditions for each pairs to trigger (and close).

For example:

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

(For more detail see `morders.tsv
file <https://github.com/maliky/kolaBitMEXBot/blob/master/kolaBitMEXBot/morders.tsv>`__)
All this and more is feasible with this bot. I do not recommend using it
to do trading below 1 order per minute unless you have special API
arrangement with BitMEX and in that case you should have an improved
bot. :o). The reason is that if you have several orders even If I use
the websockets, I make REST call that increase with the number of order
pairs to track.

Hooks: Update <2020-05-05 mar.>

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

#. Download or clone the repository
#. Install python dependencies
#. Edit settings.py with your BitMEX keys (or find a better way to pass
   them to the program)
#. Write your orders in the morder.tsv
#. Test your orders on testnet.BitMEX.com
#. Satified? Run it live! and enjoy.

Download or clone the repository
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   git clone https://github.com/maliky/kolaBitMEXBot.git
   cd kolaBitMexBot

Install python3 and dependencies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This will create a virtualen and install packages required by the
program. You need to pip3 and python3.8 installed on your system. To
install mutliple python on your system, check \`pyenv`.

.. code:: bash

   virtualenv --python=/path/to/python3.8  .
   cd /path/to/your/virtualenv/dir
   source ./bin/activate
   pip install -r /path/to/requirements.txt

Edit `settings.py <https://github.com/maliky/kolaBitMEXBot/blob/master/kola/settings.py>`__ with your BitMEX keys
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Write your orders in the `morder.tsv <https://github.com/maliky/kolaBitMEXBot/blob/master/morders.tsv>`__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Check that file to see how to write orders.

you can also get command line help with

.. code:: bash

   python multi_kola.py -h 

Test your orders on testnet.BitMEX.com
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   python run_multi_kola.py -l INFO

Satified? Run it live!
~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   python run_multi_kola.py -l INFO --live

TODO
----

-  [STRIKEOUT:linked orders] done called chained orders <2020-05-05
   mar.>

   -  That is you can an orders starting based on the state of one or
      more other orders.

-  Extend dummy bargain to have a personnal test net
-  Write hyptothesis tests

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
   ├── cancel_all.py
   ├── kola
   │   ├── bargain.py
   │   ├── chronos.py
   │   ├── connexion
   │   │   ├── auth.py
   │   │   ├── custom_ws_thread.py
   │   │   └── __init__.py
   │   ├── custom_bitmex.py
   │   ├── dummy_bitmex.py
   │   ├── __init__.py
   │   ├── orders
   │   │   ├── condition.py
   │   │   ├── hookorder.py
   │   │   ├── __init__.py
   │   │   ├── ordercond.py
   │   │   ├── orders.py
   │   │   └── trailstop.py
   │   ├── price.py
   │   ├── settings.py
   │   ├── types.py
   │   └── utils
   │       ├── argfunc.py
   │       ├── conditions.py
   │       ├── constantes.py
   │       ├── datefunc.py
   │       ├── exceptions.py
   │       ├── general.py
   │       ├── __init__.py
   │       ├── logfunc.py
   │       ├── orderfunc.py
   │       └── pricefunc.py
   ├── morders.tsv
   ├── multi_kola.py
   ├── pos_test.py
   ├── run_multi_kola.py
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
   set of required modules (see `deps <#deps>`__)
setup.py
   package file for python
