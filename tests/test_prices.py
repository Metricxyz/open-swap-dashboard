from datetime import datetime, timezone

from dashboard.prices import Trade, _select_preceding_trades, _timestamp_from_number


def test_select_preceding_trade_never_uses_future_trade():
    trades = [
        Trade("a", datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc), 100.0),
        Trade("b", datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc), 105.0),
        Trade("c", datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc), 110.0),
    ]
    targets = [
        datetime(2026, 1, 1, 11, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 0, 7, tzinfo=timezone.utc),
    ]

    selected = _select_preceding_trades(trades, targets)

    assert selected[0] is None
    assert selected[1].trade_id == "b"
    assert selected[2].trade_id == "b"


def test_exchange_timestamp_units_normalize_to_same_instant():
    seconds = 1_767_268_800
    expected = datetime.fromtimestamp(seconds, timezone.utc)

    assert _timestamp_from_number(seconds) == expected
    assert _timestamp_from_number(seconds * 1_000) == expected
    assert _timestamp_from_number(seconds * 1_000_000) == expected

