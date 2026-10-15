"""One application version for the source service and its frozen distribution."""

import json
import sys
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
APPLICATION_VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
