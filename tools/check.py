"""Run isolated offline regression suites; never load an operator's data."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    for name in list(env):
        if name.startswith('KIDOBS_'):
            env.pop(name)
    with tempfile.TemporaryDirectory(prefix='kidobs-check-') as scratch:
        env['KIDOBS_DATA_DIR'] = scratch
        env['APPDATA'] = scratch
        env['LOCALAPPDATA'] = scratch
        subprocess.run([sys.executable, '-m', 'pip', 'check'], cwd=ROOT, env=env, check=True)
        subprocess.run([sys.executable, 'app/main.py', '--selftest'], cwd=ROOT, env=env, check=True)
        for test in sorted((ROOT / 'tests').glob('test_*.py')):
            print('\nChecking ' + test.name, flush=True)
            subprocess.run([sys.executable, str(test)], cwd=ROOT, env=env, check=True)
        node = shutil.which('node')
        if node:
            for test in sorted((ROOT / 'tests').glob('test_*.js')):
                subprocess.run([node, str(test)], cwd=ROOT, env=env, check=True)
        else:
            print('Node.js not found; frontend checks skipped.')
    print('All available checks passed.')


if __name__ == '__main__':
    main()
