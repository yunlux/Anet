from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapter import PlatformAdapter, load_adapter
from .signal import DirectorySignalSink


def amesh_outbound_dir(home: Path) -> Path:
    return Path(home) / "amesh-outbound"


def amesh_lock_path(home: Path) -> Path:
    return Path(home) / "amesh-serve.lock"


LOGGER = logging.getLogger("amesh.serve")


class ServeLock:
    """Home-exclusive lock so only one ``amesh serve`` owns a node home.

    The lock is an advisory OS file lock, held for the process lifetime and
    released automatically if the process exits. It prevents two supervisors
    from polling the same ledgers or writing the same outbound sink.
    """

    def __init__(self, home: Path) -> None:
        self.path = amesh_lock_path(home)
        self._descriptor: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise RuntimeError(
                f"another amesh serve already holds {self.path.name} in this home"
            ) from exc
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> ServeLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


async def serve(
    home: Path,
    *,
    names: list[str] | tuple[str, ...],
    stop: asyncio.Event,
) -> dict[str, Any]:
    """Host the configured adapters' background loops until ``stop`` is set.

    Each adapter runs its ``run()`` loop with a directory signal sink and
    best-effort relationship projection. Signals are written to
    ``<home>/amesh-outbound/``; the operator runs ``amesh social project`` to
    fold batches into the relationship book when automatic projection is not
    available. The home-exclusive ``ServeLock`` prevents a second supervisor
    from operating the same home concurrently.
    """
    lock = ServeLock(home)
    lock.acquire()
    try:
        return await _serve(home, names=names, stop=stop)
    finally:
        lock.release()


async def _serve(
    home: Path,
    *,
    names: list[str] | tuple[str, ...],
    stop: asyncio.Event,
) -> dict[str, Any]:
    sink = DirectorySignalSink(amesh_outbound_dir(home))
    adapters: list[PlatformAdapter] = []
    tasks: list[asyncio.Task[Any]] = []

    def queue_signal(destination_id: str, kind: str, body: Mapping[str, Any]) -> str:
        del destination_id, kind
        return sink.emit(dict(body))

    def project_event(
        adapter: PlatformAdapter,
    ) -> Callable[[Mapping[str, Any]], Any] | None:
        if not hasattr(adapter, "project_event"):
            return None

        def _project(event: Mapping[str, Any]) -> Any:
            try:
                return adapter.project_event(event)
            except Exception as exc:  # pragma: no cover - node-home dependent
                LOGGER.warning(
                    "relationship projection failed for %s: %s",
                    adapter.name,
                    type(exc).__name__,
                )
                return None

        return _project

    for name in names:
        adapter = load_adapter(home, name)
        if not adapter.configured:
            adapter.close()
            continue
        if not adapter.descriptor().get("enabled", True):
            adapter.close()
            continue
        adapters.append(adapter)
        callback = project_event(adapter)
        task = asyncio.create_task(
            adapter.run(stop, queue_signal, callback),
            name=f"amesh-{adapter.name}",
        )
        tasks.append(task)
        LOGGER.info("amesh hosting adapter %s", adapter.name)

    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in adapters:
            adapter.close()

    return {
        "hosted": [adapter.name for adapter in adapters],
        "signal_count": sink.count(),
    }
