# GitHub mirror

The repository's home is the self-hosted server (origin). GitHub gets a read-only copy, so people can find,
read, star and link the gateway. Nothing is online yet (prepared 2026-11-20).

- `tools/push_github_mirror.py URL [--dry-run]`: pushes main and the tags without personal names (below), refuses a
  non-GitHub URL and a main that differs from origin/main.
- `.github/workflows/close-prs.yml`: closes pull requests and issues with a pointer to the feedback form. It only
  reaches GitHub once it is committed (it travels with main).

## Author name in the history

Origin's commits carry the author's name and email, and older versions of `LICENSE` and `alpacagw/gateway/about.py`
the name in the copyright line. The mirror is rewritten on a temporary clone (origin never is): every author,
committer and tagger becomes `Alpaca Gateway <noreply@brackfast.io>`, and the authors' names, read from the history,
are replaced in every version of every file (full name -> "The Alpaca Gateway authors", first name -> "NAME"). No
name is written in the repository for this. Checked 2026-11-20 on a local bare repo: 26 commits and tag v0.1.0, no
name or email left in files, messages or identities. The mirror's commit ids differ from origin's but stay the same
on every push. `--replace-text FILE` adds replacements; `--keep-authors` pushes the history unchanged.

## README note (for the top of README.md)

> **Read-only mirror.** This GitHub repository mirrors the Alpaca Gateway's own repository. Pull requests and
> issues are not handled here: send bugs, questions and patches through the
> [feedback form](https://brackfast.io/#feedback).

## Going online

1. Commit the README note and `.github/workflows/close-prs.yml` (and this file, or leave it local); push to origin.
2. On GitHub, create the empty public repository `alpaca-gateway` (no README, license or .gitignore from GitHub).
   - Description: "Local gateway between trading apps and Alpaca: holds your API keys, speaks a small msgpack-over-WebSocket protocol."
   - Website: https://brackfast.io
   - Topics: alpaca, trading, websocket, msgpack, gateway, python
3. Settings → General: Issues, Wiki, Projects and Discussions off. Pull requests cannot be switched off on every
   plan; the workflow closes them.
4. Settings → Actions → General: allow actions; workflow permissions "Read and write" (or keep the per-workflow
   `permissions:` block, which the workflow has).
5. `uv run --no-project tools/push_github_mirror.py git@github.com:OWNER/alpaca-gateway.git --dry-run`, check, then
   the same without `--dry-run`.
6. Check on GitHub: main and v0.1.0 there, the authors as intended, the README note at the top, LICENSE detected
   as MIT. Open a test pull request from a second account (or a fork) and see it closed with the message.
7. After every release: push to origin, then run step 5 again.
