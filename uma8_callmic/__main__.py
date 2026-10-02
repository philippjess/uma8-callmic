"""Einstieg: Tray-Anwendung oder Hilfsbefehle."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from . import constants as K


def _check_geometry_cli() -> int:
    from .capture import open_capture
    from .config import load
    from .geometry import check
    from .pwctl import raw_target

    cfg = load(K.CONFIG_FILE).config
    print("Bitte 10 Sekunden still sein …", flush=True)
    cap = open_capture(raw_target(), 12.0)
    try:
        time.sleep(10.5)
        block = cap.latest(10 * K.SAMPLE_RATE)
    finally:
        cap.close()
    if block is None:
        print("Keine Daten vom UMA-8 (angeschlossen, Raw-Firmware?)")
        return 1
    res = check(block, cfg.geometry())
    print(res.message)
    print(f"Kontrast: {res.contrast:.3f}")
    if res.detected is not None:
        print(f"Erkannt: Mitte {res.detected.center}, Ring {list(res.detected.ring)}")
    return 0 if res.matches_default else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="uma8-callmic", description="UMA-8 Call Mic")
    ap.add_argument("--write-config", action="store_true",
                    help="PipeWire-Konfiguration aus den Einstellungen schreiben und beenden")
    ap.add_argument("--check-geometry", action="store_true",
                    help="Kanalzuordnung 10 s lang prüfen und Ergebnis ausgeben")
    ap.add_argument("--ref-linker", action="store_true",
                    help="Echo-Referenz nur während einer Aufnahme verbinden (startet die Kette selbst)")
    args = ap.parse_args(argv)

    if args.ref_linker:  # läuft im Dienst der Kette: Meldungen ins Journal, nicht ins Log des Trays
        from .reflink import METADATA_ENV, run

        logging.basicConfig(level=logging.INFO, format="uma8-callmic --ref-linker: %(levelname)s %(message)s")
        return run(os.environ.get(METADATA_ENV) or "default")

    K.STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[RotatingFileHandler(K.STATE_DIR / "log", maxBytes=1_000_000, backupCount=2)])

    if args.write_config:
        from .chainconf import write
        from .config import load

        write(load(K.CONFIG_FILE).config)
        print(K.CHAIN_CONF)
        return 0
    if args.check_geometry:
        return _check_geometry_cli()

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication

    from .tray import TrayApp

    lock = QLockFile(str(K.STATE_DIR / "tray.lock"))
    if not lock.tryLock(100):
        print("uma8-callmic läuft bereits.")
        return 0
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("uma8-callmic")
    app.setDesktopFileName("uma8-callmic")
    tray = TrayApp(app)
    code = app.exec()
    tray.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
