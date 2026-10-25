"""Windows notification-area launcher for the SBM local file-transfer server."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import traceback
import webbrowser
from pathlib import Path


APP_NAME = "SBM File Transfer"
PORT = int(os.environ.get("SBM_PORT", "8000"))


def configure_instance_directory() -> Path:
    """Keep mutable state outside the PyInstaller bundle and install directory."""
    configured = os.environ.get("SBM_INSTANCE_DIR")
    if configured:
        target = Path(configured).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if (executable_dir / "portable.mode").exists():
            target = executable_dir / "data"
        else:
            local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            target = local_app_data / "SBM"
    else:
        return Path(__file__).resolve().parent / "instance"
    target.mkdir(parents=True, exist_ok=True)
    os.environ["SBM_INSTANCE_DIR"] = str(target)
    return target


INSTANCE_DIR = configure_instance_directory()
LOG_PATH = INSTANCE_DIR / "sbm.log"
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def bundled_resource(relative_path: str) -> Path:
    """Resolve resources from source or the PyInstaller bundle."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


class SBMTrayApp:
    def __init__(self) -> None:
        self.server = None
        self.server_thread = None
        self.audit_worker = None
        self.thumbnail_worker = None
        self._start_server()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{PORT}"

    def _start_server(self) -> None:
        import sbm
        from database import User, start_audit_flush_worker
        from flask_migrate import upgrade as migrate_upgrade
        from werkzeug.serving import make_server

        self.sbm = sbm
        with sbm.app.app_context():
            migrate_upgrade(directory=os.path.join(os.path.dirname(sbm.__file__), "migrations"))
            sbm.ensure_setup_token()
            self.first_admin_needed = User.query.filter_by(role="admin").first() is None
            self.setup_token = sbm.get_setup_token(create=False) if self.first_admin_needed else None
        self.server = make_server("0.0.0.0", PORT, sbm.app, threaded=True)
        self.audit_worker = start_audit_flush_worker(sbm.app, interval_seconds=300)
        self.thumbnail_worker = sbm.start_thumbnail_cleanup_worker()
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                              name="sbm-http-server", daemon=True)
        self.server_thread.start()
        logging.info("SBM server listening on port %s", PORT)

    def _show_first_run_setup(self) -> None:
        if not self.first_admin_needed:
            return
        text = ("SBM is ready for first-time administrator setup.\n\n"
                f"Open: {self.base_url}/setup\n\n"
                f"One-time setup token:\n{self.setup_token or '(not available)'}\n\n"
                "Use Ctrl+C while this dialog is active to copy its text.")
        ctypes.windll.user32.MessageBoxW(None, text, "Set up SBM administrator", 0x40 | 0x1000)
        webbrowser.open(self.base_url + "/setup")

    def _open_admin(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.base_url + "/admin/users")

    def _show_qr(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.base_url + "/connect")

    def _exit(self, icon, _item=None) -> None:
        icon.stop()

    def run(self) -> None:
        import pystray
        from PIL import Image

        with Image.open(bundled_resource("static/images/sbm-logo.png")) as brand_image:
            icon_image = brand_image.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        menu = pystray.Menu(
            pystray.MenuItem("Open admin", self._open_admin, default=True),
            pystray.MenuItem("Show phone QR code", self._show_qr),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit SBM", self._exit),
        )
        icon = pystray.Icon("sbm-file-transfer", icon_image, APP_NAME, menu)
        try:
            icon.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            if self.audit_worker:
                self.audit_worker.shutdown()
            if self.thumbnail_worker:
                self.thumbnail_worker.shutdown()
            if self.server:
                self.server.shutdown()
            if self.server_thread:
                self.server_thread.join(timeout=5)
        except Exception:
            logging.exception("Error while stopping SBM")


def main() -> None:
    try:
        app = SBMTrayApp()
        app._show_first_run_setup()
        app.run()
    except Exception:
        details = traceback.format_exc()
        logging.error("Launcher failed:\n%s", details)
        try:
            ctypes.windll.user32.MessageBoxW(
                None, f"SBM failed to start.\n\nDetails: {LOG_PATH}", APP_NAME, 0x10 | 0x1000)
        except Exception:
            print(details, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
