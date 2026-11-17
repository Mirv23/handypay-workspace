"""Vérification de bout en bout des relances à grand volume (v2). Se lance avec tests/run_verify.py :

    .venv\\Scripts\\python.exe tests\\run_verify.py --port 8801 --extra tests\\verify_relances.py

Passe après verify_api.py, sur le même serveur et la même base locale jetable.
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

SCR = Path(os.environ.get("VERIFY_DIR") or Path(__file__).parent)
BASE = os.environ.get("VERIFY_BASE", "http://127.0.0.1:8765")
OWNER_PW = re.search(r"Mot de passe : (\S+)", (SCR / "owner.txt").read_text(encoding="utf-8")).group(1)
TODAY = datetime.now(ZoneInfo("America/Port-au-Prince")).date()
ROOT = Path(__file__).resolve().parent.parent
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


for _ in range(40):
    try:
        httpx.get(BASE + "/healthz", timeout=2)
        break
    except Exception:
        time.sleep(0.5)

# ── Comptes de test : un membre et un observateur (ceux de verify_api sont bloqués ou changés) ──
mirv = client()
check("connexion du responsable", login(mirv, "mirv", OWNER_PW).status_code == 200)
r = mirv.post("/api/ws/members", json={"username": "rvmembre", "display_name": "Rose Vendeuse", "role": "membre"}, headers=h(mirv))
RV_PW = r.json().get("password", "")
r2 = mirv.post("/api/ws/members", json={"username": "rvobs", "display_name": "Olga Observe", "role": "observateur"}, headers=h(mirv))
OBS_PW = r2.json().get("password", "")
check("membre rvmembre et observateur rvobs créés", r.status_code == 201 and r2.status_code == 201, r.text + r2.text)
rv, obs = client(), client()
check("connexions du membre et de l'observateur", login(rv, "rvmembre", RV_PW).status_code == 200 and login(obs, "rvobs", OBS_PW).status_code == 200)

# ── Modèles : 5 en créole en plus des 5 français, seed idempotent ──
tpls = mirv.get("/api/ws/message-templates").json()["templates"]
fr = [t for t in tpls if t["lang"] == "fr"]
ht = [t for t in tpls if t["lang"] == "ht"]
check("modèles : 5 français + 5 créoles", len(fr) == 5 and len(ht) == 5, f"{len(fr)} fr, {len(ht)} ht")
check("modèles créoles marqués « à relire par l'équipe » dans leur description",
      all("à relire par l'équipe" in t["description"] for t in ht) and not any(t["description"] for t in fr), json.dumps(ht, ensure_ascii=False)[:200])
check("modèles créoles : courts, {prenom} et {moi}, « Bonjou »",
      all(t["body"].startswith("Bonjou {prenom}") and "{moi}" in t["body"] and len(t["body"]) <= 260 for t in ht))
check("modèles créoles : une étape chacun, comme les français", sorted(t["stage"] for t in ht) == sorted(t["stage"] for t in fr))
again = mirv.get("/api/ws/message-templates").json()["templates"]
check("relire les modèles n'en ajoute aucun (seed idempotent)", len(again) == 10)
gone = next(t for t in ht if t["stage"] == "carte_utilisee")
mirv.delete(f"/api/ws/message-templates/{gone['id']}", headers=h(mirv))
check("un modèle de départ supprimé ne revient pas", len(mirv.get("/api/ws/message-templates").json()["templates"]) == 9)

# Base d'avant la v2 (5 modèles français, l'un modifié par l'équipe) : la mise à jour ajoute les créoles, n'écrase rien.
sys.path.insert(0, str(ROOT))
from app import followups as fu  # noqa: E402
from app.localdb import LocalClient  # noqa: E402

old = LocalClient(Path(tempfile.mkdtemp(prefix="hp-seed-", dir=str(SCR))))
old.table("workspace_message_templates").insert([
    {"name": t["name"], "stage": t["stage"], "position": i,
     "body": "Texte revu par l'équipe" if i == 0 else t["body"]} for i, t in enumerate(fu.DEFAULT_TEMPLATES[:5])]).execute()
old.table("workspace_settings").insert({"key": "templates_seeded", "value": {"done": True}}).execute()
added_first, added_again = fu.seed_templates(old), fu.seed_templates(old)
rows = old.table("workspace_message_templates").select("*").execute().data
check("base d'avant : +5 créoles, rien d'écrasé, 2e passage sans effet",
      added_first == 5 and added_again == 0 and len(rows) == 10 and sum(1 for x in rows if x["body"] == "Texte revu par l'équipe") == 1
      and sum(1 for x in rows if x.get("lang") == "ht") == 5, f"{added_first} puis {added_again}, {len(rows)} lignes")

# ── Réglages : objectifs par personne (Michael 150, Mirv 100) et langue par défaut ──
fs = mirv.get("/api/ws/bootstrap").json()["followup_settings"]
check("objectifs par défaut : Michael 150, Mirv 100, les autres 0 ; langue fr", fs["targets"] == {"michael": 150, "mirv": 100} and fs["default_lang"] == "fr", json.dumps(fs))
seq = {"sequence": fs["sequence"], "max_attempts": fs["max_attempts"], "conversations_target": fs["conversations_target"]}
r = rv.put("/api/ws/settings/followups", json=dict(seq, targets={"rvmembre": 500}), headers=h(rv))
check("un membre ne change pas les objectifs → 403", r.status_code == 403, r.text)
r = mirv.put("/api/ws/settings/followups", json=dict(seq, targets={"inconnu": 5}), headers=h(mirv))
check("objectif pour une personne inconnue → 400", r.status_code == 400, r.text)
r = mirv.put("/api/ws/settings/followups", json=dict(seq, targets={"michael": 150, "mirv": 100, "rvmembre": 20}), headers=h(mirv))
check("le responsable règle l'objectif de rvmembre à 20", r.status_code == 200 and r.json()["settings"]["targets"]["rvmembre"] == 20, r.text)
r = mirv.put("/api/ws/settings/followups", json=seq, headers=h(mirv))
check("réglages sans objectifs : ceux en place sont gardés", r.status_code == 200 and r.json()["settings"]["targets"].get("rvmembre") == 20, r.text)

# ── Ajout rapide par collage : aperçu, doublons, rejets, puis import ──
PASTE = ("nom;téléphone\n"
         "Anne Exantus, 3790 1101\n"
         "3790 1102\n"
         "Bob Dorvil; +509 3790-1103\n"
         "Carla Noël\t50937901104\n"
         "Anne bis, 3790 1101\n"
         "Paul Henry, 3456 7804\n"
         "Pas de numéro ici\n"
         "Jean, 12345\n"
         "Didi Pierre 3790 1105\n")
r = rv.post("/api/ws/followups/paste", json={"text": PASTE, "stage": "inscrit", "source": "non lus WhatsApp", "lang": "ht"}, headers=h(rv))
pv = r.json()
check("collage, aperçu : 5 valides, 2 doublons, 2 rejets (en-tête ignoré)",
      r.status_code == 200 and not pv["committed"] and pv["valid"] == 5 and pv["duplicates"] == 2 and pv["rejected"] == 2, json.dumps(pv, ensure_ascii=False)[:400])
valid = {x["phone"]: x for x in pv["preview"]["valid"]}
check("aperçu : numéros normalisés en +509, noms lus (« nom, numéro », numéro seul, tabulation, numéro dans la ligne)",
      set(valid) == {f"+5093790110{i}" for i in range(1, 6)} and valid["+50937901101"]["name"] == "Anne Exantus"
      and valid["+50937901102"]["name"] == "" and valid["+50937901104"]["name"] == "Carla Noël" and valid["+50937901105"]["name"] == "Didi Pierre",
      json.dumps(pv["preview"]["valid"], ensure_ascii=False))
dups = {x["line"]: x for x in pv["preview"]["duplicates"]}
check("aperçu : doublon répété dans la liste (ligne 6 = ligne 2) et doublon déjà en base",
      dups.get(6, {}).get("reason") == "répété (ligne 2)" and dups.get(7, {}).get("reason") == "déjà en base" and "assignee" in dups[7],
      json.dumps(pv["preview"]["duplicates"], ensure_ascii=False))
check("aperçu : rejets avec leur ligne et leur raison",
      {(x["line"], x["reason"]) for x in pv["preview"]["rejected"]} == {(8, "pas de numéro"), (9, "numéro invalide")}, json.dumps(pv["preview"]["rejected"], ensure_ascii=False))
check("aperçu : rien n'est écrit", rv.get("/api/ws/followups", params={"view": "all", "q": "37901101"}).json()["total"] == 0)
r = rv.post("/api/ws/followups/paste", json={"text": PASTE, "stage": "inscrit", "source": "non lus WhatsApp", "lang": "ht", "commit": True}, headers=h(rv))
imp = r.json()
check("collage, import : 5 ajoutées, assignées à moi", r.status_code == 200 and imp["committed"] and imp["added"] == 5 and imp["per_person"] == {"rvmembre": 5}, json.dumps(imp, ensure_ascii=False)[:300])
mine = rv.get("/api/ws/followups", params={"view": "all", "assignee": "rvmembre", "limit": 50}).json()["items"]
check("fiches collées : étape, source et langue choisies une fois", len(mine) == 5 and all(f["stage"] == "inscrit" and f["source"] == "non lus WhatsApp" and f["lang"] == "ht" and f["status"] == "a_contacter" for f in mine), json.dumps(mine, ensure_ascii=False)[:300])
r = rv.post("/api/ws/followups/paste", json={"text": PASTE, "commit": True}, headers=h(rv))
check("recoller la même liste : 0 ajoutée, 7 doublons", r.json()["added"] == 0 and r.json()["duplicates"] == 7, r.text[:200])
r = rv.post("/api/ws/followups/paste", json={"text": "Zed, 3790 1199", "assign": "spread", "usernames": ["mirv", "michael"]}, headers=h(rv))
check("répartir au collage : réservé aux responsables → 403", r.status_code == 403, r.text)
spread_text = "\n".join(f"Client {i}, 3790 12{i:02d}" for i in range(5))
r = mirv.post("/api/ws/followups/paste", json={"text": spread_text, "assign": "spread", "usernames": ["mirv", "michael"], "commit": True}, headers=h(mirv))
check("répartir au collage selon les objectifs (150 / 100) : Michael 3, Mirv 2", r.status_code == 200 and r.json()["per_person"] == {"michael": 3, "mirv": 2}, r.text[:300])
r = mirv.post("/api/ws/followups/paste", json={"text": "x", "assign": "spread", "usernames": []}, headers=h(mirv))
check("répartir sans personne → 400", r.status_code == 400, r.text)

# ── Observateur : lecture seule ──
fid0 = mine[0]["id"]
check("observateur : collage → 403", obs.post("/api/ws/followups/paste", json={"text": "3790 1300"}, headers=h(obs)).status_code == 403)
check("observateur : action groupée → 403", obs.post("/api/ws/followups/bulk", json={"ids": [fid0], "changes": {"status": "perdu"}}, headers=h(obs)).status_code == 403)
check("observateur : « Pas de réponse » → 403", obs.post(f"/api/ws/followups/{fid0}/event", json={"kind": "sans_reponse"}, headers=h(obs)).status_code == 403)
check("observateur : « Marquer comme envoyé » → 403", obs.post(f"/api/ws/followups/{fid0}/whatsapp", json={"via": "copie"}, headers=h(obs)).status_code == 403)
check("observateur : lit la progression du jour (200)", obs.get("/api/ws/followups/today").status_code == 200)

# ── Une fiche en français dans la file, pour la langue du message ──
r = rv.post("/api/ws/followups", json={"name": "Zoé Lafrance", "phone": "3790 1150", "stage": "achat_commence", "assignee": "rvmembre"}, headers=h(rv))
fr_fiche = r.json()["followup"]
check("fiche créée sans langue : langue par défaut (fr)", r.status_code == 201 and fr_fiche["lang"] == "fr", r.text)

# ── Mode rafale côté API : la fiche suivante, et les actions qui la font avancer ──
nx = rv.get("/api/ws/followups/next").json()
check("rafale : une fiche à traiter, 6 restantes, 0 / 20 conversations", nx["followup"] is not None and nx["remaining"] == 6 and nx["conversations"] == 0 and nx["target"] == 20, json.dumps({k: nx[k] for k in ("remaining", "conversations", "target")}))
check("rafale : historique court et lien wa.me fourni", isinstance(nx["events"], list) and nx["wa_link"].startswith("https://wa.me/" + nx["followup"]["phone"].lstrip("+")))
check("« reste à traiter » (badge) = fiches restantes du mode rafale", rv.get("/api/ws/followups", params={"view": "mine", "limit": 1}).json()["to_treat"] == 6)
seen_lang = {}


def lang_ok(n):
    f, msg, tpl = n["followup"], n["message"], n["template"] or {}
    first = (f["name"] or "").split(" ")[0]
    if f["lang"] == "ht":
        ok = msg.startswith(f"Bonjou {first}".strip()) and not msg.startswith("Bonjour") and tpl.get("lang") == "ht" and "Rose" in msg
    else:
        ok = msg.startswith(f"Bonjour {first}".strip()) and tpl.get("lang") == "fr"
    seen_lang.setdefault(f["lang"], []).append(ok)


lang_ok(nx)
first_id = nx["followup"]["id"]
sk = rv.get("/api/ws/followups/next", params={"skip": first_id}).json()
check("rafale : « skip » saute la fiche affichée", sk["followup"] and sk["followup"]["id"] != first_id and sk["remaining"] == 6)
r = rv.post(f"/api/ws/followups/{first_id}/event", json={"kind": "sans_reponse"}, headers=h(rv))
ev = r.json()
check("« Pas de réponse » sans message noté : une tentative (relance J+1)", r.status_code == 200 and ev["event"]["kind"] == "message" and ev["followup"]["attempts"] == 1
      and ev["followup"]["next_followup"] == (TODAY + timedelta(days=1)).isoformat() and ev["event"]["note"].startswith("Pas de réponse"), json.dumps(ev)[:300])
nx = rv.get("/api/ws/followups/next").json()
check("rafale : la fiche suivante est une autre, 5 restantes, 1 conversation", nx["followup"]["id"] != first_id and nx["remaining"] == 5 and nx["conversations"] == 1)
lang_ok(nx)
fid = nx["followup"]["id"]
custom = "Bonjou, mesaj sa a chanje nan paj la"
r = rv.post(f"/api/ws/followups/{fid}/whatsapp", json={"template_id": (nx["template"] or {}).get("id"), "message": custom}, headers=h(rv))
w = r.json()
check("Ouvrir WhatsApp avec le message modifié : lien wa.me avec ce texte", r.status_code == 200 and unquote(w["wa_link"].split("?text=", 1)[1]) == custom and w["followup"]["attempts"] == 1, w.get("wa_link", "")[:120])
r = rv.post(f"/api/ws/followups/{fid}/event", json={"kind": "sans_reponse"}, headers=h(rv))
check("« Pas de réponse » après WhatsApp : simple note, pas de 2e tentative", r.json()["event"]["kind"] == "note" and r.json()["followup"]["attempts"] == 1, r.text[:200])
nx = rv.get("/api/ws/followups/next").json()
check("rafale : 4 restantes, 2 conversations (une fiche compte une fois)", nx["remaining"] == 4 and nx["conversations"] == 2, json.dumps({k: nx[k] for k in ("remaining", "conversations")}))
lang_ok(nx)
fid = nx["followup"]["id"]
r = rv.post(f"/api/ws/followups/{fid}/whatsapp", json={"via": "copie", "message": "copie"}, headers=h(rv))
check("Copier puis « Marquer comme envoyé » : message noté (copié pour WhatsApp Business)", r.status_code == 200 and r.json()["event"]["kind"] == "message" and "copié" in r.json()["event"]["note"], r.text[:200])
rv.post(f"/api/ws/followups/{fid}/event", json={"kind": "reponse"}, headers=h(rv))
rv.post(f"/api/ws/followups/{fid}/event", json={"kind": "close", "cards": 1}, headers=h(rv))
nx = rv.get("/api/ws/followups/next").json()
check("après A répondu puis Closé : 3 restantes, 3 conversations", nx["remaining"] == 3 and nx["conversations"] == 3)
lang_ok(nx)
fid = nx["followup"]["id"]
r = rv.post(f"/api/ws/followups/{fid}/event", json={"kind": "perdu"}, headers=h(rv))
nx = rv.get("/api/ws/followups/next").json()
check("Perdu : la fiche sort du reste (2), sans compter comme conversation (3)", r.status_code == 200 and nx["remaining"] == 2 and nx["conversations"] == 3)
while nx["followup"]:
    lang_ok(nx)
    rv.post(f"/api/ws/followups/{nx['followup']['id']}/event", json={"kind": "reponse"}, headers=h(rv))
    nx = rv.get("/api/ws/followups/next").json()
check("rafale : file vidée (0 restante, plus de fiche)", nx["followup"] is None and nx["remaining"] == 0)
check("message proposé dans la langue de la fiche (créole et français vus)",
      seen_lang.get("ht") and all(seen_lang["ht"]) and seen_lang.get("fr") and all(seen_lang["fr"]), json.dumps(seen_lang))
check("une fiche « Achat commencé » en français reçoit le modèle français de son étape",
      fr_fiche["stage"] == "achat_commence" and any(t["name"] == "Achat commencé" for t in fr))

# La langue suit la fiche : la même fiche, passée en créole, reçoit le modèle créole de son étape.
r = rv.post("/api/ws/followups", json={"name": "Jean Lang", "phone": "3790 1160", "stage": "inscrit", "assignee": "rvmembre", "lang": "fr"}, headers=h(rv))
lid = r.json()["followup"]["id"]
a = rv.get("/api/ws/followups/next").json()
rv.patch(f"/api/ws/followups/{lid}", json={"lang": "ht"}, headers=h(rv))
b = rv.get("/api/ws/followups/next").json()
check("changer la langue d'une fiche change le modèle proposé (fr → ht)",
      a["followup"]["id"] == lid and a["message"].startswith("Bonjour Jean") and b["template"]["name"] == "Enskri san KYC" and b["message"].startswith("Bonjou Jean"),
      f"{a['message'][:40]} / {b['message'][:40]}")
rv.post(f"/api/ws/followups/{lid}/event", json={"kind": "sans_reponse"}, headers=h(rv))

# ── Objectif et compteur de conversations ──
td = rv.get("/api/ws/followups/today").json()
check("progression du jour : 6 conversations / 20, reste 0", td["me"]["conversations"] == 6 and td["me"]["target"] == 20 and td["me"]["to_treat"] == 0, json.dumps(td["me"]))
pm = {p["username"]: p for p in td["people"]}
check("progression : Michael objectif 150, Mirv 100", pm["michael"]["target"] == 150 and pm["mirv"]["target"] == 100, json.dumps(td["people"])[:300])
ev = mirv.get("/api/ws/evening").json()
per = {p["username"]: p for p in ev["people"]}
check("tableau du soir : rvmembre 6 conversations / 20, reste à traiter 0",
      per["rvmembre"]["conversations"] == 6 and per["rvmembre"]["conversations_target"] == 20 and per["rvmembre"]["to_treat"] == 0, json.dumps(per.get("rvmembre"), ensure_ascii=False)[:300])
check("tableau du soir : reste à traiter de Michael = sa file due non traitée (5)", per["michael"]["to_treat"] == pm["michael"]["to_treat"] == 5, f"{per['michael']['to_treat']} / {pm['michael']['to_treat']}")
check("tableau du soir : totaux (conversations, reste à traiter de l'équipe)",
      ev["followups"]["conversations"] >= 6 and ev["followups"]["to_treat"] == td["team"]["to_treat"] and ev["followups"]["to_treat"] >= 5, json.dumps(ev["followups"]))
past = mirv.get("/api/ws/evening", params={"day": (TODAY - timedelta(days=1)).isoformat()}).json()
check("tableau du soir d'un jour passé : pas de « reste à traiter »", past["followups"]["to_treat"] is None)

# ── Actions groupées (Toutes les fiches) ──
spread_ids = [f["id"] for f in mirv.get("/api/ws/followups", params={"view": "all", "q": "Client", "limit": 50}).json()["items"]]
in2 = (TODAY + timedelta(days=2)).isoformat()
r = rv.post("/api/ws/followups/bulk", json={"ids": spread_ids, "changes": {"assignee": "rvmembre", "status": "contacte", "stage": "kyc", "next_followup": in2, "lang": "ht"}}, headers=h(rv))
check("action groupée (membre) : 5 fiches assignées, statut, étape, langue, relance programmée", r.status_code == 200 and r.json()["updated"] == 5 and len(spread_ids) == 5, r.text[:200])
got = [rv.get(f"/api/ws/followups/{i}").json() for i in spread_ids]
check("chaque fiche a bien changé, avec une note « Statut → Contacté (action groupée) »",
      all(g["followup"]["assignee"] == "rvmembre" and g["followup"]["status"] == "contacte" and g["followup"]["stage"] == "kyc"
          and g["followup"]["lang"] == "ht" and g["followup"]["next_followup"] == in2 and any("action groupée" in e["note"] for e in g["events"]) for g in got))
check("relance programmée plus tard : les fiches attendent leur date (reste 0)", rv.get("/api/ws/followups/next").json()["remaining"] == 0)
r = rv.post("/api/ws/followups/bulk", json={"ids": spread_ids[:2], "changes": {"next_followup": TODAY.isoformat()}}, headers=h(rv))
check("programmer la relance à aujourd'hui : elles reviennent dans la file (2)", r.status_code == 200 and rv.get("/api/ws/followups/next").json()["remaining"] == 2)
check("action groupée : personne inconnue → 400", rv.post("/api/ws/followups/bulk", json={"ids": spread_ids, "changes": {"assignee": "personne"}}, headers=h(rv)).status_code == 400)
check("action groupée : rien à changer → 400", rv.post("/api/ws/followups/bulk", json={"ids": spread_ids, "changes": {}}, headers=h(rv)).status_code == 400)
check("action groupée sans jeton CSRF → 403", rv.post("/api/ws/followups/bulk", json={"ids": spread_ids, "changes": {"lang": "fr"}}).status_code == 403)

# ── Un modèle créole créé par un responsable ──
r = mirv.post("/api/ws/message-templates", json={"name": "Relans", "stage": "", "lang": "ht", "description": "à relire", "body": "Bonjou {prenom} !"}, headers=h(mirv))
check("création d'un modèle en créole (langue et description gardées)", r.status_code == 201 and r.json()["template"]["lang"] == "ht" and r.json()["template"]["description"] == "à relire", r.text)

# ── Journaux : aucun numéro collé ──
log = (SCR / "server.log").read_text(encoding="utf-8", errors="replace") if (SCR / "server.log").exists() else ""
check("journal du serveur : aucun des numéros collés", log and not re.search(r"3790[ \-]?1[12]\d\d", log), "numéro trouvé" if log else "journal absent")

ok = sum(1 for r in results if r[0])
print(f"\n{ok}/{len(results)} vérifications réussies")
sys.exit(0 if ok == len(results) else 1)
