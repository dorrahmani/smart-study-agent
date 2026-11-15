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
