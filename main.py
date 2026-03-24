import sys
import logging
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon

import modules.config_manager as config_manager
from modules.ui_main import MainWindow


def setup_logging(log_level: str) -> None:
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "save_transfer.log"

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    cfg = config_manager.load()
    setup_logging(cfg.get("log_level", "INFO"))

    app = QApplication(sys.argv)
    app.setApplicationName("Game Save Transfer")
    app.setOrganizationName("RLS")

    icon_path = Path(__file__).parent / "config" / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
