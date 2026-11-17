"""L'IA déclare son travail dans l'espace d'équipe HandyPay, et y envoie les ventes.

Outil en ligne de commande, sans dépendance (bibliothèque standard seule) :
l'agent l'appelle pour lire sa file du matin, se donner une tâche, cocher ses
grands points, la terminer, et enregistrer les ventes venues de HandyPay.
L'équipe suit ce travail dans l'espace ; le détail technique (journal) reste
réservé aux responsables.

Réglages (variables d'environnement, jamais dans le code) :
- `WORKSPACE_IA_TOKEN` : jeton de l'IA, le même que sur le serveur ;
- `WORKSPACE_API_URL` : adresse de l'espace (ex. https://equipe.exemple.com).

Exemples :
    python ia_cli.py matin
    python ia_cli.py nouvelle "Corriger le blocage KYC" --point "Reproduire" --point "Corriger" --point "Vérifier en prod"
    python ia_cli.py cocher HP-12 1 2
    python ia_cli.py journal HP-12 "kyc.py : délai d'attente porté à 30 s"
    python ia_cli.py terminer HP-12 "Le KYC passe sur Android 9"
    python ia_cli.py vente --id hp-ord-48213 --cartes 1 --membre michael
    python ia_cli.py stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_API_URL = "http://127.0.0.1:8000"
PREFIX = "/api/workspace/ia"
STATUTS = ("a_faire", "en_cours", "en_revue", "bloque", "fait")
STATUT_L = {"a_faire": "à faire", "en_cours": "en cours", "en_revue": "en revue", "bloque": "bloquée", "fait": "faite"}


class Erreur(Exception):
    pass


def appel(method: str, path: str, body: Optional[dict] = None, *, opener: Callable = urllib.request.urlopen) -> dict:
    token = (os.environ.get("WORKSPACE_IA_TOKEN") or "").strip()
    if not token:
        raise Erreur("WORKSPACE_IA_TOKEN n'est pas défini : l'IA n'est pas branchée sur cet ordinateur.")
    base = (os.environ.get("WORKSPACE_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + PREFIX + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "handypay-workspace-ia/1",
    })
    try:
        with opener(req, timeout=60) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:  # noqa: BLE001 - corps illisible
            detail = None
        raise Erreur(f"{exc.code} : {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise Erreur(f"Serveur injoignable ({exc.reason}). Un serveur endormi (Render gratuit) met ~1 min à se réveiller : réessaie.")


def ligne(t: dict) -> str:
    faits = sum(1 for p in t.get("points") or [] if p.get("fait"))
    pts = f"  [{faits}/{len(t['points'])}]" if t.get("points") else ""
    quand = f"  pour le {t['echeance']}" if t.get("echeance") else ""
    raison = f"  ({t['raison']})" if t.get("raison") else ""
    return f"{t['cle']}  {t['titre']}{pts}{quand}{raison}"


def afficher_tache(t: dict) -> str:
    out = [ligne(t), f"  statut : {STATUT_L.get(t['statut'], t['statut'])} · projet : {t.get('projet') or '—'} · branche : {t.get('branche')}"]
    for p in t.get("points") or []:
        out.append(f"  {'[x]' if p['fait'] else '[ ]'} {p['n']}. {p['texte']}")
    if t.get("description"):
        out.append("  consigne : " + t["description"].replace("\n", "\n    "))
    for c in t.get("commentaires") or []:
        out.append(f"  — {c['de']} : {c['texte']}")
    return "\n".join(out)


def afficher_matin(r: dict) -> str:
    out = [f"File de l'IA — {r.get('date')}"]
    if not r.get("file"):
        out.append("  Rien dans la file. Crée une tâche (« nouvelle ») avant tout travail de plus d'une étape.")
    for t in r.get("file") or []:
        out.append("  " + ligne(t))
    if r.get("messages"):
        out.append("Messages :")
        for m in r["messages"]:
            out.append(f"  {m.get('cle') or '—'}  {m['texte']}")
    return "\n".join(out)


def principal(argv: Optional[list[str]] = None, *, opener: Callable = urllib.request.urlopen, sortie=None) -> int:
    sortie = sortie or sys.stdout
    p = argparse.ArgumentParser(prog="ia_cli", description="L'IA déclare son travail dans l'espace d'équipe HandyPay.")
    p.add_argument("--json", action="store_true", help="réponse brute du serveur")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("matin", help="ma file : tâches confiées, dans l'ordre, et messages")
    n = sub.add_parser("nouvelle", help="me donner une tâche (visible de toute l'équipe)")
    n.add_argument("titre")
    n.add_argument("--point", action="append", default=[], help="un grand point (répétable)")
    n.add_argument("--projet", default="")
    n.add_argument("--priorite", choices=("urgente", "haute", "normale", "basse"), default="normale")
    n.add_argument("--echeance", default=None, help="AAAA-MM-JJ")
    n.add_argument("--plus-tard", action="store_true", help="créer « à faire » au lieu de « en cours »")
    n.add_argument("--journal", default="", help="détail technique (responsables seulement)")
    v = sub.add_parser("voir", help="une tâche, ses points et ses commentaires")
    v.add_argument("cle")
    c = sub.add_parser("cocher", help="cocher des grands points (numéros)")
    c.add_argument("cle")
    c.add_argument("numeros", nargs="+", type=int)
    a = sub.add_parser("point", help="ajouter un grand point")
    a.add_argument("cle")
    a.add_argument("texte")
    s = sub.add_parser("statut", help="changer le statut")
    s.add_argument("cle")
    s.add_argument("statut", choices=STATUTS)
    j = sub.add_parser("journal", help="détail technique (responsables seulement)")
    j.add_argument("cle")
    j.add_argument("texte")
    m = sub.add_parser("commenter", help="un message visible de l'équipe")
    m.add_argument("cle")
    m.add_argument("texte")
    b = sub.add_parser("bloquer", help="bloquée : dire pourquoi à l'équipe")
    b.add_argument("cle")
    b.add_argument("raison")
    t = sub.add_parser("terminer", help="cocher tous les points, passer à « fait », résumer")
    t.add_argument("cle")
    t.add_argument("resume")
    ve = sub.add_parser("vente", help="enregistrer une vente HandyPay (idempotent par --id)")
    ve.add_argument("--id", required=True, help="identifiant unique de la vente dans HandyPay (external_id)")
    ve.add_argument("--cartes", type=int, default=1)
    ve.add_argument("--membre", default="", help="nom d'utilisateur du vendeur, s'il est connu")
    ve.add_argument("--jour", default=None, help="AAAA-MM-JJ (aujourd'hui à Port-au-Prince par défaut)")
    ve.add_argument("--source", default="autre", choices=("whatsapp", "appel", "pub", "parrainage", "autre"))
    sub.add_parser("stats", help="cartes du jour, de la semaine et de la campagne")
    args = p.parse_args(argv)

    try:
        if args.cmd == "matin":
            r = appel("GET", "/matin", opener=opener)
            texte = afficher_matin(r)
        elif args.cmd == "nouvelle":
            r = appel("POST", "/taches", {
                "titre": args.titre, "points": args.point, "projet": args.projet, "priorite": args.priorite,
                "echeance": args.echeance, "commencer": not args.plus_tard, "journal": args.journal,
            }, opener=opener)
            texte = "Créée : " + afficher_tache(r["tache"])
        elif args.cmd == "vente":
            body = {"external_id": args.id, "cartes": args.cartes, "membre": args.membre, "source": args.source}
            if args.jour:
                body["jour"] = args.jour
            r = appel("POST", "/ventes", body, opener=opener)
            v = r["vente"]
            debut = "Déjà enregistrée (doublon ignoré) : " if r.get("doublon") else "Vente enregistrée : "
            texte = debut + f"{v['cartes']} carte(s), {v['membre'] or 'direct HandyPay'}, {v['jour']}"
        elif args.cmd == "stats":
            r = appel("GET", "/ventes/stats", opener=opener)
            c = r["campaign"]
            texte = "\n".join([
                f"Aujourd'hui : {r['today']['cards']} / {r['today']['target']} cartes",
                f"Semaine : {r['week']['cards']}",
                f"Cumul : {c['cumul']} / {c['target']} (écart au rythme : {c['gap']:+d})",
            ])
        elif args.cmd == "voir":
            r = appel("GET", "/taches/" + args.cle, opener=opener)
            texte = afficher_tache(r["tache"])
        else:
            body: dict[str, Any] = {}
            if args.cmd == "cocher":
                body["cocher"] = args.numeros
            elif args.cmd == "point":
                body["ajouter_points"] = [args.texte]
            elif args.cmd == "statut":
                body["statut"] = args.statut
            elif args.cmd == "journal":
                body["journal"] = args.texte
            elif args.cmd == "commenter":
                body["commentaire"] = args.texte
            elif args.cmd == "bloquer":
                body.update(statut="bloque", commentaire="Bloquée : " + args.raison)
            elif args.cmd == "terminer":
                current = appel("GET", "/taches/" + args.cle, opener=opener)["tache"]
                body.update(statut="fait", cocher=[x["n"] for x in current.get("points") or [] if not x.get("fait")],
                            commentaire="Terminé : " + args.resume)
            r = appel("PATCH", "/taches/" + args.cle, body, opener=opener)
            texte = afficher_tache(r["tache"])
    except Erreur as exc:
        print("Erreur : " + str(exc), file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else texte, file=sortie)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # console Windows
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(principal())
