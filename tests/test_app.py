"""Starting the gateway: the account file (config.cfg or --config FILE)."""
from brackfastgw.gateway import app


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
	from brackfastgw.alpaca import Credentials
	from brackfastgw.gateway import redact
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
	from brackfastgw.gateway import about
	text = about.third_party_text()
	for needed in ("Omar Cornut", "Camilla Löwy", "Permission is hereby granted", "aiohttp", "msgspec", "Pascal Thomet"):
		assert needed in text, needed
	assert "NAME" not in text and "The Brackfast Gateway authors" in text


def test_settings_across_versions(tmp_path):
	"""A newer gateway's settings file read by this one: what it does not know is kept, a value that does not fit
	costs only that setting, saving one environment keeps the other (before: one bad value reset both)."""
	import msgspec
	from brackfastgw.gateway import settings
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


def test_old_folder_taken_over_once(tmp_path):
	"""The Alpaca Gateway's settings folder: its files copied, the bar cache moved; only while ours does not exist."""
	from brackfastgw.gateway import settings
	old = tmp_path / "alpacagw"
	(old / "bar-cache").mkdir(parents=True)
	(old / "settings.json").write_text('{"paper": {"port": 4111}}', encoding="utf-8")
	(old / "simulated-account-paper.json").write_text("{}", encoding="utf-8")
	(old / "bar-cache" / "alpaca-iex.sqlite").write_bytes(b"x")
	new = tmp_path / "brackfastgw"
	assert settings.take_over_old_folder(new)
	assert settings.load("paper", new / "settings.json").port == 4111
	assert (new / "simulated-account-paper.json").is_file() and (old / "settings.json").is_file()   # copied
	assert (new / "bar-cache" / "alpaca-iex.sqlite").is_file() and not (old / "bar-cache").exists()  # moved
	(old / "settings.json").write_text('{"paper": {"port": 4999}}', encoding="utf-8")
	assert not settings.take_over_old_folder(new)                                                  # once only
	assert settings.load("paper", new / "settings.json").port == 4111
	assert not settings.take_over_old_folder(tmp_path / "other" / "brackfastgw")                  # nothing to take


def test_config_file_taken_from_the_alpaca_gateways_folder(tmp_path, monkeypatch, capsys):
	"""Unzipped next to the Alpaca Gateway: its config.cfg is copied (the keys need not be entered again)."""
	(tmp_path / "alpaca-gateway").mkdir()
	(tmp_path / "alpaca-gateway" / "config.cfg").write_text("[alpaca]\nenvironment = live\nkey_id = K\nsecret_key = S\n",
		encoding="utf-8")
	(tmp_path / "brackfast-gateway").mkdir()
	monkeypatch.setattr(app, "program_folder", lambda: tmp_path / "brackfast-gateway")
	assert app.main([]) == 2                       # a live account without --live: stops before anything starts
	assert (tmp_path / "brackfast-gateway" / "config.cfg").is_file()
	assert "copied" in capsys.readouterr().err
