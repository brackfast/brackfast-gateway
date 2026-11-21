"""Starting the gateway: the account file (config.cfg or --config FILE)."""
from alpacagw.gateway import app


def test_config_file_given_must_exist(tmp_path, capsys):
	assert app.main(["--config", str(tmp_path / "typo.cfg"), "--headless"]) == 2
	assert "not found" in capsys.readouterr().err
	assert not (tmp_path / "typo.cfg").exists()                 # no template written for it


def test_config_file_given_is_read(tmp_path, capsys):
	cfg = tmp_path / "live.cfg"
	cfg.write_text("[alpaca]\nenvironment = live\nkey_id = AK\nsecret_key = SK\n", encoding="utf-8")
	assert app.main(["--config", str(cfg), "--headless"]) == 2     # its live account needs --live: read from it
	assert f"{cfg} is a live account" in capsys.readouterr().err


def test_config_file_name_found_in_the_gateways_folder(tmp_path, monkeypatch, capsys):
	(tmp_path / "second.cfg").write_text("[alpaca]\nenvironment = live\nkey_id = AK\nsecret_key = SK\n", encoding="utf-8")
	monkeypatch.setattr(app, "program_folder", lambda: tmp_path)
	monkeypatch.chdir(tmp_path.parent)                           # started elsewhere (a shortcut's start folder)
	assert app.main(["--config", "second.cfg", "--headless"]) == 2
	assert f"{tmp_path / 'second.cfg'} is a live account" in capsys.readouterr().err


def test_log_has_no_key_secret_or_account(tmp_path):
	"""The key pair and the account number are taken out of every log line (the log is passed on)."""
	import logging
	from alpacagw.client import Credentials
	from alpacagw.gateway import redact
	assert "PKSECRETKEYID123" not in repr(Credentials("PKSECRETKEYID123", "s3cr3t-v4lu3-x"))
	lines = []
	handler = logging.Handler()
	handler.emit = lambda record: lines.append(handler.format(record))
	redact.install(handler)
	log = logging.getLogger("test_redact")
	log.addHandler(handler)
	try:
		redact.credentials("PKSECRETKEYID123", "s3cr3t-v4lu3-x")
		redact.account("PA3QXYZ12345")
		log.warning("auth %s %s for %s", "PKSECRETKEYID123", "s3cr3t-v4lu3-x", "PA3QXYZ12345")
	finally:
		log.removeHandler(handler)
	assert lines == ["auth <key id> <secret> for PA…345"]


def test_third_party_licenses_have_the_texts():
	"""THIRD-PARTY-LICENSES.txt (the build writes it, the About tab opens it): Dear ImGui and GLFW have no files of
	their own in site-packages, so their texts must be in it; the packages' come from their metadata."""
	from alpacagw.gateway import about
	text = about.third_party_text()
	for needed in ("Omar Cornut", "Camilla Löwy", "Permission is hereby granted", "aiohttp", "msgspec", "Pascal Thomet"):
		assert needed in text, needed
	assert "NAME" not in text and "The Alpaca Gateway authors" in text


def test_settings_across_versions(tmp_path):
	"""A newer gateway's settings file read by this one: what it does not know is kept, a value that does not fit
	costs only that setting, saving one environment keeps the other (before: one bad value reset both)."""
	import msgspec
	from alpacagw.gateway import settings
	file = tmp_path / "settings.json"
	file.write_bytes(msgspec.json.encode({
		"paper": {"port": 4200, "quote_interval_ms": "fast", "future_option": [1, 2], "local_only": False},
		"live": {"port": 4300, "stock_feed": "sip"}, "future_section": {"x": 1}}))
	paper = settings.load("paper", file)
	assert (paper.port, paper.local_only, paper.quote_interval_ms) == (4200, False, 100)     # the bad one: default
	paper.bar_interval_ms = 500
	settings.save("paper", paper, file)
	saved = msgspec.json.decode(file.read_bytes())
	assert saved["paper"]["future_option"] == [1, 2] and saved["paper"]["bar_interval_ms"] == 500
	assert saved["live"] == {"port": 4300, "stock_feed": "sip"} and saved["future_section"] == {"x": 1}
	assert settings.load("live", file).stock_feed == "sip"
	file.write_text("{ broken", encoding="utf-8")
	assert settings.load("paper", file) == settings.GatewaySettings()
	assert (tmp_path / "settings.unreadable.json").read_text(encoding="utf-8") == "{ broken"
