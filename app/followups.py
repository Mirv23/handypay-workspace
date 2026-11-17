"""Relances WhatsApp : fiches clients, file du jour, modèles de message.

ENVOI PAR UN HUMAIN, JAMAIS AUTOMATIQUE. « Ouvrir WhatsApp » ouvre
`https://wa.me/<numéro>?text=<message rempli>` dans un nouvel onglet : la
personne relit et appuie sur Envoyer depuis le numéro de l'équipe. Aucun envoi
non officiel (risque de bannissement du numéro). L'étape suivante, l'API
officielle WhatsApp Business Cloud, est décrite dans le README.

UNE FICHE = UN NUMÉRO. Le téléphone est normalisé (`+509XXXXXXXX`) et unique :
l'import CSV écarte les doublons et le dit dans son rapport.

LA SÉQUENCE. Chaque tentative (message ou appel) programme la prochaine
relance : par défaut J+1 après la 1re, J+3 après la 2e, J+7 après la 3e (comptés
depuis la tentative). Une réponse remet le compteur à zéro. Après 3 tentatives
sans réponse, la fiche passe en « froid » : elle revient une dernière fois à la
date prévue, puis c'est à la personne de décider (perdu, pas intéressé…).

DONNÉES PERSONNELLES. Les numéros ne sortent que vers des comptes connectés ;
l'export CSV des fiches est réservé aux responsables ; aucun numéro n'est écrit
dans les journaux du serveur (cf. le filtre de `main.py`).

RELANCES À GRAND VOLUME (v2, cf. docs/relances.md) : mode rafale (la fiche
suivante à traiter, une à la fois), objectif de conversations par personne,
« reste à traiter aujourd'hui », ajout rapide par collage, actions groupées,
langue de la fiche (fr / ht) et modèles en créole. Colonnes ajoutées par
sql/relances_v2.sql.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
import uuid
from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import sales, security, timeutil
from app import workspace as ws
from app.db import count as db_count, fetch_all

logger = logging.getLogger("handypay.followups")
router = APIRouter()

T_FOLLOWUPS = "workspace_followups"
T_EVENTS = "workspace_followup_events"
T_TEMPLATES = "workspace_message_templates"

STAGES = ("inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete")
STAGE_LABEL = {
    "inscrit": "Inscrit", "kyc": "KYC validé", "achat_commence": "Achat commencé", "a_paye": "A payé",
    "carte_emise": "Carte émise", "carte_utilisee": "Carte utilisée", "a_rachete": "A racheté",
}
STATUSES = ("a_contacter", "contacte", "a_repondu", "close", "perdu", "pas_interesse", "froid")
STATUS_LABEL = {
    "a_contacter": "À contacter", "contacte": "Contacté", "a_repondu": "A répondu", "close": "Closé",
    "perdu": "Perdu", "pas_interesse": "Pas intéressé", "froid": "Froid",
}
CLOSED = ("close", "perdu", "pas_interesse")
EVENT_KINDS = ("message", "appel", "reponse", "close", "note", "perdu", "pas_interesse")
ATTEMPTS = ("message", "appel")
# Une conversation du jour : une fiche avec au moins un de ces événements, par cette personne, ce jour-là.
CONVERSATION_KINDS = ("message", "appel", "reponse", "close")
# Une fiche « traitée » aujourd'hui (par quiconque) sort du « reste à traiter ».
TREATED_KINDS = CONVERSATION_KINDS + ("perdu", "pas_interesse")
LANGS = ("fr", "ht")
LANG_LABEL = {"fr": "Français", "ht": "Créole"}

DEFAULT_FOLLOWUP = {
    "sequence": [1, 3, 7], "max_attempts": 3, "conversations_target": 300,
    # Objectif de conversations par jour et par personne (les autres : 0).
    "targets": {"michael": 150, "mirv": 100},
    # Langue d'une fiche nouvelle quand rien n'est précisé.
    "default_lang": "fr",
}

_REVIEW = "Modèle de départ en créole : à relire par l'équipe avant de l'utiliser en masse."

# Les modèles de départ : courts, naturels, à adapter par un responsable. Chacun a une clé stable :
# un modèle de départ s'ajoute une seule fois (supprimé ensuite, il ne revient pas) et n'écrase jamais
# un modèle déjà en base.
DEFAULT_TEMPLATES = [
    {"key": "fr_premier_contact", "lang": "fr", "name": "Premier contact", "stage": "",
     "body": "Bonjour {prenom}, ici {moi} de HandyPay. Vous voulez payer en ligne depuis Haïti (abonnements, achats "
             "sur internet) ? On peut vous faire une carte virtuelle aujourd'hui. Je vous explique ?"},
    {"key": "fr_inscrit", "lang": "fr", "name": "Inscrit sans KYC", "stage": "inscrit",
     "body": "Bonjour {prenom}, c'est {moi} de HandyPay. Votre compte est créé, il ne manque que la vérification "
             "d'identité pour avoir votre carte. Vous bloquez sur une étape ? Je vous aide en 2 minutes."},
    {"key": "fr_achat_commence", "lang": "fr", "name": "Achat commencé", "stage": "achat_commence",
     "body": "Bonjour {prenom}, ici {moi} de HandyPay. Vous avez commencé l'achat de votre carte sans le terminer. "
             "Un souci au paiement ? On peut le régler ensemble maintenant."},
    {"key": "fr_carte_emise", "lang": "fr", "name": "Carte jamais utilisée", "stage": "carte_emise",
     "body": "Bonjour {prenom}, c'est {moi} de HandyPay. Votre carte est prête mais pas encore utilisée. "
             "Je vous montre comment faire votre premier achat en ligne avec ?"},
    {"key": "fr_rachat", "lang": "fr", "name": "Rachat", "stage": "carte_utilisee",
     "body": "Bonjour {prenom}, ici {moi} de HandyPay. Merci d'utiliser votre carte ! Il vous en faut une autre, "
             "ou une recharge pour vos prochains achats ? Je m'en occupe."},
    {"key": "ht_premier_contact", "lang": "ht", "name": "Premye kontak", "stage": "", "description": _REVIEW,
     "body": "Bonjou {prenom}, se {moi} nan HandyPay. Ou bezwen peye sou entènèt depi Ayiti (abònman, acha an liy) ? "
             "Nou ka fè yon kat vityèl pou ou jodi a. Ou vle m esplike w ?"},
    {"key": "ht_inscrit", "lang": "ht", "name": "Enskri san KYC", "stage": "inscrit", "description": _REVIEW,
     "body": "Bonjou {prenom}, se {moi} nan HandyPay. Kont ou kreye deja, se verifikasyon idantite a sèlman ki rete "
             "pou w jwenn kat ou. Ou bloke yon kote ? M ka ede w nan 2 minit."},
    {"key": "ht_achat_commence", "lang": "ht", "name": "Acha kòmanse", "stage": "achat_commence", "description": _REVIEW,
     "body": "Bonjou {prenom}, se {moi} nan HandyPay. Ou te kòmanse achte kat ou men ou pa t fini. "
             "Ou te gen yon pwoblèm ak peman an ? Nou ka regle l ansanm kounye a."},
    {"key": "ht_carte_emise", "lang": "ht", "name": "Kat poko sèvi", "stage": "carte_emise", "description": _REVIEW,
     "body": "Bonjou {prenom}, se {moi} nan HandyPay. Kat ou pare men ou poko itilize l. "
             "Ou vle m montre w kijan pou fè premye acha w sou entènèt ak li ?"},
    {"key": "ht_rachat", "lang": "ht", "name": "Re-acha", "stage": "carte_utilisee", "description": _REVIEW,
     "body": "Bonjou {prenom}, se {moi} nan HandyPay. Mèsi dèske w ap itilize kat ou ! Ou bezwen yon lòt kat, "
             "oswa yon rechaj pou pwochen acha w yo ? M ap okipe sa."},
]
_FR_SEED_KEYS = [t["key"] for t in DEFAULT_TEMPLATES if t["lang"] == "fr"]


# ═══════════════════════════════════════════════════════════════════
# Réglages et modèles
# ═══════════════════════════════════════════════════════════════════

def normalize_followup_settings(raw: dict) -> dict:
    d = DEFAULT_FOLLOWUP
    seq = []
    for v in raw.get("sequence") if isinstance(raw.get("sequence"), list) else d["sequence"]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 90:
            seq.append(n)
    try:
        max_attempts = int(raw.get("max_attempts", d["max_attempts"]))
    except (TypeError, ValueError):
        max_attempts = d["max_attempts"]
    try:
        target = int(raw.get("conversations_target", d["conversations_target"]))
    except (TypeError, ValueError):
        target = d["conversations_target"]
    targets: dict[str, int] = {}
    raw_targets = raw.get("targets") if isinstance(raw.get("targets"), dict) else d["targets"]
    for user, value in list(raw_targets.items())[:100]:
        name = security.clean_username(str(user))
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if name and 0 < n <= 100000:
            targets[name] = n
    lang = raw.get("default_lang")
    return {
        "sequence": seq[:10] or list(d["sequence"]),
        "max_attempts": max_attempts if 1 <= max_attempts <= 20 else d["max_attempts"],
        "conversations_target": target if 1 <= target <= 100000 else d["conversations_target"],
        "targets": targets,
        "default_lang": lang if lang in LANGS else d["default_lang"],
    }


def get_followup_settings(client) -> dict:
    return normalize_followup_settings({**DEFAULT_FOLLOWUP, **sales.get_setting(client, "followups")})


def _lang(value: Any) -> str:
    return value if value in LANGS else "fr"


def _template_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "name": str(r.get("name") or ""),
        "stage": r.get("stage") if r.get("stage") in STAGES else "",
        "lang": _lang(r.get("lang")), "description": str(r.get("description") or ""),
        "body": str(r.get("body") or ""), "active": r.get("active", True) is not False,
        "position": int(r.get("position") or 0), "updated_at": r.get("updated_at"),
    }


def _read_templates(client) -> list[dict]:
    return ws._run(client.table(T_TEMPLATES).select("*").order("position").limit(200), "lecture des modèles")


def seed_templates(client, rows: Optional[list[dict]] = None) -> int:
    """Ajoute les modèles de départ qui n'ont encore jamais été posés. Idempotent.

    Le réglage « templates_seeded » garde les clés déjà posées. Une base d'avant
    la v2 (réglage sans clés, ou modèles déjà là) a eu ses 5 modèles français :
    seuls les modèles créoles s'y ajoutent. Un modèle de même nom et même langue
    déjà en base n'est jamais dupliqué ni écrasé.
    """
    rows = _read_templates(client) if rows is None else rows
    seeded = sales.get_setting(client, "templates_seeded")
    done = {str(k) for k in seeded.get("keys") or []}
    if (seeded.get("done") or rows) and not seeded.get("keys"):
        done |= set(_FR_SEED_KEYS)
    missing = [t for t in DEFAULT_TEMPLATES if t["key"] not in done]
    if not missing:
        return 0
    taken = {(" ".join(str(r.get("name") or "").split()).casefold(), _lang(r.get("lang"))) for r in rows}
    start = max([int(r.get("position") or 0) for r in rows] + [-1]) + 1
    new = []
    for t in missing:
        if (t["name"].casefold(), t["lang"]) in taken:
            continue
        new.append({"id": str(uuid.uuid4()), "name": t["name"], "stage": t["stage"], "body": t["body"],
                    "lang": t["lang"], "description": t.get("description", ""), "position": start + len(new),
                    "created_by": "système"})
    if new:
        ws._run(client.table(T_TEMPLATES).insert(new), "modèles de départ")
    sales.put_setting(client, "templates_seeded", {"done": True, "keys": sorted(done | {t["key"] for t in missing})}, "système")
    return len(new)


def list_templates(client) -> list[dict]:
    rows = _read_templates(client)
    if seed_templates(client, rows):
        rows = _read_templates(client)
    return [_template_out(r) for r in rows]


def pick_template(templates: list[dict], stage: str, lang: str) -> Optional[dict]:
    """Le modèle proposé pour une fiche : sa langue d'abord (étape, puis général), puis l'étape, puis le premier."""
    act = [t for t in templates if t["active"]]
    for group in ([t for t in act if t["lang"] == lang and t["stage"] == stage],
                  [t for t in act if t["lang"] == lang and not t["stage"]],
                  [t for t in act if t["lang"] == lang],
                  [t for t in act if t["stage"] == stage],
                  act):
        if group:
            return group[0]
    return None


def name_parts(name: str) -> tuple[str, str]:
    words = " ".join(str(name or "").split()).split(" ")
    words = [w for w in words if w]
    if not words:
        return "", ""
    return words[0], " ".join(words[1:])


def render_message(body: str, name: str, me_name: str) -> str:
    """Remplit {prenom}, {nom} (et {moi}) ; un champ vide ne laisse ni double espace ni « , ».

    `nom` : ce qui suit le prénom dans le nom complet (« Jean Pierre Louis » →
    prénom « Jean », nom « Pierre Louis »).
    """
    prenom, nom = name_parts(name)
    text = str(body or "")
    for key, value in (("prenom", prenom), ("nom", nom), ("moi", (me_name or "").split(" ")[0])):
        text = re.sub(r"\{\s*" + key + r"\s*\}", value, text, flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    text = re.sub(r"(^|\n)[ ,]+", r"\1", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# Fiches
# ═══════════════════════════════════════════════════════════════════

def fo_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""),
        "name": str(r.get("name") or ""),
        "phone": str(r.get("phone") or ""),
        "stage": r.get("stage") if r.get("stage") in STAGES else "inscrit",
        "status": r.get("status") if r.get("status") in STATUSES else "a_contacter",
        "lang": _lang(r.get("lang")),
        "assignee": r.get("assignee") or None,
        "next_followup": ws._date(r.get("next_followup")),
        "attempts": int(r.get("attempts") or 0),
        "first_contact_at": r.get("first_contact_at"),
        "last_contact_at": r.get("last_contact_at"),
        "source": str(r.get("source") or ""),
        "notes": str(r.get("notes") or ""),
        "sale_id": ws._id_or_none(r.get("sale_id")),
        "created_by": str(r.get("created_by") or ""),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _event_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "kind": r.get("kind"), "template_name": str(r.get("template_name") or ""),
        "note": str(r.get("note") or ""), "actor": str(r.get("actor") or ""), "day": ws._date(r.get("day")),
        "created_at": r.get("created_at"),
    }


def _fo_or_404(client, fid: str) -> dict:
    rid = ws._check_uuid(fid)
    rows = ws._run(client.table(T_FOLLOWUPS).select("*").eq("id", rid).limit(1), "lecture de la fiche")
    if not rows:
        raise HTTPException(status_code=404, detail="Fiche introuvable.")
    return rows[0]


def _event(client, followup_id: str, kind: str, actor: str, note: str = "", template: Optional[dict] = None) -> dict:
    row = {
        "id": str(uuid.uuid4()), "followup_id": followup_id, "kind": kind, "actor": actor,
        "note": note.strip()[:1000], "day": timeutil.today().isoformat(),
        "template_id": template["id"] if template else None, "template_name": (template or {}).get("name", "")[:80],
    }
    ws._run(client.table(T_EVENTS).insert(row), "journal de la fiche")
    return _event_out(row)


def _stage_key(raw: Any) -> Optional[str]:
    """Étape écrite à la main (« KYC fait », « achat commencé »…) → clé, ou None si inconnue."""
    text = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode().lower().strip()
    if not text:
        return ""
    if text in STAGES:
        return text
    for needle, key in (("rachet", "a_rachete"), ("rachat", "a_rachete"), ("utilis", "carte_utilisee"),
                        ("emis", "carte_emise"), ("pay", "a_paye"), ("achat", "achat_commence"),
                        ("kyc", "kyc"), ("inscri", "inscrit")):
        if needle in text:
            return key
    return None


def _check_assignee(client, username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    people = {m["username"] for m in ws._team(client) if not m.get("is_ai")}
    if username not in people:
        raise HTTPException(status_code=400, detail="Personne inconnue dans l'équipe.")
    return username


def due_filter(today: str) -> str:
    """Fiche « due » : relance prévue aujourd'hui ou avant, ou aucune date (à contacter, ou sans suite prévue).

    Une fiche à contacter dont la relance est programmée plus tard (action groupée)
    attend cette date.
    """
    return f"next_followup.lte.{today},next_followup.is.null"


def _queue_query(client, username: str, today: str, *, count: bool = False):
    q = client.table(T_FOLLOWUPS).select("*", count="exact") if count else client.table(T_FOLLOWUPS).select("*")
    return q.eq("assignee", username).not_.in_("status", list(CLOSED)).or_(due_filter(today))


def day_activity(client, iso: str) -> tuple[set[str], dict[str, set[str]]]:
    """Ce jour-là : les fiches traitées (par quiconque) et, par personne, les fiches de ses conversations."""
    events = fetch_all(
        lambda: client.table(T_EVENTS).select("followup_id,kind,actor").eq("day", iso).order("id"),
        "lecture du journal des relances",
    )
    treated: set[str] = set()
    conversations: dict[str, set[str]] = {}
    for e in events:
        fid, kind = str(e.get("followup_id") or ""), e.get("kind")
        if kind in TREATED_KINDS:
            treated.add(fid)
        if kind in CONVERSATION_KINDS:
            conversations.setdefault(str(e.get("actor") or ""), set()).add(fid)
    return treated, conversations


def due_rows(client, iso: str, username: Optional[str] = None) -> list[dict]:
    """Fiches ouvertes et dues (toutes, ou celles d'une personne) : de quoi compter et ordonner, sans numéro."""
    def build():
        q = (client.table(T_FOLLOWUPS).select("id,assignee,status,next_followup,created_at")
             .not_.in_("status", list(CLOSED)).or_(due_filter(iso)))
        if username:
            q = q.eq("assignee", username)
        return q.order("created_at").order("id")

    return fetch_all(build, "lecture de la file")


def _burst_order(r: dict) -> tuple:
    """Ordre du mode rafale : clients qui ont répondu, puis relances dues (la plus ancienne d'abord), puis nouveaux."""
    status, due = r.get("status"), ws._date(r.get("next_followup")) or ""
    group = 0 if status == "a_repondu" else (1 if due else 2)
    return (group, due, str(r.get("created_at") or ""), str(r.get("id")))


def progress(client, today: Optional[date] = None, team: Optional[list[dict]] = None) -> dict:
    """La journée de relances : conversations par personne (et objectif), reste à traiter."""
    iso = (today or timeutil.today()).isoformat()
    cfg = get_followup_settings(client)
    treated, conv = day_activity(client, iso)
    left: dict[str, int] = {}
    for r in due_rows(client, iso):
        if str(r.get("id")) not in treated:
            key = str(r.get("assignee") or "")
            left[key] = left.get(key, 0) + 1
    people = []
    for m in team if team is not None else ws._team(client):
        if m.get("is_ai"):
            continue
        user = m["username"]
        n, goal = len(conv.get(user, ())), cfg["targets"].get(user, 0)
        if m["role"] == "observateur" and not (n or goal or left.get(user)):
            continue
        people.append({"username": user, "display_name": m["display_name"], "role": m["role"],
                       "conversations": n, "target": goal, "to_treat": left.get(user, 0)})
    people.sort(key=lambda p: (-p["target"], -p["conversations"], p["display_name"].casefold()))
    return {
        "day": iso, "people": people,
        "team": {"conversations": sum(len(v) for v in conv.values()), "target": cfg["conversations_target"],
                 "targets_sum": sum(cfg["targets"].values()), "to_treat": sum(left.values()),
                 "unassigned": left.get("", 0)},
    }


def to_treat_for(client, username: str, iso: str, treated: Optional[set[str]] = None) -> int:
    if treated is None:
        treated, _ = day_activity(client, iso)
    return sum(1 for r in due_rows(client, iso, username) if str(r.get("id")) not in treated)


def _search(q, text: str):
    text = " ".join(str(text or "").split())[:60]
    if not text:
        return q
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 3 and not re.search(r"[A-Za-zÀ-ÿ]", text):
        return q.ilike("phone", f"%{digits}%")
    safe = re.sub(r"[%_,()*\\]", " ", text).strip()
    return q.ilike("name", f"%{safe}%") if safe else q


@router.get(ws.API + "/followups")
def list_followups(
    request: Request,
    view: Literal["mine", "all"] = "mine",
    stage: str = Query("", max_length=20),
    status: str = Query("", max_length=20),
    assignee: str = Query("", max_length=40),
    q: str = Query("", max_length=60),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=200),
):
    client = ws._db()
    me = ws._me(request, client)
    today = timeutil.today().isoformat()

    def build(with_count: bool):
        if view == "mine":
            query = _queue_query(client, me.username, today, count=with_count)
        else:
            query = client.table(T_FOLLOWUPS).select("*", count="exact") if with_count else client.table(T_FOLLOWUPS).select("*")
            if assignee == "__none":
                query = query.is_("assignee", "null")
            elif assignee:
                query = query.eq("assignee", assignee)
        if stage in STAGES:
            query = query.eq("stage", stage)
        if status in STATUSES:
            query = query.eq("status", status)
        return _search(query, q)

    try:
        res = build(True).order("created_at", desc=True).order("id").range(offset, offset + limit - 1).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture des fiches impossible (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Relances momentanément indisponibles.")
    rows = [r for r in (getattr(res, "data", None) or []) if isinstance(r, dict)]
    mine_due = db_count(_queue_query(client, me.username, today, count=True).limit(1), "file du jour")
    return {
        "items": [fo_out(r) for r in rows],
        "total": int(getattr(res, "count", None) or len(rows)),
        "offset": offset, "limit": limit, "mine_due": mine_due, "today": today,
        "to_treat": to_treat_for(client, me.username, today),
    }


# Ces deux routes passent AVANT /followups/{fid} : sinon « today » et « next » seraient lus comme des identifiants.

@router.get(ws.API + "/followups/today")
def followups_today(request: Request):
    """Progression du jour : conversations / objectif par personne, reste à traiter (le badge de la navigation)."""
    client = ws._db()
    me = ws._me(request, client)
    out = progress(client)
    mine = next((p for p in out["people"] if p["username"] == me.username), None)
    if mine is None:
        iso = out["day"]
        treated, conv = day_activity(client, iso)
        mine = {"username": me.username, "conversations": len(conv.get(me.username, ())),
                "target": get_followup_settings(client)["targets"].get(me.username, 0),
                "to_treat": to_treat_for(client, me.username, iso, treated)}
    out["me"] = mine
    return out


def _me_name(client, username: str) -> str:
    return next((m["display_name"] for m in ws._team(client) if m["username"] == username), username)


@router.get(ws.API + "/followups/next")
def next_followup(request: Request, skip: str = Query("", max_length=8000)):
    """Mode rafale : la prochaine fiche de MA file à traiter aujourd'hui, avec son message rempli dans sa langue.

    `skip` : identifiants (séparés par des virgules) à sauter, déjà affichés côté page.
    """
    client = ws._db()
    me = ws._me(request, client)
    iso = timeutil.today().isoformat()
    treated, conv = day_activity(client, iso)
    rows = [r for r in due_rows(client, iso, me.username) if str(r.get("id")) not in treated]
    skipped = {s.strip() for s in skip.split(",") if s.strip()}
    rows.sort(key=_burst_order)
    cfg = get_followup_settings(client)
    out: dict[str, Any] = {
        "remaining": len(rows), "conversations": len(conv.get(me.username, ())),
        "target": cfg["targets"].get(me.username, 0), "today": iso,
        "followup": None, "events": [], "template": None, "message": "", "wa_link": "",
    }
    candidates = [r for r in rows if str(r.get("id")) not in skipped]
    if not candidates:
        return out
    fo = _fo_or_404(client, str(candidates[0]["id"]))
    events = ws._run(client.table(T_EVENTS).select("*").eq("followup_id", str(fo["id"]))
                     .order("created_at", desc=True).limit(6), "journal de la fiche")
    f = fo_out(fo)
    template = pick_template(list_templates(client), f["stage"], f["lang"])
    message = render_message(template["body"], f["name"], _me_name(client, me.username)) if template else ""
    out.update(followup=f, events=[_event_out(e) for e in events], message=message,
               template={k: template[k] for k in ("id", "name", "lang", "stage")} if template else None,
               wa_link=security.wa_link(f["phone"], message), skipped=len(rows) - len(candidates))
    return out


class FollowupIn(BaseModel):
    name: str = Field(default="", max_length=120)
    phone: str = Field(min_length=1, max_length=30)
    stage: Literal["inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete"] = "inscrit"
    assignee: Optional[str] = Field(default=None, max_length=40)
    next_followup: Optional[date] = None
    source: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=2000)
    lang: Optional[Literal["fr", "ht"]] = None


