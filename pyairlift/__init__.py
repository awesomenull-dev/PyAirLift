"""PyAirLift — public API for third-party integration.

A thin, stable facade over the AirLift core in ``src.airlift`` (a pure-Python
port of the ATAirlock sandbox escape, ATC "Books sync" based).  Third-party
programs should import from this package instead of the internal module so the
core can evolve without breaking your code.

Everything is async.  A typical use for a **single self-managed device**::

    import asyncio
    from pyairlift import write_files_to_device

    async def main():
        result = await write_files_to_device(
            None,                          # first available iPhone
            "/var/mobile/Library/Caches/TelephonyUI-10",
            [("en-1---white.png", open("en-1---white.png", "rb").read())],
            log_cb=lambda msg: print(msg, flush=True),
        )
        print(result)   # {"written": [...], "failures": [...], "ok": True, ...}

    asyncio.run(main())

Lower-level control (you manage the lockdown session yourself)::

    from pyairlift import device_session, write_files

    async def main():
        async with device_session(serial) as lockdown:
            return await write_files(lockdown, "/var/...", files)

Advanced users who already hold a ``pymobiledevice3.lockdown.LockdownClient``
can call :func:`write_file` / :func:`write_files` / :func:`check_target`
directly — they are the same objects the core exposes.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import MuxException, NotPairedError
from pymobiledevice3.lockdown import LockdownClient, create_using_usbmux

from pyairlift._version import __version__

# Re-exports from the core so callers never need to reach into ``src.airlift``.
from src.airlift import (  # noqa: F401
    AirliftError,
    AirTrafficSyncRejected,
    AIRLOCK_ROOT,
    AT_C_SERVICE,
    MAX_AT_C_ASSETS_PER_SESSION,
    LogCallback,
    TESTED_BUILDS,
    check_target,
    normalize_target,
    posix_rel,
    write_file,
    write_files,
)

logger = logging.getLogger("pyairlift")

__all__ = [
    # version
    "__version__",
    # errors
    "AirliftError",
    "AirTrafficSyncRejected",
    # callbacks / typing
    "LogCallback",
    # low-level (require an open LockdownClient)
    "write_file",
    "write_files",
    "check_target",
    "normalize_target",
    "posix_rel",
    # sessions / discovery
    "device_session",
    "device_info",
    "list_devices",
    # high-level (self-contained sessions)
    "write_file_to_device",
    "write_files_to_device",
    # constants
    "TESTED_BUILDS",
    "AIRLOCK_ROOT",
    "AT_C_SERVICE",
    "MAX_AT_C_ASSETS_PER_SESSION",
]


@asynccontextmanager
async def device_session(
    serial: Optional[str] = None,
    *,
    autopair: bool = True,
    pair_timeout: Optional[float] = 120.0,
    connection_type: Optional[str] = None,
    usbmux_address: Optional[str] = None,
) -> AsyncIterator[LockdownClient]:
    """Open a lockdown connection to an iPhone and close it safely on exit.

    :param serial: usbmux serial of the device (``None`` = first available).
    :param autopair: request the on-device "Trust This Computer?" dialog when
        the host is not (or no longer) paired with the device.
    :param pair_timeout: seconds to wait for the user to accept the trust
        dialog; ``None`` waits forever.
    :param connection_type: restrict to ``"USB"`` or ``"Network"``.
    :param usbmux_address: address of the usbmuxd socket (used by the CLI
        ``--usbmux-address`` option); ``None`` for the default.
    """
    ld = await create_using_usbmux(
        serial=serial,
        autopair=autopair,
        pair_timeout=pair_timeout,
        connection_type=connection_type,
        usbmux_address=usbmux_address,
    )
    try:
        if autopair and not getattr(ld, "paired", False):
            # lockdownd accepted the pair request but the session did not
            # validate — treat it as unpaired so no caller reaches a
            # half-trusted client (its service starts would raise later).
            raise NotPairedError("device did not confirm trust")
        yield ld
    finally:
        try:
            await ld.close()
        except Exception:
            pass


async def list_devices(
    usbmux_address: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List devices currently visible to usbmuxd (no pairing side effects).

    Returns a list of descriptors::

        [{"serial": "00008110-...", "connection": "USB", "usb": True}, ...]

    Enrichment (name, iOS version, …) requires connecting to the device — use
    :func:`device_info` for that.
    """
    devices = await usbmux.list_devices(usbmux_address=usbmux_address)
    return [
        {
            "serial": device.serial,
            "connection": device.connection_type,
            "usb": device.is_usb,
        }
        for device in devices
    ]


