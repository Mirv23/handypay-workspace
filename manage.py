"""Commandes d'administration de l'espace d'équipe HandyPay.

    python manage.py create-owner --username mirv --name "Mirv" [--whatsapp "3412 3456"]
        Crée un responsable. Le mot de passe est GÉNÉRÉ et affiché UNE seule fois.
        Si la base est vide, crée aussi l'objectif de la campagne (cf. seed).

    python manage.py reset-password --username mirv
        Nouveau mot de passe (affiché une fois) ; les sessions ouvertes tombent.

    python manage.py seed
        Objectif « 3 000 cartes » et ses résultats clés reliés aux ventes, et les
        5 modèles de message de départ. Ne fait rien si c'est déjà en place.

Mêmes variables d'environnement que le serveur : sans SUPABASE_URL, la base
locale `.data/` est utilisée.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from app import auth, security
from app.db import db, run


def _out(text: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # console Windows
    except Exception:  # noqa: BLE001
        pass
    print(text)


def seed(client, actor: str) -> list[str]:
    from app import followups, sales
    from app import workspace as ws

    done = []
    if not run(client.table(ws.T_OBJECTIVES).select("id").limit(1), "lecture des objectifs"):
        cfg = sales.get_sales_settings(client)
        oid = str(uuid.uuid4())
        run(client.table(ws.T_OBJECTIVES).insert({
            "id": oid, "title": f"{cfg['total_target']:,} cartes vendues du 16 nov. au 15 déc. 2026".replace(",", " "),
            "description": f"{cfg['daily_target']} cartes par jour, marge de {cfg['margin_per_card']:g} $ par carte. "
                           "Jalons : " + ", ".join(f"{m['target']} le {m['date']}" for m in cfg["milestones"]) + ".",
            "period": f"{cfg['start']} → {cfg['end']}", "owner_username": actor, "status": "en_cours",
            "start_date": cfg["start"], "due_date": cfg["end"], "created_by": actor,
        }), "objectif")
        run(client.table(ws.T_KRS).insert([
            {"id": str(uuid.uuid4()), "objective_id": oid, "title": "Cartes vendues (cumul)", "unit": "cartes",
             "start_value": 0, "current_value": 0, "target_value": cfg["total_target"], "position": 0, "auto": "ventes_cumul"},
            {"id": str(uuid.uuid4()), "objective_id": oid, "title": "Marge cumulée", "unit": cfg["currency"],
             "start_value": 0, "current_value": 0, "target_value": cfg["total_target"] * cfg["margin_per_card"],
             "position": 1, "auto": "marge_cumul"},
        ]), "résultats clés")
        sales.refresh_auto_krs(client)
        done.append("objectif de la campagne et 2 résultats clés reliés aux ventes")
    before = sales.get_setting(client, "templates_seeded")
    followups.list_templates(client)
    if not before:
        done.append("5 modèles de message de départ")
    return done


def create_owner(args) -> int:
    client = db()
    username = args.username or input("Nom d'utilisateur (ex. mirv) : ").strip()
    name = args.name or input("Nom affiché (ex. Mirv) : ").strip()
    whatsapp = args.whatsapp if args.whatsapp is not None else input("Numéro WhatsApp (facultatif) : ").strip()
    try:
        username = auth.check_new_username(client, username)
        phone = auth.check_whatsapp(client, whatsapp or "")
        _account, password = auth.create_account(
            client, username=username, display_name=name or username, role="responsable",
            whatsapp=phone, created_by="manage.py",
        )
    except Exception as exc:  # noqa: BLE001 - HTTPException ou panne
        _out("Erreur : " + str(getattr(exc, "detail", exc)))
        return 1
    _out(f"Responsable créé : {username}" + (f" (WhatsApp {phone})" if phone else ""))
    _out("")
    _out(f"  Mot de passe : {password}")
    _out("")
    _out("Note-le maintenant : il ne sera plus jamais affiché. Change-le ensuite dans « Mon profil ».")
    for item in seed(client, username):
        _out("Ajouté : " + item)
    return 0


def reset_password(args) -> int:
    client = db()
    username = args.username or input("Nom d'utilisateur : ").strip()
    try:
        password = auth.reset_password(client, security.clean_username(username))
    except Exception as exc:  # noqa: BLE001
        _out("Erreur : " + str(getattr(exc, "detail", exc)))
        return 1
    _out(f"Nouveau mot de passe pour {username} : {password}")
    _out("Il ne sera plus jamais affiché. Les sessions ouvertes de ce compte sont fermées.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="manage.py", description="Administration de l'espace d'équipe HandyPay.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-owner", help="créer un responsable (mot de passe généré, affiché une fois)")
    c.add_argument("--username")
    c.add_argument("--name")
    c.add_argument("--whatsapp")
    r = sub.add_parser("reset-password", help="nouveau mot de passe pour un compte")
    r.add_argument("--username")
    sub.add_parser("seed", help="objectif de la campagne et modèles de message, s'ils manquent")
    args = p.parse_args(argv)
    if args.cmd == "create-owner":
        return create_owner(args)
    if args.cmd == "reset-password":
        return reset_password(args)
    if args.cmd == "seed":
        for item in seed(db(), "manage.py") or ["rien à faire : déjà en place"]:
            _out("· " + item)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
