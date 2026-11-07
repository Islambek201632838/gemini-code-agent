"""Хук Stop: пока тесты красные, агент не может закончить. Код выхода 2 возвращает модель к работе (один раз)."""
import subprocess
import sys

run = subprocess.run([sys.executable, "-m", "unittest"], capture_output=True, text=True)
if run.returncode != 0:
    print("The test suite is failing; fix it before finishing:\n" + run.stderr[-1500:], file=sys.stderr)
    sys.exit(2)
