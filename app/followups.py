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

DEFAULT_FOLLOWUP = {"sequence": [1, 3, 7], "max_attempts": 3, "conversations_target": 300}

# Les 5 modèles de départ : courts, naturels, à adapter par un responsable.
DEFAULT_TEMPLATES = [
    ("Premier contact", "",
     "Bonjour {prenom}, ici {moi} de HandyPay. Vous voulez payer en ligne depuis Haïti (abonnements, achats "
     "sur internet) ? On peut vous faire une carte virtuelle aujourd'hui. Je vous explique ?"),
    ("Inscrit sans KYC", "inscrit",
     "Bonjour {prenom}, c'est {moi} de HandyPay. Votre compte est créé, il ne manque que la vérification "
     "d'identité pour avoir votre carte. Vous bloquez sur une étape ? Je vous aide en 2 minutes."),
    ("Achat commencé", "achat_commence",
     "Bonjour {prenom}, ici {moi} de HandyPay. Vous avez commencé l'achat de votre carte sans le terminer. "
     "Un souci au paiement ? On peut le régler ensemble maintenant."),
    ("Carte jamais utilisée", "carte_emise",
     "Bonjour {prenom}, c'est {moi} de HandyPay. Votre carte est prête mais pas encore utilisée. "
     "Je vous montre comment faire votre premier achat en ligne avec ?"),
    ("Rachat", "carte_utilisee",
     "Bonjour {prenom}, ici {moi} de HandyPay. Merci d'utiliser votre carte ! Il vous en faut une autre, "
     "ou une recharge pour vos prochains achats ? Je m'en occupe."),
]


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
    return {
        "sequence": seq[:10] or list(d["sequence"]),
        "max_attempts": max_attempts if 1 <= max_attempts <= 20 else d["max_attempts"],
        "conversations_target": target if 1 <= target <= 100000 else d["conversations_target"],
    }


def get_followup_settings(client) -> dict:
    return normalize_followup_settings({**DEFAULT_FOLLOWUP, **sales.get_setting(client, "followups")})


def _template_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "name": str(r.get("name") or ""),
        "stage": r.get("stage") if r.get("stage") in STAGES else "",
        "body": str(r.get("body") or ""), "active": r.get("active", True) is not False,
        "position": int(r.get("position") or 0), "updated_at": r.get("updated_at"),
    }


def list_templates(client) -> list[dict]:
    rows = ws._run(client.table(T_TEMPLATES).select("*").order("position").limit(200), "lecture des modèles")
    if not rows and not sales.get_setting(client, "templates_seeded"):
        # Premier passage : les 5 modèles de départ, une seule fois (supprimés ensuite, ils ne reviennent pas).
        seed = [{"id": str(uuid.uuid4()), "name": n, "stage": st, "body": b, "position": i, "created_by": "système"}
                for i, (n, st, b) in enumerate(DEFAULT_TEMPLATES)]
        ws._run(client.table(T_TEMPLATES).insert(seed), "modèles de départ")
        sales.put_setting(client, "templates_seeded", {"done": True}, "système")
        rows = ws._run(client.table(T_TEMPLATES).select("*").order("position").limit(200), "lecture des modèles")
    return [_template_out(r) for r in rows]


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


def _queue_query(client, username: str, today: str, *, count: bool = False):
    q = client.table(T_FOLLOWUPS).select("*", count="exact") if count else client.table(T_FOLLOWUPS).select("*")
    return (q.eq("assignee", username).not_.in_("status", list(CLOSED))
            .or_(f"status.eq.a_contacter,next_followup.lte.{today}"))


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
    }


class FollowupIn(BaseModel):
    name: str = Field(default="", max_length=120)
    phone: str = Field(min_length=1, max_length=30)
    stage: Literal["inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete"] = "inscrit"
    assignee: Optional[str] = Field(default=None, max_length=40)
    next_followup: Optional[date] = None
    source: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=2000)


class FollowupPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=30)
    stage: Optional[Literal["inscrit", "kyc", "achat_commence", "a_paye", "carte_emise", "carte_utilisee", "a_rachete"]] = None
    status: Optional[Literal["a_contacter", "contacte", "a_repondu", "close", "perdu", "pas_interesse", "froid"]] = None
    assignee: Optional[str] = Field(default=None, max_length=40)
    next_followup: Optional[date] = None
    source: Optional[str] = Field(default=None, max_length=40)
    notes: Optional[str] = Field(default=None, max_length=2000)


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


