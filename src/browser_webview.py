from __future__ import annotations

import os
import json
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path


STUDIO_URL = "https://studio.youtube.com/"


def _prepare_profile(profile_path: Path) -> None:
    default_dir = profile_path / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = default_dir / "Preferences"
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8")) if preferences_path.exists() else {}
    except (OSError, ValueError):
        preferences = {}
    preferences["credentials_enable_service"] = False
    preferences.setdefault("profile", {})["password_manager_enabled"] = False
    preferences.setdefault("signin", {})["allowed"] = False
    preferences.setdefault("autofill", {})["profile_enabled"] = False
    preferences["autofill"]["credit_card_enabled"] = False
    preferences_path.write_text(json.dumps(preferences), encoding="utf-8")
    # Profiles created by older client versions may already contain saved
    # credentials. Remove only password/autofill stores; keep Cookies and site
    # storage so a channel remains signed in to its own Google account.
    for name in ("Login Data", "Login Data-journal", "Login Data For Account", "Web Data", "Web Data-journal"):
        try:
            (default_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _find_edge() -> str | None:
    candidates = [
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("microsoft-edge-stable"),
    ]
    for environment_name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(environment_name)
        if root:
            candidates.append(str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


def main() -> int:
    if len(sys.argv) != 5:
        return 2

    profile_path = Path(sys.argv[1]).resolve()
    proxy_url = sys.argv[2] or None
    initial_url = sys.argv[3] or STUDIO_URL
    profile_path.mkdir(parents=True, exist_ok=True)
    _prepare_profile(profile_path)
    # Arg 4 ("temporary" for the common browser) intentionally does NOT add
    # --inprivate here: InPrivate keeps cookies in memory only, so nothing
    # would land on disk for the storage-state snapshot that BrowserManager
    # takes after the browser exits. Freshness between launches is guaranteed
    # by launching from a brand-new profile directory that is deleted on close.

    # Resolve through the system DNS before Edge starts. Disabling Chromium's
    # separate AsyncDNS below makes Edge reuse this path instead of initially
    # showing ERR_NAME_NOT_RESOLVED and recovering several seconds later.
    resolver = ThreadPoolExecutor(max_workers=1)
    try:
        resolver.submit(
            socket.getaddrinfo,
            "studio.youtube.com",
            443,
            0,
            socket.SOCK_STREAM,
        ).result(timeout=1.0)
    except (OSError, TimeoutError):
        pass
    finally:
        resolver.shutdown(wait=False, cancel_futures=True)

    edge = _find_edge()
    if not edge:
        print(
            "BROWSER_ERROR:Microsoft Edge was not found. Install Microsoft Edge or add msedge.exe to PATH.",
            flush=True,
        )
        return 3

    args = [
        edge,
        f"--user-data-dir={profile_path}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-signin-promo",
        "--disable-password-generation",
        # Avoid Google/Windows automatically opening a hardware security-key
        # prompt. Password and normal OAuth login remain available.
        "--disable-features=AsyncDns,WebAuthentication,WebAuthenticationCable,WebAuthenticationPhoneSupport,PasswordManagerOnboarding,AutofillServerCommunication,EdgeIdentity,msEdgeIdentity",
    ]
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")
    else:
        args.append("--no-proxy-server")
    args.append(initial_url)

    try:
        return subprocess.call(args)
    except OSError as exc:
        print(f"BROWSER_ERROR:Cannot start Microsoft Edge: {exc}", flush=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
