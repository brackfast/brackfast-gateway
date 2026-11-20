"""Push the Alpaca Gateway to its read-only GitHub mirror: main and the tags, nothing else.

	uv run --no-project tools/push_github_mirror.py git@github.com:OWNER/alpaca-gateway.git --dry-run
	uv run --no-project tools/push_github_mirror.py git@github.com:OWNER/alpaca-gateway.git

origin (the self-hosted server) stays the real repository; the mirror only follows it. The script refuses to run
without a GitHub URL, and when local main differs from origin/main (push to origin first, so the mirror never
gets ahead of it).

The mirror carries no personal name: on a temporary bare clone (origin's history is never touched), git filter-repo
(through uvx) makes every author, committer and tagger `IDENTITY`, and replaces the authors' names in every version
of every file (the full name, its spellings without umlauts -> `AUTHORS`, the first name alone -> `FIRST_NAME`).
The names are read from the history, so none is written in this repository. filter-repo gives the same commit ids
for the same input, so pushing again later needs no force.

	--replace-text FILE   more replacements (git filter-repo's expressions file)
	--keep-authors        push origin's history as it is, names included
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GITHUB_URL = re.compile(r"^(git@github\.com:|https://github\.com/)[\w.-]+/[\w.-]+?(\.git)?$")
REFSPECS = ["refs/heads/main:refs/heads/main", "refs/tags/*:refs/tags/*"]
IDENTITY = "Alpaca Gateway <noreply@brackfast.io>"
AUTHORS = "The Alpaca Gateway authors"
FIRST_NAME = "NAME"
UMLAUTS = [{"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"},
	{"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U", "ß": "ss"}]


def git(*args: str, cwd: Path = ROOT, capture: bool = True) -> str:
	result = subprocess.run(["git", *args], cwd=cwd, text=True, check=True, encoding="utf-8",
		stdout=subprocess.PIPE if capture else None)
	return (result.stdout or "").strip()


def check_main() -> None:
	"""Local main = origin/main, after a fetch: the mirror gets exactly what the server has."""
	git("fetch", "--quiet", "--tags", "origin")
	main, upstream = git("rev-parse", "main"), git("rev-parse", "origin/main")
	if main != upstream:
		sys.exit(f"main ({main[:9]}) differs from origin/main ({upstream[:9]}): push or pull first.")


def people(repo: Path) -> tuple[set[str], set[str]]:
	"""Every name and email of the commits' authors and committers and of the tags' taggers."""
	names = set(git("log", "--all", "--format=%an%n%cn", cwd=repo).splitlines())
	emails = set(git("log", "--all", "--format=%ae%n%ce", cwd=repo).split())
	for line in git("for-each-ref", "refs/tags", "--format=%(taggername)\t%(taggeremail)", cwd=repo).splitlines():
		name, _, email = line.partition("\t")
		names.add(name)
		emails.add(email.strip("<>"))
	return {n.strip() for n in names} - {""}, emails - {""}


def replacements(names: set[str]) -> list[str]:
	"""filter-repo expressions: full names (as written and without umlauts) first, then the first names."""
	full = {spelling for name in names if " " in name
		for spelling in [name, *(name.translate(str.maketrans(table)) for table in UMLAUTS)]}
	first = {name.split()[0] for name in names if " " in name}
	lines = [f"{n}==>{AUTHORS}" for n in sorted(full, key=len, reverse=True)]
	return lines + [f"regex:\\b{re.escape(n)}\\b==>{FIRST_NAME}" for n in sorted(first)]


def rewritten_clone(work: Path, extra: Path | None) -> Path:
	"""A bare clone of ROOT without personal names; ROOT and origin stay as they are."""
	clone = work / "mirror.git"
	git("clone", "--quiet", "--bare", "--no-local", str(ROOT), str(clone))
	names, emails = people(clone)
	mailmap, expressions = work / "mailmap", work / "replace.txt"
	mailmap.write_text("".join(f"{IDENTITY} <{e}>\n" for e in sorted(emails)), encoding="utf-8")
	text = replacements(names)
	if extra:
		text += extra.read_text(encoding="utf-8").splitlines()
	expressions.write_text("\n".join(text) + "\n", encoding="utf-8")
	subprocess.run(["uvx", "git-filter-repo", "--force", "--mailmap", str(mailmap), "--replace-text", str(expressions)],
		cwd=clone, check=True)
	left = [n for n in people(clone)[0] if n != IDENTITY.split(" <")[0]]
	if left:
		sys.exit(f"names left in the rewritten history: {left}")
	return clone


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("url", help="the GitHub repository, git@github.com:OWNER/NAME.git or https://github.com/OWNER/NAME")
	parser.add_argument("--dry-run", action="store_true", help="show what would be pushed, push nothing")
	parser.add_argument("--replace-text", type=Path, help="more replacements: a git filter-repo expressions file")
	parser.add_argument("--keep-authors", action="store_true", help="push the history as it is, names included")
	args = parser.parse_args()
	if not GITHUB_URL.match(args.url):
		sys.exit(f"not a GitHub repository URL: {args.url!r}")
	if args.replace_text and not args.replace_text.is_file():
		sys.exit(f"no such file: {args.replace_text}")
	if args.keep_authors and args.replace_text:
		sys.exit("--keep-authors pushes the history unchanged: no --replace-text with it")
	check_main()
	with tempfile.TemporaryDirectory(prefix="alpacagw-mirror-") as tmp:
		source = ROOT if args.keep_authors else rewritten_clone(Path(tmp), args.replace_text)
		push = ["push", "--porcelain", *(["--dry-run"] if args.dry_run else []), args.url, *REFSPECS]
		print("git", " ".join(push), f"(from {source})")
		git(*push, cwd=source, capture=False)


if __name__ == "__main__":
	main()
