import os
import sys
from pathlib import Path

# Tests use local fixture pages only; run the browser headless.
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("BROWSER_SLOW_MO_MS", "0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True
