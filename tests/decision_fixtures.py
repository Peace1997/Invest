from __future__ import annotations

from datetime import date, datetime


def decision_signal(**overrides):
    signal = {
        "signal_id": "signal-1",
        "created_at": datetime(2026, 7, 13, 9, 28),
        "trade_date": date(2026, 7, 13),
        "source": "premarket",
        "style": None,
        "symbol": "000001",
        "name": "平安银行",
        "asset_type": "stock",
        "action": "短线关注",
        "score": None,
        "ref_price": 10.0,
        "horizon_days": 1,
        "factors": "{}",
        "rationale": "量价异动",
        "status": "active",
    }
    signal.update(overrides)
    return signal
