from __future__ import annotations

import sys
import urllib.parse

def main() -> int:
    if len(sys.argv) not in (3, 4):
        return 2
    target_url = sys.argv[1]
    redirect_uri = sys.argv[2]
    # Optional user-data dir pre-seeded with the saved common browser storage
    # state: lets Google recognize the account so no manual login is needed.
    user_data_dir = sys.argv[3] if len(sys.argv) == 4 else None
    try:
        import webview
    except ImportError:
        print("OAUTH_ERROR:pywebview is not installed", flush=True)
        return 3
    redirect_prefix = redirect_uri.split("?", 1)[0].rstrip("/")
    window = webview.create_window(
        "Google Authorization",
        target_url,
        width=1000,
        height=760,
        on_top=True,
        text_select=True,
        focus=True,
    )

    completed = False

    def handle_url(candidate: str | None) -> None:
        nonlocal completed
        if completed or not candidate or not candidate.startswith(redirect_prefix):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(candidate).query)
        if "state" not in query or ("code" not in query and "error" not in query):
            return
        completed = True
        print(f"OAUTH_REDIRECT:{candidate}", flush=True)
        try:
            window.destroy()
        except Exception:
            pass

    def find_url(value) -> None:
        if isinstance(value, str):
            handle_url(value)
        elif isinstance(value, dict):
            for item in value.values():
                find_url(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                find_url(item)

    def on_request(*args, **kwargs) -> None:
        find_url(args)
        find_url(kwargs)

    def on_loaded() -> None:
        # Called once per completed top-level navigation, not in a polling loop.
        try:
            handle_url(window.get_current_url())
        except Exception:
            pass

    window.events.request_sent += on_request
    window.events.loaded += on_loaded
    window.events.shown += lambda: print("OAUTH_READY:", flush=True)
    if user_data_dir:
        webview.start(gui="edgechromium", private_mode=False, storage_path=user_data_dir)
    else:
        webview.start(gui="edgechromium", private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
