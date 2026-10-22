"""Create a project-local environment, install dependencies, then run Yuanji Free."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup-only', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--serve', action='store_true', help='Only start the local HTTP service')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print('Python 3.10 or newer is required. Python 3.12 is recommended.')
        return 1
    env = ROOT / '.venv'
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('Creating .venv ...', flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    requirements = ROOT / 'requirements.txt'
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    receipt = env / '.kidobs-requirements.sha256'
    if not receipt.is_file() or receipt.read_text('ascii').strip() != digest:
        print('Installing dependencies into .venv ...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)],
                       cwd=ROOT, check=True)
        receipt.write_text(digest, 'ascii')
    if args.setup_only:
        print('Environment ready.')
        return 0
    if args.serve:
        if not 1 <= args.port <= 65535:
            parser.error('--port must be between 1 and 65535')
        command = [str(python), '-m', 'uvicorn', 'server:app', '--app-dir', str(ROOT / 'app'),
                   '--host', '127.0.0.1', '--port', str(args.port)]
    else:
        command = [str(python), str(ROOT / 'app' / 'main.py')]
        if args.selftest:
            command.append('--selftest')
        if args.browser:
            command.append('--browser')
    child_env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    # A small KEY=VALUE file; values are never executed or expanded as commands.
    env_file = ROOT / '.env'
    if env_file.is_file():
        for line in env_file.read_text('utf-8-sig').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not sep or not re.fullmatch(r'KIDOBS_[A-Z0-9_]+', key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
                value = value[1:-1]
            if value:
                child_env.setdefault(key, value)
    return subprocess.call(command, cwd=ROOT, env=child_env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print('Startup failed:', str(exc), file=sys.stderr)
        raise SystemExit(1)
