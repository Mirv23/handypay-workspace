"""Vérification de bout en bout de l'API (mode local). Se lance avec tests/run_verify.py."""
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

import os
SCR = Path(os.environ.get("VERIFY_DIR") or Path(__file__).parent)
BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8765")
TOKEN = (SCR / "ia_token.txt").read_text().strip()
OWNER_PW = re.search(r"Mot de passe : (\S+)", (SCR / "owner.txt").read_text(encoding="utf-8")).group(1)
TODAY = datetime.now(ZoneInfo("America/Port-au-Prince")).date()
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


# ── Page et en-têtes ────────────────────────────────────────────────
for _ in range(40):
    try:
        httpx.get(BASE + "/healthz", timeout=2)
        break
    except Exception:
        time.sleep(0.5)
mirv = client()
page = mirv.get("/")
check("page / servie (200) avec CSP et X-Frame-Options", page.status_code == 200 and "default-src 'self'" in page.headers.get("content-security-policy", "") and page.headers.get("x-frame-options") == "DENY")
check("titre « HandyPay · Espace d'équipe »", "<title>HandyPay · Espace d&#39;équipe</title>" in page.text or "<title>HandyPay · Espace d'équipe</title>" in page.text, page.text[:300])
check("cookie hp_csrf posé par la page", bool(mirv.cookies.get("hp_csrf")))
check("/api/ws/bootstrap sans session → 401", mirv.get("/api/ws/bootstrap").status_code == 401)

# ── Connexion ───────────────────────────────────────────────────────
r = mirv.post("/api/auth/login", json={"identifier": "mirv", "password": OWNER_PW})
check("connexion SANS en-tête CSRF → 403", r.status_code == 403, r.text)
r = login(mirv, "mirv", "mauvais-mot-de-passe")
check("mauvais mot de passe → 401", r.status_code == 401, r.text)
r = login(mirv, "+509 3412-0001", OWNER_PW)
check("connexion du responsable par son numéro WhatsApp → 200", r.status_code == 200, r.text)
sess = mirv.cookies.get("hp_session", "")
check("cookie de session posé, signé (charge.signature)", sess.count(".") == 1)
setc = r.headers.get_list("set-cookie")
check("cookie de session HttpOnly + SameSite=Lax", any("hp_session=" in x and "HttpOnly" in x and "samesite=lax" in x.lower() for x in setc), str(setc))
boot = mirv.get("/api/ws/bootstrap").json()
check("bootstrap : rôle responsable, clé HP, réglages ventes", boot["me"]["role"] == "responsable" and boot["task_key"] == "HP" and boot["sales_settings"]["daily_target"] == 100 and boot["sales_settings"]["total_target"] == 3000)
check("bootstrap : jalons 700/1400/2100/3000", [m["target"] for m in boot["sales_settings"]["milestones"]] == [700, 1400, 2100, 3000])

# ── CSRF sur une requête qui modifie ────────────────────────────────
r = mirv.post("/api/ws/members", json={"username": "x1", "display_name": "X"})
check("POST /api/ws/members sans CSRF → 403", r.status_code == 403, r.text)
r = mirv.post("/api/ws/members", json={"username": "x1", "display_name": "X"}, headers={"X-CSRF-Token": "a" * 43})
check("POST avec un jeton CSRF faux → 403", r.status_code == 403, r.text)

# ── Créer un membre, un observateur ─────────────────────────────────
r = mirv.post("/api/ws/members", json={"username": "michael", "display_name": "Michael", "whatsapp": "3412 0002", "role": "membre", "title": "Commercial"}, headers=h(mirv))
check("création du membre michael → 201", r.status_code == 201, r.text)
mi = r.json()
MICHAEL_PW = mi.get("password", "")
check("mot de passe généré renvoyé une fois (16 car. + tirets)", re.fullmatch(r"[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3}", MICHAEL_PW or "") is not None)
check("lien « Envoyer par WhatsApp » vers wa.me/50934120002 avec le mot de passe", mi.get("wa_link", "").startswith("https://wa.me/50934120002?text=") and MICHAEL_PW in unquote(mi["wa_link"]))
r = mirv.post("/api/ws/members", json={"username": "obs", "display_name": "Observatrice", "role": "observateur"}, headers=h(mirv))
OBS_PW = r.json().get("password", "")
check("création de l'observateur → 201, lien wa.me sans numéro", r.status_code == 201 and r.json()["wa_link"].startswith("https://wa.me/?text="), r.text)
team = mirv.get("/api/ws/bootstrap").json()["members"]
check("équipe : mirv, michael, obs + IA", sorted(m["username"] for m in team) == ["ia", "michael", "mirv", "obs"], str([m["username"] for m in team]))
check("aucune empreinte de mot de passe dans le bootstrap", "scrypt$" not in json.dumps(team))

