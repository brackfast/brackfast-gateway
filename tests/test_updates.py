"""Check for updates: the newest gateway in the website's download index against the running one."""
import json

from alpacagw.gateway import updates

NAMES = ["brackfast-0.9.0-20261201-windows.zip", "alpaca-gateway-0.1.0-20261116-windows-x86_64.zip",
	"alpaca-gateway-0.2.0-20261201-windows-x86_64.zip", "alpaca-gateway-0.2.1-20261205-linux-x86_64.tar.xz",
	"alpaca-gateway-0.3.0b1-20261210-windows-x86_64.zip", "alpaca-gateway-0.2.0-changes.md"]


def test_version_order():
	keys = [updates.version_key(v) for v in ("0.2.0.dev3+g12ab", "0.2.0a1", "0.2.0b1", "0.2.0rc1", "0.2.0", "0.2.1")]
	assert keys == sorted(keys) and len(set(keys)) == 6
	assert updates.version_key("nonsense") is None


def test_newest_for_this_system():
	assert updates.newest(NAMES, system="windows-x86_64") == "0.2.0"                      # not Brackfast's, not Linux
	assert updates.newest(NAMES, system="windows-x86_64", prereleases=True) == "0.3.0b1"
	assert updates.newest(NAMES, system="linux-x86_64") == "0.2.1"
	assert updates.newest(NAMES, system="linux-aarch64") is None
	assert updates.SYSTEM.split("-")[-1] in ("x86_64", "aarch64", "arm64", "i386", "x86")    # named as the archives


def _check(current, names=NAMES, fail=False):
	def fetch(url):
		if fail:
			raise OSError("no route")
		return json.dumps({"files": [{"name": n} for n in names]}).encode()
	c = updates.UpdateCheck(current=current, fetch=fetch, system="windows-x86_64")
	c.run()                                                                               # what start's thread runs
	return c


def test_check_results():
	c = _check("0.1.0")
	assert (c.state, c.latest) == (updates.AVAILABLE, "0.2.0") and "0.2.0 is available" in c.text
	assert _check("0.2.0").state == updates.NEWEST
	assert _check("0.3.0a1").latest == "0.3.0b1"                                          # a pre-release: pre-releases too
	failed = _check("0.1.0", fail=True)
	assert failed.state == updates.FAILED and "not reached" in failed.text and failed.latest == ""


def test_the_website():
	assert updates.INDEX_URL == "https://brackfast.io/download/index.json"               # as Brackfast's check
	assert updates.DOWNLOAD_PAGE == "https://brackfast.io/download.html"
