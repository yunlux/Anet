#!/usr/bin/env python3
"""Launch a checkout-free one-click Anet deployment on POSIX platforms.

The downloaded files are kept in a temporary directory for the duration of
the installer.  The platform entry point remains responsible for all runtime,
node-home, control-page, duplicate-preflight, and service-manager behavior.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_REPOSITORY = "https://github.com/yunlux/Anet"
DEFAULT_SCRIPT_REF = "main"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
SCRIPT_FILES = {
    "wsl": "install_wsl_oneclick.py",
    "linux": "install_linux_oneclick.py",
    "macos": "install_macos_oneclick.py",
    "termux": "install_termux_oneclick.py",
}
MODULE_FILES = (
    "install_preflight.py",
    "posix_oneclick.py",
    "posix_runtime_installer.py",
)


class BootstrapError(RuntimeError):
    """Raised when the checkout-free bootstrap cannot prepare its entry point."""


def detect_platform() -> str:
    """Return the platform selector understood by the POSIX installers."""

    if sys.platform == "darwin":
        return "macos"
    if sys.platform != "linux":
        raise BootstrapError(f"unsupported POSIX platform: {sys.platform}")
    prefix = os.environ.get("PREFIX", "")
    if os.environ.get("TERMUX_VERSION") or prefix.startswith(
        "/data/data/com.termux/"
    ):
        return "termux"
    if os.environ.get("WSL_INTEROP"):
        return "wsl"
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        release = ""
    return "wsl" if "microsoft" in release.casefold() else "linux"


def normalize_script_ref(value: str) -> str:
    """Validate the Git ref used to fetch the bootstrap helper files."""

    reference = str(value).strip()
    if (
        not reference
        or not all(
            character.isalnum() or character in "._/-" for character in reference
        )
        or reference.startswith(("/", "."))
        or ".." in reference
        or "//" in reference
        or reference.endswith((".", "/"))
    ):
        raise BootstrapError("script ref contains an invalid Git reference")
    return reference


def github_raw_url(repository: str, script_ref: str, filename: str) -> str:
    """Build a raw GitHub URL for one known repository script."""

    parsed = urllib.parse.urlparse(str(repository).strip())
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise BootstrapError(
            "checkout-free bootstrap requires an HTTPS GitHub repository URL"
        )
    if parsed.query or parsed.fragment:
        raise BootstrapError("repository URL must not contain a query or fragment")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise BootstrapError(
            "GitHub repository URL must contain exactly an owner and repository"
        )
    repository_name = parts[1]
    if repository_name.endswith(".git"):
        repository_name = repository_name[:-4]
    if not repository_name or any(
        not part or part in {".", ".."} for part in (parts[0], repository_name)
    ):
        raise BootstrapError("GitHub repository URL is invalid")
    return (
        "https://raw.githubusercontent.com/"
        f"{urllib.parse.quote(parts[0], safe='')}/"
        f"{urllib.parse.quote(repository_name, safe='')}/"
        f"{urllib.parse.quote(script_ref, safe='/._-')}/scripts/"
        f"{urllib.parse.quote(filename, safe='')}"
    )


def download_source(url: str, destination: Path) -> None:
    """Download one small Python helper into the temporary bootstrap root."""

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain",
            "User-Agent": "Anet-POSIX-Bootstrap/0.12.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
    except Exception as exc:
        raise BootstrapError(f"failed to download bootstrap source: {url}") from exc
    if len(data) > MAX_SOURCE_BYTES:
        raise BootstrapError(f"bootstrap source exceeds the 4 MiB limit: {url}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapError(f"bootstrap source is not UTF-8: {url}") from exc
    destination.write_bytes(data)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Fetch and run Anet's platform one-click installer without a checkout."
        )
    )
    result.add_argument(
        "--platform",
        choices=("auto", "wsl", "linux", "macos", "termux"),
        default="auto",
        help="target platform; auto detects WSL, Linux, macOS, or Termux",
    )
    result.add_argument(
        "--repository",
        default=None,
        help=(
            "explicit HTTPS GitHub repository containing bootstrap scripts "
            "(default: official Anet repository; the control page is not used "
            "to select bootstrap code)"
        ),
    )
    result.add_argument(
        "--script-ref",
        default=None,
        help="Git branch, tag, or commit used for the downloaded scripts (default: main)",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    bootstrap_args, installer_args = parser().parse_known_args(argv)
    platform_name = (
        detect_platform() if bootstrap_args.platform == "auto" else bootstrap_args.platform
    )
    # The control page is untrusted input until the installed CLI performs
    # control-verify. It may choose the runtime/software source, but it must
    # not choose Python source that this bootstrap executes before verification.
    repository = str(bootstrap_args.repository or DEFAULT_REPOSITORY).strip()
    script_ref = normalize_script_ref(
        str(bootstrap_args.script_ref or DEFAULT_SCRIPT_REF).strip()
    )
    entry_name = SCRIPT_FILES[platform_name]

    with tempfile.TemporaryDirectory(prefix="anet-posix-bootstrap-") as temporary:
        root = Path(temporary)
        for filename in (*MODULE_FILES, entry_name):
            download_source(
                github_raw_url(repository, script_ref, filename),
                root / filename,
            )
        sys.path.insert(0, str(root))
        original_argv = sys.argv
        sys.argv = [str(root / entry_name), *installer_args]
        try:
            runpy.run_path(str(root / entry_name), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            print(code, file=sys.stderr)
            return 1
        finally:
            sys.argv = original_argv
            sys.path.remove(str(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
