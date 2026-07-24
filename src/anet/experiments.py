from __future__ import annotations

import asyncio
import json
import math
import os
import random
import statistics
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from .node import AnetNode
from .packet import now_ms


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower)


def _acked_paths(result: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item["path_id"])
            for item in result.get("delivery_paths", [])
            if item.get("state") == "acked"
        }
    )


async def run_probe_series(
    node: AnetNode,
    destination_id: str,
    *,
    count: int = 10,
    timeout: float = 15.0,
    spacing: float = 1.0,
    carrier_grace: float = 3.0,
    payload_bytes: int = 0,
    qos: str = "control",
    output_path: Path | None = None,
) -> dict[str, Any]:
    count = max(1, min(int(count), 100_000))
    spacing = max(0.0, min(float(spacing), 3600.0))
    started_ms = now_ms()
    started_monotonic = time.perf_counter()
    results: list[dict[str, Any]] = []
    output = Path(output_path).expanduser().resolve() if output_path else None
    handle = None
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = output.open("a", encoding="utf-8", newline="\n")
    try:
        for sequence in range(1, count + 1):
            result = await node.probe(
                destination_id,
                timeout=timeout,
                carrier_grace=carrier_grace,
                payload_bytes=payload_bytes,
                qos=qos,
            )
            observation = {
                "type": "anet.probe.observation.v1",
                "series_started_ms": started_ms,
                "sequence": sequence,
                "observed_ms": now_ms(),
                **result,
                "acked_paths": _acked_paths(result),
            }
            results.append(observation)
            if handle is not None:
                handle.write(json.dumps(observation, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if sequence < count and spacing:
                await asyncio.sleep(spacing)
    finally:
        if handle is not None:
            handle.close()

    successes = [item for item in results if item["ok"]]
    elapsed = [float(item["elapsed_ms"]) for item in successes]
    path_counts: Counter[str] = Counter()
    for item in results:
        for path in item["acked_paths"]:
            path_counts[path] += 1
    transitions = 0
    previous = None
    for item in results:
        route = (item.get("route_after") or {}).get("selected_path")
        if previous is not None and route != previous:
            transitions += 1
        previous = route
    summary = {
        "type": "anet.probe.summary.v1",
        "started_ms": started_ms,
        "finished_ms": now_ms(),
        "duration_ms": round((time.perf_counter() - started_monotonic) * 1000.0, 3),
        "peer_id": destination_id,
        "qos": qos,
        "payload_bytes": max(0, min(int(payload_bytes), 4 * 1024 * 1024)),
        "count": count,
        "successes": len(successes),
        "failures": count - len(successes),
        "success_rate": len(successes) / count,
        "latency_ms": {
            "min": round(min(elapsed), 3) if elapsed else None,
            "mean": round(statistics.fmean(elapsed), 3) if elapsed else None,
            "p50": round(_percentile(elapsed, 0.50), 3) if elapsed else None,
            "p95": round(_percentile(elapsed, 0.95), 3) if elapsed else None,
            "max": round(max(elapsed), 3) if elapsed else None,
        },
        "acked_path_counts": dict(sorted(path_counts.items())),
        "route_transitions": transitions,
        "output": str(output) if output else "",
    }
    if output is not None:
        with output.open("a", encoding="utf-8", newline="\n") as summary_handle:
            summary_handle.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
            summary_handle.flush()
            os.fsync(summary_handle.fileno())
    return {"summary": summary, "observations": results}


async def monitor_probes(
    node: AnetNode,
    destination_id: str,
    *,
    output_path: Path,
    stop_event: asyncio.Event,
    interval: float = 60.0,
    jitter: float = 0.25,
    timeout: float = 20.0,
    carrier_grace: float = 3.0,
    payload_bytes: int = 0,
    qos: str = "control",
    max_observations: int = 0,
) -> dict[str, Any]:
    interval = max(0.1, min(float(interval), 86400.0))
    jitter = max(0.0, min(float(jitter), 0.95))
    max_observations = max(0, int(max_observations))
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists():
        output.touch(mode=0o600)
    else:
        with suppress(OSError):
            os.chmod(output, 0o600)
    started_ms = now_ms()
    sequence = 0
    successes = 0
    path_counts: Counter[str] = Counter()
    rng = random.SystemRandom()

    while not stop_event.is_set() and (not max_observations or sequence < max_observations):
        sequence += 1
        result = await node.probe(
            destination_id,
            timeout=timeout,
            carrier_grace=carrier_grace,
            payload_bytes=payload_bytes,
            qos=qos,
        )
        paths = _acked_paths(result)
        successes += int(bool(result["ok"]))
        path_counts.update(paths)
        observation = {
            "type": "anet.monitor.observation.v1",
            "monitor_started_ms": started_ms,
            "sequence": sequence,
            "observed_ms": now_ms(),
            **result,
            "acked_paths": paths,
        }
        with output.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(observation, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if max_observations and sequence >= max_observations:
            break
        delay = interval * (1.0 + rng.uniform(-jitter, jitter))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.1, delay))
        except asyncio.TimeoutError:
            pass

    summary = {
        "type": "anet.monitor.summary.v1",
        "started_ms": started_ms,
        "finished_ms": now_ms(),
        "peer_id": destination_id,
        "observations": sequence,
        "successes": successes,
        "failures": sequence - successes,
        "success_rate": successes / sequence if sequence else None,
        "acked_path_counts": dict(sorted(path_counts.items())),
        "output": str(output),
    }
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary
