"""Telemetry tests — JSONL output for visualization."""

import json
from pathlib import Path

import pytest

from backtester.telemetry import TelemetrySink
from active_backtester import simulate
from tests.conftest import make_candle_series


class TestTelemetrySink:
    @pytest.mark.unit
    def test_writes_jsonl_line(self, tmp_path: Path):
        p = tmp_path / "events.jsonl"
        t = TelemetrySink(path=str(p), run_id="run123", enabled=True)
        t.emit("run_start", 1700000000, payload={"x": 1})

        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["run_id"] == "run123"
        assert obj["event"] == "run_start"
        assert obj["ts"] == 1700000000
        assert obj["payload"]["x"] == 1


class TestSimulationTelemetry:
    @pytest.mark.integration
    def test_emits_candle_events_with_baseline_and_strategy(self, tmp_path: Path, pool_data):
        # Use an out-of-range fixed range so no positions are opened; we still emit per-candle telemetry.
        candles = make_candle_series(5, start_price=3000.0, price_delta=10.0)
        sink_path = tmp_path / "sim.jsonl"
        telemetry = TelemetrySink(path=str(sink_path), run_id="run456", enabled=True)

        positions, wallet, snapshots = simulate(
            candles,
            pool_data,
            "ETH",
            100000.0,
            conn=None,
            fixed_range=(4000.0, 5000.0),
            telemetry=telemetry,
            initial_eth=16.666667,
        )

        assert positions == []
        assert len(snapshots) == 5

        events = [json.loads(line) for line in sink_path.read_text(encoding="utf-8").splitlines()]
        event_types = [e["event"] for e in events]
        assert "run_start" in event_types
        assert "run_end" in event_types
        assert event_types.count("candle") == 5

        candle_events = [e for e in events if e["event"] == "candle"]
        for e in candle_events:
            payload = e["payload"]
            assert "baseline_hodl_value_usd" in payload
            assert "strategy_total_value_usd" in payload
            assert payload["in_position"] is False

