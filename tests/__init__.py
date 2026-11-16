"""Test package. On a fresh clone there is no config/courses.yaml yet (it is personal and gitignored):
the suites then run against the shipped example (config/courses.example.yaml), copied once into place.
Replace it with your own courses afterwards (python scripts/setup_check.py explains how)."""
import shutil as _shutil
from pathlib import Path as _Path

_CFG = _Path(__file__).resolve().parent.parent / 'config'
for _name in ('courses', 'professors'):
    _real, _example = _CFG / f'{_name}.yaml', _CFG / f'{_name}.example.yaml'
    if not _real.exists() and _example.exists():
        _shutil.copyfile(_example, _real)

# The full suites test the student domain too, whatever the local .env says (SSA_STUDY=0 is the default for a new
# setup). `SSA_TEST_MODE=assistant-only` (tests/test_assistant_only.py) runs with it off instead.
import os as _os
import sys as _sys
_sys.path.insert(0, str(_CFG.parent)) if str(_CFG.parent) not in _sys.path else None
from config import features as _features  # noqa: E402
if _os.environ.get('SSA_TEST_MODE') == 'assistant-only':
    _features.set_study(False)
else:
    _features.set_study(True)
