The Alpaca Gateway runs on your computer between your trading programs and your Alpaca account
(https://alpaca.markets). It is the only program that holds your Alpaca API key. Your trading
programs connect to the gateway, and the gateway places their orders and passes on account
updates and market data.

Nothing needs to be installed: everything the gateway needs is in this folder.


STARTING THE GATEWAY
--------------------

- Windows: double-click "Alpaca Gateway.cmd".
- Linux: double-click alpaca-gateway, or run ./alpaca-gateway in a terminal.

The first start writes config.cfg into this folder (see "Your Alpaca account" below). If config.cfg
has no API key, the window asks for one:

1. Choose Paper or Live at the top.
2. Enter the Key ID and Secret Key of that Alpaca account. You create them on Alpaca's dashboard,
   under API Keys.
3. Click Log in.

Keys entered in the window are kept in memory for this run only and never written to a file.

Once logged in, the window shows:

- The account: a blue PAPER ACCOUNT bar, or an orange LIVE ACCOUNT bar.
- Connection Status: the connections to Alpaca (trading, the trading stream, the market data
  stream) and to your trading programs. Green rows are up, red rows are down; the gateway
  reconnects by itself. The market data stream stays grey until a program asks for market data.
- Clients: the trading programs connected right now.
- Log: what the gateway did, newest last.

Keep the gateway running while you trade. Closing its window (after a confirmation) stops it, and
your trading programs then lose their connection.


CONNECTING YOUR TRADING PROGRAM
-------------------------------

Point your trading program at:

    ws://127.0.0.1:4102/

The same for a paper and a live account. The port can be changed in the settings.


YOUR ALPACA ACCOUNT: config.cfg
-------------------------------

config.cfg in this folder holds the account. The gateway only reads it and never writes your keys
into it. Open it with a text editor:

    [alpaca]
    environment = paper        ; paper | live
    key_id =
    secret_key =

- environment: paper (practice money) or live (real money).
- key_id, secret_key: your Alpaca API key. Fill them in to log in automatically at every start, or
  leave them empty to enter them in the window each time.

Anyone who can read config.cfg can trade your account. If you fill in the keys, keep the folder
private.


TRADING WITH REAL MONEY
-----------------------

A live account needs one extra step, so that it is never started by accident:

- With the keys in config.cfg (environment = live), start the gateway with the option --live.
  Without it, the gateway refuses to start.
- Without keys in config.cfg, choose Live in the login window.

The window shows the orange LIVE ACCOUNT bar while the gateway trades with real money.


SETTINGS
--------

Click the gear in the window's title bar. Paper and live have separate settings. Click Save, then
restart the gateway: the new settings apply from the next start.

- Data feed: where the market data comes from.
    iex          free, from the IEX exchange only
    sip          all US exchanges (a paid Alpaca subscription)
    delayed_sip  all US exchanges, 15 minutes delayed
- Socket Port: the port your trading programs connect to (default 4102).
- Allow local connections only: on by default, so that only programs on this computer can connect.
  Turn it off only to trade from another computer in a network you trust: any computer that
  reaches the port can then trade your account.
- Quotes, Forming bar: how often, at most, the gateway sends a symbol's newest quote and its
  forming one-minute bar to your trading programs, in milliseconds (default 100 and 250).

The settings are stored in:

    Windows:  %APPDATA%\alpacagw\settings.json
    Linux:    ~/.config/alpacagw/settings.json

They hold no API keys.


OPTIONS
-------

Options can be added to the start, for example in a Windows shortcut to "Alpaca Gateway.cmd" or
after ./alpaca-gateway on Linux:

    --live          start with the live account of config.cfg
    --headless      no window (config.cfg must then hold the keys)
    --port PORT     the port for this start, instead of the setting
    --host ADDRESS  the network address to listen on, instead of the setting


IF SOMETHING GOES WRONG
-----------------------

The gateway writes its log to alpacagw.log in this folder. If the gateway does not start or does
something unexpected, look there first, and send that file along with any question.


REMOVING IT
-----------

Delete this folder, and the settings folder listed under Settings.


LICENSES
--------

The Alpaca Gateway is free software under the MIT license (LICENSE). python/ is a standalone
Python with the libraries the gateway uses; their licenses are in python/ and next to each library
in python's site-packages folder.
