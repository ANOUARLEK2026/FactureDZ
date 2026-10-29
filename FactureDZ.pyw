# -*- coding: utf-8 -*-
"""Double-click entry point: .pyw runs under pythonw, so no console window ever appears."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from facturedz.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
