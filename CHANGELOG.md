# Changes

What each version brings, for users. The build writes the section of the version it builds to
`dist/alpaca-gateway-<version>-changes.md`, next to the archives (for the website). Add a section
`## X.Y.Z` before tagging `vX.Y.Z`; the newest comes first.

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