# ── Tâches (fonctions d'origine) ────────────────────────────────────
r = mirv.post("/api/ws/projects", json={"name": "Campagne de test"}, headers=h(mirv))
pid = r.json()["project"]["id"]
r = mirv.post("/api/ws/tasks", json={"project_id": pid, "title": "Vider la file WhatsApp", "assignee": "michael"}, headers=h(mirv))
task = r.json()["task"]
check("tâche créée, numéro 1", r.status_code == 201 and task["number"] >= 1, r.text)
r = mirv.post(f"/api/ws/tasks/{task['id']}/comments", json={"body": "Go @michael"}, headers=h(mirv))
check("commentaire avec mention → 201", r.status_code == 201, r.text)
r = mirv.get("/api/ws/export.csv")
check("export CSV des tâches → 200", r.status_code == 200 and "Vider la file WhatsApp" in r.text)
r = mirv.post("/api/ws/templates/campagne_3000/apply", json={"start_date": "2026-11-16", "decider": "mirv"}, headers=h(mirv))
check("modèle « Campagne 3 000 cartes » appliqué", r.status_code == 201 and r.json()["tasks"] >= 10, r.text)

# ── Import CSV : 5 fiches dont 1 doublon ────────────────────────────
csv_text = "nom;téléphone;étape;source;note\n" \
           "Jean Pierre Louis;3456 7801;inscrit;pub;\n" \
           "Marie Claire;+509 3456-7802;achat commencé;whatsapp;a demandé le prix\n" \
           "Rose Joseph;50934567803;carte émise;parrainage;\n" \
           "Paul Henry;3456 7804;KYC;pub;\n" \
           "Jean P. Louis (doublon);+509 3456 7801;inscrit;pub;même numéro que la ligne 2\n"
r = mirv.post("/api/ws/followups/import", json={"csv": csv_text, "source": "test"}, headers=h(mirv))
rep = r.json()
check("import : 4 ajoutées, 1 doublon, 0 rejetée", r.status_code == 200 and rep["added"] == 4 and rep["duplicates"] == 1 and rep["rejected"] == 0, json.dumps(rep, ensure_ascii=False))
r = mirv.post("/api/ws/followups/import", json={"csv": "Inconnu;12;inscrit\nAutre;3456 7809;étape bizarre\n"}, headers=h(mirv))
rep2 = r.json()
check("import : numéro invalide et étape inconnue rejetés avec la ligne", rep2["rejected"] == 2 and {x["line"] for x in rep2["rejected_lines"]} == {1, 2}, json.dumps(rep2, ensure_ascii=False))
r = mirv.post("/api/ws/followups/distribute", json={"usernames": ["mirv", "michael"]}, headers=h(mirv))
check("répartition équitable : 2 fiches chacun", r.status_code == 200 and r.json()["per_person"] == {"mirv": 2, "michael": 2}, r.text)

q = mirv.get("/api/ws/followups", params={"view": "mine"}).json()
check("« Ma file du jour » de mirv : 2 fiches, assignées à lui", q["total"] == 2 and all(i["assignee"] == "mirv" for i in q["items"]) and q["mine_due"] == 2, json.dumps(q)[:300])
tpls = mirv.get("/api/ws/message-templates").json()["templates"]
check("5 modèles de départ en français (les 5 créoles : verify_relances.py)", len([t for t in tpls if t.get("lang", "fr") == "fr"]) == 5, str(len(tpls)))
fo1, fo2 = q["items"][0], q["items"][1]
tpl1 = next((t for t in tpls if t["stage"] == fo1["stage"]), next(t for t in tpls if t["stage"] == ""))

