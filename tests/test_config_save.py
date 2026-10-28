from alpacagw.client import load_config
from alpacagw.client.config import save_config

TEMPLATE = """; my paper account
[alpaca]
environment = paper        ; paper | live
key_id = <your key id>
secret_key = <your secret key>

[other]
x = 1
"""


def test_save_keeps_comments_and_other_sections(tmp_path):
	path = tmp_path / "paper.cfg"
	path.write_text(TEMPLATE)
	save_config(path, {"key_id": "PK1", "secret_key": "s3", "stock_feed": "sip"})
	text = path.read_text()
	assert text.startswith("; my paper account\n[alpaca]\nenvironment = paper        ; paper | live\n")
	assert "key_id = PK1\nsecret_key = s3\nstock_feed = sip\n\n[other]\nx = 1\n" in text
	config = load_config(path)
	assert (config.credentials.key_id, config.stock_feed) == ("PK1", "sip")

	save_config(path, {"environment": "live"})
	assert "environment = live    ; paper | live" in path.read_text()


def test_save_creates_the_file(tmp_path):
	path = tmp_path / "new.cfg"
	save_config(path, {"environment": "paper", "key_id": "a", "secret_key": "b"})
	assert load_config(path).environment == "paper"
