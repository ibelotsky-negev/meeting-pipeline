#!/usr/bin/env python3
# Loop enforcement hook. ASCII only.
# Exit 0 = allow stop. Exit 2 = block stop, stderr fed back to Claude.
import hashlib, json, pathlib, subprocess, sys

MAX_BLOCKS = 5
STATE = pathlib.Path(".claude/.loop_state.json")

def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"attempts": 0, "last_hash": ""}

def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s))

def main():
    try:
        json.load(sys.stdin)  # consume hook payload
    except Exception:
        pass

    checks = [
        ("syntax", [sys.executable, "-m", "py_compile", "app.py"]),
        ("lint",   [sys.executable, "-m", "ruff", "check", ".", "--quiet"]),
        ("tests",  [sys.executable, "-m", "pytest", "-q", "-x", "--timeout=30"]),
    ]
    failures = []
    for name, cmd in checks:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            failures.append("[" + name + " FAILED]\n" + (r.stdout + r.stderr)[-1500:])

    if not failures:
        if STATE.exists():
            STATE.unlink()
        sys.exit(0)

    state = load_state()
    state["attempts"] += 1
    out = "\n".join(failures)
    h = hashlib.md5(out.encode()).hexdigest()
    repeat = (h == state.get("last_hash"))
    state["last_hash"] = h
    save_state(state)

    if state["attempts"] >= MAX_BLOCKS:
        # Limit reached: allow stop so the session can report what is broken.
        sys.exit(0)

    msg = out + "\nFix the root cause. Checks rerun automatically when you finish."
    if repeat:
        msg += "\nSAME ERROR TWICE IN A ROW. Stop guessing. Invoke @fixer or change diagnosis."
    sys.stderr.write(msg)
    sys.exit(2)

main()
