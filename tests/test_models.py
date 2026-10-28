from decimal import Decimal

import msgspec
import pytest

from alpacagw.client import (
	BarsParams, Credentials, OrderRequest, ReplaceOrderRequest, TradeUpdateMsg, TradingMessage, load_config,
)
from alpacagw.client.http import encode_params
from alpacagw.client.models import Order


def test_bracket_request_body():
	req = OrderRequest.bracket("AAPL", "buy", 10, take_profit=201.5, stop_loss=195.1, limit_price=198.2,
		client_order_id="c1")
	body = msgspec.json.decode(msgspec.json.encode(req))
	assert body == {
		"symbol": "AAPL", "side": "buy", "type": "limit", "time_in_force": "gtc", "qty": "10",
		"limit_price": "198.2", "client_order_id": "c1", "order_class": "bracket",
		"take_profit": {"limit_price": "201.5"}, "stop_loss": {"stop_price": "195.1"},
	}


def test_market_needs_qty_or_notional():
	with pytest.raises(ValueError):
		OrderRequest.market("AAPL", "buy")
	assert msgspec.json.decode(msgspec.json.encode(ReplaceOrderRequest(limit_price=Decimal("1.5")))) == {"limit_price": "1.5"}


def test_encode_params():
	params = BarsParams(symbols=["AAPL", "MSFT"], timeframe="1Min", limit=1000)
	assert encode_params(params) == {"symbols": "AAPL,MSFT", "timeframe": "1Min", "limit": "1000", "adjustment": "split"}
	assert encode_params({"nested": True, "x": None, "empty": []}) == {"nested": "true"}


ORDER = {
	"id": "e1", "client_order_id": "c1", "symbol": "AAPL", "status": "filled", "side": "buy", "type": "limit",
	"time_in_force": "gtc", "created_at": "2026-10-28T14:30:00.123456Z", "qty": "10", "filled_qty": "10",
	"filled_avg_price": "198.19", "limit_price": "198.2", "order_class": "bracket", "some_new_field": 1,
	"legs": [
		{"id": "tp1", "client_order_id": "c1-tp", "symbol": "AAPL", "status": "new", "side": "sell",
			"type": "limit", "time_in_force": "gtc", "created_at": "2026-10-28T14:30:00Z", "limit_price": "201.5"},
		{"id": "sl1", "client_order_id": "c1-sl", "symbol": "AAPL", "status": "held", "side": "sell",
			"type": "stop", "time_in_force": "gtc", "created_at": "2026-10-28T14:30:00Z", "stop_price": "195.1"},
	],
}


def test_order_decodes_with_legs_and_unknown_fields():
	order = msgspec.json.decode(msgspec.json.encode(ORDER), type=Order)
	assert order.filled_avg_price == Decimal("198.19")
	assert not order.is_open
	assert [leg.id for leg in order.legs] == ["tp1", "sl1"]
	assert order.legs[1].is_open


def test_trade_update_message():
	raw = msgspec.json.encode({"stream": "trade_updates", "data": {
		"event": "fill", "order": ORDER, "price": "198.19", "qty": "10", "position_qty": "10",
		"timestamp": "2026-10-28T14:30:01Z"}})
	msg = msgspec.json.decode(raw, type=TradingMessage)
	assert isinstance(msg, TradeUpdateMsg)
	assert msg.data.symbol == "AAPL" and msg.data.qty == Decimal(10)


def test_config(tmp_path):
	path = tmp_path / "paper.cfg"
	path.write_text("[alpaca]\nenvironment = paper\nkey_id = PK1\nsecret_key = s3cret\n")
	config = load_config(path)
	assert config.environment == "paper" and not config.is_live
	assert config.stock_feed == "iex"
	assert "s3cret" not in repr(config.credentials)

	path.write_text("[alpaca]\nenvironment = paper\nkey_id = <your key id>\nsecret_key = x\n")
	with pytest.raises(ValueError, match="required"):
		load_config(path)
	path.write_text("[alpaca]\nkey_id = a\nsecret_key = b\n")
	with pytest.raises(ValueError, match="environment"):
		load_config(path)
	assert Credentials("a", "b").headers() == {"APCA-API-KEY-ID": "a", "APCA-API-SECRET-KEY": "b"}


def test_config_without_credentials(tmp_path):
	from alpacagw.gateway.app import TEMPLATE
	path = tmp_path / "config.cfg"
	path.write_text(TEMPLATE)
	config = load_config(path, require_credentials=False)
	assert config.environment == "paper" and config.credentials is None
	with pytest.raises(ValueError, match="required"):
		load_config(path)


def test_gateway_settings(tmp_path):
	from alpacagw.gateway import settings
	file = tmp_path / "settings.json"
	assert settings.load("paper", file).port_for("paper") == 4102
	settings.save("paper", settings.GatewaySettings(stock_feed="sip", port=4555, local_only=False), file)
	settings.save("live", settings.GatewaySettings(), file)
	s = settings.load("paper", file)
	assert (s.stock_feed, s.port_for("paper"), s.host) == ("sip", 4555, "0.0.0.0")
	assert settings.load("live", file).port_for("live") == 4101 and settings.load("live", file).host == "127.0.0.1"
