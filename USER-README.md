The Brackfast Gateway (formerly the Alpaca Gateway) runs on your computer between your trading
programs and the market: your Alpaca account (https://alpaca.markets), or Yahoo Finance's prices
with a simulated account. It is the only program that holds your Alpaca API key. Your trading
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

Another file instead of config.cfg, for example one per account: start the gateway with
--config and the file's name (see Options). It is written the same way.


TRADING WITH REAL MONEY
-----------------------

A live account needs one extra step, so that it is never started by accident:

- With the keys in config.cfg (environment = live), start the gateway with the option --live.
  Without it, the gateway refuses to start.
- Without keys in config.cfg, choose Live in the login window.

The window shows the orange LIVE ACCOUNT bar while the gateway trades with real money.


SETTINGS
--------

Click the gear in the window's title bar (its About tab shows the version and the software the
gateway uses). Paper and live have separate settings. Click Save, then
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
- Check for updates at start (the About tab, saved at once): on by default (see Updates).

The settings are stored in:

    Windows:  %APPDATA%\alpacagw\settings.json
    Linux:    ~/.config/alpacagw/settings.json

They hold no API keys.


YAHOO FINANCE
-------------

The gateway can take its market data from Yahoo Finance instead of Alpaca. You need no account and
no keys: click "Use Yahoo Finance" in the login dialog, or choose it in Settings, "Data source"
(then restart the gateway). The account is then the simulated one (see SIMULATED ACCOUNT), as
Yahoo has no orders.

    - It is unofficial. Yahoo offers no interface for programs; the gateway reads what Yahoo's
      web site reads. Yahoo can change or block this at any time, and Yahoo's terms of use apply
      to you. Nothing about you is sent.
    - History: 1-minute bars for the last 30 days, 2 minutes to 1 hour for the last 60 days,
      daily, weekly and monthly bars for many years.
    - Live prices are Yahoo's last price. Yahoo sends no bid and ask, so both show that price and
      simulated orders fill at it, without a spread.
    - Between 20:00 and 04:00 New York the price moves (overnight trading) but no bars are drawn.

To go back to Alpaca: Settings, "Data source", Alpaca, then restart.


BAR CACHE
---------

The gateway keeps the historical bars it has fetched on disk, so a chart opened again gets its
history at once and Alpaca is asked only for the newest bars. Settings, "Keep historical bars on
disk" switches it off; "At most ... MB per data source" limits its size (the bars used longest ago
go first). Each data feed (iex, sip, delayed_sip) has a file of its own. The Bar Cache tab of the
gateway's window (next to Clients and Log) lists them with their sizes; Clear empties one, Clear
All every one. The files are in:

    Windows:  %APPDATA%lpacagwar-cache    Linux:    ~/.config/alpacagw/bar-cache/

The files hold market data only and can be deleted at any time.


SIMULATED ACCOUNT
-----------------

Settings, tab Simulation, "Simulated account" (then restart the gateway): orders no longer go to Alpaca. They stay
in the gateway and fill from Alpaca's quotes; your Alpaca keys are used for the market data only.
The account bar says SIMULATED ACCOUNT, and trading apps see a paper account named SIMULATED.

    - A buy fills at the ask, a sell at the bid, always the whole order.
    - A limit order fills at its limit once the price reaches it, a stop at the price once it
      passes the stop.
    - Fills happen from 09:30 to 16:00 New York, as at the exchange. "Fill outside regular hours"
      fills whenever there is a price, also at night with the last price of the closed market.
    - The account starts with the "Starting cash" and is kept across restarts. "Reset the
      simulated account" in the settings makes it new.

It is for trying things out and for testing apps. Real fills are worse than simulated ones: there
is no queue, no partial fill, and no fee, margin or buying power rule.


UPDATES
-------

At start, the gateway asks the website which version is the newest; only that request is sent,
nothing about you or your account. When there is a newer one, a green line under the account bar
says so, with a button for the download page (x hides it). Settings, About: Check for updates.
Without a window (--headless) the log says it.


OPTIONS
-------

Options can be added to the start, for example in a Windows shortcut to "Alpaca Gateway.cmd" or
after ./alpaca-gateway on Linux:

    --config FILE   the account from FILE instead of config.cfg (FILE must exist)
    --live          start with the live account of config.cfg (or of FILE)
    --headless      no window (config.cfg or FILE must then hold the keys)
    --simulate      a simulated account for this start (see SIMULATED ACCOUNT)
    --yahoo         Yahoo Finance's market data for this start (see YAHOO FINANCE)
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

The Brackfast Gateway is free software under the MIT license (LICENSE). python/ is a standalone
Python with the libraries the gateway uses; their copyright notices and licenses are in
THIRD-PARTY-LICENSES.txt (also Settings, About: Open the licenses).
