# Changes

What each version brings, for users. The build writes the section of the version it builds to
`dist/alpaca-gateway-<version>-changes.md`, next to the archives (for the website). Add a section
`## X.Y.Z` before tagging `vX.Y.Z`; the newest comes first.

## 0.2.0

- **One port for paper and live:** trading programs connect to ws://127.0.0.1:4102/ for either account
  (before, a live account was on 4101). The port can still be changed in the settings.
- **Paper or live at a glance:** the account shows in blue for paper, orange for live.
- **Live bars without false wicks:** the forming minute takes its prices only from the trades Alpaca's own
  bars count. Odd lots at old prices and trades priced off the market (average price, derivatively priced)
  drew long wicks into it until the minute's bar arrived. A late trade of a finished minute no longer
  starts a forming bar for it again.
- **Another account file:** `--config FILE` reads the account from FILE instead of config.cfg (for
  example one file per account).
- **Check for updates:** at start (Settings, Gateway) and with Check for updates in Settings, About; a
  newer version shows as a line under the account bar, with the download page a click away.
- **CTRL-C in the terminal:** stops the gateway as Quit does, without an error message (a second CTRL-C
  closes the window at once).
- **About in Settings:** the version, the update check and the licenses are a tab of Settings now. The
  licenses of the software the gateway ships with are in THIRD-PARTY-LICENSES.txt (Open the licenses);
  before, those of Dear ImGui, GLFW and two fonts were missing.
- **A bolder icon:** easier to see in the title bar and the taskbar.

## 0.1.0

The first release.

- **One place for your Alpaca API key:** trading programs connect to the gateway on your computer
  instead of to Alpaca, and need no keys of their own.
- **Paper and live accounts:** the keys come from config.cfg or are entered at start and kept in
  memory only; a live account needs a deliberate extra step.
- **Orders:** bracket orders, single orders, one-cancels-other exits, changing and cancelling
  orders, closing positions. Every order is checked before it goes to Alpaca.
- **Tracking:** orders placed elsewhere (Alpaca's website, other programs) are followed too; account
  and positions reach the programs whenever they change.
- **Market data:** historical bars, live one-minute bars and quotes from IEX (free), SIP or delayed
  SIP.
- **Window:** the account, the status of every connection, the connected programs and a log;
  settings for the data feed, the port and local-only connections.
- **Windows and Linux:** one folder that runs as it is, nothing to install.
