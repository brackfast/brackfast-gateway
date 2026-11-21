# Building and releasing the Alpaca Gateway

```bash
uv run --no-project tools/build_dist.py             # Windows and Linux; --windows / --linux for one
uv run --no-project tools/build_dist.py --dir       # Windows as the folder dist/alpaca-gateway (to try it)
```

The output is `dist/alpaca-gateway-<version>-<date>-windows-x86_64.zip` and
`dist/alpaca-gateway-<version>-<date>-linux-x86_64.tar.xz`, about 29 MB each. Each holds one folder
`alpaca-gateway/` that runs as it is, with nothing to install:

- **The launcher:** `Alpaca Gateway.cmd` on Windows, `alpaca-gateway` on Linux.
- **`alpacagw/`:** the gateway as Python source. It is not compiled.
- **`python/`:** a standalone Python 3.13 from python-build-standalone, the build uv uses. The
  libraries from `uv.lock` are in its site-packages.
- **`README.txt`:** [USER-README.md](USER-README.md), the guide for users (starting, the account,
  settings, options), and `LICENSE`. README.md, PROTOCOL.md and this file are for developers and stay out.

Both archives can be built on either system:

- **Python:** comes from its release archive and is checked against a pinned SHA-256.
- **Libraries:** uv installs their wheels for the target platform.

The downloads are kept in `build/cache`.

The version is the git tag `vX.Y.Z`, nothing in the files holds it. Release with
`git tag -a v0.2.0 -m "Alpaca Gateway 0.2.0"` and `git push origin main v0.2.0`, then build. When uv
installs the gateway, hatch-vcs writes `alpacagw/_version.py` (not in git), which `alpacagw.__version__` reads;
the build reinstalls it first, so the archives and the program always show the current version. A commit
after the tag gives `X.Y.(Z+1).devN+g<commit>` (and a warning from the build), uncommitted changes add
`.d<date>`.

**Changes per version:** [CHANGELOG.md](CHANGELOG.md) has a section `## X.Y.Z` for every release, written for
users. Add it before tagging. A release build writes it to `dist/alpaca-gateway-<version>-changes.md` next to
the archives, for the website, and stops if the section is missing. A development build (`0.1.1.dev8+g...`) writes `## Next`, the changes since the last release, if there is such a section.

**After a release:** update the GitHub mirror ([MIRROR.md](MIRROR.md)).

On Linux the Python needs glibc 2.28 or newer. The Linux build was checked headless in WSL (Ubuntu 22.04).
`config.cfg` and `alpacagw.log` live in the program's folder.
