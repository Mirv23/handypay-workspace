"""Lance les vérifications de bout en bout sur une base locale neuve et jetable.

    .venv\\Scripts\\python.exe tests\\run_verify.py            (port 8765)
    .venv\\Scripts\\python.exe tests\\run_verify.py --port 8801

Tout se passe dans un dossier temporaire : aucune donnée n'est écrite dans le
projet, le `.env` du projet n'est pas lu (APP_ENV=local, SUPABASE_URL vide), et
le serveur est arrêté à la fin. Code de sortie 0 seulement si tout passe.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="hp-verify-"))
    data = work / "data"
    ia_token = secrets.token_urlsafe(40)
    (work / "ia_token.txt").write_text(ia_token, encoding="utf-8")

    env = dict(os.environ)
    env.update(
        APP_ENV="local",
        SUPABASE_URL="",
        SUPABASE_SERVICE_KEY="",
        LOCAL_DATA_DIR=str(data),
        SESSION_SECRET=secrets.token_urlsafe(48),
        WORKSPACE_IA_TOKEN=ia_token,
        WORKSPACE_GITHUB_SECRET=secrets.token_urlsafe(32),
        WORKSPACE_DIGEST_INTERVAL_S="0",
        VERIFY_DIR=str(work),
        VERIFY_BASE=f"http://127.0.0.1:{args.port}",
        PYTHONIOENCODING="utf-8",
    )

    owner = subprocess.run(
        [PY, "manage.py", "create-owner", "--username", "mirv", "--name", "Mirv", "--whatsapp", "3412 0001"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    if owner.returncode != 0:
        print(owner.stdout, owner.stderr)
        return 1
    (work / "owner.txt").write_text(owner.stdout, encoding="utf-8")

    log = open(work / "server.log", "w", encoding="utf-8")
    srv = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--port", str(args.port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{args.port}/healthz", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001 - le serveur démarre
                pass
            time.sleep(0.5)
        res = subprocess.run([PY, str(ROOT / "tests" / "verify_api.py")], env=env, text=True, encoding="utf-8",
                             capture_output=True)
        print(res.stdout)
        if res.stderr.strip():
            print(res.stderr[-3000:])
        return 0 if res.returncode == 0 and "FAIL" not in res.stdout else 1
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:  # noqa: BLE001
            srv.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