@router.post(ws.API + "/followups/{fid}/whatsapp")
def open_whatsapp(fid: str, body: WhatsAppIn, request: Request):
    """« Ouvrir WhatsApp » : le lien wa.me (message rempli), et la tentative enregistrée."""
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
    me_name = next((m["display_name"] for m in ws._team(client) if m["username"] == me.username), me.username)
    message = render_message(template["body"], fo.get("name") or "", me_name) if template else ""
    link = security.wa_link(fo.get("phone") or "", message)
    changes = _attempt_changes(fo, get_followup_settings(client), timeutil.today())
    rows = ws._run(client.table(T_FOLLOWUPS).update(changes).eq("id", str(fo["id"])), "relance")
    event = _event(client, str(fo["id"]), "message", me.username, template=template)
    return {"followup": fo_out(rows[0] if rows else {**fo, **changes}), "wa_link": link, "message": message, "event": event}


class EventIn(BaseModel):
    kind: Literal["reponse", "close", "perdu", "pas_interesse", "note", "appel"]
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
    if body.kind == "reponse":
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
    event = _event(client, str(fo["id"]), body.kind, me.username, note)
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


def import_rows(client, items: list[tuple[int, dict]], *, actor: str, assignee: Optional[str] = None,
                default_stage: str = "inscrit", default_source: str = "", advance_stage: bool = False) -> dict:
    """Ajoute les fiches nouvelles ; un numéro déjà connu (en base ou plus haut dans le fichier) est un doublon."""
    report: dict[str, Any] = {"added": 0, "duplicates": 0, "rejected": 0, "updated": 0, "rejected_lines": []}
    clean: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for n, f in items:
        phone = security.normalize_phone(f.get("phone", ""))
        if not phone:
            report["rejected"] += 1
            report["rejected_lines"].append({"line": n, "reason": "numéro manquant ou invalide"})
            continue
        stage = _stage_key(f.get("stage"))
        if stage is None:
            report["rejected"] += 1
            report["rejected_lines"].append({"line": n, "reason": "étape inconnue"})
            continue
        if phone in seen:
            report["duplicates"] += 1
            continue
        seen.add(phone)
        clean.append((n, {
            "name": " ".join(str(f.get("name") or "").split())[:120], "phone": phone, "stage": stage or default_stage,
            "source": (" ".join(str(f.get("source") or "").split()) or default_source)[:40],
            "notes": str(f.get("notes") or "").strip()[:2000],
        }))
    existing: dict[str, dict] = {}
    phones = [r["phone"] for _, r in clean]
    for i in range(0, len(phones), 150):
        for r in ws._run(client.table(T_FOLLOWUPS).select("id,phone,stage").in_("phone", phones[i:i + 150]).limit(200), "lecture des fiches"):
            existing[str(r.get("phone"))] = r
    new_rows = []
    for _n, r in clean:
        known = existing.get(r["phone"])
        if known:
            report["duplicates"] += 1
            if advance_stage and STAGES.index(r["stage"]) > STAGES.index(known.get("stage") if known.get("stage") in STAGES else "inscrit"):
                ws._run(client.table(T_FOLLOWUPS).update({"stage": r["stage"]}).eq("id", str(known["id"])), "étape de la fiche")
                report["updated"] += 1
            continue
        new_rows.append(dict(r, id=str(uuid.uuid4()), assignee=assignee, created_by=actor))
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


def _template_fields(body: TemplateIn) -> dict:
    if body.stage and body.stage not in STAGES:
        raise HTTPException(status_code=400, detail="Étape inconnue.")
    name = " ".join(body.name.split())
    if not name or not body.body.strip():
        raise HTTPException(status_code=400, detail="Nom et message sont obligatoires.")
    return {"name": name, "stage": body.stage, "body": body.body.strip(), "active": body.active, "position": body.position}


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


@router.put(ws.API + "/settings/followups")
def put_followup_settings(body: FollowupSettingsIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    value = normalize_followup_settings(body.model_dump())
    sales.put_setting(client, "followups", value, me.username)
    return {"settings": value}