# ── Ouvrir WhatsApp : lien et relance programmée ────────────────────
r = mirv.post(f"/api/ws/followups/{fo1['id']}/whatsapp", json={"template_id": tpl1["id"]}, headers=h(mirv))
w = r.json()
digits = fo1["phone"].lstrip("+")
first = (fo1["name"] or "").split(" ")[0]
check("lien wa.me/<numéro>?text=<message rempli>", w["wa_link"].startswith(f"https://wa.me/{digits}?text=") and first in unquote(w["wa_link"]) and "{prenom}" not in unquote(w["wa_link"]), w.get("wa_link", "")[:120])
check("message rempli avec le prénom et « Mirv »", w["message"].startswith(f"Bonjour {first},") and "Mirv" in w["message"], w["message"][:80])
check("1re tentative : contacté, relance J+1", w["followup"]["attempts"] == 1 and w["followup"]["status"] == "contacte" and w["followup"]["next_followup"] == (TODAY + timedelta(days=1)).isoformat(), json.dumps(w["followup"])[:200])
w2 = mirv.post(f"/api/ws/followups/{fo1['id']}/whatsapp", json={"template_id": tpl1["id"]}, headers=h(mirv)).json()
check("2e tentative : relance J+3", w2["followup"]["attempts"] == 2 and w2["followup"]["next_followup"] == (TODAY + timedelta(days=3)).isoformat())
w3 = mirv.post(f"/api/ws/followups/{fo1['id']}/whatsapp", json={}, headers=h(mirv)).json()
check("3e tentative sans réponse : « froid », relance J+7", w3["followup"]["status"] == "froid" and w3["followup"]["next_followup"] == (TODAY + timedelta(days=7)).isoformat(), json.dumps(w3["followup"])[:200])
check("sans modèle : lien wa.me sans texte", w3["wa_link"] == f"https://wa.me/{digits}")

# ── A répondu puis Closé : la vente au nom de la personne connectée ─
mirv.post(f"/api/ws/followups/{fo2['id']}/whatsapp", json={}, headers=h(mirv))
r = mirv.post(f"/api/ws/followups/{fo2['id']}/event", json={"kind": "reponse"}, headers=h(mirv))
check("« A répondu » : statut a_repondu, tentatives remises à 0", r.json()["followup"]["status"] == "a_repondu" and r.json()["followup"]["attempts"] == 0)
r = mirv.post(f"/api/ws/followups/{fo2['id']}/event", json={"kind": "close", "cards": 2}, headers=h(mirv))
cl = r.json()
check("« Closé » : vente créée pour mirv, 2 cartes, reliée à la fiche", r.status_code == 200 and cl["sale"]["username"] == "mirv" and cl["sale"]["cards"] == 2 and cl["sale"]["followup_id"] == fo2["id"] and cl["followup"]["status"] == "close" and cl["followup"]["sale_id"] == cl["sale"]["id"], json.dumps(cl)[:300])

# ── Michael : + Carte closée, annulation ────────────────────────────
mic = client()
check("connexion de michael par nom d'utilisateur", login(mic, "michael", MICHAEL_PW).status_code == 200)
r = mic.post("/api/ws/sales", json={"cards": 1}, headers=h(mic))
ms = r.json()["sale"]
check("« + Carte closée » de michael → vente à son nom, aujourd'hui", r.status_code == 201 and ms["username"] == "michael" and ms["day"] == TODAY.isoformat(), r.text)
r = mic.post("/api/ws/sales", json={"cards": 1, "username": "mirv"}, headers=h(mic))
check("un membre ne saisit pas pour quelqu'un d'autre → 403", r.status_code == 403, r.text)
r = mic.delete(f"/api/ws/sales/{cl['sale']['id']}", headers=h(mic))
check("michael ne peut pas annuler la vente de mirv → 403", r.status_code == 403, r.text)
r = mic.delete(f"/api/ws/sales/{ms['id']}", headers=h(mic))
check("michael annule sa propre saisie du jour", r.status_code == 200 and r.json()["sale"]["cancelled"], r.text)

