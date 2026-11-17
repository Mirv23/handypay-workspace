"""Vérification de bout en bout du module Finances (mode local).

Se lance après verify_api.py, sur le même serveur :
    .venv\\Scripts\\python.exe tests\\run_verify.py --port 8803 --extra tests\\verify_finances.py

Les ventes des mois de contrôle sont envoyées par la porte de l'IA, dans des mois
passés (9 à 4 mois avant aujourd'hui) : les chiffres de verify_api.py (mois en
cours) ne s'en mêlent pas.
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

SCR = Path(os.environ.get("VERIFY_DIR") or Path(__file__).parent)
BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8765")
TOKEN = (SCR / "ia_token.txt").read_text().strip()
OWNER_PW = re.search(r"Mot de passe : (\S+)", (SCR / "owner.txt").read_text(encoding="utf-8")).group(1)
TODAY = datetime.now(ZoneInfo("America/Port-au-Prince")).date()
NBSP = " "
results = []


def check(label, cond, detail=""):
    results.append((bool(cond), label, detail))
    print(("OK   " if cond else "FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))


def client():
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.get("/")
    assert r.status_code == 200, r.status_code
    return c


def h(c):
    return {"X-CSRF-Token": c.cookies.get("hp_csrf", "")}


def login(c, ident, pw):
    return c.post("/api/auth/login", json={"identifier": ident, "password": pw}, headers=h(c))


def shift(key, n):
    y, m = int(key[:4]), int(key[5:7]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def by_user(st, u):
    return next((p for p in st["people"] if p["username"] == u), {})


def step(st, key):
    return next(s for s in st["steps"] if s["key"] == key)


for _ in range(40):
    try:
        httpx.get(BASE + "/healthz", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

CUR = f"{TODAY.year:04d}-{TODAY.month:02d}"
M0, M1, M2, M3, M4, M5 = (shift(CUR, n) for n in (-9, -8, -7, -6, -5, -4))

# ── L'équipe : 4 personnes payées (Mirv, Michael, Handy, Wydlyn) et un observateur ─
mirv = client()
check("connexion du responsable", login(mirv, "mirv", OWNER_PW).status_code == 200)
pw = {}
for u, n, role in (("handy", "Handy", "membre"), ("wydlyn", "Wydlyn", "membre"), ("lecteur", "Lecteur", "observateur")):
    r = mirv.post("/api/ws/members", json={"username": u, "display_name": n, "role": role}, headers=h(mirv))
    pw[u] = r.json().get("password") if r.status_code == 201 else \
        mirv.post(f"/api/ws/members/{u}/password", headers=h(mirv)).json().get("password")
pw["michael"] = mirv.post("/api/ws/members/michael/password", headers=h(mirv)).json().get("password")
sess = {"mirv": mirv}
for u in ("michael", "handy", "wydlyn", "lecteur"):
    sess[u] = client()
    login(sess[u], u, pw[u])
mic, obs = sess["michael"], sess["lecteur"]

# ── Réglages : valeurs de départ, lecture par un membre ─────────────
r = mic.get("/api/ws/settings/finances")
cfg = r.json().get("settings", {})
check("réglages de départ lisibles par un membre : 4 charges (1 920 $), 500 $, dette 5 000 $ à 1 000 $/mois, réserve 10 %, primes 50 %, accord au-dessus de 200 $",
      r.status_code == 200 and sum(c["amount"] for c in cfg["fixed_charges"]) == 1920 and cfg["default_salary"] == 500
      and cfg["debt_initial"] == 5000 and cfg["debt_monthly"] == 1000 and cfg["reserve_pct"] == 10
      and cfg["bonus_pct"] == 50 and cfg["approval_threshold"] == 200 and cfg["salaries"] is None, json.dumps(cfg)[:300])
r = mic.put("/api/ws/settings/finances", json=dict(cfg, start_month=M0), headers=h(mic))
check("un membre ne change pas les réglages → 403", r.status_code == 403, r.text)
r = mirv.put("/api/ws/settings/finances", json=dict(cfg, start_month=M0))
check("réglages sans jeton CSRF → 403", r.status_code == 403, r.text)
r = mirv.put("/api/ws/settings/finances", json=dict(cfg, start_month=M0, salaries=[{"username": "inconnu", "amount": 500}]), headers=h(mirv))
check("salaire pour une personne inconnue → 400", r.status_code == 400, r.text)
r = mirv.put("/api/ws/settings/finances", json=dict(cfg, start_month=M0), headers=h(mirv))
check(f"le responsable fixe le début du suivi ({M0})", r.status_code == 200 and r.json()["settings"]["start_month"] == M0, r.text)

# ── Ventes des mois de contrôle, par la porte de l'IA ───────────────
ia = httpx.Client(base_url=BASE, timeout=60, headers={"Authorization": "Bearer " + TOKEN})


def sell(month, cards, tag):
    lots, left, i = [], cards, 0
    while left > 0:
        n = min(500, left)
        left -= n
        i += 1
        lots.append({"external_id": f"fin-{tag}-{i}", "cartes": n, "membre": ("michael", "mirv", "handy")[i % 3], "jour": f"{month}-1{i % 9}"})
    return ia.post("/api/workspace/ia/ventes/lot", json={"ventes": lots})


for month, cards, tag in ((M0, 1000, "m0"), (M1, 3000, "m1"), (M2, 300, "m2"), (M3, 1000, "m3")):
    r = sell(month, cards, tag)
    check(f"porte IA : {cards} cartes vendues en {month}", r.status_code == 200 and sum(v["cartes"] for v in r.json()["ventes"]) == cards, r.text[:200])

# ── Seuil : 547 cartes, 436 sans dette ──────────────────────────────
s0 = mirv.get("/api/ws/finances", params={"mois": M0}).json()
check("seuil avec les valeurs de départ : 547 cartes par mois", s0["threshold"]["cards"] == 547, json.dumps(s0["threshold"]))
check("seuil une fois la dette remboursée : 436 cartes", s0["threshold"]["cards_without_debt"] == 436, json.dumps(s0["threshold"]))
check("explication en une phrase, montants « 1 920 $ » (espace insécable)",
      f"1{NBSP}920{NBSP}$" in step(s0, "charges")["explain"] and all(s["explain"] for s in s0["steps"]), step(s0, "charges")["explain"])

# ── Contrôle à 1 000 cartes ─────────────────────────────────────────
mp = by_user(s0, "mirv")
check("1 000 cartes : marge 10 000 $", s0["margin"]["cards"] == 1000 and s0["margin"]["total"] == 10000, json.dumps(s0["margin"]))
check("1 000 cartes : charges 1 920 $, salaires 2 000 $, dette 1 000 $ payés",
      step(s0, "charges")["paid"] == 1920 and step(s0, "salaires")["paid"] == 2000 and step(s0, "dette")["paid"] == 1000,
      json.dumps([(s["key"], s.get("paid")) for s in s0["steps"]]))
check("1 000 cartes : réserve 1 000 $, surplus 4 080 $", s0["reserve"] == 1000 and s0["surplus"]["total"] == 4080, json.dumps(s0["surplus"]))
check("1 000 cartes : prime 510 $ par personne, 4 personnes payées", s0["surplus"]["bonus_each"] == 510 and s0["surplus"]["people"] == 4,
      json.dumps(s0["surplus"]))
check("1 000 cartes : total 1 010 $ par personne (Mirv, Michael, Handy, Wydlyn)",
      sorted(p["username"] for p in s0["people"]) == ["handy", "michael", "mirv", "wydlyn"] and all(p["total_due"] == 1010 for p in s0["people"]),
      json.dumps([(p["username"], p["total_due"]) for p in s0["people"]]))
check("1 000 cartes : rien ne manque, aucun arriéré", s0["covered"] and s0["arrears_out"]["total"] == 0)

# ── Contrôle à 3 000 cartes ─────────────────────────────────────────
s1 = mirv.get("/api/ws/finances", params={"mois": M1}).json()
check("3 000 cartes : prime 2 760 $ par personne", s1["surplus"]["bonus_each"] == 2760, json.dumps(s1["surplus"]))
check("3 000 cartes : total 3 260 $ par personne", all(p["total_due"] == 3260 for p in s1["people"]) and len(s1["people"]) == 4,
      json.dumps([(p["username"], p["total_due"]) for p in s1["people"]]))

# ── Mois sous le seuil, arriéré reporté au mois suivant ─────────────
s2 = mirv.get("/api/ws/finances", params={"mois": M2}).json()
check("300 cartes : sous le seuil (247 cartes manquent)", not s2["covered"] and s2["threshold"]["cards_left"] == 247,
      json.dumps(s2["threshold"]))
check("300 cartes : charges payées, salaires payés à 1 080 $ sur 2 000 $ (270 $ chacun)",
      step(s2, "charges")["missing"] == 0 and step(s2, "salaires")["paid"] == 1080 and by_user(s2, "mirv")["salary"] == 270,
      json.dumps(step(s2, "salaires"))[:300])
check("300 cartes : dette non payée (1 000 $), aucune prime",
      step(s2, "dette")["missing"] == 1000 and s2["surplus"]["bonus_each"] == 0 and s2["surplus"]["total"] == 0)
check("300 cartes : arriéré reporté 1 920 $ (920 $ de salaires, 1 000 $ de dette)",
      s2["arrears_out"]["total"] == 1920 and s2["arrears_out"]["debt"] == 1000 and s2["arrears_out"]["people"].get("mirv") == 230,
      json.dumps(s2["arrears_out"]))
s3 = mirv.get("/api/ws/finances", params={"mois": M3}).json()
m3 = by_user(s3, "mirv")
check("mois suivant : l'arriéré de 1 920 $ arrive en tête", s3["arrears_in"]["total"] == 1920 and step(s3, "dette")["due"] == 2000,
      json.dumps(s3["arrears_in"]))
check("mois suivant : salaire dû 730 $ (500 + 230 d'arriéré), payé avant la prime",
      m3.get("salary_due") == 730 and m3.get("salary") == 730 and m3.get("arrears_in") == 230, json.dumps(m3))
check("mois suivant : prime 270 $ seulement après le rattrapage, total 1 000 $",
      s3["surplus"]["bonus_each"] == 270 and m3.get("total_due") == 1000 and s3["arrears_out"]["total"] == 0, json.dumps(s3["surplus"]))

# ── Versement : le « reste à verser » baisse ────────────────────────
before = by_user(s0, "mirv")["remaining"]
r = mirv.post("/api/ws/finances/payouts", json={"username": "mirv", "month": M0, "kind": "salaire", "day": f"{M0}-28", "amount": 500, "method": "moncash"}, headers=h(mirv))
check("versement de 500 $ (MonCash) enregistré par le responsable", r.status_code == 201 and r.json()["payout"]["amount"] == 500, r.text)
after = by_user(mirv.get("/api/ws/finances", params={"mois": M0}).json(), "mirv")
check(f"reste à verser de Mirv : {before:g} $ → 510 $ (déjà versé 500 $)", before == 1010 and after["remaining"] == 510 and after["paid_out"] == 500, json.dumps(after))
r = mic.post("/api/ws/finances/payouts", json={"username": "michael", "month": M0, "kind": "prime", "day": f"{M0}-28", "amount": 510, "method": "natcash"}, headers=h(mic))
check("un membre n'enregistre pas de versement → 403", r.status_code == 403, r.text)
r = mirv.post("/api/ws/finances/payouts", json={"username": "michael", "month": M0, "kind": "prime", "day": f"{M0}-28", "amount": 510, "method": "virement"}, headers=h(mirv))
check("moyen de paiement inconnu → 422", r.status_code == 422, r.text[:200])

# ── Dette : un remboursement fait baisser le solde ──────────────────
d = mic.get("/api/ws/finances/debt").json()
check("dette : solde de départ 5 000 $ (lu par un membre)", d["balance"] == 5000 and d["initial"] == 5000, json.dumps(d)[:200])
r = mic.post("/api/ws/finances/debt/payments", json={"day": f"{M4}-05", "amount": 1000, "method": "especes"}, headers=h(mic))
check("un membre n'enregistre pas de remboursement → 403", r.status_code == 403, r.text)
r = mirv.post("/api/ws/finances/debt/payments", json={"day": f"{M4}-05", "amount": 1000, "method": "handypay", "note": "1re mensualité"}, headers=h(mirv))
check("remboursement de 1 000 $ → solde 4 000 $", r.status_code == 201 and r.json()["balance"] == 4000, r.text)
check("journal de la dette : 1 remboursement, solde 4 000 $", (lambda x: x["balance"] == 4000 and len(x["payments"]) == 1)(mic.get("/api/ws/finances/debt").json()))
r = mirv.post("/api/ws/finances/debt/payments", json={"day": f"{M4}-06", "amount": 4500, "method": "especes"}, headers=h(mirv))
check("rembourser plus que le solde → 400", r.status_code == 400, r.text)
r = mirv.post("/api/ws/finances/debt/payments", json={"day": f"{M4}-20", "amount": 4000, "method": "especes"}, headers=h(mirv))
check("dette soldée (4 000 $) → solde 0 $", r.status_code == 201 and r.json()["balance"] == 0, r.text)
s5 = mirv.get("/api/ws/finances", params={"mois": M5}).json()
check("mois suivant le dernier remboursement : seuil 436 cartes, dette à 0 $",
      s5["threshold"]["cards"] == 436 and step(s5, "dette")["due"] == 0 and s5["debt"]["balance"] == 0, json.dumps(s5["threshold"]))
check("les mois d'avant gardent leur dette : seuil de " + M0 + " toujours 547",
      mirv.get("/api/ws/finances", params={"mois": M0}).json()["threshold"]["cards"] == 547)

# ── Autre revenu : il s'ajoute à la marge ───────────────────────────
r = mirv.post("/api/ws/finances/incomes", json={"day": f"{M5}-10", "label": "Vente de cours", "amount": 250}, headers=h(mirv))
s5 = mirv.get("/api/ws/finances", params={"mois": M5}).json()
check("autre revenu (vente de cours, 250 $) ajouté à la marge du mois", r.status_code == 201 and s5["margin"]["other_income"] == 250 and s5["margin"]["total"] == 250,
      json.dumps(s5["margin"]))

# ── Dépenses : au-dessus du seuil, accord de chaque personne payée ──
day = TODAY.isoformat()
r = mic.post("/api/ws/finances/expenses", json={"day": day, "category": "Matériel", "amount": 350, "paid_by": "entreprise", "note": "Téléphone de test"}, headers=h(mic))
e = r.json().get("expense", {})
check("Michael propose 350 $ de matériel : en attente, accord des 4 personnes payées demandé",
      r.status_code == 201 and e["status"] == "en_attente" and e["needs_approval"] and sorted(e["required"]) == ["handy", "michael", "mirv", "wydlyn"], r.text[:300])
check("proposer vaut accord : il manque Mirv, Handy, Wydlyn", sorted(e.get("missing", [])) == ["handy", "mirv", "wydlyn"], json.dumps(e)[:300])
eid = e.get("id", "")
check("l'observateur ne peut pas approuver → 403", obs.post(f"/api/ws/finances/expenses/{eid}/approve", headers=h(obs)).status_code == 403)
r = mirv.post(f"/api/ws/finances/expenses/{eid}/validate", headers=h(mirv))
check("un responsable ne passe pas outre l'accord de chacun (valider → 409)", r.status_code == 409, r.text)
r = sess["mirv"].post(f"/api/ws/finances/expenses/{eid}/approve")
check("« J'approuve » sans jeton CSRF → 403", r.status_code == 403, r.text)
st_a = sess["mirv"].post(f"/api/ws/finances/expenses/{eid}/approve", headers=h(mirv)).json()["expense"]["status"]
st_b = sess["handy"].post(f"/api/ws/finances/expenses/{eid}/approve", headers=h(sess["handy"])).json()["expense"]["status"]
check("après Mirv puis Handy : toujours en attente (3 accords sur 4)", st_a == "en_attente" and st_b == "en_attente", f"{st_a} {st_b}")
r = sess["wydlyn"].post(f"/api/ws/finances/expenses/{eid}/approve", headers=h(sess["wydlyn"]))
e = r.json().get("expense", {})
check("Wydlyn approuve en dernier → dépense validée", r.status_code == 200 and e["status"] == "validee" and len(e["approvals"]) == 4 and not e["missing"], r.text[:300])
sc = mirv.get("/api/ws/finances").json()
check("relevé du mois : dépense validée comptée (350 $ hors charges fixes, pris sur la croissance)",
      sc["expenses"]["validated_total"] >= 350 and sc["expenses"]["other_total"] >= 350 and any(x["id"] == eid for x in sc["expenses"]["month"]),
      json.dumps(sc["expenses"])[:300])
r = mic.post("/api/ws/finances/expenses", json={"day": day, "category": "Internet", "amount": 60, "paid_by": "michael"}, headers=h(mic))
small = r.json().get("expense", {})
check("sous le seuil, proposée par un membre : en attente d'un responsable", r.status_code == 201 and small["status"] == "en_attente" and not small["needs_approval"], r.text[:200])
check("un membre ne valide pas → 403", mic.post(f"/api/ws/finances/expenses/{small.get('id')}/validate", headers=h(mic)).status_code == 403)
r = mirv.post(f"/api/ws/finances/expenses/{small.get('id')}/validate", headers=h(mirv))
check("le responsable valide la dépense sous le seuil", r.status_code == 200 and r.json()["expense"]["status"] == "validee", r.text[:200])
r = mic.post("/api/ws/finances/expenses", json={"day": day, "category": "Publicité", "amount": 900}, headers=h(mic))
big = r.json().get("expense", {})
r = mirv.post(f"/api/ws/finances/expenses/{big.get('id')}/refuse", json={"reason": "Pas ce mois-ci"}, headers=h(mirv))
check("le responsable refuse un achat en attente", r.status_code == 200 and r.json()["expense"]["status"] == "refusee" and r.json()["expense"]["refusal_reason"] == "Pas ce mois-ci", r.text[:200])
r = sess["handy"].post(f"/api/ws/finances/expenses/{big.get('id')}/approve", headers=h(sess["handy"]))
check("approuver une dépense refusée → 409", r.status_code == 409, r.text)

# ── Droits ──────────────────────────────────────────────────────────
r = mic.get("/api/ws/finances", params={"mois": M0})
check("un membre lit le relevé complet (cascade et tableau par personne)", r.status_code == 200 and len(r.json()["steps"]) == 5 and len(r.json()["people"]) == 4 and r.json()["can_manage"] is False)
check("observateur : relevé → 403", obs.get("/api/ws/finances").status_code == 403)
check("observateur : dette, réglages → 403", obs.get("/api/ws/finances/debt").status_code == 403 and obs.get("/api/ws/settings/finances").status_code == 403)
check("observateur : proposer une dépense → 403", obs.post("/api/ws/finances/expenses", json={"day": day, "category": "x", "amount": 5}, headers=h(obs)).status_code == 403)
check("observateur : export → 403", obs.get("/api/ws/finances/export.csv").status_code == 403)
notifs = obs.get("/api/ws/notifications").json().get("notifications", [])
check("observateur : aucune notification de finances", not any(n.get("kind") == "fin_accord" for n in notifs))
notifs = sess["wydlyn"].get("/api/ws/notifications").json().get("notifications", [])
check("une personne payée est prévenue quand son accord est demandé", any(n.get("kind") == "fin_accord" for n in notifs), json.dumps(notifs)[:200])
check("export CSV du relevé par un membre → 403", mic.get("/api/ws/finances/export.csv", params={"mois": M0}).status_code == 403)
r = mirv.get("/api/ws/finances/export.csv", params={"mois": M0})
check("export CSV du relevé par le responsable : prime 510, total 1010, reste 510 pour Mirv",
      r.status_code == 200 and "text/csv" in r.headers.get("content-type", "") and "Prime par personne;510" in r.text and "Mirv;500;0;500;510;1010;500;510" in r.text,
      r.text[:400])
check("mois invalide → 400", mirv.get("/api/ws/finances", params={"mois": "2026-13"}).status_code == 400)

# ── Mois en cours : projection de fin de mois ───────────────────────
pj = mirv.get("/api/ws/finances").json()
check("mois en cours : projection de fin de mois au rythme du mois", pj["is_current"] and pj["projection"] is not None and pj["projection"]["cards"] >= pj["margin"]["cards"],
      json.dumps(pj["projection"])[:200])

ok = sum(1 for r in results if r[0])
print(f"\n{ok}/{len(results)} vérifications réussies")
(SCR / "verify_finances_result.json").write_text(json.dumps({"ok": ok, "total": len(results), "fails": [r[1] for r in results if not r[0]]}, ensure_ascii=False, indent=1), encoding="utf-8")
sys.exit(0 if ok == len(results) else 1)
