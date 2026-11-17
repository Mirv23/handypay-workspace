"""Vérifications de sécurité (mode local). Se lance avec tests/run_verify.py, EN DERNIER :

    .venv\\Scripts\\python.exe tests\\run_verify.py --port 8804 --extra tests\\verify_finances.py tests\\verify_relances.py tests\\verify_mobile.py tests\\verify_securite.py

Passe après les autres suites, sur le même serveur et la même base locale jetable. Les échecs de
connexion volontaires portent un X-Forwarded-For de test (203.0.113.x) : la limite par adresse IP du
poste de test (127.0.0.1) n'est pas entamée.

Couvre : en-têtes et CSP à nonce, CSRF sur TOUTES les routes qui modifient, session signée (forgée,
expirée, mauvaise version, compte retiré), adresse IP non falsifiable et blocage atomique par compte,
droits (observateur, membre, finances, projets restreints, porte de l'IA, webhook GitHub), injection
CSV dans les trois exports, fuites (journaux, fil d'activité), compatibilité avec PostgREST (416,
plafond de 1 000 lignes, faux client local aussi strict que la vraie base) et schéma SQL (RLS,
unicités attendues par le code).
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCR = Path(os.environ.get("VERIFY_DIR") or Path(__file__).parent)
BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8765")
TOKEN = (SCR / "ia_token.txt").read_text().strip()
OWNER_PW = re.search(r"Mot de passe : (\S+)", (SCR / "owner.txt").read_text(encoding="utf-8")).group(1)
SECRET = os.environ.get("SESSION_SECRET", "")
GH_SECRET = os.environ.get("WORKSPACE_GITHUB_SECRET", "")
TODAY = datetime.now(ZoneInfo("America/Port-au-Prince")).date()
TAG = secrets.token_hex(3)
results = []


def check(label, cond, detail=""):
    results.append((bool(cond), label, detail))
    print(("OK   " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))


def client(**kw):
    c = httpx.Client(base_url=BASE, timeout=30, **kw)
    r = c.get("/")
    assert r.status_code == 200, r.status_code
    return c


def h(c):
    return {"X-CSRF-Token": c.cookies.get("hp_csrf", "")}


def login(c, ident, pw, ip=None):
    headers = dict(h(c), **({"X-Forwarded-For": ip} if ip else {}))
    return c.post("/api/auth/login", json={"identifier": ident, "password": pw}, headers=headers)


def member(c, username, role="membre", **extra):
    r = c.post("/api/ws/members", json=dict({"username": username, "display_name": username.capitalize(), "role": role}, **extra), headers=h(c))
    assert r.status_code == 201, r.text
    return r.json()["password"]


def as_user(username, pw):
    c = client()
    assert login(c, username, pw).status_code == 200
    return c


for _ in range(40):
    try:
        httpx.get(BASE + "/healthz", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

mirv = client()
check("connexion du responsable", login(mirv, "mirv", OWNER_PW).status_code == 200)
SEC_M, SEC_O = f"secm{TAG}", f"seco{TAG}"
mem = as_user(SEC_M, member(mirv, SEC_M))
obs = as_user(SEC_O, member(mirv, SEC_O, role="observateur"))

# ═══ 1. En-têtes et CSP ══════════════════════════════════════════════════
anon = client()
p1, p2 = anon.get("/"), anon.get("/")
csp = p1.headers.get("content-security-policy", "")
script_src = next((d for d in csp.split(";") if d.strip().startswith("script-src")), "")
nonce = re.search(r"'nonce-([0-9a-f]{32})'", script_src)
check("page : script-src avec un nonce, sans 'unsafe-inline'", bool(nonce) and "unsafe-inline" not in script_src, script_src)
tags = re.findall(r"<script\b[^>]*>", p1.text)
exec_tags = [t for t in tags if "application/json" not in t]
check("page : chaque <script> exécutable porte le nonce de la réponse",
      bool(nonce) and exec_tags and all(f'nonce="{nonce.group(1)}"' in t for t in exec_tags), str(exec_tags)[:200])
n2 = re.search(r"'nonce-([0-9a-f]{32})'", p2.headers.get("content-security-policy", ""))
check("page : nonce différent à chaque réponse", bool(nonce and n2) and nonce.group(1) != n2.group(1))
markup = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", p1.text, flags=re.S)
inline = re.search(r"<[^>]+\son[a-z]+\s*=", markup)
check("page : aucun attribut on…= dans le HTML servi (la CSP le bloquerait)", not inline, inline.group(0)[:80] if inline else "")
pc = mirv.get("/compteur")
ccsp = pc.headers.get("content-security-policy", "")
cn = re.search(r"'nonce-([0-9a-f]{32})'", ccsp)
ctags = [t for t in re.findall(r"<script\b[^>]*>", pc.text) if "application/json" not in t]
check("/compteur : CSP à nonce, scripts marqués, bloc JSON intact",
      pc.status_code == 200 and cn and "unsafe-inline" not in ccsp.split("script-src", 1)[1].split(";")[0]
      and all(f'nonce="{cn.group(1)}"' in t for t in ctags) and '<script id="c-init" type="application/json">' in pc.text, ccsp)
redir = anon.get("/compteur", follow_redirects=False)
check("/compteur sans session (303) : CSP sans nonce", redir.status_code == 303 and "nonce-" not in redir.headers.get("content-security-policy", ""))
api = mirv.get("/api/ws/bootstrap")
hd = api.headers
check("API : CSP default-src 'none', frame-ancestors 'none', DENY, nosniff, no-referrer, no-store",
      hd.get("content-security-policy") == "default-src 'none'; frame-ancestors 'none'" and hd.get("x-frame-options") == "DENY"
      and hd.get("x-content-type-options") == "nosniff" and hd.get("referrer-policy") == "no-referrer" and "no-store" in hd.get("cache-control", ""),
      json.dumps(dict(hd))[:300])
check("local : pas de HSTS (HTTP)", "strict-transport-security" not in hd)

# Production (sans réseau) : HSTS, cookies Secure, CSP à nonce.
prod_code = r"""
import json
from fastapi.responses import Response
from app import auth, main
r = main._secure(Response(), "/", "ab12")
r2 = Response(); auth.issue_session(r2, "mirv", 1)
print(json.dumps({"headers": dict(r.headers), "cookies": r2.headers.getlist("set-cookie")}))
"""
penv = dict(os.environ, APP_ENV="production", SUPABASE_URL="https://exemple.invalid", SUPABASE_SERVICE_KEY="x", COOKIE_SECURE="")
pout = subprocess.run([sys.executable, "-c", prod_code], cwd=ROOT, env=penv, capture_output=True, text=True, encoding="utf-8")
try:
    prod = json.loads(pout.stdout.strip().splitlines()[-1])
except Exception:  # noqa: BLE001
    prod = {"headers": {}, "cookies": []}
ph, pck = prod["headers"], prod["cookies"]
check("production : HSTS (2 ans, sous-domaines)", ph.get("strict-transport-security") == "max-age=63072000; includeSubDomains", pout.stderr[-300:] or str(ph)[:200])
check("production : CSP de la page avec le nonce", "'nonce-ab12'" in ph.get("content-security-policy", ""))
check("production : cookies de session et CSRF « Secure », session HttpOnly, SameSite=Lax",
      len(pck) == 2 and all("Secure" in c and "samesite=lax" in c.lower() for c in pck)
      and any(c.startswith("hp_session=") and "HttpOnly" in c for c in pck) and any(c.startswith("hp_csrf=") and "HttpOnly" not in c for c in pck), str(pck))

# ═══ 2. CSRF sur toutes les routes qui modifient ═════════════════════════
from fastapi.routing import APIRoute  # noqa: E402

from app.main import app  # noqa: E402

def all_routes(routes):
    for route in routes:
        sub = getattr(route, "original_router", None)  # routeur inclus (FastAPI récent)
        yield from all_routes(sub.routes) if sub is not None else [route]


mutating = []
for route in all_routes(app.routes):
    if isinstance(route, APIRoute) and route.path.startswith(("/api/ws/", "/api/auth/")):
        for method in sorted(route.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            if route.path != "/api/auth/login":
                mutating.append((method, re.sub(r"\{[^}]+\}", str(uuid.uuid4()), route.path)))
check(f"CSRF : {len(mutating)} routes qui modifient trouvées (finances, relances, mobile compris)",
      len(mutating) > 60 and any("/finances/" in p for _, p in mutating) and any("/followups/" in p for _, p in mutating), str(len(mutating)))
no_header = [f"{m} {p}" for m, p in mutating if mirv.request(m, p, json={}).status_code != 403]
check("CSRF : sans en-tête X-CSRF-Token, chaque route → 403 (session valide pourtant)", not no_header, ", ".join(no_header[:5]))
bad = [f"{m} {p}" for m, p in mutating if mirv.request(m, p, json={}, headers={"X-CSRF-Token": "x" * 43}).status_code != 403]
check("CSRF : jeton faux (≠ cookie) → 403 partout", not bad, ", ".join(bad[:5]))
# Double soumission cohérente mais jeton étranger à la session (cookie hp_csrf posé par un tiers).
forged = client()
forged.cookies.set("hp_session", mirv.cookies.get("hp_session"))
forged.cookies.set("hp_csrf", "t" * 43)
r = forged.post("/api/ws/sales", json={"cards": 1}, headers={"X-CSRF-Token": "t" * 43})
check("CSRF : cookie = en-tête mais pas le jeton de la session → 403", r.status_code == 403, r.text)
r = forged.post("/api/auth/logout", headers={"X-CSRF-Token": "t" * 43})
check("CSRF : déconnexion forcée depuis un autre site impossible (403)", r.status_code == 403 and mirv.get("/api/ws/bootstrap").status_code == 200, r.text)

# ═══ 3. Session signée ═══════════════════════════════════════════════════
from app import security  # noqa: E402


def with_cookie(value):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.cookies.set("hp_session", value)
    return c


ok_cookie = security.sign_session(SECRET, "mirv", 1, 600, "c" * 43)
check("session fabriquée avec le bon secret → 200 (contrôle du test)", with_cookie(ok_cookie).get("/api/ws/bootstrap").status_code == 200)
check("session expirée → 401", with_cookie(security.sign_session(SECRET, "mirv", 1, -60, "c" * 43)).get("/api/ws/bootstrap").status_code == 401)
check("session d'une ancienne version (mot de passe changé) → 401", with_cookie(security.sign_session(SECRET, "mirv", 99, 600, "c" * 43)).get("/api/ws/bootstrap").status_code == 401)
check("session signée avec un autre secret → 401", with_cookie(security.sign_session("z" * 48, "mirv", 1, 600, "c" * 43)).get("/api/ws/bootstrap").status_code == 401)
body, _, sig = ok_cookie.partition(".")
tampered = security._b64(json.dumps({"u": "mirv", "v": 1, "exp": int(time.time()) + 600, "c": "d" * 43}).encode()) + "." + sig
check("charge modifiée, signature d'origine → 401", with_cookie(tampered).get("/api/ws/bootstrap").status_code == 401)
check("session au nom de l'IA → 403 (nom réservé)", with_cookie(security.sign_session(SECRET, "ia", 1, 600, "c" * 43)).get("/api/ws/bootstrap").status_code in (401, 403))
SEC_OFF = f"secx{TAG}"
off = as_user(SEC_OFF, member(mirv, SEC_OFF))
mirv.delete(f"/api/ws/members/{SEC_OFF}", headers=h(mirv))
check("compte retiré : sa session ouverte tombe aussitôt (401)", off.get("/api/ws/bootstrap").status_code == 401)

# ═══ 4. Connexion : adresse IP et blocage ════════════════════════════════
import starlette.requests  # noqa: E402
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

from app import auth  # noqa: E402


def through_proxy(headers):
    """La pile de production : uvicorn --proxy-headers --forwarded-allow-ips='*' puis auth.client_ip."""
    seen = {}

    async def inner(scope, receive, send):
        seen["uvicorn"] = scope["client"][0]
        seen["app"] = auth.client_ip(starlette.requests.Request(scope))

    scope = {"type": "http", "client": ("10.21.157.68", 4321), "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    asyncio.run(ProxyHeadersMiddleware(inner, trusted_hosts="*")(scope, None, None))
    return seen


s = through_proxy([("X-Forwarded-For", "6.6.6.6, 99.61.165.29, 104.22.17.40"), ("CF-Connecting-IP", "99.61.165.29")])
check("IP : derrière Render, uvicorn retient l'adresse écrite par le client, la limite retient CF-Connecting-IP",
      s == {"uvicorn": "6.6.6.6", "app": "99.61.165.29"}, str(s))
s = through_proxy([("X-Forwarded-For", "6.6.6.6, 99.61.165.29")])
check("IP : un seul proxy, sans Cloudflare → dernière adresse de X-Forwarded-For", s["app"] == "99.61.165.29", str(s))
check("IP : une adresse IPv6 compte pour son /64 (l'abonné en change à volonté), IPv4 telle quelle",
      auth.ip_bucket("2001:db8:1:2::1") == auth.ip_bucket("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
      and auth.ip_bucket("2001:db8:1:3::1") != auth.ip_bucket("2001:db8:1:2::1") and auth.ip_bucket("203.0.113.9") == "203.0.113.9"
      and auth.ip_bucket("::ffff:203.0.113.9") == "203.0.113.9", auth.ip_bucket("2001:db8:1:2::1"))
s = through_proxy([("X-Forwarded-For", "pas-une-ip")])
check("IP : en-tête illisible → clé commune, jamais le texte du client", s["app"] == "?" and s["uvicorn"] == "pas-une-ip", str(s))

ipc = client()
codes = [login(ipc, f"inconnu{TAG}{i}", "x", ip="203.0.113.50").status_code for i in range(60)]
check("IP : 60 échecs (identifiants différents) depuis une adresse → 401 à chaque fois", codes == [401] * 60, str(sorted(set(codes))))
check("IP : 61e essai depuis cette adresse → 429", login(ipc, f"autre{TAG}", "x", ip="203.0.113.50").status_code == 429)
check("IP : préfixer un faux X-Forwarded-For ne contourne pas la limite (429)",
      login(ipc, f"autre{TAG}b", "x", ip="198.18.0.1, 203.0.113.50").status_code == 429)
check("IP : une autre adresse n'est pas touchée (401)", login(ipc, f"autre{TAG}c", "x", ip="203.0.113.51").status_code == 401)
r = ipc.post("/api/auth/login", json={"identifier": f"autre{TAG}d", "password": "x"},
             headers=dict(h(ipc), **{"X-Forwarded-For": "203.0.113.50", "CF-Connecting-IP": "203.0.113.52"}))
check("IP : CF-Connecting-IP (Cloudflare) prime sur X-Forwarded-For", r.status_code == 401, r.text)

SEC_R = f"secr{TAG}"
pw_r = member(mirv, SEC_R)
racers = [client() for _ in range(16)]
with ThreadPoolExecutor(max_workers=16) as pool:
    burst = list(pool.map(lambda c: login(c, SEC_R, "mauvais-mot-de-passe", ip="203.0.113.60").status_code, racers))
r = login(client(), SEC_R, pw_r, ip="203.0.113.60")
check("blocage : 16 essais lancés en même temps → le compte est bloqué ensuite (429, même avec le bon mot de passe)",
      r.status_code == 429 and all(c in (401, 429) for c in burst), f"{burst} puis {r.status_code}")
r = mirv.post(f"/api/ws/members/{SEC_R}/password", headers=h(mirv))
check("blocage : un nouveau mot de passe donné par un responsable le lève (200)",
      r.status_code == 200 and login(client(), SEC_R, r.json()["password"], ip="203.0.113.60").status_code == 200, r.text[:200])
SEC_Q = f"secq{TAG}"
pw_q = member(mirv, SEC_Q)
qc = client()
codes = [login(qc, SEC_Q, "faux", ip="203.0.113.61").status_code for _ in range(8)]
locked_q = login(qc, SEC_Q, pw_q, ip="203.0.113.61").status_code
cli = subprocess.run([sys.executable, "manage.py", "reset-password", "--username", SEC_Q], cwd=ROOT, env=os.environ.copy(),
                     capture_output=True, text=True, encoding="utf-8")
new_q = re.search(r"Nouveau mot de passe pour \S+ : (\S+)", cli.stdout)
check("blocage : « manage.py reset-password » (autre processus) le lève aussi",
      codes == [401] * 8 and locked_q == 429 and new_q and login(client(), SEC_Q, new_q.group(1), ip="203.0.113.61").status_code == 200,
      f"{codes} {locked_q} {cli.stdout[-200:]} {cli.stderr[-200:]}")

# ═══ 5. Droits ═══════════════════════════════════════════════════════════
# Finances : observateur exclu, membre en lecture (proposer seulement).
for path in ("/api/ws/finances", "/api/ws/finances/debt", "/api/ws/settings/finances", "/api/ws/finances/export.csv"):
    check(f"observateur : GET {path} → 403", obs.get(path).status_code == 403)
check("observateur : proposer une dépense → 403",
      obs.post("/api/ws/finances/expenses", json={"day": TODAY.isoformat(), "category": "Test", "amount": 10}, headers=h(obs)).status_code == 403)
check("membre : lit le relevé (200)", mem.get("/api/ws/finances").status_code == 200)
cfg = mirv.get("/api/ws/settings/finances").json()["settings"]
check("membre : réglages des finances → 403", mem.put("/api/ws/settings/finances", json=cfg, headers=h(mem)).status_code == 403)
check("membre : versement → 403", mem.post("/api/ws/finances/payouts", json={"username": SEC_M, "month": TODAY.isoformat()[:7], "kind": "prime",
                                                                            "day": TODAY.isoformat(), "amount": 5, "method": "especes"}, headers=h(mem)).status_code == 403)
check("membre : export du relevé → 403", mem.get("/api/ws/finances/export.csv").status_code == 403)
r = mem.post("/api/ws/finances/expenses", json={"day": TODAY.isoformat(), "category": "Test", "amount": 10}, headers=h(mem))
check("membre : propose une dépense (201, en attente)", r.status_code == 201 and r.json()["expense"]["status"] == "en_attente", r.text[:200])
eid = r.json()["expense"]["id"] if r.status_code == 201 else str(uuid.uuid4())
check("membre : valide une dépense → 403", mem.post(f"/api/ws/finances/expenses/{eid}/validate", headers=h(mem)).status_code == 403)
check("finances : montant < 1 centime refusé (422, pas un 503 de la contrainte CHECK)",
      mem.post("/api/ws/finances/expenses", json={"day": TODAY.isoformat(), "category": "Test", "amount": 0.001}, headers=h(mem)).status_code == 422)

# Objectif : un observateur désigné responsable ne le supprime pas.
r = mirv.post("/api/ws/objectives", json={"title": f"Objectif {TAG}", "owner_username": SEC_O}, headers=h(mirv))
oid = r.json()["objective"]["id"]
check("observateur responsable d'un objectif : suppression → 403", obs.delete(f"/api/ws/objectives/{oid}", headers=h(obs)).status_code == 403)
mirv.delete(f"/api/ws/objectives/{oid}", headers=h(mirv))

# Projet restreint : tout identifiant hors périmètre répond 404.
team_p = mirv.post("/api/ws/projects", json={"name": f"Équipe {TAG}"}, headers=h(mirv)).json()["project"]
secret_p = mirv.post("/api/ws/projects", json={"name": f"Restreint {TAG}", "visibility": "restreint"}, headers=h(mirv)).json()["project"]
st = mirv.post("/api/ws/tasks", json={"project_id": secret_p["id"], "title": f"Tâche secrète {TAG}", "description": f"Pour @{SEC_M}"}, headers=h(mirv)).json()["task"]
sdoc = mirv.post("/api/ws/docs", json={"title": f"Doc secret {TAG}", "project_id": secret_p["id"]}, headers=h(mirv)).json()["doc"]
gone = mirv.post("/api/ws/docs", json={"title": f"Doc effacé {TAG}", "project_id": secret_p["id"]}, headers=h(mirv)).json()["doc"]
check("restreint : membre hors projet, GET de la tâche → 404", mem.get(f"/api/ws/tasks/{st['id']}").status_code == 404)
check("restreint : PATCH de la tâche → 404", mem.patch(f"/api/ws/tasks/{st['id']}", json={"title": "x"}, headers=h(mem)).status_code == 404)
check("restreint : commentaire → 404", mem.post(f"/api/ws/tasks/{st['id']}/comments", json={"body": "x"}, headers=h(mem)).status_code == 404)
check("restreint : suivre la tâche → 404", mem.post(f"/api/ws/tasks/{st['id']}/watch", json={"watch": True}, headers=h(mem)).status_code == 404)
check("restreint : corbeille → 404", mem.delete(f"/api/ws/tasks/{st['id']}", headers=h(mem)).status_code == 404)
check("restreint : document → 404", mem.get(f"/api/ws/docs/{sdoc['id']}").status_code == 404)
check("restreint : modifier le projet → 404", mem.patch(f"/api/ws/projects/{secret_p['id']}", json={"name": "x"}, headers=h(mem)).status_code == 404)
check("restreint : créer une tâche dedans → 404",
      mem.post("/api/ws/tasks", json={"project_id": secret_p["id"], "title": "x"}, headers=h(mem)).status_code == 404)
check("restreint : créer un document dedans → 404",
      mem.post("/api/ws/docs", json={"title": "x", "project_id": secret_p["id"]}, headers=h(mem)).status_code == 404)
own = mem.post("/api/ws/tasks", json={"project_id": team_p["id"], "title": f"À moi {TAG}"}, headers=h(mem)).json()["task"]
check("restreint : déplacer sa tâche dedans → 404",
      mem.patch(f"/api/ws/tasks/{own['id']}", json={"project_id": secret_p["id"]}, headers=h(mem)).status_code == 404)
r = mem.post("/api/ws/tasks/bulk", json={"ids": [st["id"]], "changes": {"status": "fait"}}, headers=h(mem))
check("restreint : action groupée → comptée « introuvable », tâche intacte",
      r.status_code == 200 and r.json()["missing"] == 1 and mirv.get(f"/api/ws/tasks/{st['id']}").json()["task"]["status"] == "a_faire", r.text[:200])
check("restreint : absente de la liste et de l'export du membre",
      st["id"] not in {t["id"] for t in mem.get("/api/ws/tasks").json()["tasks"]} and f"Tâche secrète {TAG}" not in mem.get("/api/ws/export.csv").text)
check("restreint : la mention ne notifie pas un membre hors projet",
      not any(f"Tâche secrète {TAG}" in (n.get("text") or "") for n in mem.get("/api/ws/notifications").json()["notifications"]))
mirv.delete(f"/api/ws/docs/{gone['id']}", headers=h(mirv))
feed_m, feed_o = mem.get("/api/ws/activity").text, obs.get("/api/ws/activity").text
leaks = [s for s in (f"Tâche secrète {TAG}", f"Doc secret {TAG}", f"Doc effacé {TAG}", f"Restreint {TAG}") if s in feed_m or s in feed_o]
check("fil d'activité : rien du projet restreint (ni tâche, ni document supprimé) pour un membre ou un observateur", not leaks, str(leaks))
check("fil d'activité : la suppression reste visible du responsable", f"Doc effacé {TAG}" in mirv.get("/api/ws/activity").text)

# Porte de l'IA : jeton, portée.
ia = httpx.Client(base_url=BASE, timeout=30)
auth_h = {"Authorization": "Bearer " + TOKEN}
check("porte IA : jeton faux de même longueur → 401", ia.get("/api/workspace/ia/matin", headers={"Authorization": "Bearer " + "x" * len(TOKEN)}).status_code == 401)
check("porte IA : « bearer » en minuscules accepté (200)", ia.get("/api/workspace/ia/matin", headers={"Authorization": "bearer " + TOKEN}).status_code == 200)
check("porte IA : le jeton n'ouvre pas l'API de la page (/api/ws → 401)", ia.get("/api/ws/bootstrap", headers=auth_h).status_code == 401)
check("porte IA : tâche d'un projet restreint → 404", ia.get(f"/api/workspace/ia/taches/HP-{st['number']}", headers=auth_h).status_code == 404)
r = ia.patch(f"/api/workspace/ia/taches/HP-{own['number']}", json={"statut": "fait"}, headers=auth_h)
check("porte IA : changer le statut d'une tâche qui n'est pas la sienne → 403", r.status_code == 403, r.text[:200])

# Webhook GitHub : signature, rejeu, décisions.


def gh(payload, event="push", delivery=None, sign=True, secret=None):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new((secret or GH_SECRET).encode(), raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid.uuid4())}
    if sign:
        headers["X-Hub-Signature-256"] = sig
    return ia.post("/api/workspace/github", content=raw, headers=headers)


dec = mirv.post("/api/ws/tasks", json={"project_id": team_p["id"], "title": f"Décision {TAG}", "labels": ["décision"]}, headers=h(mirv)).json()["task"]
plain = mirv.post("/api/ws/tasks", json={"project_id": team_p["id"], "title": f"Simple {TAG}"}, headers=h(mirv)).json()["task"]


def push(n):
    return {"ref": "refs/heads/main", "repository": {"default_branch": "main", "full_name": "org/depot"},
            "commits": [{"id": secrets.token_hex(20), "message": f"fixes HP-{n}", "url": "https://github.com/org/depot/commit/x",
                         "author": {"username": "quelquun", "name": "Quelqu'un"}}]}


check("webhook : sans signature → 401", gh(push(plain["number"]), sign=False).status_code == 401)
check("webhook : signature d'un autre secret → 401", gh(push(plain["number"]), secret="autre-secret").status_code == 401)
d = str(uuid.uuid4())
r1, r2 = gh(push(plain["number"]), delivery=d), gh(push(plain["number"]), delivery=d)
check("webhook : livraison signée traitée, rejouée (même X-GitHub-Delivery) → ignorée",
      r1.status_code == 200 and r1.json().get("linked") == 1 and r2.json().get("duplicate") is True, r1.text + r2.text)
check("webhook : « fixes HP-n » ferme une tâche ordinaire (contrôle du test)",
      mirv.get(f"/api/ws/tasks/{plain['id']}").json()["task"]["status"] == "fait")
gh(push(dec["number"]))
check("webhook : « fixes HP-n » ne ferme JAMAIS une décision",
      mirv.get(f"/api/ws/tasks/{dec['id']}").json()["task"]["status"] != "fait")

# ═══ 6. Injection CSV dans les trois exports ═════════════════════════════
evil = "=HYPERLINK(\"http://exemple.invalid\",\"x\")"
mirv.post("/api/ws/tasks", json={"project_id": team_p["id"], "title": evil + TAG}, headers=h(mirv))
csv_t = mirv.get("/api/ws/export.csv").text
check("export des tâches : cellule « = » neutralisée (')", "'" + evil.replace('"', '""') + TAG in csv_t and "\n" + evil not in csv_t)
r = mirv.post("/api/ws/followups", json={"name": "@SUM(1+1) " + TAG, "phone": f"+1 202 555 {secrets.randbelow(9000) + 1000}", "notes": "-2+3 " + TAG,
                                         "source": "+cmd"}, headers=h(mirv))
fo = r.json()["followup"]
csv_f = mirv.get("/api/ws/followups-export.csv").text
check("export des fiches : « @ », « - », « + » (et le numéro +…) neutralisés",
      r.status_code == 201 and f"'@SUM(1+1) {TAG}" in csv_f and f"'-2+3 {TAG}" in csv_f and "'+cmd" in csv_f and "'" + fo["phone"] in csv_f, r.text[:200])
fin_cfg = mirv.get("/api/ws/settings/finances").json()["settings"]
evil_cfg = dict(fin_cfg, fixed_charges=fin_cfg["fixed_charges"] + [{"label": "=1+2" + TAG[:3], "amount": 1, "note": ""}])
r = mirv.put("/api/ws/settings/finances", json=evil_cfg, headers=h(mirv))
csv_r = mirv.get("/api/ws/finances/export.csv").text
mirv.put("/api/ws/settings/finances", json=fin_cfg, headers=h(mirv))
check("export du relevé : libellé de charge « = » neutralisé",
      r.status_code == 200 and "'=1+2" + TAG[:3] in csv_r and ";=1+2" not in csv_r and "\n=1+2" not in csv_r, r.text[:200])

# ═══ 7. Fuites : journaux, fil d'activité, compteur ══════════════════════
probe = f"3412{secrets.randbelow(9000) + 1000}"
mirv.get("/api/ws/followups", params={"view": "all", "q": probe})
mirv.get(f"/api/ws/followups/509{probe}")
time.sleep(0.5)
log = (SCR / "server.log").read_text(encoding="utf-8", errors="replace") if (SCR / "server.log").exists() else ""
check("journaux : aucun numéro (ni recherche ?q=…, ni chemin)", log and probe not in log and fo["phone"].lstrip("+") not in log.replace(" ", ""), probe)
feed = obs.get("/api/ws/activity").text
check("fil d'activité de l'observateur : aucun numéro ni nom de client",
      "+509" not in feed and f"@SUM(1+1) {TAG}" not in feed and fo["phone"] not in feed)
cnt = mem.get("/api/ws/compteur").text
check("compteur : aucun nom ni numéro de client", fo["phone"] not in cnt and fo["phone"][1:] not in cnt and TAG not in cnt and "+509" not in cnt)

# ═══ 8. Compatibilité PostgREST / Supabase ═══════════════════════════════
r = mirv.get("/api/ws/followups", params={"view": "all", "offset": 100000, "limit": 50})
check("relances : page au-delà du total → 200 et liste vide (PostgREST répondrait 416 au compte exact)",
      r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] >= 1, r.text[:200])
r = mirv.get("/api/ws/followups", params={"view": "mine", "offset": 50, "limit": 50})
check("relances : « Ma file » page 2 vidée → 200", r.status_code == 200 and r.json()["items"] == [], r.text[:200])

big = []
for path in sorted((ROOT / "app").glob("*.py")):
    code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("#"))
    for m in re.finditer(r"\.limit\(([^)]*)\)", code):
        arg = m.group(1).replace("_", "")
        if "TASKLIMIT" in arg.upper() or (arg.isdigit() and int(arg) > 1000):
            big.append(f"{path.name}: .limit({m.group(1)})")
check("aucune lecture .limit(> 1 000) : Supabase plafonne à 1 000 lignes (lire page par page)", not big, ", ".join(big))

from app import localdb  # noqa: E402

sql_text = "\n".join((ROOT / "sql" / n).read_text(encoding="utf-8") for n in localdb.SQL_FILES)
sql_cols = localdb.read_sql_columns()
drift = [t for t in set(sql_cols) | set(localdb.SCHEMA) if set(sql_cols.get(t, {})) != set(localdb.SCHEMA.get(t, {}).get("columns", {}))]
check(f"schéma : faux client local = sql/*.sql ({len(sql_cols)} tables, mêmes colonnes)", len(sql_cols) == 27 and not drift, str(drift))
rls = set()
for block in re.findall(r"do \$\$(.*?)end \$\$;", sql_text, re.S):
    if all(s in block for s in ("enable row level security", "force row level security", "revoke all on table public.%I from anon, authenticated")):
        rls |= set(re.findall(r"'(\w+)'", re.search(r"array\[(.*?)\]", block, re.S).group(1)))
created = set(re.findall(r"create table if not exists public\.(\w+)", sql_text))
check("schéma : RLS activée ET forcée, droits retirés à anon/authenticated, sur chaque table", created and created <= rls, str(created - rls))
check("schéma : aucune policy (la clé anon ne lit rien)", "create policy" not in sql_text.lower())
uniques = {
    "ventes.external_id": r"external_id text unique",
    "fiches.phone": r"phone text not null unique",
    "accords (expense_id, username)": r"unique \(expense_id, username\)",
    "notes du soir (username, day)": r"unique \(username, day\)",
    "liens GitHub (task_id, kind, ref)": r"unique \(task_id, kind, ref\)",
    "tâches.number": r"create unique index if not exists workspace_tasks_number_idx on public\.workspace_tasks \(number\)",
    "comptes.whatsapp": r"whatsapp text unique",
}
missing = [k for k, pat in uniques.items() if not re.search(pat, sql_text)]
check("schéma : contraintes d'unicité attendues par le code présentes", not missing, ", ".join(missing))


def refused(fn):
    try:
        fn()
    except (localdb.LocalAPIError, ValueError):
        return True
    return False


ldb = localdb.LocalClient(Path(tempfile.mkdtemp(prefix="hp-secu-")))
S = "workspace_sales"
check("faux client : colonne inconnue (select, filtre, order) refusée",
      refused(lambda: ldb.table(S).select("id,inconnue").execute()) and refused(lambda: ldb.table(S).select("*").eq("inconnue", 1).execute())
      and refused(lambda: ldb.table(S).select("*").order("inconnue").execute()))
check("faux client : opérateur inconnu dans or_ refusé", refused(lambda: ldb.table(S).select("*").or_("day.lte.2026-11-17,day.environ.x").execute()))
check("faux client : uuid ou date invalides refusés (filtre et écriture)",
      refused(lambda: ldb.table(S).select("*").eq("id", "abc").execute()) and refused(lambda: ldb.table(S).select("*").gte("day", "hier").execute())
      and refused(lambda: ldb.table(S).insert({"day": TODAY.isoformat(), "followup_id": ""}).execute()))
check("faux client : NULL dans une colonne not null refusé", refused(lambda: ldb.table(S).insert({"cards": 1}).execute()))
check("faux client : NaN refusé", refused(lambda: ldb.table("workspace_tasks").update({"position": float("nan")}).eq("id", str(uuid.uuid4())).execute()))
check("faux client : upsert sans contrainte d'unicité sur on_conflict refusé",
      refused(lambda: ldb.table("workspace_fin_approvals").upsert({"expense_id": str(uuid.uuid4()), "username": "x"}, on_conflict="username").execute()))
check("faux client : update / delete sans filtre refusés",
      refused(lambda: ldb.table(S).update({"note": "x"}).execute()) and refused(lambda: ldb.table(S).delete().execute()))
ldb.table(S).insert([{"day": TODAY.isoformat(), "cards": 1} for _ in range(1005)]).execute()
res = ldb.table(S).select("id", count="exact").limit(5000).execute()
check("faux client : 1 000 lignes au plus par lecture, comme Supabase", len(res.data) == 1000 and res.count == 1005, f"{len(res.data)} / {res.count}")
check("faux client : décalage au-delà du total avec compte exact → refusé (416)",
      refused(lambda: ldb.table(S).select("id", count="exact").range(2000, 2010).execute())
      and len(ldb.table(S).select("id").range(2000, 2010).execute().data) == 0)
check("faux client : insertion multiple, colonne omise par une ligne → NULL (donc refusée si not null)",
      refused(lambda: ldb.table(S).insert([{"day": TODAY.isoformat(), "note": "a"}, {"day": TODAY.isoformat()}]).execute()))

# ═══ 9. Page : déconnexion ═══════════════════════════════════════════════
src = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
lg = re.search(r"async function logout\(\)\{(.*?)\}\s*\n?\s*\$\(\"#logout-btn\"\)", src, re.S)
check("page : la déconnexion recharge la page (rien de la session précédente ne reste)", bool(lg) and "location.reload()" in lg.group(1))

ok = sum(1 for x in results if x[0])
print(f"\n{ok}/{len(results)} vérifications réussies")
sys.exit(0 if ok == len(results) else 1)
