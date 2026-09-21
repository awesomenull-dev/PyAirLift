# PyAirLift

Cross-platform implementation of the AirLift (ATAirlock) sandbox-escape file
writer for iPhones, built on [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).
Works on Linux, Windows and macOS, over USB or Wi-Fi (whatever transport
`lockdown_session` provides).

AirLift pushes **new** files into arbitrary sandbox-external directories on the
device (e.g. `/var/mobile/Library/Caches/TelephonyUI-10`) by abusing the
Apple Books AirTraffic sync (ATC). Because ATAirlock uses a rename, only files
whose names are **not already present** can be written.

## Install

```bash
pip install -e .
```

Python 3.9+, dependency: `pymobiledevice3>=10.6`.

## CLI

```bash
# List connected devices (JSON for machines)
pyairlift devices --json

# Show details (name, iOS version) of each device
pyairlift devices --details

# Validate an iPhone for AirLift
pyairlift check --no-autopair

# Write files into a device directory (leaf name = file name)
pyairlift write /var/mobile/Library/Caches/TelephonyUI-10 en-1---white.png @3x.png

# Push every file of a local directory into a device directory
pyairlift write-dir ./theme/ /var/mobile/Library/Caches/TelephonyUI-10

# Trace the ATC sync session on stderr, pick a specific device
pyairlift write /var/mobile/Media/x ./f.bin --udid 00008120-0006 --verbose
```

`--udid` accepts a partial serial. Exit codes: `0` success, `1` the device
refused / a step failed, `2` usage error. With `--json` only the JSON document
is printed on stdout; all progress goes to stderr.

### JSON output examples

`pyairlift devices --json`:

```json
{
  "command": "devices",
  "devices": [
    {
      "serial": "00008120-0006155436F0E01E",
      "connection": "USB",
      "usb": true
    }
  ]
}
```

`pyairlift check --json`:

```json
{
  "command": "check",
  "ok": true,
  "product": "iPhone15,4",
  "version": "27.0",
  "build": "24A437",
  "tested": false
}
```

`pyairlift write ... --json` — the write result:

```json
{
  "command": "write",
  "targetDirectory": "/var/mobile/Library/Caches/TelephonyUI-10",
  "written": ["en-1---white.png"],
  "failures": [],
  "ok": true
}
```

## Python API

```python
import asyncio
from pyairlift import write_files_to_device, AirliftError

async def main():
    with open("en-1---white.png", "rb") as fh:
        png = fh.read()

    result = await write_files_to_device(
        None,  # first available iPhone, or a serial
        "/var/mobile/Library/Caches/TelephonyUI-10",
        [("en-1---white.png", png)],
        log_cb=lambda msg: print(msg, flush=True),
    )
    print(result["written"], result["failures"])  # {"ok": True, ...}

asyncio.run(main())
```

Lower-level control (you manage the lockdown session):

```python
from pyairlift import device_session, write_files

async def main():
    async with device_session(serial) as lockdown:
        return await write_files(lockdown, "/var/mobile/Media/x", files)
```

Functions expecting an already-open `LockdownClient` (for advanced callers):

- `write_file(lockdown, target_dir, leaf, payload, log_cb=None)`
- `write_files(lockdown, target_dir, files, log_cb=None)` — batch, degrades to
  per-file sessions on failure
- `check_target(lockdown, log_cb=None)` — validate + warn on untested builds
- `normalize_target(path)`, `posix_rel(path, base)`

Device discovery:

- `list_devices()` — usbmuxd devices, no pairing side effects
- `device_info(serial)` — name, iOS version/build, model, pairing status
- `device_session(serial, *, autopair=True, pair_timeout=120, ...)` — safe
  lockdown context manager

Errors: `AirliftError`, `AirTrafficSyncRejected`. Constants: `TESTED_BUILDS`,
`AIRLOCK_ROOT`, `AT_C_SERVICE`, `MAX_AT_C_ASSETS_PER_SESSION`.

## Notes

- Verified builds are in `TESTED_BUILDS`,
  other builds are warned about but not blocked.
- The device must be unlocked and trust this computer; otherwise the ATC
  session is rejected.
- Only **new** names can be written (ATAirlock renames). Re-applying the same
  files is a no-op reported as `failures`.

## Credits

- [0xjohnnydev](https://github.com/0xjohnnydev/) for the original airlift (`airlift.py`, `AirCard` exploit.rs FFI)
- [GoldenNugget](https://github.com/awesomenull-dev/GoldenNugget) for the Python implementation this project ports
