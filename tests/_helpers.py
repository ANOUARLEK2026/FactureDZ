import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class HomeTestCase(unittest.TestCase):
    """Gives every test its own empty data folder (FACTUREDZ_HOME)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self._old = os.environ.get("FACTUREDZ_HOME")
        os.environ["FACTUREDZ_HOME"] = str(self.home)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("FACTUREDZ_HOME", None)
        else:
            os.environ["FACTUREDZ_HOME"] = self._old
