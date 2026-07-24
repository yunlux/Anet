from __future__ import annotations

import asyncio
import json

from anet.experiments import monitor_probes, run_probe_series


class FakeProbeNode:
    def __init__(self) -> None:
        self.calls = 0

    async def probe(  # noqa: ANN201
        self,
        destination_id,  # noqa: ANN001
        *,
        timeout,  # noqa: ANN001
        carrier_grace,  # noqa: ANN001
        payload_bytes,  # noqa: ANN001
        qos,  # noqa: ANN001
    ):
        del carrier_grace
        self.calls += 1
        ok = self.calls != 3
        return {
            "ok": ok,
            "packet_id": f"{self.calls:032x}",
            "peer_id": destination_id,
            "qos": qos,
            "payload_bytes": payload_bytes,
            "elapsed_ms": float(self.calls * 10),
            "delivery_paths": (
                [{"path_id": "direct", "state": "acked"}]
                if ok
                else [{"path_id": "direct", "state": "sent"}]
            ),
            "route_after": {"selected_path": "direct" if self.calls < 3 else "webdav:dav"},
        }


def test_probe_series_writes_observations_and_summary(tmp_path) -> None:
    output = tmp_path / "experiment.jsonl"
    result = asyncio.run(
        run_probe_series(
            FakeProbeNode(),  # type: ignore[arg-type]
            "peer",
            count=4,
            spacing=0,
            output_path=output,
        )
    )
    summary = result["summary"]
    assert summary["successes"] == 3
    assert summary["failures"] == 1
    assert summary["success_rate"] == 0.75
    assert summary["latency_ms"] == {
        "min": 10.0,
        "mean": 23.333,
        "p50": 20.0,
        "p95": 38.0,
        "max": 40.0,
    }
    assert summary["acked_path_counts"] == {"direct": 3}
    assert summary["route_transitions"] == 1
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5
    assert lines[0]["type"] == "anet.probe.observation.v1"
    assert lines[-1]["type"] == "anet.probe.summary.v1"


def test_monitor_flushes_each_observation_and_stops_at_limit(tmp_path) -> None:
    output = tmp_path / "monitor.jsonl"
    summary = asyncio.run(
        monitor_probes(
            FakeProbeNode(),  # type: ignore[arg-type]
            "peer",
            output_path=output,
            stop_event=asyncio.Event(),
            interval=0.1,
            jitter=0,
            max_observations=3,
        )
    )
    assert summary["observations"] == 3
    assert summary["successes"] == 2
    assert summary["failures"] == 1
    assert summary["acked_path_counts"] == {"direct": 2}
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == [
        "anet.monitor.observation.v1",
        "anet.monitor.observation.v1",
        "anet.monitor.observation.v1",
        "anet.monitor.summary.v1",
    ]
