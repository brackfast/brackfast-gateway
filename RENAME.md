# Rename: Alpaca Gateway -> Brackfast Gateway

Decided 2026-11-25. The gateway has several data sources (Alpaca, Yahoo Finance, more planned) and a simulated
account, so "Alpaca" no longer fits. Brackfast is the family name: Brackfast Gateway now; later the order app
becomes **Brackfast Orders** (today plain "Brackfast") and Chartfast **Brackfast Charts**.

So from now on, new texts say "Brackfast" only for the family; where the order app is meant, they name it so it can
become "Brackfast Orders" by one replace (e.g. the website's product list: "Brackfast Gateway" next to the order
app's entry, not "Brackfast" vs "the gateway"). The order app's own rename (title, archive `brackfast-orders-...`,
its update check and settings folder, the bridge release as here) is a separate step, after this one, and uses this
file's pattern.

"Alpaca" stays where it means Alpaca: the data source / broker (`AlpacaBroker`, `market.py`, `client/` = Alpaca's
API client, the `[alpaca]` section of config.cfg, the "Alpaca API key" field, Settings' data source "Alpaca").

## Status (2026-11-25)

- Step A done (13dab23 here, 5d7b649 Brackfast, d08dc13 Chartfast).
- Step B 1-4 done: the package `brackfastgw` (`client` -> `alpaca`), the settings folder taken over at start
  (`settings.take_over_old_folder`), config.cfg from a neighbouring `alpaca-gateway` folder, logs, AppUserModelID,
  `updates.PRODUCT`, `build_dist.py` (names, launchers, `--bridge`), the Download page (`RENAMED`), Brackfast's and
  Chartfast's connector `brackfast-gateway` with `gateway_host` / `gateway_port` (Brackfast settings version 6).
  Checked: tests of all three; a headless start with an old `alpacagw` folder took it over and listened on its port;
  Chartfast's gateway_check against it.
- Step B 5 done: authors / mirror identity "The Brackfast Gateway authors" (73c08ad), the repo renamed on the git
  server (joa/brackfast-gateway), the folder `../brackfast-gateway` (path sources, venvs), the GitHub repository
  renamed to brackfast/brackfast-gateway with the new description, its history pushed again with `--force`
  (checked on a fresh clone: identity "Brackfast Gateway", no personal name in files or messages).
- Open: the first release with `--bridge`; the website deployed.

## New names

| | Old | New |
|---|---|---|
| Product | Alpaca Gateway | Brackfast Gateway |
| Package / import | `alpacagw` | `brackfastgw` |
| Command | `alpacagw` | `brackfastgw` |
| Repo, folder | `joa/alpaca-gateway`, `../alpaca-gateway` | `joa/brackfast-gateway`, `../brackfast-gateway` |
| GitHub mirror | github.com/brackfast/alpaca-gateway | github.com/brackfast/brackfast-gateway (renamed: GitHub redirects the old URL) |
| Archives | `alpaca-gateway-<version>-<date>-<system>` | `brackfast-gateway-<version>-<date>-<system>` |
| Folder in the archive, launcher | `alpaca-gateway/`, `Alpaca Gateway.cmd` / `alpaca-gateway` | `brackfast-gateway/`, `Brackfast Gateway.cmd` / `brackfast-gateway` |
| Settings folder | `%APPDATA%\alpacagw` (`~/.config/alpacagw`) | `%APPDATA%\brackfastgw` (`~/.config/brackfastgw`) |
| Log files | `alpacagw-<environment>.log` | `brackfastgw-<environment>.log` |
| Windows AppUserModelID | `AlpacaGateway.AlpacaGateway` | `Brackfast.Gateway` |
| Copyright / mirror identity | The Alpaca Gateway authors | The Brackfast Gateway authors |
| Protocol | the gateway protocol (PROTOCOL.md) | the Brackfast Gateway protocol: the messages unchanged, only the title |
| Brackfast's connector | `alpaca-gateway`, "Alpaca Gateway" | `brackfast-gateway`, "Brackfast Gateway" |
| Brackfast's / Chartfast's settings | `alpaca_host`, `alpaca_port` | `gateway_host`, `gateway_port` |

Open question: `alpacagw.client` (Alpaca's REST / stream client) could become `brackfastgw.alpaca`, so that the
package says what it is next to `yahoo.py` and later sources. Recommended, in the same step as the package rename.

## Where the old name occurs (2026-11-25, `git grep -i 'alpacagw|alpaca[ -_]gateway'`)

### alpaca-gateway: 46 files

- **Package and imports:** `alpacagw/` everywhere (relative imports mostly; logger names `alpacagw.*`), `pyproject.toml`
  (name, description, script, version file, packages), `uv.lock`, `.gitignore`, `alpacagw/_version.py` (written).
- **Shown to users:** `gateway/window.py` (title bar, window title "Alpaca Gateway - paper", update note "Alpaca Gateway
  X is available"), `gateway/about.py` (title, COPYRIGHT), `gateway/app.py` (argparse prog / description, messages
  "alpacagw: ...", config.cfg template "; Alpaca Gateway: the account", the log name), `LICENSE` (copyright line).
- **System:** `gateway/settings.py` (`%APPDATA%\alpacagw`: settings.json, `bar-cache/`, the simulation files),
  `gateway/updates.py` (`PRODUCT = "alpaca-gateway"`), `gateway/window.py` (AppUserModelID).
- **Build / tools:** `tools/build_dist.py` (27: NAME, launchers, folder, titles, changes file), `tools/push_github_mirror.py`
  (URL, IDENTITY, AUTHORS, temp prefix), `tools/make_icon.py` (6), `assets/logo.svg`, `assets/logo-wordmark.svg`
  (the wordmark's text: to redraw), `gateway/icon_data.py` (generated).
- **Docs:** README.md (14), BUILDING.md (11), USER-README.md (10), MIRROR.md (6), PROTOCOL.md, CHANGELOG.md
  (old sections stay; a line in Next).
- **Tests / examples:** 12 test files (imports, mostly), `examples/paper_bracket.py`, `examples/demo.py`.

### brackfast: ~55 files

- **Code:** `alpacagw.protocol` imported in `app.py`, `bracket.py`, `chart.py`, `panel.py`, `series.py`, `session.py`,
  `connectors/alpaca_gateway.py`, `connectors/ib_gateway.py`, `tools/paper_test.py`, tests. `pyproject.toml`
  (dependency + path source `../alpaca-gateway`), `uv.lock`, `tools/build_exe.py` (`OWN`).
- **Connector:** `connectors/alpaca_gateway.py` -> `connectors/gateway.py` (kind `brackfast-gateway`), `app.py`
  `GATEWAYS`, `settings.py` (`connector` default, `alpaca_host` / `alpaca_port`: a settings version step that converts
  them and `"alpaca-gateway"`; the old names into `RETIRED`).
- **Comments only:** `look.py`, `titlebar.py`, `app.py`, `connectors/base.py` ("as the Alpaca Gateway's").
- **Tools:** `tools/paper_test.py` (`--alpaca URL` -> `--gateway URL`, `--alpaca` kept as an alias for a while),
  `tools/deploy.py` (old Linux archive names: history, stays).
- **Website:** `download.html` (12: the product card, the "Windows asks" text, the JS product list with id and GitHub
  URL), `index.html` (the scheme's label and description), `faq.md`, `privacy.html`, `download/index.json` (generated by
  `make_downloads.py`: needs to know the new product name too).
- **Docs:** CLAUDE.md (14), README.md, USER-README.md, BUILDING.md, TESTING.md, CHANGELOG.md (a line in Next).
- **Stays as it is:** `website/download/alpaca-gateway-*` (old releases and changes files), `marketing/video-18`,
  `marketing/video-21` (published videos and their captions), `marketing/replay/*` (comments; update if touched).

### chartfast: 8 files

- `pyproject.toml` (dependency + path source), `uv.lock`, `chartfast/data.py`, `chartfast/demo.py`, tests (imports).
- `chartfast/app.py` (`GATEWAYS`, `alpaca_host` / `alpaca_port`), `chartfast/store.py` (`connector` default:
  a settings step as in Brackfast), CLAUDE.md.
- Its own rename to Brackfast Charts is a separate step, later.

### Elsewhere

- Memory / CLAUDE.md of both projects (sibling description, paths).
- The local git server: rename the repo (joa/alpaca-gateway -> joa/brackfast-gateway), then `git remote set-url` in
  the checkout.
- The folder `C:\Users\joach\Documents\alpaca-gateway` -> `brackfast-gateway`: the path sources of Brackfast and
  Chartfast, `uv sync` in all three afterwards (the venvs keep absolute paths).

## What breaks for installed users, and the remedy

1. **Settings, simulated account, bar cache** live in `%APPDATA%\alpacagw`. At start, when `brackfastgw` does not
   exist and `alpacagw` does: copy settings.json and the simulation files, move `bar-cache/` (can be large; an old
   version would rebuild it). Logged once.
2. **config.cfg** is next to the program, in the old `alpaca-gateway/` folder. The new folder has none: the login
   dialog asks for the keys (as on a first start). The release notes and USER-README say: copy config.cfg over.
3. **Update check of old gateways** looks for `alpaca-gateway-*` in index.json. Bridge: the first Brackfast Gateway
   release is also published once as `alpaca-gateway-<version>-...` (the same archive, renamed and signed), so old
   gateways show "available"; its Download page then shows the Brackfast Gateway. After that, only the new name.
4. **Brackfast users:** the connector value and host / port settings are converted by the settings step; no action.
5. **Taskbar pins** of the old launcher point to `Alpaca Gateway.cmd` in the old folder: pin anew (release notes).

## Order of work

**Step A: what users see** (no installed thing breaks)
- Gateway: window / title bar / About / update note / argparse texts / config.cfg template, LICENSE + COPYRIGHT,
  README, USER-README, PROTOCOL.md title, CHANGELOG line, the wordmark.
- Brackfast + Chartfast: the connector's label "Brackfast Gateway"; website (download, index, faq, privacy); docs.

**Step B: system names** (all three repos in one go, then the server and the folder)
1. Package `alpacagw` -> `brackfastgw` (and `client` -> `alpaca`, if agreed), pyproject, imports, logger names, tests.
2. Settings folder with the migration (1. above), log names, AppUserModelID.
3. Updates: `PRODUCT`, `build_dist.py` (archive, folder, launchers), `make_downloads.py`, the bridge release.
4. Brackfast / Chartfast: dependency + path, connector module and kind, settings step, paper_test option.
5. Rename the repo on the git server, the folder, the GitHub mirror (rename on GitHub, then `push_github_mirror.py`
   with the new URL), MIRROR.md, both CLAUDE.md, memory.
6. Check: tests in all three; the built gateway starts with old settings in `%APPDATA%\alpacagw` and takes them over;
   Brackfast with old settings connects; an old gateway's update check finds the bridge release (test index via
   the update URL override).
