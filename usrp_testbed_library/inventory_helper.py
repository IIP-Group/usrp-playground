"""
Host-side helper that watches the shared signals/tasks volume for USRP
discovery requests from the entrypoint container and answers them by
running `uhd_find_devices` (which needs USB/network access only the host
has).

Protocol:
    * Entrypoint touches  ${WATCH_DIR}/discover_request
    * Helper notices the file, runs `uhd_find_devices`, parses the output,
      writes the result atomically to ${WATCH_DIR}/discover_result.json
      and removes the request file
    * Result schema:
        {"timestamp": <iso>, "devices": [
            {"serial": "...", "type": "...", "product": "...",
             "name": "...", "addr": "...", "args": "<raw arg string>"}, ...
        ]}

Run via start-daemons.sh; no UHD lock is held, so this is safe to run
alongside the TX/RX daemons.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _watch_dir() -> Path:
    return Path(os.environ.get("INVENTORY_WATCH_DIR", "/data/inventory"))


def _parse_uhd_output(text: str) -> list[dict]:
    """Parse the `uhd_find_devices` output blocks into a list of dicts."""
    devices: list[dict] = []
    # Each device block starts with "-- UHD Device N" header and contains a
    # "Device Address:" line followed by indented "key: value" pairs.
    blocks = re.split(r"-{2,}\s*\n--\s*UHD Device\s+\d+\s*\n-{2,}", text)
    for block in blocks[1:]:           # first split chunk is the preamble
        dev: dict[str, str] = {}
        for line in block.splitlines():
            m = re.match(r"\s*([A-Za-z_][\w-]*)\s*:\s*(\S.*)$", line)
            if m:
                dev[m.group(1)] = m.group(2).strip()
        if dev:
            # Build a UHD-compatible args string. Prefer serial (USB), fall
            # back to addr (network).
            args_parts = []
            if dev.get("type"):
                args_parts.append(f"type={dev['type']}")
            if dev.get("serial"):
                args_parts.append(f"serial={dev['serial']}")
            elif dev.get("addr"):
                args_parts.append(f"addr={dev['addr']}")
            dev["args"] = ",".join(args_parts)
            devices.append(dev)
    return devices


def discover() -> dict:
    """Run uhd_find_devices and return parsed result. Uses sudo because some
    USB devices are only enumerable as root."""
    result = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "devices": [], "error": None, "raw": ""}
    try:
        proc = subprocess.run(
            ["sudo", "-n", "uhd_find_devices"],
            capture_output=True, text=True, timeout=30,
        )
        result["raw"] = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
        if proc.returncode != 0 and not proc.stdout.strip():
            result["error"] = (
                proc.stderr.strip()
                or f"uhd_find_devices exited with code {proc.returncode}"
            )
            return result
        result["devices"] = _parse_uhd_output(proc.stdout)
    except subprocess.TimeoutExpired:
        result["error"] = "uhd_find_devices timed out"
    except FileNotFoundError:
        result["error"] = "uhd_find_devices not installed on host"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def main() -> None:
    watch = _watch_dir()
    watch.mkdir(parents=True, exist_ok=True)
    request = watch / "discover_request"
    result_path = watch / "discover_result.json"

    # Initial discovery on startup so the UI has something to show right away.
    _write_atomic(result_path, discover())
    print(f"[inventory] initial discovery written to {result_path}", flush=True)

    print(f"[inventory] watching {request} for discovery triggers …", flush=True)
    while True:
        if request.exists():
            try:
                request.unlink()
            except FileNotFoundError:
                pass
            print(f"[inventory] discovery request received", flush=True)
            data = discover()
            _write_atomic(result_path, data)
            n = len(data["devices"])
            print(f"[inventory] {n} device(s) found "
                  + (data.get("error") or ""), flush=True)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