Stage = Literal["inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete"]
Status = Literal["a_contacter", "contacte", "a_repondu", "close", "perdu", "pas_interesse", "froid"]


class FollowupPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=30)
    stage: Optional[Stage] = None
    status: Optional[Status] = None
    assignee: Optional[str] = Field(default=None, max_length=40)
    next_followup: Optional[date] = None
    source: Optional[str] = Field(default=None, max_length=40)
    notes: Optional[str] = Field(default=None, max_length=2000)
    lang: Optional[Literal["fr", "ht"]] = None


def _phone_taken(client, phone: str, exclude: str = "") -> bool:
    rows = ws._run(client.table(T_FOLLOWUPS).select("id").eq("phone", phone).limit(1), "lecture des fiches")
    return bool(rows) and str(rows[0].get("id")) != exclude


@router.post(ws.API + "/followups", status_code=201)
def create_followup(body: FollowupIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    phone = security.normalize_phone(body.phone)
    if not phone:
        raise HTTPException(status_code=400, detail="Numéro invalide (8 chiffres haïtiens, ou +indicatif).")
    if _phone_taken(client, phone):
        raise HTTPException(status_code=409, detail="Ce numéro a déjà une fiche.")
    row = {
        "id": str(uuid.uuid4()), "name": " ".join(body.name.split()), "phone": phone, "stage": body.stage,
        "assignee": _check_assignee(client, body.assignee), "source": body.source.strip(),
        "notes": body.notes.strip(), "created_by": me.username,
        "next_followup": body.next_followup.isoformat() if body.next_followup else None,
        "lang": body.lang or get_followup_settings(client)["default_lang"],
    }
    inserted = ws._run(client.table(T_FOLLOWUPS).insert(row), "création de la fiche")
    return {"followup": fo_out(inserted[0] if inserted else row)}


@router.get(ws.API + "/followups/{fid}")
def get_followup(fid: str, request: Request):
    client = ws._db()
    ws._me(request, client)
    row = _fo_or_404(client, fid)
    events = ws._run(client.table(T_EVENTS).select("*").eq("followup_id", str(row["id"])).order("created_at", desc=True).limit(100), "journal de la fiche")
    return {"followup": fo_out(row), "events": [_event_out(e) for e in events]}


@router.patch(ws.API + "/followups/{fid}")
def update_followup(fid: str, body: FollowupPatch, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    current = _fo_or_404(client, fid)
    changes: dict[str, Any] = {}
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "phone":
            phone = security.normalize_phone(value or "")
            if not phone:
                raise HTTPException(status_code=400, detail="Numéro invalide.")
            if _phone_taken(client, phone, exclude=str(current["id"])):
                raise HTTPException(status_code=409, detail="Ce numéro a déjà une fiche.")
            changes[key] = phone
        elif key == "assignee":
            changes[key] = _check_assignee(client, value)
        elif key == "next_followup":
            changes[key] = value.isoformat() if value else None
        elif key in ("name", "source"):
            changes[key] = " ".join(str(value or "").split())
        elif key == "notes":
            changes[key] = str(value or "").strip()
        elif value is not None:
            changes[key] = value
    if not changes:
        return {"followup": fo_out(current)}
    rows = ws._run(client.table(T_FOLLOWUPS).update(changes).eq("id", str(current["id"])), "modification de la fiche")
    if "status" in changes and changes["status"] != current.get("status"):
        _event(client, str(current["id"]), "note", me.username, f"Statut → {STATUS_LABEL[changes['status']]}")
    return {"followup": fo_out(rows[0] if rows else {**current, **changes})}


def _attempt_changes(fo: dict, cfg: dict, today: date) -> dict:
    """Une tentative de plus : prochaine relance selon la séquence, « froid » au-delà du maximum."""
    attempts = int(fo.get("attempts") or 0) + 1
    seq = cfg["sequence"]
    delay = seq[min(attempts - 1, len(seq) - 1)]
    status = fo.get("status") if fo.get("status") == "a_repondu" else "contacte"
    if attempts >= cfg["max_attempts"]:
        status = "froid"
    now = ws._now()
    out = {"attempts": attempts, "status": status, "next_followup": (today + timedelta(days=delay)).isoformat(), "last_contact_at": now}
    if not fo.get("first_contact_at"):
        out["first_contact_at"] = now
    return out


class WhatsAppIn(BaseModel):
    template_id: Optional[str] = Field(default=None, max_length=40)
    # Le message relu et modifié dans la page (mode rafale) ; sinon, le modèle rempli par le serveur.
    message: Optional[str] = Field(default=None, max_length=2000)
    # « wa » : lien wa.me ouvert ; « copie » : message copié pour WhatsApp Business, marqué envoyé à la main.
    via: Literal["wa", "copie"] = "wa"


@router.post(ws.API + "/followups/{fid}/whatsapp")
def open_whatsapp(fid: str, body: WhatsAppIn, request: Request):
    """« Ouvrir WhatsApp » (ou « Marquer comme envoyé » après une copie) : le lien wa.me, et la tentative enregistrée."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    fo = _fo_or_404(client, fid)
    template = None
    if body.template_id:
        tid = ws._check_uuid(body.template_id)
        rows = ws._run(client.table(T_TEMPLATES).select("*").eq("id", tid).limit(1), "lecture du modèle")
        if not rows:
            raise HTTPException(status_code=404, detail="Modèle introuvable.")
        template = _template_out(rows[0])
    if body.message is not None:
        message = body.message.strip()
    else:
        message = render_message(template["body"], fo.get("name") or "", _me_name(client, me.username)) if template else ""
    link = security.wa_link(fo.get("phone") or "", message)
    changes = _attempt_changes(fo, get_followup_settings(client), timeutil.today())
    rows = ws._run(client.table(T_FOLLOWUPS).update(changes).eq("id", str(fo["id"])), "relance")
    event = _event(client, str(fo["id"]), "message", me.username, "copié pour WhatsApp Business" if body.via == "copie" else "",
                   template=template)
    return {"followup": fo_out(rows[0] if rows else {**fo, **changes}), "wa_link": link, "message": message, "event": event}


class EventIn(BaseModel):
    # « sans_reponse » : « Pas de réponse » du mode rafale (cf. add_event).
    kind: Literal["reponse", "close", "perdu", "pas_interesse", "note", "appel", "sans_reponse"]
    note: str = Field(default="", max_length=1000)
    cards: int = Field(default=1, ge=1, le=500)


@router.post(ws.API + "/followups/{fid}/event")
def add_event(fid: str, body: EventIn, request: Request):
    """« A répondu », « Closé » (crée la vente), « Perdu », « Pas intéressé », appel ou note."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    fo = _fo_or_404(client, fid)
    today = timeutil.today()
    changes: dict[str, Any] = {}
    sale = None
    note = body.note.strip()
    kind = body.kind
    if kind == "sans_reponse":
        # « Pas de réponse » : la fiche a été contactée sans réponse. Si un message ou un appel est déjà noté
        # aujourd'hui (« Ouvrir WhatsApp »), la relance suivante est déjà programmée : simple note au journal.
        # Sinon, la personne a contacté le client hors de la page : c'est une tentative (message), qui
        # programme la relance suivante comme d'habitude. Aucun nouveau type d'événement en base.
        tried = ws._run(client.table(T_EVENTS).select("id").eq("followup_id", str(fo["id"])).eq("day", today.isoformat())
                        .in_("kind", list(ATTEMPTS)).limit(1), "journal de la fiche")
        note = "Pas de réponse" + (f" · {note}" if note else "")
        if tried:
            kind = "note"
        else:
            kind = "message"
            changes = _attempt_changes(fo, get_followup_settings(client), today)
    elif body.kind == "reponse":
        changes = {"status": "a_repondu", "attempts": 0, "next_followup": today.isoformat(), "last_contact_at": ws._now()}
    elif body.kind == "appel":
        changes = _attempt_changes(fo, get_followup_settings(client), today)
    elif body.kind == "close":
        sale = sales.record_sale(
            client, username=me.username, cards=body.cards, day=today, created_by=me.username,
            client_name=fo.get("name") or "", client_phone=fo.get("phone") or "", source="whatsapp",
            note=note, followup_id=str(fo["id"]),
        )
        # Un client qui avait déjà sa carte rachète ; sinon, il vient de payer.
        stage = "a_rachete" if fo.get("stage") in ("carte_emise", "carte_utilisee", "a_rachete") else "a_paye"
        changes = {"status": "close", "next_followup": None, "attempts": 0, "sale_id": sale["id"], "stage": stage,
                   "last_contact_at": ws._now()}
        note = f"{body.cards} carte{'s' if body.cards > 1 else ''}" + (f" · {note}" if note else "")
    elif body.kind in ("perdu", "pas_interesse"):
        changes = {"status": body.kind, "next_followup": None}
    elif body.kind == "note" and not note:
        raise HTTPException(status_code=400, detail="La note est vide.")
    if changes:
        rows = ws._run(client.table(T_FOLLOWUPS).update(changes).eq("id", str(fo["id"])), "mise à jour de la fiche")
        fo = rows[0] if rows else {**fo, **changes}
    event = _event(client, str(fo["id"]), kind, me.username, note)
    return {"followup": fo_out(fo), "event": event, "sale": sale}


# ═══════════════════════════════════════════════════════════════════
# Import CSV, répartition, export
# ═══════════════════════════════════════════════════════════════════

_HEADERS = {
    "name": ("nom", "name", "client", "prenom et nom", "nom complet", "prenom"),
    "phone": ("telephone", "tel", "phone", "numero", "whatsapp", "mobile", "portable", "contact"),
    "stage": ("etape", "stage", "parcours", "etape du parcours"),
    "source": ("source", "origine", "canal"),
    "notes": ("note", "notes", "commentaire", "commentaires", "remarque"),
}
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 2_000_000


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower().strip()


def parse_csv(text: str) -> tuple[list[tuple[int, dict]], list[dict]]:
    """Lignes (n°, champs) et rejets de lecture. Colonnes : nom, téléphone, étape, source, note."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    first = lines[0] if lines else ""
    delimiter = max((";", ",", "\t"), key=first.count) if first else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], []
    header = [_plain(h) for h in rows[0]]
    mapping: dict[str, int] = {}
    for field, names in _HEADERS.items():
        for i, h in enumerate(header):
            if h in names and field not in mapping:
                mapping[field] = i
    start = 1
    if "phone" not in mapping:
        # Pas d'en-tête reconnu : l'ordre attendu (nom, téléphone, étape, source, note).
        mapping, start = {"name": 0, "phone": 1, "stage": 2, "source": 3, "notes": 4}, 0
    out, rejected = [], []
    for n, cells in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in cells):
            continue
        if len(out) >= MAX_IMPORT_ROWS:
            rejected.append({"line": n, "reason": f"au-delà de {MAX_IMPORT_ROWS} lignes"})
            continue
        out.append((n, {f: (cells[i].strip() if i < len(cells) else "") for f, i in mapping.items()}))
    return out, rejected


class ImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=MAX_IMPORT_BYTES)
    assignee: Optional[str] = Field(default=None, max_length=40)
    default_stage: Literal["inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete"] = "inscrit"
    source: str = Field(default="", max_length=40)


def prepare_rows(items: list[tuple[int, dict]], *, default_stage: str = "inscrit",
                 default_source: str = "") -> tuple[list[tuple[int, dict]], list[dict], list[dict]]:
    """Normalise (+509), vérifie l'étape, écarte les numéros répétés : (lignes propres, rejets, répétitions)."""
    clean: list[tuple[int, dict]] = []
    rejected: list[dict] = []
    repeats: list[dict] = []
    seen: dict[str, int] = {}
    for n, f in items:
        phone = security.normalize_phone(f.get("phone", ""))
        if not phone:
            rejected.append({"line": n, "reason": "numéro manquant ou invalide"})
            continue
        stage = _stage_key(f.get("stage"))
        if stage is None:
            rejected.append({"line": n, "reason": "étape inconnue"})
            continue
        name = " ".join(str(f.get("name") or "").split())[:120]
        if phone in seen:
            repeats.append({"line": n, "name": name, "phone": phone, "first_line": seen[phone]})
            continue
        seen[phone] = n
        clean.append((n, {
            "name": name, "phone": phone, "stage": stage or default_stage,
            "source": (" ".join(str(f.get("source") or "").split()) or default_source)[:40],
            "notes": str(f.get("notes") or "").strip()[:2000],
        }))
    return clean, rejected, repeats


def known_phones(client, phones: list[str]) -> dict[str, dict]:
    """Fiches déjà en base pour ces numéros (normalisés)."""
    existing: dict[str, dict] = {}
    for i in range(0, len(phones), 150):
        for r in ws._run(client.table(T_FOLLOWUPS).select("id,phone,stage,status,assignee").in_("phone", phones[i:i + 150]).limit(200),
                         "lecture des fiches"):
            existing[str(r.get("phone"))] = r
    return existing


def import_rows(client, items: list[tuple[int, dict]], *, actor: str, assignee: Optional[str] = None,
                default_stage: str = "inscrit", default_source: str = "", advance_stage: bool = False,
                lang: Optional[str] = None, assignees: Optional[list[str]] = None) -> dict:
    """Ajoute les fiches nouvelles ; un numéro déjà connu (en base ou plus haut dans le fichier) est un doublon.

    `assignees` : une personne par fiche nouvelle, dans l'ordre (répartition) ; sinon `assignee` pour toutes.
    """
    report: dict[str, Any] = {"added": 0, "duplicates": 0, "rejected": 0, "updated": 0, "rejected_lines": [], "per_person": {}}
    clean, rejected, repeats = prepare_rows(items, default_stage=default_stage, default_source=default_source)
    report["rejected"] = len(rejected)
    report["rejected_lines"] = rejected
    report["duplicates"] = len(repeats)
    existing = known_phones(client, [r["phone"] for _, r in clean])
    lang = lang if lang in LANGS else get_followup_settings(client)["default_lang"]
    new_rows = []
    for _n, r in clean:
        known = existing.get(r["phone"])
        if known:
            report["duplicates"] += 1
            if advance_stage and STAGES.index(r["stage"]) > STAGES.index(known.get("stage") if known.get("stage") in STAGES else "inscrit"):
                ws._run(client.table(T_FOLLOWUPS).update({"stage": r["stage"]}).eq("id", str(known["id"])), "étape de la fiche")
                report["updated"] += 1
            continue
        who = assignees[len(new_rows) % len(assignees)] if assignees else assignee
        new_rows.append(dict(r, id=str(uuid.uuid4()), assignee=who, created_by=actor, lang=lang))
        if who:
            report["per_person"][who] = report["per_person"].get(who, 0) + 1
    for i in range(0, len(new_rows), 200):
        ws._run(client.table(T_FOLLOWUPS).insert(new_rows[i:i + 200]), "import des fiches")
    report["added"] = len(new_rows)
    report["rejected_lines"] = report["rejected_lines"][:50]
    return report


@router.post(ws.API + "/followups/import")
def import_csv(body: ImportIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    items, rejected = parse_csv(body.csv)
    report = import_rows(client, items, actor=me.username, assignee=_check_assignee(client, body.assignee),
                         default_stage=body.default_stage, default_source=body.source.strip())
    report["rejected"] += len(rejected)
    report["rejected_lines"] = (rejected + report["rejected_lines"])[:50]
    report["lines"] = len(items) + len(rejected)
    ws._log(client, me.username, "followups_imported", detail={k: report[k] for k in ("added", "duplicates", "rejected")})
    return report


# ═══════════════════════════════════════════════════════════════════
# Ajout rapide par collage (vider le stock de messages non lus)
# ═══════════════════════════════════════════════════════════════════

MAX_PASTE_BYTES = 500_000
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d \-().]{6,22}\d")


def parse_paste(text: str) -> tuple[list[tuple[int, dict]], list[dict]]:
    """Une ligne par client : « nom, numéro » ou le numéro seul ; séparateur « , », « ; » ou tabulation.

    Le numéro est la DERNIÈRE cellule qui en est un (« numéro, nom » marche aussi) ; à défaut, un numéro
    écrit dans la ligne (« Jean Pierre 3456 7801 »). Une première ligne d'en-tête (« nom ; téléphone ») est
    ignorée. Renvoie les lignes (n°, champs) et les rejets de lecture, avec un extrait de la ligne.
    """
    items: list[tuple[int, dict]] = []
    rejected: list[dict] = []
    first = True
    for n, raw in enumerate(str(text or "").lstrip("﻿").splitlines(), start=1):
        line = " ".join(raw.replace("\t", "\u0001").split()).replace("\u0001", "\t").strip()
        if not line:
            continue
        cells = [c.strip() for c in re.split(r"[\t;,]", line) if c.strip()]
        if first and any(_plain(c) in _HEADERS["phone"] for c in cells):
            first = False
            continue
        first = False
        if len(items) >= MAX_IMPORT_ROWS:
            rejected.append({"line": n, "reason": f"au-delà de {MAX_IMPORT_ROWS} lignes", "text": line[:60]})
            continue
        idx = next((i for i in range(len(cells) - 1, -1, -1)
                    if security.looks_like_phone(cells[i]) and security.normalize_phone(cells[i])), None)
        if idx is not None:
            phone, name = cells[idx], " ".join(cells[:idx] + cells[idx + 1:])
        else:
            m = _PHONE_IN_TEXT.search(line)
            if not m or not security.normalize_phone(m.group(0)):
                reason = "numéro invalide" if re.search(r"\d{3}", line) else "pas de numéro"
                rejected.append({"line": n, "reason": reason, "text": line[:60]})
                continue
            phone = m.group(0)
            name = re.sub(r"[\t;,]+", " ", line[:m.start()] + " " + line[m.end():])
        name = " ".join(name.strip(" -:").split())
        items.append((n, {"name": name, "phone": phone, "stage": "", "source": "", "notes": ""}))
    return items, rejected


def spread_sequence(count: int, people: list[str], weights: dict[str, int]) -> list[str]:
    """`count` attributions entrelacées, proportionnelles aux poids (tourniquet pondéré lissé)."""
    w = {p: max(1, int(weights.get(p) or 0)) for p in people}
    if not all(weights.get(p) for p in people):
        w = {p: 1 for p in people}  # un objectif manquant : parts égales
    total, cur, out = sum(w.values()), {p: 0 for p in people}, []
    for _ in range(count):
        for p in people:
            cur[p] += w[p]
        best = max(people, key=lambda p: cur[p])
        cur[best] -= total
        out.append(best)
    return out


class PasteIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_PASTE_BYTES)
    stage: Stage = "inscrit"
    source: str = Field(default="", max_length=40)
    lang: Optional[Literal["fr", "ht"]] = None
    assign: Literal["me", "spread"] = "me"
    usernames: list[str] = Field(default_factory=list, max_length=20)
    commit: bool = False


@router.post(ws.API + "/followups/paste")
def paste_followups(body: PasteIn, request: Request):
    """Aperçu (commit=false) puis import (commit=true) d'une liste collée. Même logique que l'import CSV."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    cfg = get_followup_settings(client)
    people: list[str] = []
    if body.assign == "spread":
        # Répartir entre plusieurs personnes : même droit que « Répartir ».
        ws._require(me, "responsable")
        people = list(dict.fromkeys(_check_assignee(client, u) for u in body.usernames if u))
        if not people:
            raise HTTPException(status_code=400, detail="Choisis au moins une personne pour répartir.")
    items, read_rejects = parse_paste(body.text)
    source = " ".join(body.source.split())[:40]
    clean, rejected, repeats = prepare_rows(items, default_stage=body.stage, default_source=source)
    known = known_phones(client, [r["phone"] for _, r in clean])
    names = {m["username"]: m["display_name"] for m in ws._team(client)}
    new = [(n, r) for n, r in clean if r["phone"] not in known]
    plan = spread_sequence(len(new), people, cfg["targets"]) if people else [me.username] * len(new)
    rejects = sorted(read_rejects + [dict(x, text="") for x in rejected], key=lambda x: x["line"])
    duplicates = sorted(
        [{"line": x["line"], "name": x["name"], "phone": x["phone"], "reason": f"répété (ligne {x['first_line']})"} for x in repeats]
        + [{"line": n, "name": r["name"], "phone": r["phone"], "reason": "déjà en base",
            "assignee": known[r["phone"]].get("assignee") or None,
            "assignee_name": names.get(known[r["phone"]].get("assignee") or "", ""),
            "status": known[r["phone"]].get("status") if known[r["phone"]].get("status") in STATUSES else "a_contacter"}
           for n, r in clean if r["phone"] in known],
        key=lambda x: x["line"])
    per_person: dict[str, int] = {}
    for who in plan:
        if who:
            per_person[who] = per_person.get(who, 0) + 1
    out: dict[str, Any] = {
        "lines": len(items) + len(read_rejects), "valid": len(new), "duplicates": len(duplicates),
        "rejected": len(rejects), "per_person": per_person, "committed": False,
        "lang": body.lang or cfg["default_lang"],
        "preview": {
            "valid": [{"line": n, "name": r["name"], "phone": r["phone"], "assignee": plan[i]} for i, (n, r) in enumerate(new[:200])],
            "duplicates": duplicates[:200], "rejected": rejects[:200],
        },
    }
    if not body.commit:
        return out
    report = import_rows(client, items, actor=me.username, default_stage=body.stage, default_source=source,
                         lang=body.lang or cfg["default_lang"], assignees=plan or None, assignee=me.username)
    report["rejected"] += len(read_rejects)
    out.update(committed=True, added=report["added"], per_person=report["per_person"],
               duplicates=report["duplicates"], rejected=report["rejected"])
    ws._log(client, me.username, "followups_imported",
            detail={"added": report["added"], "duplicates": report["duplicates"], "rejected": report["rejected"], "via": "collage"})
    return out


# ═══════════════════════════════════════════════════════════════════
# Actions groupées (« Toutes les fiches ») : mêmes droits qu'une fiche seule
# ═══════════════════════════════════════════════════════════════════

class BulkFollowupChanges(BaseModel):
    assignee: Optional[str] = Field(default=None, max_length=40)
    status: Optional[Status] = None
    stage: Optional[Stage] = None
    next_followup: Optional[date] = None
    lang: Optional[Literal["fr", "ht"]] = None


class BulkFollowupIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    changes: BulkFollowupChanges


@router.post(ws.API + "/followups/bulk")
def bulk_followups(body: BulkFollowupIn, request: Request):
    """Assigner, changer le statut, l'étape ou la langue, programmer une relance : sur plusieurs fiches d'un coup."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    changes: dict[str, Any] = {}
    for key, value in body.changes.model_dump(exclude_unset=True).items():
        if key == "assignee":
            changes[key] = _check_assignee(client, value)
        elif key == "next_followup":
            changes[key] = value.isoformat() if value else None
        elif value is not None:
            changes[key] = value
    if not changes:
        raise HTTPException(status_code=400, detail="Rien à changer.")
    ids = list(dict.fromkeys(ws._check_uuid(i) for i in body.ids))
    before: dict[str, dict] = {}
    for i in range(0, len(ids), 150):
        for r in ws._run(client.table(T_FOLLOWUPS).select("id,status").in_("id", ids[i:i + 150]).limit(200), "lecture des fiches"):
            before[str(r["id"])] = r
    found = [i for i in ids if i in before]
    for i in range(0, len(found), 150):
        ws._run(client.table(T_FOLLOWUPS).update(changes).in_("id", found[i:i + 150]), "actions groupées")
    if "status" in changes:
        day = timeutil.today().isoformat()
        events = [{"id": str(uuid.uuid4()), "followup_id": fid, "kind": "note", "actor": me.username,
                   "note": f"Statut → {STATUS_LABEL[changes['status']]} (action groupée)", "day": day}
                  for fid in found if before[fid].get("status") != changes["status"]]
        for i in range(0, len(events), 200):
            ws._run(client.table(T_EVENTS).insert(events[i:i + 200]), "journal des fiches")
    ws._log(client, me.username, "followups_bulk", detail={"count": len(found), "fields": sorted(changes)})
    return {"updated": len(found), "missing": len(ids) - len(found), "changes": changes}


class DistributeIn(BaseModel):
    usernames: list[str] = Field(min_length=1, max_length=20)
    stage: str = Field(default="", max_length=20)
    max_per_person: Optional[int] = Field(default=None, ge=1, le=10000)


@router.post(ws.API + "/followups/distribute")
def distribute(body: DistributeIn, request: Request):
    """Répartit équitablement les fiches non assignées (et encore ouvertes) entre les personnes choisies."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    people = list(dict.fromkeys(_check_assignee(client, u) for u in body.usernames if u))
    if not people:
        raise HTTPException(status_code=400, detail="Choisis au moins une personne.")

    def build():
        q = client.table(T_FOLLOWUPS).select("id").is_("assignee", "null").not_.in_("status", list(CLOSED))
        if body.stage in STAGES:
            q = q.eq("stage", body.stage)
        return q.order("created_at", desc=True).order("id")

    ids = [str(r["id"]) for r in fetch_all(build, "lecture des fiches à répartir")]
    if body.max_per_person:
        ids = ids[: body.max_per_person * len(people)]
    buckets: dict[str, list[str]] = {u: [] for u in people}
    for i, fid in enumerate(ids):
        buckets[people[i % len(people)]].append(fid)
    for user, chunk in buckets.items():
        for i in range(0, len(chunk), 100):
            ws._run(client.table(T_FOLLOWUPS).update({"assignee": user}).in_("id", chunk[i:i + 100]), "répartition")
    counts = {u: len(v) for u, v in buckets.items()}
    ws._log(client, me.username, "followups_distributed", detail={"total": len(ids), "per_person": counts})
    return {"total": len(ids), "per_person": counts}


@router.get(ws.API + "/followups-export.csv")
def export_followups(request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    rows = fetch_all(lambda: client.table(T_FOLLOWUPS).select("*").order("created_at").order("id"), "lecture des fiches")
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Nom", "Téléphone", "Étape", "Statut", "Assignée à", "Prochaine relance", "Tentatives",
                     "Dernier contact", "Source", "Notes", "Créée le"])
    for r in rows:
        f = fo_out(r)
        writer.writerow([ws._csv_cell(v) for v in (
            f["name"], f["phone"], STAGE_LABEL[f["stage"]], STATUS_LABEL[f["status"]], f["assignee"] or "",
            f["next_followup"] or "", f["attempts"], str(f["last_contact_at"] or "")[:16].replace("T", " "),
            f["source"], f["notes"], str(f["created_at"] or "")[:10],
        )])
    ws._log(client, me.username, "followups_exported", detail={"rows": len(rows)})
    return Response(
        content="﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="handypay-relances-{timeutil.today().isoformat()}.csv"'},
    )


# ═══════════════════════════════════════════════════════════════════
# Modèles de message et réglages (responsables)
# ═══════════════════════════════════════════════════════════════════

@router.get(ws.API + "/message-templates")
def get_templates(request: Request):
    client = ws._db()
    ws._me(request, client)
    return {"templates": list_templates(client)}


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    stage: str = Field(default="", max_length=20)
    body: str = Field(min_length=1, max_length=1000)
    active: bool = True
    position: int = Field(default=0, ge=0, le=1000)
    lang: Literal["fr", "ht"] = "fr"
    description: str = Field(default="", max_length=200)


def _template_fields(body: TemplateIn) -> dict:
    if body.stage and body.stage not in STAGES:
        raise HTTPException(status_code=400, detail="Étape inconnue.")
    name = " ".join(body.name.split())
    if not name or not body.body.strip():
        raise HTTPException(status_code=400, detail="Nom et message sont obligatoires.")
    return {"name": name, "stage": body.stage, "body": body.body.strip(), "active": body.active, "position": body.position,
            "lang": body.lang, "description": " ".join(body.description.split())}


@router.post(ws.API + "/message-templates", status_code=201)
def create_template(body: TemplateIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    row = dict(_template_fields(body), id=str(uuid.uuid4()), created_by=me.username)
    inserted = ws._run(client.table(T_TEMPLATES).insert(row), "création du modèle")
    return {"template": _template_out(inserted[0] if inserted else row)}


@router.patch(ws.API + "/message-templates/{tid}")
def update_template(tid: str, body: TemplateIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    rid = ws._check_uuid(tid)
    rows = ws._run(client.table(T_TEMPLATES).update(_template_fields(body)).eq("id", rid), "modification du modèle")
    if not rows:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    return {"template": _template_out(rows[0])}


@router.delete(ws.API + "/message-templates/{tid}")
def delete_template(tid: str, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    rid = ws._check_uuid(tid)
    ws._run(client.table(T_TEMPLATES).delete().eq("id", rid), "suppression du modèle")
    return {"ok": True}


class FollowupSettingsIn(BaseModel):
    sequence: list[int] = Field(min_length=1, max_length=10)
    max_attempts: int = Field(ge=1, le=20)
    conversations_target: int = Field(ge=1, le=100000)
    # Absents : on garde les valeurs en place.
    targets: Optional[dict[str, int]] = Field(default=None, max_length=100)
    default_lang: Optional[Literal["fr", "ht"]] = None


@router.put(ws.API + "/settings/followups")
def put_followup_settings(body: FollowupSettingsIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    current = get_followup_settings(client)
    data = body.model_dump()
    if body.targets is None:
        data["targets"] = current["targets"]
    else:
        people = {m["username"] for m in ws._team(client) if not m.get("is_ai")}
        unknown = [u for u in body.targets if u not in people]
        if unknown:
            raise HTTPException(status_code=400, detail="Personne inconnue dans l'équipe.")
        if any(not 0 <= v <= 100000 for v in body.targets.values()):
            raise HTTPException(status_code=400, detail="Objectif de conversations invalide (0 à 100 000).")
    if body.default_lang is None:
        data["default_lang"] = current["default_lang"]
    value = normalize_followup_settings(data)
    sales.put_setting(client, "followups", value, me.username)
    ws._log(client, me.username, "settings_followups", detail={"targets": value["targets"], "default_lang": value["default_lang"]})
    return {"settings": value}
