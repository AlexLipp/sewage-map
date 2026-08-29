"""Central import bootstrap for directly executed company cleaner scripts."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys


def load_common():
    """Import the package common module from a script launched by file path."""
    repository_root = Path(__file__).resolve().parents[2]
    root_text = str(repository_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return importlib.import_module("clean_EIR_stopstartdata.raw_to_standardised.common")


common = load_common()
