# GitHub mirror

The repository's home is the self-hosted server (origin). GitHub has a read-only copy,
https://github.com/brackfast/alpaca-gateway (online since 2026-11-21), so people can find, read, star and link the
gateway. Nothing is committed on GitHub; it only follows origin.

- `tools/push_github_mirror.py URL [--dry-run]`: pushes main and the tags without personal names (below), refuses a
  non-GitHub URL and a main that differs from origin/main.
- `.github/workflows/close-prs.yml`: closes pull requests and issues with a pointer to the feedback form.
- The README's first lines say it is a read-only mirror.

## Updating the mirror

After pushing to origin (every release, or whenever GitHub should catch up):

```
uv run --no-project tools/push_github_mirror.py https://github.com/brackfast/alpaca-gateway.git
```

It signs in through git's credential helper. Here that is the Git Credential Manager; with the `gh` CLI signed in
instead, put `gh auth git-credential` in front for this one command:

```
GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=credential.https://github.com.helper GIT_CONFIG_VALUE_0= \
GIT_CONFIG_KEY_1=credential.https://github.com.helper GIT_CONFIG_VALUE_1='!gh auth git-credential' \
uv run --no-project tools/push_github_mirror.py https://github.com/brackfast/alpaca-gateway.git
```

## Author name in the history

Origin's commits carry the author's name and email, and older versions of `LICENSE` and `alpacagw/gateway/about.py`
the name in the copyright line. The mirror is rewritten on a temporary clone (origin never is): every author,
committer and tagger becomes `Alpaca Gateway <noreply@brackfast.io>`, and the authors' names, read from the history,
are replaced in every version of every file (full name -> "The Alpaca Gateway authors", first name -> "NAME"). No
name is written in the repository for this. The mirror's commit ids differ from origin's but stay the same on every
push, so no push needs force. `--replace-text FILE` adds replacements; `--keep-authors` pushes the history unchanged.
Checked 2026-11-21 on a fresh clone from GitHub: 33 commits, tags v0.1.0 to v0.3.0, no name or email in files,
messages or identities.

## The GitHub side

- Organisation `brackfast` (member list private), repository public; Issues, Wiki, Projects and Discussions off.
  Pull requests cannot be switched off; the workflow closes them.
- Description: "Local gateway between trading apps and Alpaca: holds your API keys, speaks a small msgpack-over-WebSocket protocol."
  Website https://brackfast.io; topics alpaca, trading, websocket, msgpack, gateway, python.
- Not yet checked: a pull request from another account closed by the workflow.