# ── Porte de l'IA : vente HandyPay idempotente ──────────────────────
ia = httpx.Client(base_url=BASE, timeout=30)
r = ia.post("/api/workspace/ia/ventes", json={"external_id": "hp-ord-0001", "cartes": 3, "membre": "michael"})
check("porte IA sans jeton → 401", r.status_code == 401, r.text)
auth = {"Authorization": "Bearer " + TOKEN}
r1 = ia.post("/api/workspace/ia/ventes", json={"external_id": "hp-ord-0001", "cartes": 3, "membre": "michael", "source": "pub"}, headers=auth)
r2 = ia.post("/api/workspace/ia/ventes", json={"external_id": "hp-ord-0001", "cartes": 3, "membre": "michael", "source": "pub"}, headers=auth)
check("1er envoi : vente enregistrée (doublon=false), origine handypay", r1.status_code == 200 and r1.json()["doublon"] is False and r1.json()["vente"]["origine"] == "handypay", r1.text)
check("2e envoi même external_id : doublon=true, même vente", r2.status_code == 200 and r2.json()["doublon"] is True and r2.json()["vente"]["id"] == r1.json()["vente"]["id"], r2.text)
r = ia.post("/api/workspace/ia/ventes", json={"cartes": 1}, headers=auth)
check("external_id obligatoire → 422", r.status_code == 422)
sales_today = mirv.get("/api/ws/sales", params={"day": TODAY.isoformat()}).json()["sales"]
check("une seule ligne avec external_id hp-ord-0001", sum(1 for s in sales_today if s["external_id"] == "hp-ord-0001") == 1)
r = mic.delete(f"/api/ws/sales/{r1.json()['vente']['id']}", headers=h(mic))
check("un membre n'annule pas une vente HandyPay → 403", r.status_code == 403)
r = ia.get("/api/workspace/ia/taches/HP-1", headers=auth)
check("porte IA : tâche HP-1 lisible par sa clé", r.status_code == 200 and r.json()["tache"]["cle"] == "HP-1", r.text[:200])
r = ia.post("/api/workspace/ia/fiches", json={"fiches": [{"nom": "Nouveau", "telephone": "3456 7810", "etape": "inscrit"}, {"nom": "Rose", "telephone": "50934567803", "etape": "carte utilisée"}]}, headers=auth)
check("porte IA : fiches (1 ajoutée, 1 doublon dont l'étape avance)", r.status_code == 200 and r.json()["ajoutees"] == 1 and r.json()["doublons"] == 1 and r.json()["etapes_avancees"] == 1, r.text)

# ── Stats et objectif relié aux ventes ──────────────────────────────
st = mirv.get("/api/ws/sales/stats").json()
by = {p["username"]: p["cards"] for p in st["today"]["by_person"]}
check("stats du jour : 5 cartes (mirv 2 + HandyPay 3 pour michael ; saisie annulée exclue)", st["today"]["cards"] == 5 and by.get("mirv") == 2 and by.get("michael") == 3, json.dumps(st["today"])[:300])
camp = st["campaign"]
elapsed = (TODAY - date(2026, 11, 16)).days + 1
check(f"cumul 5, attendu {100 * elapsed} (rythme), écart {5 - 100 * elapsed}, prochain jalon 700 le 22 nov.", camp["cumul"] == 5 and camp["expected"] == 100 * elapsed and camp["gap"] == 5 - 100 * elapsed and camp["next_milestone"]["date"] == "2026-11-22", json.dumps(camp)[:300])
check("marge cumulée 50 $", camp["margin"] == 50)
check("série de 30 jours (16 nov. → 15 déc.)", len(st["series"]) == 30)
kr = [k for o in mirv.get("/api/ws/bootstrap").json()["objectives"] for k in o["key_results"] if k["auto"] == "ventes_cumul"]
check("résultat clé relié aux ventes mis à jour tout seul (5)", kr and kr[0]["current_value"] == 5, json.dumps(kr)[:200])

