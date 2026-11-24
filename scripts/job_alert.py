"""Email the sheet's alert recipients that a scheduled job failed (used by run_job.ps1).

One alert per job per 6-hour window: the Apps Script drops repeats of the same
delivery ID, so a job that fails every 15 minutes does not flood the inbox.
"""
from __future__ import annotations

import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import apps_script  # noqa: E402


def main() -> int:
    if not apps_script.enabled():
        return 0
    now = datetime.now(UTC)
    job = os.environ.get("BRIDGE_JOB_NAME", "job")
    bucket = now.hour - now.hour % 6
    try:
        apps_script.send(
            "alert",
            {
                "component": f"Scheduled job: {job}",
                "generatedAt": now.isoformat(),
                "host": platform.node(),
                "summary": os.environ.get("BRIDGE_JOB_SUMMARY", "A scheduled job failed")[:240],
                "detail": os.environ.get("BRIDGE_JOB_DETAIL", "")[-3000:],
            },
            delivery_id=f"job-failed:{job}:{now:%Y%m%d}{bucket:02d}",
        )
    except apps_script.AppsScriptError as exc:
        print(f"Could not send the failure alert: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
