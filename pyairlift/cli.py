"""pyairlift — command-line interface for shell / third-party scripting.

Third-party scripts and CI pipelines can drive AirLift without writing Python::

    # List what is connected (machine-readable):
    pyairlift devices --json

    # Validate an iPhone for AirLift:
    pyairlift check --udid 00008110-0000000000000000

    # Write one or more files into a device directory:
    pyairlift write /var/mobile/Library/Caches/TelephonyUI-10 en-1---white.png @3x.png

    # Push every file of a local directory (leaf = file name):
    pyairlift write-dir ./theme/ /var/mobile/Library/Caches/TelephonyUI-10 --json

Exit codes: 0 = success, 1 = the device refused / a step failed,
2 = usage error (argparse).  With ``--json`` only the JSON document is printed
to stdout; all progress/tracing goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

from pyairlift._version import __version__


class _CliError(Exception):
    """Local, dependency-free error for CLI-level resolution failures."""


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _error(message: str) -> int:
    print(f"pyairlift: error: {message}", file=sys.stderr)
    return 1


def _run(awaitable) -> Any:
    return asyncio.run(awaitable)


def _normalize_serial(serial: str) -> str:
    return serial.replace("-", "").lower()


async def _resolve_serial(udid: Optional[str], usbmux_address: Optional[str]) -> Optional[str]:
    """Turn a (possibly partial) ``--udid`` into the full usbmux serial."""
    if not udid:
        return None
    try:
        rows = await async_list_devices(usbmux_address)
    except Exception as exc:
        raise _CliError(f"cannot reach usbmuxd — is it running? ({type(exc).__name__}: {exc})") from exc
    wanted = _normalize_serial(udid)
    matches = [
        row["serial"]
        for row in rows
        if _normalize_serial(row["serial"]).startswith(wanted)
    ]
    if not matches:
        raise _CliError(f"no connected device matches '{udid}'")
    return matches[0]


async def async_list_devices(usbmux_address: Optional[str]) -> List[dict[str, Any]]:
    from pyairlift import list_devices

    return await list_devices(usbmux_address=usbmux_address)


def _write_errors() -> tuple:
    """pymobiledevice3 exceptions worth translating to friendly CLI text."""
    from pymobiledevice3.exceptions import (
        FatalPairingError,
        NotPairedError,
        PairingDialogResponsePendingError,
        PasswordRequiredError,
        UserDeniedPairingError,
    )

    return (
        NotPairedError,
        PasswordRequiredError,
        UserDeniedPairingError,
        PairingDialogResponsePendingError,
        FatalPairingError,
    )


def _translate(exc: BaseException) -> str:
    name = type(exc).__name__
    if "usbmux" in name.lower():
        return ("cannot reach usbmuxd — is it running? "
                f"({name}: {exc})")
    if name == "PasswordRequiredError":
        return ("the device is locked. Unlock your iPhone, then tap "
                "\u201cTrust This Computer\u201d when the dialog appears and try again.")
    if type(exc).__name__ in ("UserDeniedPairingError", "FatalPairingError"):
        return ("the trust request was declined on the device; tap "
                "\u201cTrust This Computer\u201d and try again.")
    if type(exc).__name__ in ("PairingDialogResponsePendingError", "NotPairedError"):
        return ("this computer is not trusted by the iPhone. Unlock the device, "
                "tap \u201cTrust This Computer\u201d when iOS asks, then try again.")
    return f"{name}: {exc}"


# --------------------------------------------------------------------------- #
# devices

def _add_session_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--udid", "--serial", dest="udid", default=None,
                        help="usbmux serial of the iPhone (default: first available)")
    parser.add_argument("--connection-type", choices=("USB", "Network"), default=None,
                        help="limit to USB or Wi-Fi/Network devices")
    parser.add_argument("--usbmux-address", default=None,
                        help="usbmuxd socket (e.g. HOST:PORT for a remote usbmuxd)")
    parser.add_argument("--pair-timeout", type=float, default=120.0, metavar="SECONDS",
                        help="seconds to wait for the on-device trust dialog (default: 120)")
    parser.add_argument("--no-autopair", dest="autopair", action="store_false", default=True,
                        help="never start a pairing session (avoids the trust dialog)")


async def _cmd_devices(args) -> int:
    from pyairlift import AirliftError, device_info, list_devices

    try:
        rows = await list_devices(args.usbmux_address)
    except Exception as exc:
        hint = " is it running?" if "usbmux" in type(exc).__name__.lower() else ""
        return _error(f"cannot list devices{hint}: {_translate(exc)}")

    if args.udid:
        wanted = _normalize_serial(args.udid)
        matched = [row for row in rows
                   if _normalize_serial(row["serial"]).startswith(wanted)]
        if not matched:
            print(f"ERROR: device {args.udid} not found.", file=sys.stderr)
            return 1
        rows = matched

    if args.details:
        detailed = []
        for row in rows:
            try:
                detailed.append(await device_info(
                    row["serial"], usbmux_address=args.usbmux_address,
                    connection_type=None))
            except AirliftError as exc:
                row = dict(row)
                row["error"] = str(exc)
                detailed.append(row)
        rows = detailed

    if args.json:
        _print_json({"command": "devices", "devices": rows})
    elif not rows:
        print("No devices connected.")
    else:
        for row in rows:
            parts = [row["serial"]]
            if row.get("name"):
                parts.append(row["name"])
            if row.get("ios_version"):
                parts.append(f"iOS {row['ios_version']} ({row.get('ios_build') or '?'})")
            if row.get("error"):
                parts.append(f"[error: {row['error']}]")
            print("  ".join(str(part) for part in parts))
    return 0


# --------------------------------------------------------------------------- #
# check

async def _cmd_check(args) -> int:
    from pyairlift import AirliftError, TESTED_BUILDS, check_target, device_session

    try:
        serial = await _resolve_serial(args.udid, args.usbmux_address)
    except _CliError as exc:
        return _error(str(exc))
    try:
        async with device_session(
            serial,
            autopair=args.autopair,
            pair_timeout=args.pair_timeout,
            connection_type=args.connection_type,
            usbmux_address=args.usbmux_address,
        ) as lockdown:
            info = check_target(lockdown, log_cb=lambda message: print(message, file=sys.stderr))
    except (AirliftError, OSError, TimeoutError, ConnectionError) as exc:
        return _error(_translate(exc))
    except _write_errors() as exc:
        return _error(_translate(exc))

    if args.json:
        _print_json({"command": "check", "ok": True, **info})
    else:
        verdict = ("supported (tested build)" if info["tested"]
                   else "UNVERIFIED BUILD — proceed at your own risk")
        print(f"{info['product']} on iOS {info['version']} ({info['build']})")
        print(f"AirLift: {verdict}")
        if not info["tested"]:
            tested = "; ".join(f"{v} {b}" for v, b in sorted(TESTED_BUILDS))
            print(f"  tested builds: {tested or 'none'}")
    return 0


# --------------------------------------------------------------------------- #
# write / write-dir

async def _cmd_write(args) -> int:
    from pyairlift import write_files_to_device

    files: list[tuple[str, bytes]] = []
    for path in args.files:
        try:
            payload = Path(path).read_bytes()
        except OSError as exc:
            return _error(f"cannot read '{path}': {exc}")
        leaf = Path(path).name
        if not payload:
            print(f"pyairlift: warning: '{path}' is empty, skipping", file=sys.stderr)
            continue
        files.append((leaf, payload))
    if not files:
        return _error("no files to write")

    printer = _ProgressPrinter(args)
    try:
        sequential_serial = await _resolve_serial(args.udid, args.usbmux_address)
    except _CliError as exc:
        return _error(str(exc))
    try:
        result = await write_files_to_device(
            sequential_serial,
            args.target_dir,
            files,
            log_cb=printer.log,
            autopair=args.autopair,
            pair_timeout=args.pair_timeout,
            connection_type=args.connection_type,
            usbmux_address=args.usbmux_address,
        )
    except (AirliftError, OSError, TimeoutError, ConnectionError) as exc:
        return _error(_translate(exc))
    except _write_errors() as exc:
        return _error(_translate(exc))

    return _print_write_result(result, args)


async def _cmd_write_dir(args) -> int:
    from pyairlift import write_files_to_device

    directory = Path(args.directory)
    if not directory.is_dir():
        return _error(f"not a directory: {args.directory}")

    files: list[tuple[str, bytes]] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        try:
            payload = entry.read_bytes()
        except OSError as exc:
            print(f"pyairlift: warning: cannot read '{entry}': {exc}", file=sys.stderr)
            continue
        if not payload:
            print(f"pyairlift: warning: '{entry}' is empty, skipping", file=sys.stderr)
            continue
        files.append((entry.name, payload))
    if not files:
        return _error(f"no non-empty files in '{args.directory}'")

    printer = _ProgressPrinter(args)
    try:
        sequential_serial = await _resolve_serial(args.udid, args.usbmux_address)
    except _CliError as exc:
        return _error(str(exc))
    try:
        result = await write_files_to_device(
            sequential_serial,
            args.target_dir,
            files,
            log_cb=printer.log,
            autopair=args.autopair,
            pair_timeout=args.pair_timeout,
            connection_type=args.connection_type,
            usbmux_address=args.usbmux_address,
        )
    except (AirliftError, OSError, TimeoutError, ConnectionError) as exc:
        return _error(_translate(exc))
    except _write_errors() as exc:
        return _error(_translate(exc))

    return _print_write_result(result, args)


class _ProgressPrinter:
    """Streams AirLift tracing to stderr; silent unless ``--verbose``."""

    def __init__(self, args):
        self.verbose = bool(getattr(args, "verbose", False))

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"airlift: {message}", file=sys.stderr, flush=True)


def _write_fields(result: dict[str, Any]) -> tuple[list[str], list[str], bool, bool]:
    written = result.get("written", []) or []
    failures = result.get("failures", []) or []
    rejected = bool(result.get("rejected"))
    ok = bool(result.get("ok", not failures))
    return written, failures, rejected, ok


def _print_write_result(result: dict[str, Any], args) -> int:
    if args.json:
        payload = {"command": args.command, **result}
        _print_json(payload)
        written, failures, rejected, ok = _write_fields(result)
        return 0 if (ok and not rejected) else 1

    written, failures, rejected, ok = _write_fields(result)
    if rejected:
        print(f"Wrote 0 file(s) — the device rejected the sync session. "
              "Unlock the iPhone and keep it awake, open Apple Books once, "
              "close other syncing clients, then retry.", file=sys.stderr)
        return 1
    if getattr(args, "quiet", False):
        print(f"Wrote {len(written)} file(s) to {result.get('targetDirectory', '?')}")
        if failures:
            print(f"Skipped {len(failures)} file(s).", file=sys.stderr)
        return 0 if ok else 1
    print(f"Wrote {len(written)} file(s) to {result.get('targetDirectory', '?')}:")
    for leaf in written:
        print(f"  + {leaf}")
    if failures:
        print(f"Skipped {len(failures)} file(s) (already present, or write failed):")
        for leaf in failures:
            print(f"  ~ {leaf}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# parser / entry point

def _build_parser() -> argparse.ArgumentParser:
    global_parent = argparse.ArgumentParser(add_help=False)
    global_parent.add_argument("--json", action="store_true",
                               help="emit machine-readable JSON on stdout")
    global_parent.add_argument("-q", "--quiet", action="store_true",
                               help="suppress non-essential output")

    parser = argparse.ArgumentParser(
        prog="pyairlift",
        description="AirLift (ATAirlock sandbox escape) file writer for iPhones — "
                    "cross-platform, pymobiledevice3-based.",
        parents=[global_parent],
    )
    parser.add_argument("--version", action="version", version=f"pyairlift {__version__}")

    subp = parser.add_subparsers(dest="command", required=True)

    p_devices = subp.add_parser("devices", help="List connected iOS devices",
                                parents=[global_parent])
    p_devices.add_argument("--details", action="store_true",
                           help="also open a lockdown session per device (name, iOS, …)")
    p_devices.add_argument("--udid", default=None,
                           help="only this device (prefix match)")
    p_devices.add_argument("--usbmux-address", default=None)
    p_devices.set_defaults(func=_cmd_devices)

    p_check = subp.add_parser("check", help="Validate an iPhone for AirLift",
                              parents=[global_parent])
    _add_session_args(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_write = subp.add_parser("write",
                              help="Write local files into a device directory",
                              parents=[global_parent])
    p_write.add_argument("target_dir", help="absolute target directory on the device")
    p_write.add_argument("files", nargs="+", help="local files to write (leaf = file name)")
    p_write.add_argument("-v", "--verbose", action="store_true",
                         help="trace the ATC sync session on stderr")
    _add_session_args(p_write)
    p_write.set_defaults(func=_cmd_write)

    p_write_dir = subp.add_parser("write-dir",
                                  help="Write every file of a local directory (flat)",
                                  parents=[global_parent])
    p_write_dir.add_argument("directory", help="local directory whose files are written")
    p_write_dir.add_argument("target_dir", help="absolute target directory on the device")
    p_write_dir.add_argument("-v", "--verbose", action="store_true",
                             help="trace the ATC sync session on stderr")
    _add_session_args(p_write_dir)
    p_write_dir.set_defaults(func=_cmd_write_dir)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args.func(args))
    except KeyboardInterrupt:
        print("pyairlift: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the shell
        return _error(_translate(exc))


if __name__ == "__main__":
    sys.exit(main())