async def device_info(
    serial: Optional[str] = None,
    *,
    autopair: bool = False,
    pair_timeout: Optional[float] = 10.0,
    connection_type: Optional[str] = None,
    usbmux_address: Optional[str] = None,
) -> dict[str, Any]:
    """Return a descriptor of a connected iPhone.

    ``autopair`` defaults to False so discovery never pops a trust dialog; set
    it True to trigger one when you intend to use the device right away.

    On failure (no device, locked, not paired) an ``AirliftError`` is raised
    describing the problem.
    """
    try:
        async with device_session(
            serial,
            autopair=autopair,
            pair_timeout=pair_timeout,
            connection_type=connection_type,
            usbmux_address=usbmux_address,
        ) as ld:
            values = getattr(ld, "all_values", {}) or {}
            version = values.get("ProductVersion") or ""
            build = values.get("BuildVersion") or ""
            product = values.get("ProductType") or ""
            returned = {
                "serial": values.get("UniqueDeviceID") or serial,
                "name": values.get("DeviceName"),
                "device_class": values.get("DeviceClass"),
                "model": values.get("ProductType"),
                "hardware": values.get("HardwareModel"),
                "arch": values.get("CPUArchitecture"),
                "ios_version": version,
                "ios_build": build,
                "paired": bool(getattr(ld, "paired", True)),
                "tested": (version, build) in TESTED_BUILDS,
                "iPhone": isinstance(product, str) and product.startswith("iPhone"),
            }
            if not returned["iPhone"]:
                raise AirliftError(
                    f"AirLift requires a physical iPhone; got ProductType={product!r}"
                )
            return returned
    except NotPairedError:
        raise AirliftError(
            "the host is not trusted by this iPhone — unlock the device and "
            'tap "Trust This Computer" when iOS asks, then try again'
        ) from None
    except MuxException as exc:
        raise AirliftError(
            "cannot reach usbmuxd — is it running? "
            f"({type(exc).__name__}: {exc})"
        ) from exc


async def write_file_to_device(
    serial: Optional[str],
    target_dir: str,
    leaf: str,
    payload: bytes,
    *,
    log_cb: Optional[LogCallback] = None,
    **session_kwargs: Any,
) -> dict[str, Any]:
    """Write a single new file ``target_dir/leaf``, opening a session for you.

    ``session_kwargs`` are forwarded to :func:`device_session` (``autopair``,
    ``pair_timeout``, ``connection_type``, ``usbmux_address``).
    """
    async with device_session(serial, **session_kwargs) as lockdown:
        return await write_file(lockdown, target_dir, leaf, payload, log_cb=log_cb)


async def write_files_to_device(
    serial: Optional[str],
    target_dir: str,
    files: list[tuple[str, bytes]],
    *,
    log_cb: Optional[LogCallback] = None,
    **session_kwargs: Any,
) -> dict[str, Any]:
    """Write a batch of new files below ``target_dir``, opening a session for you.

    See :func:`write_files` for the return shape / failure semantics;
    ``session_kwargs`` are forwarded to :func:`device_session`.
    """
    async with device_session(serial, **session_kwargs) as lockdown:
        return await write_files(lockdown, target_dir, files, log_cb=log_cb)