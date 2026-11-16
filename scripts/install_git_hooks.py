"""
scripts/install_git_hooks.py — installs deploy/git-hooks/pre-push into this clone's .git/hooks.

    python scripts/install_git_hooks.py

After this every `git push` first runs scripts/release_scan.py and is refused on any finding.
(The same scan runs on GitHub in .github/workflows/release-scan.yml, as a second net.)
"""
from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        git_dir = subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.strip()
    except Exception:
        print('not a git clone — nothing to install')
        return 1
    hooks = (ROOT / git_dir).resolve() / 'hooks'
    hooks.mkdir(parents=True, exist_ok=True)
    src, dst = ROOT / 'deploy' / 'git-hooks' / 'pre-push', hooks / 'pre-push'
    if dst.exists() and dst.read_bytes() != src.read_bytes():
        shutil.copyfile(dst, dst.with_suffix('.bak'))
        print(f'kept your previous hook as {dst.with_suffix(".bak").name}')
    shutil.copyfile(src, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f'OK: pre-push hook installed: {dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