# ── Tableau du soir ────────────────────────────────────────────────
r = mirv.put("/api/ws/evening/note", json={"blockers": ["KYC refusé (photo floue)", "Paiement MonCash en échec", ""], "bugs": "Écran de paiement figé sur Android 9", "note": "Bonne journée", "unread_left": 0}, headers=h(mirv))
check("formulaire du soir enregistré", r.status_code == 200, r.text)
ev = mirv.get("/api/ws/evening").json()
f = ev["followups"]
check("soir : 4 relances envoyées, 1 réponse (25 %), 1 close après relance (100 % des réponses)", f["sent"] == 4 and f["replies"] == 1 and f["reply_rate"] == 0.25 and f["closes_after"] == 1 and f["close_rate"] == 1.0, json.dumps(f))
check("soir : 5 cartes, cible 100, conversations visées 300", ev["sales"]["cards"] == 5 and ev["sales"]["target"] == 100 and f["conversations_target"] == 300)
pm = next(p for p in ev["people"] if p["username"] == "mirv")
check("soir par personne : mirv 2 cartes, 4 relances, note avec 0 non-lu", pm["cards"] == 2 and pm["sent"] == 4 and pm["note"]["unread_left"] == 0 and pm["note"]["blockers"][0].startswith("KYC"), json.dumps(pm, ensure_ascii=False)[:300])
prev = mirv.get("/api/ws/digest/preview").json()
check("e-mail du matin (mirv) : section Ventes (hier, équipe, cumul)", "Ventes</h3>" in prev["html"] and "L'équipe hier" in prev["html"].replace("&#x27;", "'"), prev["subject"])
prev = mic.get("/api/ws/digest/preview").json()
check("e-mail du matin (michael) : « Relances du jour », 2 fiches dans sa file", "Relances du jour" in prev["html"] and "2 fiches dans ta file" in prev["html"] and prev["subject"].startswith("Ta journée HandyPay : 2 relances"), prev["subject"])

# ── Observateur : lecture seule ─────────────────────────────────────
obs = client()
check("connexion de l'observateur", login(obs, "obs", OBS_PW).status_code == 200)
check("observateur : lit les stats (200)", obs.get("/api/ws/sales/stats").status_code == 200)
check("observateur : POST /sales → 403", obs.post("/api/ws/sales", json={"cards": 1}, headers=h(obs)).status_code == 403)
check("observateur : POST /followups → 403", obs.post("/api/ws/followups", json={"phone": "3456 7899"}, headers=h(obs)).status_code == 403)
check("observateur : « Ouvrir WhatsApp » → 403", obs.post(f"/api/ws/followups/{fo1['id']}/whatsapp", json={}, headers=h(obs)).status_code == 403)
check("observateur : formulaire du soir → 403", obs.put("/api/ws/evening/note", json={"note": "x"}, headers=h(obs)).status_code == 403)
check("observateur : création de tâche → 403", obs.post("/api/ws/tasks", json={"project_id": pid, "title": "x"}, headers=h(obs)).status_code == 403)

# ── Export des fiches réservé aux responsables ──────────────────────
check("export CSV des fiches par un membre → 403", mic.get("/api/ws/followups-export.csv").status_code == 403)
r = mirv.get("/api/ws/followups-export.csv")
check("export CSV des fiches par le responsable → 200 avec les numéros", r.status_code == 200 and "+50934567802" in r.text)

# ── Changement de mot de passe : les autres sessions tombent ────────
other = client()
login(other, "michael", MICHAEL_PW)
r = mic.post("/api/auth/password", json={"current": MICHAEL_PW, "new": "un-nouveau-mot-de-passe"}, headers=h(mic))
check("michael change son mot de passe", r.status_code == 200, r.text)
check("sa session sur cet appareil reste valide", mic.get("/api/ws/bootstrap").status_code == 200)
check("son autre session est fermée (401)", other.get("/api/ws/bootstrap").status_code == 401)

# ── Verrouillage : 8 échecs bloquent l'identifiant 15 min ───────────
lk = client()
codes = [login(lk, "obs", "faux-mdp-%d" % i).status_code for i in range(8)]
r = login(lk, "obs", OBS_PW)
check("8 échecs → 9e essai bloqué (429), même avec le bon mot de passe", codes == [401] * 8 and r.status_code == 429, f"{codes} puis {r.status_code}")
codes = [login(lk, "inconnu", "x").status_code for i in range(9)]
check("identifiant inconnu : même comportement (8 × 401 puis 429)", codes == [401] * 8 + [429], str(codes))

# ── Déconnexion ─────────────────────────────────────────────────────
mirv.post("/api/auth/logout", headers=h(mirv))
check("après déconnexion → 401", mirv.get("/api/ws/bootstrap").status_code == 401)

ok = sum(1 for r in results if r[0])
print(f"\n{ok}/{len(results)} vérifications réussies")
(SCR / "verify_result.json").write_text(json.dumps({"ok": ok, "total": len(results), "fails": [r[1] for r in results if not r[0]]}, ensure_ascii=False, indent=1), encoding="utf-8")
sys.exit(0 if ok == len(results) else 1)
