"""Seed LARP Detector's dedicated LinkedIn Playwright profile.

This opens LinkedIn in a headed Chromium window and waits for the user to
finish login. Credentials are entered only into LinkedIn and are never read,
logged, copied, or stored by this script. Playwright persists the resulting
browser session in the local profile directory used by the scan engine.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

def default_profile_dir() -> Path:
    configured = os.environ.get("LARP_LINKEDIN_PROFILE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "LARP Detector"
        / "linkedin-profile"
    )


def has_linkedin_auth_cookie(profile_dir: Path) -> bool:
    cookie_paths = (
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    )
    for cookie_path in cookie_paths:
        if not cookie_path.exists():
            continue
        try:
            uri = cookie_path.as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                row = connection.execute(
                    "select 1 from cookies "
                    "where name = 'li_at' and host_key like '%linkedin%' limit 1"
                ).fetchone()
            finally:
                connection.close()
            if row:
                return True
        except Exception:
            continue
    return False


def stop_browser(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def main() -> int:
    profile_dir = default_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_binary = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    if not chrome_binary.exists():
        print("Google Chrome is required for the one-time LinkedIn login.")
        return 1

    process = subprocess.Popen(
        [
            str(chrome_binary),
            f"--user-data-dir={profile_dir}",
            "--password-store=basic",
            "--use-mock-keychain",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.linkedin.com/login",
        ],
        start_new_session=True,
    )
    try:
        print("Complete LinkedIn login in the Google Chrome window.")
        print("LARP Detector will detect the session and close this setup automatically.")

        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            if has_linkedin_auth_cookie(profile_dir):
                print("LinkedIn session saved for LARP Detector.")
                time.sleep(1)
                return 0
            if process.poll() is not None:
                print("Google Chrome closed before LinkedIn login completed.")
                return 1
            time.sleep(1)

        print("LinkedIn login setup timed out. Run it again when ready.")
        return 1
    finally:
        stop_browser(process)


if __name__ == "__main__":
    raise SystemExit(main())
