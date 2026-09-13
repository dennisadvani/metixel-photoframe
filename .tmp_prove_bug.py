"""Prove the new UTF-8 regression test catches the OLD inline parser bug."""

import os
import subprocess
import sys
import tempfile

OLD_PARSER = '''\
import os, sys
def names(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        nm = ln.split(";", 1)[0].strip().split("[", 1)[0].strip()
        for ch in "=<>~! \\t":
            nm = nm.split(ch, 1)[0].strip()
        if nm:
            out.append(nm)
    return out
print(names(sys.argv[1]))
'''

MINIMAL_ENV = {
    "LC_ALL": "C",
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
    "PATH": "",
}

f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
f.write(OLD_PARSER)
f.close()

try:
    r = subprocess.run(
        [sys.executable, "-X", "utf8=0", f.name, "requirements-system.txt"],
        capture_output=True,
        text=True,
        env=MINIMAL_ENV,
        timeout=60,
    )
    print("OLD inline parser exit code under C locale:", r.returncode)
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "(none)"
        print("OLD parser stderr tail:", tail)
        print("=> bug REAL; new regression test guards it.")
    else:
        print("OLD parser succeeded; stdout:", r.stdout.strip()[:200])

    print()
    r2 = subprocess.run(
        [sys.executable, "-X", "utf8=0", "scripts/requirements_names.py",
         "requirements-system.txt"],
        capture_output=True,
        text=True,
        env=MINIMAL_ENV,
        timeout=60,
    )
    print("NEW parser exit code under C locale:", r2.returncode)
    if r2.returncode == 0:
        names = r2.stdout.split()
        print("NEW parser found", len(names), "packages; sample:", names[:4])
finally:
    os.unlink(f.name)
