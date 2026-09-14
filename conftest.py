"""Root conftest: make the repository root importable without an editable install.

The project layout intentionally keeps CodePilot packages at the repository root
(``domain``, ``a2a``, ``agents``, ``tools``, ``rules``, ``sandbox``, ``repositories``,
``apps``, ``evals``) as required by the project constitution and the SRS chapter 13.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
