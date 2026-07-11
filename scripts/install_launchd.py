from __future__ import annotations

import argparse
import plistlib
from pathlib import Path


LABEL = "com.kunjin.daily-sync"


def build_plist(project_root: Path, home: Path) -> dict:
    logs = home / ".local" / "state" / "kunjin" / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/python3",
            "-m",
            "kunjin.cli",
            "--json",
            "sync",
            "daily",
        ],
        "EnvironmentVariables": {
            "PYTHONPATH": str(project_root / "src"),
            "PYTHONPYCACHEPREFIX": "/private/tmp/kunjin-pycache",
        },
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": 18, "Minute": 30} for weekday in range(2, 7)
        ],
        "StandardOutPath": str(logs / "daily-sync.out.log"),
        "StandardErrorPath": str(logs / "daily-sync.err.log"),
        "RunAtLoad": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the KunJin daily sync LaunchAgent")
    parser.add_argument("--project-root", default="/Users/yanzihao/KunJin")
    parser.add_argument("--output")
    parser.add_argument("--home", help="Override the home directory for isolated verification")
    args = parser.parse_args()
    home = Path(args.home) if args.home else Path.home()
    output = Path(args.output) if args.output else home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    output.parent.mkdir(parents=True, exist_ok=True)
    (home / ".local" / "state" / "kunjin" / "logs").mkdir(parents=True, exist_ok=True)
    with output.open("wb") as file_handle:
        plistlib.dump(build_plist(Path(args.project_root), home), file_handle, sort_keys=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
