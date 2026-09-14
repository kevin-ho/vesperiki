"""Keep the repository tree free of compiled-bytecode leakage.

Stale ``__pycache__`` directories embed machine-specific absolute paths via
``co_filename`` in their ``*.pyc`` files; they must never leak into an
open-source tree.
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Sweep away any pre-existing __pycache__ directly inside tests/ and vesperiki/."""
    repo_root = Path(__file__).resolve().parent.parent
    for package_dir in ("tests", "vesperiki"):
        shutil.rmtree(repo_root / package_dir / "__pycache__", ignore_errors=True)
