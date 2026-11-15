"""
utils/sandbox.py — run untrusted homework code (Java / Python) safely.

On the VM (Linux + bubblewrap) the code runs in its own namespaces:
  * no network                     (--unshare-all)
  * no /home  → the project, .env, tokens and keys simply don't exist inside
  * /usr and /etc read-only, a private /tmp, only the work dir is writable
  * empty environment              (no GEMINI_API_KEY etc. — the parent has them)
  * wall-clock timeout that kills the whole tree, CPU-seconds and file-size limits

Without bubblewrap (the Windows dev box) it still scrubs the environment and
enforces the timeout, and says so: `isolated=False` in the result.

    r = run_sandboxed(['javac', 'Main.java'], workdir)
    r.ok, r.stdout, r.stderr, r.timed_out
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUT = 20_000
FILE_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass
class SandboxResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool
    isolated: bool

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out


def bwrap_available() -> bool:
    return sys.platform.startswith('linux') and shutil.which('bwrap') is not None


def python_interpreter() -> str:
    """A system Python visible inside the sandbox (the venv lives under the hidden /home)."""
    if not bwrap_available():
        return sys.executable
    for p in ('/usr/bin/python3.12', '/usr/bin/python3'):
        if os.path.exists(p):
            return p
    return 'python3'


def _bwrap_prefix(workdir: Path) -> list[str]:
    w = str(workdir)
    pre = ['bwrap', '--ro-bind', '/usr', '/usr', '--ro-bind', '/etc', '/etc',
           '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for link in ('lib', 'lib64', 'bin', 'sbin'):        # merged-/usr layout
        if os.path.islink(f'/{link}'):
            pre += ['--symlink', f'usr/{link}', f'/{link}']
    return pre + ['--bind', w, w, '--chdir', w,
                  '--unshare-all', '--die-with-parent', '--new-session']


def _limits(cpu_seconds: int, file_bytes: int = FILE_LIMIT_BYTES, memory_bytes: int | None = None):
    def apply():                                            # runs in the child (POSIX only)
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if memory_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    return apply


def run_sandboxed(cmd: list[str], workdir: Path, timeout: int = 15,
                  stdin: str | None = None, extra_env: dict | None = None,
                  file_bytes: int = FILE_LIMIT_BYTES, memory_bytes: int | None = None) -> SandboxResult:
    workdir = Path(workdir).resolve()
    isolated = bwrap_available()
    full = (_bwrap_prefix(workdir) + cmd) if isolated else cmd
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(workdir),
           'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', **(extra_env or {})}
    if sys.platform == 'win32':                              # dev box: keep what Windows needs
        env.update({k: os.environ[k] for k in ('SYSTEMROOT', 'PATH', 'TEMP', 'TMP') if k in os.environ})
    posix = os.name == 'posix'
    proc = subprocess.Popen(
        full, cwd=str(workdir), env=env, text=True, encoding='utf-8', errors='replace',
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=posix, preexec_fn=_limits(timeout + 5, file_bytes, memory_bytes) if posix else None)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            if posix:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        out, err = proc.communicate()
        timed_out = True
    return SandboxResult(proc.returncode if proc.returncode is not None else -9,
                         (out or '')[:MAX_OUT], (err or '')[:MAX_OUT], timed_out, isolated)
