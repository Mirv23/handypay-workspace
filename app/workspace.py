"""HandyPay · Espace d'équipe : projets, tâches, objectifs, documents, équipe.

Repris du module « espace d'équipe » de JaKOB, devenu une application à part
pour l'équipe HandyPay. Les ventes (`sales.py`), les relances WhatsApp
(`followups.py`) et le tableau du soir (`evening.py`) s'y ajoutent.

QUI PEUT QUOI. Chaque personne a son compte (`auth.py`). Le rôle est lu EN BASE
à chaque requête, jamais pris dans la requête :
- `responsable` : tout, y compris les comptes, les réglages et les projets restreints ;
- `membre` : crée et modifie projets, tâches, objectifs, cycles, documents,
  ventes et relances ;
- `observateur` : lit, commente et suit des tâches.

ISOLATION. Un projet `restreint` n'est visible que de ses membres listés, de son
responsable et des responsables de l'espace. Tout identifiant hors de ce
périmètre (projet, tâche, commentaire, document) répond 404, comme s'il
n'existait pas ; les notifications ne partent qu'aux personnes qui voient le
projet, pour qu'une mention ne fasse pas fuiter un titre.

PANNE. Si une lecture échoue : 503 « indisponible », jamais une liste vide
présentée comme vraie. Le journal d'activité et les notifications,
secondaires, n'interrompent jamais l'action principale.

L'IA, MEMBRE DE L'ÉQUIPE. Un membre virtuel, `ia`, figure toujours dans
l'équipe : on peut lui confier une tâche comme à quiconque. Aucun compte ne
peut s'en servir (nom réservé) ; l'IA agit par sa propre porte, `connect.py`,
avec son jeton. Sur une tâche confiée à l'IA, l'équipe voit les grands points
(titre, étapes, statut, PR) ; le détail technique (commits, journal de l'IA)
est réservé aux responsables.

DÉCISIONS. Une tâche marquée « décision » ne se valide (« Fait ») que par la
personne à qui elle est confiée : c'est elle qui tranche. Ni l'IA, ni GitHub,
ni un collègue ne la ferment à sa place ; seul un responsable peut la confier
à quelqu'un d'autre.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import uuid
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db as dbmod, security, timeutil

logger = logging.getLogger("handypay.workspace")
router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_PATH = "/"
API = "/api/ws"
APP_NAME = "HandyPay · Espace d'équipe"

# Rien d'extérieur, aucun cadre. Les liens wa.me sont des navigations (nouvel
# onglet), pas des requêtes de la page : la CSP ne les gêne pas. Application
# installable : le manifeste et le service worker viennent du même site (mobile.py).
# Scripts : pas de 'unsafe-inline'. Seuls les <script> de la page, marqués du
# nonce tiré pour CETTE réponse, s'exécutent : un script ou un attribut
# `onerror=` injecté dans la page serait bloqué par le navigateur. Les styles
# gardent 'unsafe-inline' (attributs style= partout dans la page).
WORKSPACE_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'{nonce}; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def page_csp(nonce: str = "") -> str:
    """CSP d'une page HTML (« / », « /compteur ») ; sans nonce, aucun script en ligne ne s'exécute."""
    return WORKSPACE_CSP.replace("{nonce}", f" 'nonce-{nonce}'" if nonce else "")


def page_nonce(request: Request) -> str:
    """Nonce CSP de cette réponse : le gardien (main.py) le relit pour écrire l'en-tête."""
    nonce = security.new_nonce()
    request.state.csp_nonce = nonce
    return nonce

T_ACCOUNTS = auth.T_ACCOUNTS
T_MEMBERS = "workspace_members"
T_PROJECTS = "workspace_projects"
T_TASKS = "workspace_tasks"
T_COMMENTS = "workspace_comments"
T_ACTIVITY = "workspace_activity"
T_NOTIFS = "workspace_notifications"
T_OBJECTIVES = "workspace_objectives"
T_KRS = "workspace_key_results"
T_CHECKINS = "workspace_checkins"
T_CYCLES = "workspace_cycles"
T_DOCS = "workspace_docs"
T_DIGEST_RUNS = "workspace_digest_runs"
T_GH_LINKS = "workspace_github_links"
T_GH_DELIVERIES = "workspace_github_deliveries"
T_AI_JOURNAL = "workspace_ai_journal"
T_SETTINGS = "workspace_settings"

# Le membre virtuel « IA » et la clé des tâches (« HP-12 ») : la clé est ce que
# l'on écrit dans un commit, une branche ou une PR pour les relier à la tâche.
AI_USER = "ia"
TASK_KEY = "HP"
DECISION_LABEL = "décision"
_GH_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
AI_MEMBER = {
    "username": AI_USER, "role": "membre", "display_name": "IA", "title": "Agent de développement",
    "color": "#7C3AED", "capacity_hours": 0, "active": True,
}

STATUSES = ("a_faire", "en_cours", "en_revue", "bloque", "fait")
PRIORITIES = ("urgente", "haute", "normale", "basse")
RECURRENCES = ("", "quotidienne", "hebdomadaire", "mensuelle")
RANK = {"observateur": 0, "membre": 1, "responsable": 2}
KR_AUTO = ("", "ventes_cumul", "marge_cumul")

# Bornes de lecture : une équipe de quelques personnes reste très en dessous.
# Au-delà, la réponse le dit (`truncated`) au lieu de taire le reste.
_TASK_LIMIT = 5000
_PROJECT_LIMIT = 500
# Les tâches terminées depuis plus longtemps ne chargent pas la page ; la
# corbeille et l'export les retrouvent.
_DONE_HORIZON_DAYS = 120

_MENTION_RE = re.compile(r"@([A-Za-z0-9_.\-]{2,80})")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_STATUS_LABEL = {
    "a_faire": "À faire", "en_cours": "En cours", "en_revue": "En revue",
    "bloque": "Bloqué", "fait": "Fait",
}


# ═══════════════════════════════════════════════════════════════════
# Accès à la base
# ═══════════════════════════════════════════════════════════════════

def _svc():
    return dbmod.client_or_none()


def _db():
    return dbmod.db()


def _run(query, what: str) -> list[dict]:
    return dbmod.run(query, what)


def _now() -> str:
    return timeutil.now_iso()


def _today() -> date:
    """Aujourd'hui à Port-au-Prince."""
    return timeutil.today()


# ═══════════════════════════════════════════════════════════════════
# Normalisation des lignes
# ═══════════════════════════════════════════════════════════════════

def _date(value: Any) -> Optional[str]:
    return str(value)[:10] if value else None


def _num(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _strs(value: Any) -> list[str]:
    return [str(v) for v in (value or []) if v is not None and str(v)]


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _id_or_none(value: Any) -> Optional[str]:
    return str(value) if value else None


def _task_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""),
        "number": int(r.get("number") or 0),
        "project_id": str(r.get("project_id") or ""),
        "title": str(r.get("title") or ""),
        "description": str(r.get("description") or ""),
        "status": r.get("status") if r.get("status") in STATUSES else "a_faire",
        "priority": r.get("priority") if r.get("priority") in PRIORITIES else "normale",
        "assignee": r.get("assignee") or None,
        "start_date": _date(r.get("start_date")),
        "due_date": _date(r.get("due_date")),
        "labels": _strs(r.get("labels")),
        "estimate_hours": _num(r.get("estimate_hours"), None),
        "spent_hours": _num(r.get("spent_hours")),
        "checklist": _json_list(r.get("checklist")),
        "links": _json_list(r.get("links")),
        "blocked_by": _strs(r.get("blocked_by")),
        "watchers": _strs(r.get("watchers")),
        "recurrence": r.get("recurrence") if r.get("recurrence") in RECURRENCES else "",
        "cycle_id": _id_or_none(r.get("cycle_id")),
        "key_result_id": _id_or_none(r.get("key_result_id")),
        "position": _num(r.get("position")),
        "completed_at": r.get("completed_at"),
        "github": github_summary(r.get("github")),
        "archived": bool(r.get("archived")),
        "created_by": str(r.get("created_by") or ""),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def github_summary(value: Any) -> dict:
    """Résumé GitHub tenu par le webhook, relu prudemment (colonne jsonb)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    value = value if isinstance(value, dict) else {}
    pr = value.get("pr")
    state = value.get("state") if value.get("state") in ("ouverte", "brouillon", "fusionnee", "fermee") else ""
    url = str(value.get("url") or "")
    return {
        "commits": max(0, int(_num(value.get("commits"), 0) or 0)),
        "pr": int(pr) if isinstance(pr, int) or (isinstance(pr, str) and pr.isdigit()) else None,
        "state": state,
        "url": url if url.startswith("https://github.com/") else "",
    }


def task_key(number: Any) -> str:
    return f"{TASK_KEY}-{int(number or 0)}"


def _project_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""),
        "name": str(r.get("name") or ""),
        "description": str(r.get("description") or ""),
        "color": r.get("color") if _COLOR_RE.match(str(r.get("color") or "")) else "#0B6FA4",
        "icon": str(r.get("icon") or ""),
        "status": r.get("status") or "actif",
        "owner_username": str(r.get("owner_username") or ""),
        "visibility": "restreint" if r.get("visibility") == "restreint" else "equipe",
        "members": _strs(r.get("members")),
        "start_date": _date(r.get("start_date")),
        "due_date": _date(r.get("due_date")),
        "objective_id": _id_or_none(r.get("objective_id")),
        "created_by": str(r.get("created_by") or ""),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _member_out(r: dict, role: Optional[str] = None, with_email: bool = False) -> dict:
    username = str(r.get("username") or "")
    out = {
        "username": username,
        "role": role or (r.get("role") if r.get("role") in RANK else "observateur"),
        "display_name": str(r.get("display_name") or "") or username,
        "title": str(r.get("title") or ""),
        "color": r.get("color") if _COLOR_RE.match(str(r.get("color") or "")) else "#0B6FA4",
        "capacity_hours": _num(r.get("capacity_hours"), 30.0),
        "active": r.get("active", True) is not False,
        "is_ai": username == AI_USER,
        "github_login": str(r.get("github_login") or ""),
    }
    if with_email:
        # Coordonnées : visibles de la personne elle-même et des responsables
        # seulement (cf. bootstrap).
        out["email"] = str(r.get("email") or "")
        out["daily_digest"] = r.get("daily_digest", True) is not False
        out["whatsapp"] = str(r.get("whatsapp") or "")
    return out


def kr_progress(kr: dict) -> float:
    """Avancement d'un résultat clé entre 0 et 1, dans les deux sens.

    Une cible plus basse que le départ (réduire un délai, un taux d'abandon)
    marche aussi : on mesure le chemin parcouru vers la cible.
    """
    start = _num(kr.get("start_value")) or 0.0
    current = _num(kr.get("current_value")) or 0.0
    target = _num(kr.get("target_value")) or 0.0
    if target == start:
        return 1.0 if current == target else 0.0
    return max(0.0, min(1.0, (current - start) / (target - start)))


def _kr_out(r: dict) -> dict:
    out = {
        "id": str(r.get("id") or ""),
        "objective_id": str(r.get("objective_id") or ""),
        "title": str(r.get("title") or ""),
        "unit": str(r.get("unit") or ""),
        "start_value": _num(r.get("start_value")),
        "current_value": _num(r.get("current_value")),
        "target_value": _num(r.get("target_value")),
        "owner_username": r.get("owner_username") or None,
        "position": int(r.get("position") or 0),
        "auto": r.get("auto") if r.get("auto") in KR_AUTO else "",
        "updated_at": r.get("updated_at"),
    }
    out["progress"] = round(kr_progress(out), 4)
    return out


def _objective_out(r: dict, krs: list[dict]) -> dict:
    mine = sorted((k for k in krs if k["objective_id"] == str(r.get("id"))), key=lambda k: k["position"])
    return {
        "id": str(r.get("id") or ""),
        "title": str(r.get("title") or ""),
        "description": str(r.get("description") or ""),
        "period": str(r.get("period") or ""),
        "owner_username": r.get("owner_username") or None,
        "status": r.get("status") or "en_cours",
        "start_date": _date(r.get("start_date")),
        "due_date": _date(r.get("due_date")),
        "created_by": str(r.get("created_by") or ""),
        "key_results": mine,
        "progress": round(sum(k["progress"] for k in mine) / len(mine), 4) if mine else 0.0,
        "updated_at": r.get("updated_at"),
    }


def _cycle_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""),
        "name": str(r.get("name") or ""),
        "goal": str(r.get("goal") or ""),
        "start_date": _date(r.get("start_date")),
        "end_date": _date(r.get("end_date")),
        "created_by": str(r.get("created_by") or ""),
    }


def _doc_out(r: dict, with_body: bool = True) -> dict:
    body = str(r.get("body") or "")
    out = {
        "id": str(r.get("id") or ""),
        "project_id": _id_or_none(r.get("project_id")),
        "title": str(r.get("title") or ""),
        "kind": r.get("kind") or "note",
        "pinned": bool(r.get("pinned")),
        "author": str(r.get("author") or ""),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "excerpt": " ".join(body.split())[:180],
    }
    if with_body:
        out["body"] = body
    return out


# ═══════════════════════════════════════════════════════════════════
# Identité et droits (lus en base, jamais pris dans la requête)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Me:
    username: str
    role: str
    panel_role: str = ""

    def at_least(self, role: str) -> bool:
        return RANK.get(self.role, -1) >= RANK[role]


def _me(request: Request, client) -> Me:
    """La personne connectée : le middleware a vérifié la session ET relu le compte."""
    cached = getattr(request.state, "workspace_me", None)
    if isinstance(cached, Me):
        return cached
    identity = getattr(request.state, "user", None)
    if not isinstance(identity, dict) or not identity.get("username"):
        raise HTTPException(status_code=401, detail="Session requise.")
    username = str(identity["username"])
    if username == AI_USER:
        # Le nom de l'IA ne s'emprunte pas : elle passe par sa propre porte.
        raise HTTPException(status_code=403, detail="Ce nom est réservé à l'IA de l'espace.")
    role = identity.get("role") if identity.get("role") in RANK else "observateur"
    me = Me(username, role, "")
    request.state.workspace_me = me
    return me


def _require(me: Me, role: str) -> None:
    if not me.at_least(role):
        needed = {"membre": "membre", "responsable": "responsable"}.get(role, role)
        raise HTTPException(status_code=403, detail=f"Ton rôle dans l'espace ne permet pas cette action (il faut être {needed}).")


def _accounts(client) -> list[dict]:
    """Comptes, SANS empreinte de mot de passe (colonnes choisies)."""
    return _run(client.table(T_ACCOUNTS).select(auth.PUBLIC_COLS).limit(500), "lecture des comptes")


def _team(client, with_email: bool = False) -> list[dict]:
    """Comptes actifs (rôle, nom, numéro) fusionnés avec leur fiche (poste, couleur…), plus l'IA."""
    profiles = {str(r.get("username")): r for r in _run(client.table(T_MEMBERS).select("*").limit(500), "lecture des membres")}
    team: dict[str, dict] = {}
    for a in _accounts(client):
        name = str(a.get("username") or "")
        if not name or a.get("active") is False or name == AI_USER:
            continue
        merged = dict(profiles.get(name) or {}, **{k: a.get(k) for k in ("username", "display_name", "role", "whatsapp")})
        team[name] = _member_out(merged, with_email=with_email)
    members = sorted(team.values(), key=lambda m: m["display_name"].casefold())
    # L'IA en dernier : toujours là, sans adresse (elle lit sa file par l'API).
    ai = _member_out(AI_MEMBER, with_email=with_email)
    if with_email:
        ai.update(email="", daily_digest=False, whatsapp="")
    return members + [ai]


def _sees(username: str, role: str, project: dict) -> bool:
    if project.get("visibility") != "restreint":
        return True
    return (
        role == "responsable"
        or username == project.get("owner_username")
        or username in (project.get("members") or [])
    )


def _can_see(me: Me, project: dict) -> bool:
    return _sees(me.username, me.role, project)


def _can_manage(me: Me, project: dict) -> bool:
    return me.role == "responsable" or me.username == project.get("owner_username")


def _all_projects(client) -> list[dict]:
    rows = _run(client.table(T_PROJECTS).select("*").limit(_PROJECT_LIMIT), "lecture des projets")
    return [_project_out(r) for r in rows]


def _visible_projects(client, me: Me) -> list[dict]:
    return [p for p in _all_projects(client) if _can_see(me, p)]


def _check_uuid(value: Any) -> str:
    """Identifiant valide ou 404 : un identifiant mal formé n'existe pas."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="Introuvable.")


def _project_or_404(client, me: Me, project_id: Any) -> dict:
    pid = _check_uuid(project_id)
    rows = _run(client.table(T_PROJECTS).select("*").eq("id", pid).limit(1), "lecture du projet")
    if not rows or not _can_see(me, rows[0]):
        raise HTTPException(status_code=404, detail="Projet introuvable.")
    return _project_out(rows[0])


def _task_or_404(client, me: Me, task_id: Any) -> tuple[dict, dict]:
    tid = _check_uuid(task_id)
    rows = _run(client.table(T_TASKS).select("*").eq("id", tid).limit(1), "lecture de la tâche")
    if not rows:
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    projects = _run(client.table(T_PROJECTS).select("*").eq("id", rows[0].get("project_id")).limit(1), "lecture du projet")
    if not projects or not _can_see(me, projects[0]):
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    return rows[0], _project_out(projects[0])


# ═══════════════════════════════════════════════════════════════════
# Journal et notifications (secondaires : jamais bloquants)
# ═══════════════════════════════════════════════════════════════════

def _log(client, actor: str, action: str, *, project_id=None, task_id=None, detail: Optional[dict] = None) -> None:
    row = {
        "actor": actor, "action": action[:40], "project_id": project_id,
        "task_id": task_id, "detail": detail or {},
    }
    try:
        client.table(T_ACTIVITY).insert(row).execute()
    except Exception as exc:  # noqa: BLE001 - le journal ne bloque jamais l'action
        logger.warning("Journal de l'espace indisponible (%s)", type(exc).__name__)


def _notify(client, team: list[dict], recipients, actor: str, kind: str, text: str, *, project: Optional[dict] = None, task_id=None) -> None:
    """Prévient les destinataires QUI VOIENT le projet, jamais l'auteur."""
    roles = {m["username"]: m["role"] for m in team}
    rows = []
    for name in sorted({r for r in recipients if r}):
        if name == actor or name not in roles:
            continue
        if project is not None and not _sees(name, roles[name], project):
            continue
        rows.append({
            "recipient": name, "kind": kind[:30], "actor": actor, "text": text[:300],
            "project_id": project["id"] if project else None, "task_id": task_id,
        })
    if not rows:
        return
    try:
        client.table(T_NOTIFS).insert(rows).execute()
    except Exception as exc:  # noqa: BLE001 - la notification ne bloque jamais l'action
        logger.warning("Notifications de l'espace indisponibles (%s)", type(exc).__name__)


def mentions(text: str, team: list[dict]) -> set[str]:
    """Pseudos mentionnés (@pseudo) qui existent dans l'équipe, casse ignorée."""
    known = {m["username"].casefold(): m["username"] for m in team}
    return {known[m.casefold()] for m in _MENTION_RE.findall(text or "") if m.casefold() in known}


def _short(title: str) -> str:
    title = " ".join((title or "").split())
    return title if len(title) <= 80 else title[:79] + "…"


# ═══════════════════════════════════════════════════════════════════
# Validation des tâches
# ═══════════════════════════════════════════════════════════════════

Status = Literal["a_faire", "en_cours", "en_revue", "bloque", "fait"]
Priority = Literal["urgente", "haute", "normale", "basse"]
Recurrence = Literal["", "quotidienne", "hebdomadaire", "mensuelle"]


class ChecklistItem(BaseModel):
    id: str = Field(default="", max_length=40)
    text: str = Field(min_length=1, max_length=200)
    done: bool = False


class LinkItem(BaseModel):
    title: str = Field(default="", max_length=120)
    url: str = Field(min_length=1, max_length=500)


class TaskIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    status: Status = "a_faire"
    priority: Priority = "normale"
    assignee: Optional[str] = Field(default=None, max_length=80)
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    labels: list[str] = Field(default_factory=list, max_length=12)
    estimate_hours: Optional[float] = Field(default=None, ge=0, le=999)
    checklist: list[ChecklistItem] = Field(default_factory=list, max_length=60)
    links: list[LinkItem] = Field(default_factory=list, max_length=10)
    recurrence: Recurrence = ""
    cycle_id: Optional[str] = Field(default=None, max_length=40)
    key_result_id: Optional[str] = Field(default=None, max_length=40)
    blocked_by: list[str] = Field(default_factory=list, max_length=20)
    position: Optional[float] = None


class TaskPatch(BaseModel):
    project_id: Optional[str] = Field(default=None, max_length=40)
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    status: Optional[Status] = None
    priority: Optional[Priority] = None
    assignee: Optional[str] = Field(default=None, max_length=80)
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    labels: Optional[list[str]] = Field(default=None, max_length=12)
    estimate_hours: Optional[float] = Field(default=None, ge=0, le=999)
    spent_hours: Optional[float] = Field(default=None, ge=0, le=9999)
    checklist: Optional[list[ChecklistItem]] = Field(default=None, max_length=60)
    links: Optional[list[LinkItem]] = Field(default=None, max_length=10)
    recurrence: Optional[Recurrence] = None
    cycle_id: Optional[str] = Field(default=None, max_length=40)
    key_result_id: Optional[str] = Field(default=None, max_length=40)
    blocked_by: Optional[list[str]] = Field(default=None, max_length=20)
    position: Optional[float] = None
    archived: Optional[bool] = None


def clean_labels(values) -> list[str]:
    out: list[str] = []
    for value in values or []:
        label = " ".join(str(value).split()).casefold()[:30]
        if label and label not in out:
            out.append(label)
    return out[:12]


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s<>\"']+$", url, flags=re.I):
        raise HTTPException(status_code=400, detail="Lien invalide : il doit commencer par http:// ou https://.")
    return url


def _exists(client, table: str, value: Any, what: str) -> str:
    vid = _check_uuid(value)
    if not _run(client.table(table).select("id").eq("id", vid).limit(1), f"lecture {what}"):
        raise HTTPException(status_code=400, detail=f"{what.capitalize()} inconnu.")
    return vid


def _task_fields(data: dict, client, names: set[str]) -> dict:
    """Valide et normalise les champs PRÉSENTS dans `data` (création ou modification)."""
    row: dict[str, Any] = {}
    for key, value in data.items():
        if key == "title":
            value = " ".join(str(value).split()) if value is not None else ""
            if not value:
                raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
            row[key] = value
        elif key == "description":
            row[key] = str(value or "").strip()
        elif key in ("status", "priority", "recurrence"):
            if value is None:
                continue
            row[key] = value
        elif key == "assignee":
            if value in (None, ""):
                row[key] = None
            elif value not in names:
                raise HTTPException(status_code=400, detail="Responsable inconnu dans l'équipe.")
            else:
                row[key] = value
        elif key in ("start_date", "due_date"):
            row[key] = value.isoformat() if isinstance(value, date) else None
        elif key == "labels":
            row[key] = clean_labels(value)
        elif key in ("estimate_hours",):
            row[key] = None if value is None else round(float(value), 1)
        elif key == "spent_hours":
            row[key] = round(float(value or 0), 1)
        elif key == "checklist":
            row[key] = [
                {"id": (i.get("id") or uuid.uuid4().hex[:12])[:40], "text": " ".join(i["text"].split())[:200], "done": bool(i.get("done"))}
                for i in (value or []) if " ".join(str(i.get("text") or "").split())
            ]
        elif key == "links":
            row[key] = [{"title": str(i.get("title") or "").strip()[:120], "url": clean_url(i.get("url"))} for i in (value or [])]
        elif key == "blocked_by":
            ids = []
            for v in value or []:
                try:
                    vid = str(uuid.UUID(str(v)))
                except (ValueError, TypeError):
                    continue
                if vid not in ids:
                    ids.append(vid)
            row[key] = ids[:20]
        elif key == "cycle_id":
            row[key] = _exists(client, T_CYCLES, value, "cycle") if value else None
        elif key == "key_result_id":
            row[key] = _exists(client, T_KRS, value, "résultat clé") if value else None
        elif key == "position":
            if value is not None:
                row[key] = float(value)
        elif key == "archived":
            if value is not None:
                row[key] = bool(value)
    return row


def _check_dates(start: Optional[str], end: Optional[str], what: str = "L'échéance") -> None:
    if start and end and end < start:
        raise HTTPException(status_code=400, detail=f"{what} ne peut pas précéder la date de début.")


def next_occurrence(day: date, recurrence: str) -> date:
    if recurrence == "quotidienne":
        return day + timedelta(days=1)
    if recurrence == "hebdomadaire":
        return day + timedelta(days=7)
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _spawn_next(client, task: dict, actor: str) -> Optional[dict]:
    """Crée l'occurrence suivante d'une tâche récurrente qu'on vient de finir."""
    base = date.fromisoformat(task["due_date"]) if task.get("due_date") else _today()
    due = next_occurrence(base, task["recurrence"])
    start = None
    if task.get("start_date") and task.get("due_date"):
        gap = date.fromisoformat(task["due_date"]) - date.fromisoformat(task["start_date"])
        start = (due - gap).isoformat()
    row = {
        "id": str(uuid.uuid4()), "project_id": task["project_id"], "title": task["title"],
        "description": task["description"], "status": "a_faire", "priority": task["priority"],
        "assignee": task.get("assignee"), "start_date": start, "due_date": due.isoformat(),
        "labels": task["labels"], "estimate_hours": task.get("estimate_hours"), "spent_hours": 0,
        "checklist": [dict(i, done=False) for i in task["checklist"]], "links": task["links"],
        "blocked_by": [], "watchers": task["watchers"], "recurrence": task["recurrence"],
        "cycle_id": None, "key_result_id": task.get("key_result_id"), "position": time.time(),
        "created_by": actor,
    }
    inserted = _run(client.table(T_TASKS).insert(row), "création de l'occurrence suivante")
    nxt = _task_out(inserted[0] if inserted else row)
    _run(client.table(T_TASKS).update({"recurrence_next": nxt["id"]}).eq("id", task["id"]), "lien vers l'occurrence suivante")
    _log(client, actor, "task_recurred", project_id=task["project_id"], task_id=nxt["id"], detail={"from": task["id"], "due_date": nxt["due_date"]})
    return nxt


# ═══════════════════════════════════════════════════════════════════
# Page
# ═══════════════════════════════════════════════════════════════════

@router.get(PAGE_PATH, include_in_schema=False)
def workspace_page(request: Request):
    """Coquille HTML : aucune donnée, tout passe ensuite par l'API et la session."""
    response = _templates.TemplateResponse(request, "index.html", {"app_name": APP_NAME, "csp_nonce": page_nonce(request)})
    if not request.cookies.get(security.CSRF_COOKIE):
        auth.set_csrf_cookie(response)
    return response


# ═══════════════════════════════════════════════════════════════════
# Démarrage de la page
# ═══════════════════════════════════════════════════════════════════

@router.get(API + "/bootstrap")
def bootstrap(request: Request):
    client = _db()
    me = _me(request, client)
    team = _team(client, with_email=True)
    projects = _visible_projects(client, me)
    krs = [_kr_out(r) for r in _run(client.table(T_KRS).select("*").limit(1000), "lecture des résultats clés")]
    objectives = [_objective_out(r, krs) for r in _run(client.table(T_OBJECTIVES).select("*").limit(300), "lecture des objectifs")]
    cycles = sorted((_cycle_out(r) for r in _run(client.table(T_CYCLES).select("*").limit(300), "lecture des cycles")), key=lambda c: c["start_date"] or "", reverse=True)
    unread = _run(client.table(T_NOTIFS).select("id,project_id").eq("recipient", me.username).is_("read_at", "null").limit(500), "lecture des notifications")
    visible = {p["id"] for p in projects}
    profile = next((m for m in team if m["username"] == me.username),
                   _member_out({"username": me.username}, role=me.role, with_email=True))
    if me.role != "responsable":
        # Les coordonnées des collègues ne regardent que les responsables.
        team = [{k: v for k, v in m.items() if k not in ("email", "daily_digest", "whatsapp")} for m in team]
    from app import followups, sales

    return {
        "me": dict(profile, role=me.role, panel_role=me.panel_role),
        "app_name": APP_NAME,
        "today": _today().isoformat(),
        "sales_settings": sales.get_sales_settings(client),
        "followup_settings": followups.get_followup_settings(client),
        "followup_labels": {"stages": followups.STAGE_LABEL, "statuses": followups.STATUS_LABEL},
        "sale_sources": sales.SOURCE_LABEL,
        "members": team,
        "projects": projects,
        "objectives": objectives,
        "cycles": cycles,
        "unread": sum(1 for n in unread if not n.get("project_id") or n.get("project_id") in visible),
        "templates": [
            {"key": k, "name": t["name"], "summary": t["summary"], "icon": t["icon"], "tasks": len(t["tasks"]),
             "decisions": len(t.get("decisions") or []), "ai": len(t.get("ai") or [])}
            for k, t in TEMPLATES.items()
        ],
        "task_key": TASK_KEY,
        "github": github_public(),
        "server_time": _now(),
    }


def github_public() -> dict:
    """Ce que la page doit savoir de GitHub : le dépôt (liens) et s'il est branché."""
    repo = (os.environ.get("WORKSPACE_GITHUB_REPO") or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$", repo):
        repo = ""
    return {"repo": repo, "connected": bool((os.environ.get("WORKSPACE_GITHUB_SECRET") or "").strip())}


# ═══════════════════════════════════════════════════════════════════
# Tâches
# ═══════════════════════════════════════════════════════════════════

@router.get(API + "/tasks")
def list_tasks(
    request: Request,
    since: str = Query("", max_length=40),
    archived: bool = False,
    all_done: bool = False,
):
    """Tâches des projets visibles.

    `since` (horodatage ISO renvoyé par l'appel précédent) ne rend que ce qui a
    changé, archivées comprises, pour que la page retire ce qui est parti.
    """
    client = _db()
    me = _me(request, client)
    server_time = _now()
    visible = {p["id"] for p in _visible_projects(client, me)}
    if since:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Paramètre since invalide.")

    def build():
        query = client.table(T_TASKS).select("*")
        query = query.gt("updated_at", since) if since else query.eq("archived", archived)
        return query.order("id")

    # Page par page : Supabase ne rend jamais plus de 1 000 lignes d'un coup,
    # même avec .limit(5000) ; au-delà, des tâches manquaient sans le dire.
    rows = dbmod.fetch_all(build, "lecture des tâches", cap=_TASK_LIMIT)
    horizon = (datetime.now(timezone.utc) - timedelta(days=_DONE_HORIZON_DAYS)).isoformat()
    tasks = []
    for r in rows:
        if str(r.get("project_id")) not in visible:
            continue
        if not since and not archived and not all_done and r.get("status") == "fait" and str(r.get("completed_at") or "9999") < horizon:
            continue
        tasks.append(_task_out(r))
    return {"tasks": tasks, "server_time": server_time, "truncated": len(rows) >= _TASK_LIMIT}


@router.post(API + "/tasks", status_code=201)
def create_task(body: TaskIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    project = _project_or_404(client, me, body.project_id)
    team = _team(client)
    data = body.model_dump()
    data.pop("project_id")
    row = _task_fields(data, client, {m["username"] for m in team})
    _check_dates(row.get("start_date"), row.get("due_date"))
    row.update(
        id=str(uuid.uuid4()), project_id=project["id"], created_by=me.username,
        watchers=sorted({me.username} | ({row["assignee"]} if row.get("assignee") else set())),
        position=row.get("position", time.time()),
    )
    if row.get("status") == "fait":
        row["completed_at"] = _now()
    inserted = _run(client.table(T_TASKS).insert(row), "création de la tâche")
    task = _task_out(inserted[0] if inserted else row)
    _log(client, me.username, "task_created", project_id=project["id"], task_id=task["id"], detail={"title": _short(task["title"])})
    if task["assignee"]:
        _notify(client, team, {task["assignee"]}, me.username, "assigned", f"{me.username} t'a confié « {_short(task['title'])} »", project=project, task_id=task["id"])
    mentioned = mentions(task["description"], team) - {task["assignee"]}
    _notify(client, team, mentioned, me.username, "mention", f"{me.username} t'a mentionné dans « {_short(task['title'])} »", project=project, task_id=task["id"])
    return {"task": task}


@router.get(API + "/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    row, project = _task_or_404(client, me, task_id)
    tid = str(row["id"])
    comments = _run(client.table(T_COMMENTS).select("*").eq("task_id", tid).order("created_at").limit(300), "lecture des commentaires")
    activity = _run(client.table(T_ACTIVITY).select("*").eq("task_id", tid).order("created_at", desc=True).limit(100), "lecture de l'historique")
    task = _task_out(row)
    # Tâche de l'IA : l'équipe voit les grands points ; le détail technique
    # (commits, journal) ne sort que pour les responsables.
    details = task["assignee"] != AI_USER or me.role == "responsable"
    links = [_link_out(r) for r in _optional(client.table(T_GH_LINKS).select("*").eq("task_id", tid).order("created_at", desc=True).limit(100))]
    journal = []
    if me.role == "responsable":
        journal = [
            {"body": str(r.get("body") or ""), "created_at": r.get("created_at")}
            for r in _optional(client.table(T_AI_JOURNAL).select("*").eq("task_id", tid).order("created_at", desc=True).limit(100))
        ]
    return {
        "task": task,
        "project": project,
        "comments": [_comment_out(c) for c in comments],
        "activity": [_activity_out(a) for a in activity],
        "github": [x for x in links if details or x["kind"] == "pr"],
        "ai_journal": journal,
        "details_hidden": not details,
    }


def _link_out(r: dict) -> dict:
    url = str(r.get("url") or "")
    return {
        "kind": "pr" if r.get("kind") == "pr" else "commit",
        "ref": str(r.get("ref") or "")[:64],
        "title": str(r.get("title") or ""),
        "url": url if url.startswith("https://github.com/") else "",
        "state": str(r.get("state") or ""),
        "author": str(r.get("author") or ""),
        "by_ai": bool(r.get("by_ai")),
        "created_at": r.get("created_at"),
    }


def _optional(query) -> list[dict]:
    """Lecture d'un complément (GitHub, journal) : absent, la tâche s'affiche quand même.

    Ces tables arrivent avec une version plus récente du script SQL ; tant
    qu'il n'a pas été rejoué, la fiche reste lisible, sans la section.
    """
    try:
        res = query.execute()
    except Exception as exc:  # noqa: BLE001 - complément facultatif
        logger.info("Espace d'équipe : complément indisponible (%s)", type(exc).__name__)
        return []
    data = getattr(res, "data", None)
    return [r for r in (data or []) if isinstance(r, dict)]


def _guard_decision(me: Me, current: dict, changes: dict, became_done: bool) -> None:
    """Une décision se valide par la personne qui tranche, et par elle seule."""
    decider = current["assignee"]
    if became_done and (decider is None and me.role != "responsable" or decider is not None and me.username != decider):
        who = decider or "un responsable"
        raise HTTPException(status_code=403, detail=f"Décision stratégique : seule la validation de {who} la clôt.")
    if "assignee" in changes and changes["assignee"] != decider and me.role != "responsable":
        raise HTTPException(status_code=403, detail="Seul un responsable peut confier une décision à quelqu'un d'autre.")


def _apply_patch(client, me: Me, team: list[dict], task_id: str, data: dict) -> dict:
    """Modifie une tâche ; partagé par la modification simple et l'action groupée."""
    current_row, project = _task_or_404(client, me, task_id)
    current = _task_out(current_row)
    if not data:
        return {"task": current, "spawned": None}
    target = project
    if data.get("project_id") and data["project_id"] != current["project_id"]:
        target = _project_or_404(client, me, data["project_id"])
    data.pop("project_id", None)
    changes = _task_fields(data, client, {m["username"] for m in team})
    if target["id"] != current["project_id"]:
        changes["project_id"] = target["id"]
    _check_dates(changes.get("start_date", current["start_date"]), changes.get("due_date", current["due_date"]))
    new_status = changes.get("status", current["status"])
    became_done = new_status == "fait" and current["status"] != "fait"
    if DECISION_LABEL in current["labels"]:
        _guard_decision(me, current, changes, became_done)
    if became_done:
        changes["completed_at"] = _now()
    elif new_status != "fait" and current["status"] == "fait":
        changes["completed_at"] = None
    if changes.get("assignee") and changes["assignee"] not in current["watchers"]:
        changes["watchers"] = sorted(set(current["watchers"]) | {changes["assignee"]})
    changes = {k: v for k, v in changes.items() if current.get(k) != v or k == "completed_at"}
    if not changes:
        return {"task": current, "spawned": None}
    updated = _run(client.table(T_TASKS).update(changes).eq("id", current["id"]), "modification de la tâche")
    task = _task_out(updated[0]) if updated else _task_out({**current_row, **changes, "updated_at": _now()})

    spawned = None
    if became_done and task["recurrence"] and not current_row.get("recurrence_next"):
        spawned = _spawn_next(client, task, me.username)

    tracked = ("status", "assignee", "due_date", "priority", "title", "project_id", "cycle_id", "archived")
    diff = {k: {"de": current.get(k), "a": task.get(k)} for k in tracked if k in changes}
    if diff:
        _log(client, me.username, "task_updated", project_id=task["project_id"], task_id=task["id"], detail=diff)
    title = _short(task["title"])
    if "assignee" in changes and task["assignee"]:
        _notify(client, team, {task["assignee"]}, me.username, "assigned", f"{me.username} t'a confié « {title} »", project=target, task_id=task["id"])
    if "status" in changes:
        watchers = set(task["watchers"]) | {task["created_by"]}
        _notify(client, team, watchers - {task["assignee"] if "assignee" in changes else None}, me.username, "status",
                f"« {title} » → {_STATUS_LABEL[task['status']]} ({me.username})", project=target, task_id=task["id"])
    if "description" in changes:
        before = mentions(current["description"], team)
        _notify(client, team, mentions(task["description"], team) - before, me.username, "mention",
                f"{me.username} t'a mentionné dans « {title} »", project=target, task_id=task["id"])
    return {"task": task, "spawned": spawned}


@router.patch(API + "/tasks/{task_id}")
def update_task(task_id: str, body: TaskPatch, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    return _apply_patch(client, me, _team(client), task_id, body.model_dump(exclude_unset=True))


class BulkChanges(BaseModel):
    status: Optional[Status] = None
    priority: Optional[Priority] = None
    assignee: Optional[str] = Field(default=None, max_length=80)
    due_date: Optional[date] = None
    cycle_id: Optional[str] = Field(default=None, max_length=40)
    project_id: Optional[str] = Field(default=None, max_length=40)
    archived: Optional[bool] = None


class BulkIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    changes: BulkChanges


@router.post(API + "/tasks/bulk")
def bulk_update(body: BulkIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    team = _team(client)
    data = body.changes.model_dump(exclude_unset=True)
    tasks, missing = [], 0
    for task_id in dict.fromkeys(body.ids):
        try:
            tasks.append(_apply_patch(client, me, team, task_id, dict(data))["task"])
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            missing += 1
    return {"tasks": tasks, "missing": missing}


@router.delete(API + "/tasks/{task_id}")
def archive_task(task_id: str, request: Request):
    """Met la tâche à la corbeille (restaurable) ; rien n'est effacé."""
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    return _apply_patch(client, me, _team(client), task_id, {"archived": True})


class WatchIn(BaseModel):
    watch: bool = True


@router.post(API + "/tasks/{task_id}/watch")
def watch_task(task_id: str, body: WatchIn, request: Request):
    """Suivre une tâche : affaire personnelle, ouverte aux observateurs."""
    client = _db()
    me = _me(request, client)
    row, _project = _task_or_404(client, me, task_id)
    watchers = set(_strs(row.get("watchers")))
    watchers = watchers | {me.username} if body.watch else watchers - {me.username}
    updated = _run(client.table(T_TASKS).update({"watchers": sorted(watchers)}).eq("id", str(row["id"])), "suivi de la tâche")
    return {"task": _task_out(updated[0] if updated else dict(row, watchers=sorted(watchers)))}


# ═══════════════════════════════════════════════════════════════════
# Commentaires
# ═══════════════════════════════════════════════════════════════════

def _comment_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "task_id": str(r.get("task_id") or ""),
        "author": str(r.get("author") or ""), "body": str(r.get("body") or ""),
        "created_at": r.get("created_at"), "edited_at": r.get("edited_at"),
    }


def _activity_out(r: dict) -> dict:
    detail = r.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {}
    return {
        "id": r.get("id"), "actor": str(r.get("actor") or ""), "action": str(r.get("action") or ""),
        "project_id": _id_or_none(r.get("project_id")), "task_id": _id_or_none(r.get("task_id")),
        "detail": detail if isinstance(detail, dict) else {}, "created_at": r.get("created_at"),
    }


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


@router.post(API + "/tasks/{task_id}/comments", status_code=201)
def add_comment(task_id: str, body: CommentIn, request: Request):
    client = _db()
    me = _me(request, client)
    row, project = _task_or_404(client, me, task_id)
    return {"comment": add_comment_as(client, me, _team(client), row, project, body.body)}


def add_comment_as(client, me: Me, team: list[dict], row: dict, project: dict, text: str) -> dict:
    """Ajoute un commentaire et prévient qui doit l'être (partagé avec l'IA)."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Le commentaire est vide.")
    comment = {"id": str(uuid.uuid4()), "task_id": str(row["id"]), "author": me.username, "body": text[:5000]}
    inserted = _run(client.table(T_COMMENTS).insert(comment), "ajout du commentaire")
    task = _task_out(row)
    if me.username not in task["watchers"]:
        _run(client.table(T_TASKS).update({"watchers": sorted(set(task["watchers"]) | {me.username})}).eq("id", task["id"]), "suivi de la tâche")
    title = _short(task["title"])
    mentioned = mentions(text, team)
    _notify(client, team, mentioned, me.username, "mention", f"{me.username} t'a mentionné dans « {title} »", project=project, task_id=task["id"])
    others = (set(task["watchers"]) | ({task["assignee"]} if task["assignee"] else set())) - mentioned
    _notify(client, team, others, me.username, "comment", f"{me.username} a commenté « {title} »", project=project, task_id=task["id"])
    _log(client, me.username, "comment_added", project_id=project["id"], task_id=task["id"])
    return _comment_out(inserted[0] if inserted else comment)


@router.delete(API + "/comments/{comment_id}")
def delete_comment(comment_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    cid = _check_uuid(comment_id)
    rows = _run(client.table(T_COMMENTS).select("*").eq("id", cid).limit(1), "lecture du commentaire")
    if not rows:
        raise HTTPException(status_code=404, detail="Commentaire introuvable.")
    _task_or_404(client, me, rows[0].get("task_id"))
    if rows[0].get("author") != me.username and me.role != "responsable":
        raise HTTPException(status_code=403, detail="Seul son auteur ou un responsable peut supprimer ce commentaire.")
    _run(client.table(T_COMMENTS).delete().eq("id", cid), "suppression du commentaire")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Projets
# ═══════════════════════════════════════════════════════════════════

class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    color: str = Field(default="#0B6FA4", max_length=7)
    icon: str = Field(default="", max_length=8)
    status: Literal["actif", "pause", "termine", "archive"] = "actif"
    owner_username: Optional[str] = Field(default=None, max_length=80)
    visibility: Literal["equipe", "restreint"] = "equipe"
    members: list[str] = Field(default_factory=list, max_length=50)
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    objective_id: Optional[str] = Field(default=None, max_length=40)


class ProjectPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=4000)
    color: Optional[str] = Field(default=None, max_length=7)
    icon: Optional[str] = Field(default=None, max_length=8)
    status: Optional[Literal["actif", "pause", "termine", "archive"]] = None
    owner_username: Optional[str] = Field(default=None, max_length=80)
    visibility: Optional[Literal["equipe", "restreint"]] = None
    members: Optional[list[str]] = Field(default=None, max_length=50)
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    objective_id: Optional[str] = Field(default=None, max_length=40)


def _project_fields(data: dict, client, names: set[str]) -> dict:
    row: dict[str, Any] = {}
    for key, value in data.items():
        if key == "name":
            value = " ".join(str(value or "").split())
            if not value:
                raise HTTPException(status_code=400, detail="Le nom du projet est obligatoire.")
            row[key] = value
        elif key == "description":
            row[key] = str(value or "").strip()
        elif key == "color":
            if value is not None:
                if not _COLOR_RE.match(value):
                    raise HTTPException(status_code=400, detail="Couleur invalide (format #RRGGBB).")
                row[key] = value
        elif key == "icon":
            row[key] = str(value or "").strip()
        elif key in ("status", "visibility"):
            if value is not None:
                row[key] = value
        elif key == "owner_username":
            if value and value not in names:
                raise HTTPException(status_code=400, detail="Responsable du projet inconnu dans l'équipe.")
            if value is not None:
                row[key] = value or ""
        elif key == "members":
            unknown = [m for m in value or [] if m not in names]
            if unknown:
                raise HTTPException(status_code=400, detail="Membre du projet inconnu dans l'équipe.")
            row[key] = sorted(set(value or []))
        elif key in ("start_date", "due_date"):
            row[key] = value.isoformat() if isinstance(value, date) else None
        elif key == "objective_id":
            row[key] = _exists(client, T_OBJECTIVES, value, "objectif") if value else None
    return row


@router.post(API + "/projects", status_code=201)
def create_project(body: ProjectIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    team = _team(client)
    row = _project_fields(body.model_dump(), client, {m["username"] for m in team})
    _check_dates(row.get("start_date"), row.get("due_date"), "La date de fin")
    row.setdefault("owner_username", me.username)
    if not row["owner_username"]:
        row["owner_username"] = me.username
    if row.get("visibility") == "restreint":
        row["members"] = sorted(set(row.get("members") or []) | {me.username})
    row.update(id=str(uuid.uuid4()), created_by=me.username)
    inserted = _run(client.table(T_PROJECTS).insert(row), "création du projet")
    project = _project_out(inserted[0] if inserted else row)
    _log(client, me.username, "project_created", project_id=project["id"], detail={"name": project["name"]})
    return {"project": project}


@router.patch(API + "/projects/{project_id}")
def update_project(project_id: str, body: ProjectPatch, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _project_or_404(client, me, project_id)
    data = body.model_dump(exclude_unset=True)
    sensitive = {"visibility", "members", "owner_username"} & set(data)
    if sensitive and not _can_manage(me, current):
        raise HTTPException(status_code=403, detail="Seul le responsable du projet ou de l'espace change sa visibilité, ses membres ou son responsable.")
    if data.get("status") == "archive" and not _can_manage(me, current):
        raise HTTPException(status_code=403, detail="Seul le responsable du projet ou de l'espace peut l'archiver.")
    team = _team(client)
    changes = _project_fields(data, client, {m["username"] for m in team})
    _check_dates(changes.get("start_date", current["start_date"]), changes.get("due_date", current["due_date"]), "La date de fin")
    if (
        {"visibility", "members"} & set(changes)
        and changes.get("visibility", current["visibility"]) == "restreint"
        and me.role != "responsable"
    ):
        # Celui qui restreint un projet ne doit pas s'en exclure par mégarde.
        changes["members"] = sorted(set(changes.get("members", current["members"])) | {me.username})
    if not changes:
        return {"project": current}
    updated = _run(client.table(T_PROJECTS).update(changes).eq("id", current["id"]), "modification du projet")
    project = _project_out(updated[0] if updated else {**current, **changes})
    _log(client, me.username, "project_updated", project_id=project["id"], detail={k: True for k in changes})
    return {"project": project}


# ═══════════════════════════════════════════════════════════════════
# Membres (comptes de l'espace)
# ═══════════════════════════════════════════════════════════════════

def clean_github_login(raw: Any) -> Optional[str]:
    """Pseudo GitHub : vide → None, mal formé → 400. Sert à nommer l'auteur d'un commit."""
    text = str(raw or "").strip().lstrip("@")
    if not text:
        return None
    if not _GH_LOGIN_RE.match(text):
        raise HTTPException(status_code=400, detail="Pseudo GitHub invalide (lettres, chiffres et tirets).")
    return text


def clean_member_email(raw: Any) -> Optional[str]:
    """Adresse de l'e-mail du matin : vide → None, mal formée → 400."""
    text = str(raw or "").strip()
    if not text:
        return None
    from app import email_sender

    cleaned = email_sender.clean_email(text)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Adresse e-mail invalide.")
    return cleaned


Role = Literal["responsable", "membre", "observateur"]
_ACCOUNT_FIELDS = ("display_name", "role", "whatsapp", "active")


class MemberCreate(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1, max_length=60)
    whatsapp: str = Field(default="", max_length=30)
    role: Role = "membre"
    email: str = Field(default="", max_length=254)
    daily_digest: bool = True
    github_login: str = Field(default="", max_length=40)
    title: str = Field(default="", max_length=60)
    color: str = Field(default="#0B6FA4", max_length=7)
    capacity_hours: float = Field(default=30, ge=0, le=80)


class MemberIn(BaseModel):
    role: Optional[Role] = None
    display_name: Optional[str] = Field(default=None, max_length=60)
    whatsapp: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, max_length=254)
    daily_digest: Optional[bool] = None
    github_login: Optional[str] = Field(default=None, max_length=40)
    title: Optional[str] = Field(default=None, max_length=60)
    color: Optional[str] = Field(default=None, max_length=7)
    capacity_hours: Optional[float] = Field(default=None, ge=0, le=80)
    active: Optional[bool] = None


class ProfileIn(BaseModel):
    email: Optional[str] = Field(default=None, max_length=254)
    daily_digest: Optional[bool] = None
    github_login: Optional[str] = Field(default=None, max_length=40)
    display_name: Optional[str] = Field(default=None, max_length=60)
    whatsapp: Optional[str] = Field(default=None, max_length=30)
    title: Optional[str] = Field(default=None, max_length=60)
    color: Optional[str] = Field(default=None, max_length=7)
    capacity_hours: Optional[float] = Field(default=None, ge=0, le=80)


def _member_changes(client, username: str, data: dict) -> tuple[dict, dict]:
    """Sépare et valide : ce qui va au compte (nom, rôle, numéro, actif), ce qui va à la fiche."""
    account: dict[str, Any] = {}
    profile: dict[str, Any] = {}
    for key, value in data.items():
        if value is None and key not in ("whatsapp", "email", "github_login"):
            continue
        if key == "display_name":
            value = " ".join(str(value).split())
            if not value:
                raise HTTPException(status_code=400, detail="Le nom affiché est obligatoire.")
            account[key] = value
        elif key == "whatsapp":
            account[key] = auth.check_whatsapp(client, value or "", exclude=username)
        elif key in ("role", "active"):
            account[key] = value
        elif key == "color":
            if not _COLOR_RE.match(value):
                raise HTTPException(status_code=400, detail="Couleur invalide (format #RRGGBB).")
            profile[key] = value
        elif key == "title":
            profile[key] = " ".join(str(value).split())
        elif key == "email":
            profile[key] = clean_member_email(value)
        elif key == "github_login":
            profile[key] = clean_github_login(value)
        elif key == "capacity_hours":
            profile[key] = round(float(value), 1)
        elif key == "daily_digest":
            profile[key] = bool(value)
    return account, profile


def _save_member(client, username: str, account: dict, profile: dict, added_by: str) -> None:
    if account:
        _run(client.table(T_ACCOUNTS).update(account).eq("username", username), "modification du compte")
    if profile:
        existing = _run(client.table(T_MEMBERS).select("username").eq("username", username).limit(1), "lecture du membre")
        if existing:
            _run(client.table(T_MEMBERS).update(profile).eq("username", username), "modification de la fiche")
        else:
            _run(client.table(T_MEMBERS).insert(dict(profile, username=username, added_by=added_by)), "création de la fiche")


def _member_view(client, username: str) -> dict:
    found = next((m for m in _team(client, with_email=True) if m["username"] == username), None)
    if found:
        return found
    acc = auth.account_by_username(client, username) or {"username": username}
    return _member_out(acc, with_email=True)


def _other_owners(client, username: str) -> int:
    return sum(1 for a in _accounts(client)
               if a.get("role") == "responsable" and a.get("active") is not False and a.get("username") != username)


def _invite(account: dict, password: str) -> dict:
    text = auth.invite_text(account.get("display_name") or account["username"], account["username"], password, account.get("whatsapp"))
    return {"password": password, "invite_text": text, "wa_link": security.wa_link(account.get("whatsapp") or "", text)}


@router.post(API + "/members", status_code=201)
def create_member(body: MemberCreate, request: Request):
    """Nouveau compte : le mot de passe généré n'est montré qu'UNE fois, ici."""
    client = _db()
    me = _me(request, client)
    _require(me, "responsable")
    username = auth.check_new_username(client, body.username)
    whatsapp = auth.check_whatsapp(client, body.whatsapp)
    if not _COLOR_RE.match(body.color):
        raise HTTPException(status_code=400, detail="Couleur invalide (format #RRGGBB).")
    profile = {
        "title": " ".join(body.title.split()), "color": body.color, "capacity_hours": round(body.capacity_hours, 1),
        "email": clean_member_email(body.email), "daily_digest": body.daily_digest,
        "github_login": clean_github_login(body.github_login),
    }
    account, password = auth.create_account(
        client, username=username, display_name=body.display_name, role=body.role, whatsapp=whatsapp,
        created_by=me.username, profile=profile,
    )
    _log(client, me.username, "member_added", detail={"username": username, "role": body.role})
    return dict(_invite(account, password), member=_member_view(client, username))


@router.post(API + "/members/{username}/password")
def reset_member_password(username: str, request: Request):
    """Nouveau mot de passe pour un membre (oubli) : ses sessions ouvertes tombent."""
    client = _db()
    me = _me(request, client)
    _require(me, "responsable")
    if username == AI_USER:
        raise HTTPException(status_code=400, detail="L'IA n'a pas de mot de passe : elle passe par son jeton.")
    password = auth.reset_password(client, username)
    account = auth.account_by_username(client, username) or {"username": username}
    _log(client, me.username, "member_password", detail={"username": username})
    return dict(_invite(account, password), member=_member_view(client, username))


@router.patch(API + "/members/me")
def update_my_profile(body: ProfileIn, request: Request):
    """Son propre profil (nom affiché, numéro, poste, couleur, disponibilité) ; jamais son rôle."""
    client = _db()
    me = _me(request, client)
    account, profile = _member_changes(client, me.username, body.model_dump(exclude_unset=True))
    _save_member(client, me.username, account, profile, me.username)
    return {"member": dict(_member_view(client, me.username), role=me.role)}


@router.put(API + "/members/{username}")
def upsert_member(username: str, body: MemberIn, request: Request):
    """Un responsable règle un membre : rôle, nom, numéro, fiche, ou le réactive."""
    client = _db()
    me = _me(request, client)
    _require(me, "responsable")
    if username == AI_USER:
        raise HTTPException(status_code=400, detail="L'IA fait partie de l'équipe d'office ; elle ne se règle pas ici.")
    if not auth.account_by_username(client, username):
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    data = body.model_dump(exclude_unset=True)
    demoted = ("role" in data and data["role"] != "responsable") or data.get("active") is False
    if demoted and username == me.username and not _other_owners(client, username):
        raise HTTPException(status_code=409, detail="Il faut au moins un autre responsable avant de quitter ce rôle.")
    if data.get("active") is False and username == me.username:
        raise HTTPException(status_code=409, detail="Tu ne peux pas désactiver ton propre compte.")
    account, profile = _member_changes(client, username, data)
    _save_member(client, username, account, profile, me.username)
    _log(client, me.username, "member_updated", detail={"username": username, **{k: v for k, v in account.items() if k in ("role", "active")}})
    return {"member": _member_view(client, username)}


@router.delete(API + "/members/{username}")
def remove_member(username: str, request: Request):
    """Retire un membre : compte désactivé (plus de connexion), ses tâches et ventes restent."""
    client = _db()
    me = _me(request, client)
    _require(me, "responsable")
    if username == me.username:
        raise HTTPException(status_code=409, detail="Tu ne peux pas te retirer toi-même de l'espace.")
    if not auth.account_by_username(client, username):
        raise HTTPException(status_code=404, detail="Membre introuvable.")
    _run(client.table(T_ACCOUNTS).update({"active": False}).eq("username", username), "retrait du membre")
    _log(client, me.username, "member_removed", detail={"username": username})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Objectifs, résultats clés et points d'étape
# ═══════════════════════════════════════════════════════════════════

class ObjectiveIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    period: str = Field(default="", max_length=40)
    owner_username: Optional[str] = Field(default=None, max_length=80)
    status: Literal["en_cours", "atteint", "abandonne"] = "en_cours"
    start_date: Optional[date] = None
    due_date: Optional[date] = None


class KeyResultIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    unit: str = Field(default="", max_length=20)
    start_value: float = Field(default=0, ge=-1e12, le=1e12)
    current_value: Optional[float] = Field(default=None, ge=-1e12, le=1e12)
    target_value: float = Field(default=0, ge=-1e12, le=1e12)
    owner_username: Optional[str] = Field(default=None, max_length=80)
    position: int = Field(default=0, ge=0, le=1000)
    # Mise à jour automatique depuis les ventes : cumul des cartes ou de la marge.
    auto: Literal["", "ventes_cumul", "marge_cumul"] = ""


class CheckinIn(BaseModel):
    value: float = Field(ge=-1e12, le=1e12)
    note: str = Field(default="", max_length=500)


def _objective_fields(data: dict, names: set[str]) -> dict:
    row = {}
    for key, value in data.items():
        if key == "title":
            value = " ".join(str(value or "").split())
            if not value:
                raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
            row[key] = value
        elif key in ("description", "period"):
            row[key] = str(value or "").strip()
        elif key == "owner_username":
            if value and value not in names:
                raise HTTPException(status_code=400, detail="Responsable inconnu dans l'équipe.")
            row[key] = value or None
        elif key in ("start_date", "due_date"):
            row[key] = value.isoformat() if isinstance(value, date) else None
        elif key == "status" and value is not None:
            row[key] = value
    return row


def _objective_or_404(client, objective_id: str) -> dict:
    oid = _check_uuid(objective_id)
    rows = _run(client.table(T_OBJECTIVES).select("*").eq("id", oid).limit(1), "lecture de l'objectif")
    if not rows:
        raise HTTPException(status_code=404, detail="Objectif introuvable.")
    return rows[0]


def _objective_full(client, row: dict) -> dict:
    krs = [_kr_out(r) for r in _run(client.table(T_KRS).select("*").eq("objective_id", str(row["id"])).limit(50), "lecture des résultats clés")]
    return _objective_out(row, krs)


@router.post(API + "/objectives", status_code=201)
def create_objective(body: ObjectiveIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    row = _objective_fields(body.model_dump(), {m["username"] for m in _team(client)})
    _check_dates(row.get("start_date"), row.get("due_date"))
    row.update(id=str(uuid.uuid4()), created_by=me.username)
    row["owner_username"] = row.get("owner_username") or me.username
    inserted = _run(client.table(T_OBJECTIVES).insert(row), "création de l'objectif")
    _log(client, me.username, "objective_created", detail={"title": row["title"]})
    return {"objective": _objective_out(inserted[0] if inserted else row, [])}


@router.patch(API + "/objectives/{objective_id}")
def update_objective(objective_id: str, body: ObjectiveIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _objective_or_404(client, objective_id)
    changes = _objective_fields(body.model_dump(exclude_unset=True), {m["username"] for m in _team(client)})
    _check_dates(changes.get("start_date", _date(current.get("start_date"))), changes.get("due_date", _date(current.get("due_date"))))
    updated = _run(client.table(T_OBJECTIVES).update(changes).eq("id", str(current["id"])), "modification de l'objectif") if changes else []
    return {"objective": _objective_full(client, updated[0] if updated else {**current, **changes})}


@router.delete(API + "/objectives/{objective_id}")
def delete_objective(objective_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    current = _objective_or_404(client, objective_id)
    if me.role != "responsable" and me.username not in (current.get("owner_username"), current.get("created_by")):
        raise HTTPException(status_code=403, detail="Seul son responsable ou un responsable de l'espace supprime un objectif.")
    _run(client.table(T_OBJECTIVES).delete().eq("id", str(current["id"])), "suppression de l'objectif")
    _log(client, me.username, "objective_deleted", detail={"title": current.get("title")})
    return {"ok": True}


@router.post(API + "/objectives/{objective_id}/key-results", status_code=201)
def create_key_result(objective_id: str, body: KeyResultIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    objective = _objective_or_404(client, objective_id)
    names = {m["username"] for m in _team(client)}
    if body.owner_username and body.owner_username not in names:
        raise HTTPException(status_code=400, detail="Responsable inconnu dans l'équipe.")
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
    row = {
        "id": str(uuid.uuid4()), "objective_id": str(objective["id"]), "title": title, "unit": body.unit.strip(),
        "start_value": body.start_value, "target_value": body.target_value,
        "current_value": body.start_value if body.current_value is None else body.current_value,
        "owner_username": body.owner_username or None, "position": body.position, "auto": body.auto,
    }
    inserted = _run(client.table(T_KRS).insert(row), "création du résultat clé")
    if body.auto:
        from app import sales

        sales.refresh_auto_krs(client)
        return {"key_result": _kr_out(_kr_or_404(client, row["id"]))}
    return {"key_result": _kr_out(inserted[0] if inserted else row)}


def _kr_or_404(client, kr_id: str) -> dict:
    kid = _check_uuid(kr_id)
    rows = _run(client.table(T_KRS).select("*").eq("id", kid).limit(1), "lecture du résultat clé")
    if not rows:
        raise HTTPException(status_code=404, detail="Résultat clé introuvable.")
    return rows[0]


@router.patch(API + "/key-results/{kr_id}")
def update_key_result(kr_id: str, body: KeyResultIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _kr_or_404(client, kr_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("owner_username") and data["owner_username"] not in {m["username"] for m in _team(client)}:
        raise HTTPException(status_code=400, detail="Responsable inconnu dans l'équipe.")
    if "title" in data:
        data["title"] = " ".join(data["title"].split())
    if "unit" in data:
        data["unit"] = data["unit"].strip()
    if data.get("current_value", 0) is None:
        data.pop("current_value")
    updated = _run(client.table(T_KRS).update(data).eq("id", str(current["id"])), "modification du résultat clé") if data else []
    if data.get("auto"):
        from app import sales

        sales.refresh_auto_krs(client)
        return {"key_result": _kr_out(_kr_or_404(client, str(current["id"])))}
    return {"key_result": _kr_out(updated[0] if updated else {**current, **data})}


@router.delete(API + "/key-results/{kr_id}")
def delete_key_result(kr_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _kr_or_404(client, kr_id)
    _run(client.table(T_KRS).delete().eq("id", str(current["id"])), "suppression du résultat clé")
    return {"ok": True}


@router.get(API + "/key-results/{kr_id}/checkins")
def list_checkins(kr_id: str, request: Request):
    client = _db()
    _me(request, client)
    current = _kr_or_404(client, kr_id)
    rows = _run(client.table(T_CHECKINS).select("*").eq("key_result_id", str(current["id"])).order("created_at").limit(500), "lecture des points d'étape")
    return {"checkins": [
        {"id": str(r.get("id")), "value": _num(r.get("value")), "note": str(r.get("note") or ""),
         "author": str(r.get("author") or ""), "created_at": r.get("created_at")}
        for r in rows
    ]}


@router.post(API + "/key-results/{kr_id}/checkins", status_code=201)
def add_checkin(kr_id: str, body: CheckinIn, request: Request):
    """Point d'étape : nouvelle valeur du résultat clé, avec une note."""
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _kr_or_404(client, kr_id)
    row = {"id": str(uuid.uuid4()), "key_result_id": str(current["id"]), "value": body.value, "note": body.note.strip(), "author": me.username}
    _run(client.table(T_CHECKINS).insert(row), "ajout du point d'étape")
    updated = _run(client.table(T_KRS).update({"current_value": body.value}).eq("id", str(current["id"])), "mise à jour du résultat clé")
    _log(client, me.username, "checkin", detail={"key_result": current.get("title"), "value": body.value})
    return {"key_result": _kr_out(updated[0] if updated else dict(current, current_value=body.value))}


# ═══════════════════════════════════════════════════════════════════
# Cycles
# ═══════════════════════════════════════════════════════════════════

class CycleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(default="", max_length=500)
    start_date: date
    end_date: date


def _check_cycle(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(status_code=400, detail="La fin du cycle ne peut pas précéder son début.")
    if (end - start).days > 92:
        raise HTTPException(status_code=400, detail="Un cycle dure au plus trois mois.")


@router.post(API + "/cycles", status_code=201)
def create_cycle(body: CycleIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    _check_cycle(body.start_date, body.end_date)
    row = {
        "id": str(uuid.uuid4()), "name": " ".join(body.name.split()), "goal": body.goal.strip(),
        "start_date": body.start_date.isoformat(), "end_date": body.end_date.isoformat(), "created_by": me.username,
    }
    inserted = _run(client.table(T_CYCLES).insert(row), "création du cycle")
    _log(client, me.username, "cycle_created", detail={"name": row["name"]})
    return {"cycle": _cycle_out(inserted[0] if inserted else row)}


@router.patch(API + "/cycles/{cycle_id}")
def update_cycle(cycle_id: str, body: CycleIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    cid = _check_uuid(cycle_id)
    if not _run(client.table(T_CYCLES).select("id").eq("id", cid).limit(1), "lecture du cycle"):
        raise HTTPException(status_code=404, detail="Cycle introuvable.")
    _check_cycle(body.start_date, body.end_date)
    row = {"name": " ".join(body.name.split()), "goal": body.goal.strip(), "start_date": body.start_date.isoformat(), "end_date": body.end_date.isoformat()}
    updated = _run(client.table(T_CYCLES).update(row).eq("id", cid), "modification du cycle")
    return {"cycle": _cycle_out(updated[0] if updated else dict(row, id=cid))}


@router.delete(API + "/cycles/{cycle_id}")
def delete_cycle(cycle_id: str, request: Request):
    """Supprime le cycle ; ses tâches restent, simplement hors cycle."""
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    cid = _check_uuid(cycle_id)
    rows = _run(client.table(T_CYCLES).select("*").eq("id", cid).limit(1), "lecture du cycle")
    if not rows:
        raise HTTPException(status_code=404, detail="Cycle introuvable.")
    if me.role != "responsable" and rows[0].get("created_by") != me.username:
        raise HTTPException(status_code=403, detail="Seul son créateur ou un responsable supprime un cycle.")
    _run(client.table(T_CYCLES).delete().eq("id", cid), "suppression du cycle")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Documents
# ═══════════════════════════════════════════════════════════════════

class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(default="", max_length=100000)
    kind: Literal["note", "reunion", "decision", "guide"] = "note"
    project_id: Optional[str] = Field(default=None, max_length=40)
    pinned: bool = False


def _doc_or_404(client, me: Me, doc_id: str) -> dict:
    did = _check_uuid(doc_id)
    rows = _run(client.table(T_DOCS).select("*").eq("id", did).limit(1), "lecture du document")
    if not rows:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    if rows[0].get("project_id"):
        try:
            _project_or_404(client, me, rows[0]["project_id"])
        except HTTPException:
            raise HTTPException(status_code=404, detail="Document introuvable.")
    return rows[0]


@router.get(API + "/docs")
def list_docs(request: Request):
    client = _db()
    me = _me(request, client)
    visible = {p["id"] for p in _visible_projects(client, me)}
    rows = _run(client.table(T_DOCS).select("*").order("updated_at", desc=True).limit(500), "lecture des documents")
    return {"docs": [_doc_out(r, with_body=False) for r in rows if not r.get("project_id") or str(r["project_id"]) in visible]}


@router.get(API + "/docs/{doc_id}")
def get_doc(doc_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    return {"doc": _doc_out(_doc_or_404(client, me, doc_id))}


@router.post(API + "/docs", status_code=201)
def create_doc(body: DocIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    project_id = _project_or_404(client, me, body.project_id)["id"] if body.project_id else None
    row = {
        "id": str(uuid.uuid4()), "title": " ".join(body.title.split()), "body": body.body,
        "kind": body.kind, "project_id": project_id, "pinned": body.pinned, "author": me.username,
    }
    if not row["title"]:
        raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
    inserted = _run(client.table(T_DOCS).insert(row), "création du document")
    _log(client, me.username, "doc_created", project_id=project_id, detail={"title": row["title"]})
    team = _team(client)
    project = _project_or_404(client, me, project_id) if project_id else None
    _notify(client, team, mentions(body.body, team), me.username, "mention", f"{me.username} t'a mentionné dans le document « {_short(row['title'])} »", project=project)
    return {"doc": _doc_out(inserted[0] if inserted else row)}


@router.patch(API + "/docs/{doc_id}")
def update_doc(doc_id: str, body: DocIn, request: Request):
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    current = _doc_or_404(client, me, doc_id)
    data = body.model_dump(exclude_unset=True)
    if "project_id" in data:
        data["project_id"] = _project_or_404(client, me, data["project_id"])["id"] if data["project_id"] else None
    if "title" in data:
        data["title"] = " ".join(data["title"].split())
        if not data["title"]:
            raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
    updated = _run(client.table(T_DOCS).update(data).eq("id", str(current["id"])), "modification du document") if data else []
    return {"doc": _doc_out(updated[0] if updated else {**current, **data})}


@router.delete(API + "/docs/{doc_id}")
def delete_doc(doc_id: str, request: Request):
    client = _db()
    me = _me(request, client)
    current = _doc_or_404(client, me, doc_id)
    if current.get("author") != me.username and me.role != "responsable":
        raise HTTPException(status_code=403, detail="Seul son auteur ou un responsable supprime un document.")
    _run(client.table(T_DOCS).delete().eq("id", str(current["id"])), "suppression du document")
    # Avec son projet : le fil d'activité (lisible des observateurs) filtre les projets restreints.
    _log(client, me.username, "doc_deleted", project_id=_id_or_none(current.get("project_id")), detail={"title": current.get("title")})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# Notifications et fil d'activité
# ═══════════════════════════════════════════════════════════════════

@router.get(API + "/notifications")
def list_notifications(request: Request, limit: int = Query(60, ge=1, le=200)):
    client = _db()
    me = _me(request, client)
    visible = {p["id"] for p in _visible_projects(client, me)}
    rows = _run(client.table(T_NOTIFS).select("*").eq("recipient", me.username).order("created_at", desc=True).limit(limit), "lecture des notifications")
    # Un projet devenu restreint entre-temps : sa notification disparaît aussi.
    items = [
        {"id": str(r.get("id")), "kind": r.get("kind"), "actor": r.get("actor"), "text": r.get("text"),
         "project_id": _id_or_none(r.get("project_id")), "task_id": _id_or_none(r.get("task_id")),
         "read": bool(r.get("read_at")), "created_at": r.get("created_at")}
        for r in rows if not r.get("project_id") or str(r["project_id"]) in visible
    ]
    return {"notifications": items, "unread": sum(1 for n in items if not n["read"])}


class ReadIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)
    all: bool = False


@router.post(API + "/notifications/read")
def mark_read(body: ReadIn, request: Request):
    client = _db()
    me = _me(request, client)
    query = client.table(T_NOTIFS).update({"read_at": _now()}).eq("recipient", me.username).is_("read_at", "null")
    if not body.all:
        ids = []
        for value in body.ids:
            try:
                ids.append(str(uuid.UUID(value)))
            except ValueError:
                continue
        if not ids:
            return {"ok": True}
        query = query.in_("id", ids)
    _run(query, "lecture des notifications")
    return {"ok": True}


@router.get(API + "/activity")
def activity_feed(request: Request, limit: int = Query(80, ge=1, le=300)):
    client = _db()
    me = _me(request, client)
    visible = {p["id"] for p in _visible_projects(client, me)}
    rows = _run(client.table(T_ACTIVITY).select("*").order("created_at", desc=True).limit(limit * 2), "lecture du fil d'activité")
    items = [_activity_out(r) for r in rows if not r.get("project_id") or str(r["project_id"]) in visible]
    if me.role != "responsable":
        # Les changements de membres ne regardent que les responsables.
        items = [a for a in items if not a["action"].startswith("member_")]
    return {"activity": items[:limit]}


# ═══════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════

def _csv_cell(value: Any) -> str:
    """Cellule CSV inoffensive : une formule (=, +, -, @) n'est jamais exécutée par le tableur."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get(API + "/export.csv")
def export_csv(request: Request, archived: bool = False):
    client = _db()
    me = _me(request, client)
    projects = {p["id"]: p for p in _visible_projects(client, me)}
    cycles = {str(r.get("id")): r.get("name") for r in _run(client.table(T_CYCLES).select("id,name").limit(300), "lecture des cycles")}
    rows = dbmod.fetch_all(lambda: client.table(T_TASKS).select("*").eq("archived", archived).order("number").order("id"),
                           "lecture des tâches", cap=_TASK_LIMIT)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Numéro", "Projet", "Titre", "Statut", "Priorité", "Responsable", "Début", "Échéance",
                     "Étiquettes", "Estimation (h)", "Temps passé (h)", "Cycle", "Créée le", "Terminée le"])
    for r in rows:
        if str(r.get("project_id")) not in projects:
            continue
        t = _task_out(r)
        writer.writerow([_csv_cell(v) for v in (
            t["number"], projects[t["project_id"]]["name"], t["title"], _STATUS_LABEL[t["status"]], t["priority"],
            t["assignee"] or "", t["start_date"] or "", t["due_date"] or "", ", ".join(t["labels"]),
            t["estimate_hours"] if t["estimate_hours"] is not None else "", t["spent_hours"],
            cycles.get(t["cycle_id"] or "", ""), str(t["created_at"] or "")[:10], str(t["completed_at"] or "")[:10],
        )])
    # BOM : Excel ouvre alors les accents correctement.
    return Response(
        content="﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="handypay-taches-{_today().isoformat()}.csv"'},
    )


# ═══════════════════════════════════════════════════════════════════
# Modèles de projets
# ═══════════════════════════════════════════════════════════════════

# Chaque tâche : (titre, étiquette, jour d'échéance depuis le départ, priorité,
# estimation en heures, description, récurrence). Les modèles décrivent une
# méthode ; les seuls chiffres sont ceux de l'objectif de l'équipe (100 cartes
# par jour, 3 000 au total, jalons hebdomadaires, 300 conversations par jour).
# Résultat clé : (titre, unité, départ, cible[, mise à jour automatique]).
TEMPLATES: dict[str, dict] = {
    "campagne_3000": {
        "name": "Campagne 3 000 cartes",
        "icon": "💳",
        "color": "#0B6FA4",
        "summary": "100 cartes par jour pendant 30 jours : file WhatsApp vidée, relances, jalons chaque semaine, blocages traités.",
        "days": 29,
        "objective": {
            "title": "3 000 cartes vendues en 30 jours",
            "period": "30 jours",
            "key_results": [
                ("Cartes vendues (cumul)", "cartes", 0, 3000, "ventes_cumul"),
                ("Marge cumulée", "USD", 0, 30000, "marge_cumul"),
                ("Conversations WhatsApp par jour", "conversations", 0, 300),
            ],
        },
        "tasks": [
            ("Importer les clients à relancer dans « Relances »", "relances", 0, "urgente", 2,
             "CSV : nom, téléphone, étape, source, note. Les doublons (même numéro) sont écartés ; le rapport d'import dit combien.", ""),
            ("Relire et ajuster les 5 modèles de message", "relances", 0, "haute", 1,
             "Court, naturel, une seule question. Variables {prenom} et {nom}.", ""),
            ("Vider la file WhatsApp (plus de 2 000 non lus)", "whatsapp", 3, "urgente", 12,
             "Répartir les conversations non lues ; réponse le jour même, zéro message non lu le soir.", ""),
            ("Point de 9 h : objectif du jour et file de chacun", "rituel", 0, "normale", 0.5,
             "100 cartes, 300 conversations : qui prend quoi aujourd'hui.", "quotidienne"),
            ("Tableau du soir : blocages, bugs, non-lus", "rituel", 0, "normale", 0.5,
             "Chacun remplit son formulaire du soir : les 3 blocages entendus, les bugs ouverts, une note.", "quotidienne"),
            ("Jalon : 700 cartes", "jalon", 6, "haute", 0, "Cumul attendu à la fin de la première semaine.", ""),
            ("Jalon : 1 400 cartes", "jalon", 13, "haute", 0, "Cumul attendu à la fin de la deuxième semaine.", ""),
            ("Jalon : 2 100 cartes", "jalon", 20, "haute", 0, "Cumul attendu à la fin de la troisième semaine.", ""),
            ("Jalon : 3 000 cartes", "jalon", 29, "urgente", 0, "Objectif de la campagne.", ""),
            ("Regrouper les blocages entendus (KYC, paiement, carte)", "produit", 5, "haute", 3,
             "Les blocages du soir, regroupés : ce qui empêche un inscrit d'aller jusqu'à la carte.", ""),
            ("Corriger le blocage n° 1 du parcours d'achat", "produit", 10, "haute", 8,
             "D'après les blocages du soir et les bugs ouverts.", ""),
            ("Envoyer les ventes HandyPay vers l'espace (porte de l'IA)", "technique", 3, "haute", 4,
             "POST /api/workspace/ia/ventes avec un external_id unique : chaque carte payée compte toute seule, sans doublon.", ""),
            ("Choisir l'offre de rachat (deuxième carte)", "ventes", 7, "haute", 2,
             "Ce qu'on propose aux clients dont la carte a déjà servi.", ""),
            ("Revue de la semaine : cartes, réponses, closes", "rituel", 6, "normale", 1,
             "Où en est-on face au jalon ; ce qu'on garde, ce qu'on change.", "hebdomadaire"),
        ],
        "decisions": [
            "Choisir l'offre de rachat (deuxième carte)",
            "Relire et ajuster les 5 modèles de message",
        ],
        "ai": [
            "Envoyer les ventes HandyPay vers l'espace (porte de l'IA)",
            "Corriger le blocage n° 1 du parcours d'achat",
        ],
    },
    "relance": {
        "name": "Campagne de relance WhatsApp",
        "icon": "💬",
        "color": "#1E9E57",
        "summary": "Une étape du parcours ciblée, un modèle de message, J+1, J+3, J+7, puis le bilan.",
        "days": 10,
        "tasks": [
            ("Choisir l'étape ciblée et le message", "brief", 0, "haute", 1,
             "Ex. inscrits sans KYC, achats commencés, cartes jamais utilisées. Un seul message par campagne.", ""),
            ("Importer les fiches et les répartir", "relances", 0, "haute", 1, "Import CSV, puis « Répartir » entre les personnes choisies.", ""),
            ("Premier message à toutes les fiches", "relances", 1, "urgente", 4, "Bouton « Ouvrir WhatsApp » sur chaque fiche de sa file.", ""),
            ("Relance J+1", "relances", 2, "haute", 2, "", ""),
            ("Relance J+3", "relances", 4, "haute", 2, "", ""),
            ("Relance J+7", "relances", 8, "normale", 2, "Après 3 tentatives sans réponse, la fiche passe en « froid ».", ""),
            ("Bilan : réponses, closes, ce qu'on garde", "mesure", 10, "normale", 1, "Tableau du soir et document « décision ».", ""),
        ],
    },
    "sprint": {
        "name": "Cycle de développement (2 semaines)",
        "icon": "🚀",
        "color": "#6B46C1",
        "summary": "Un cycle de deux semaines avec sa planification, sa démo et sa rétrospective.",
        "days": 14,
        "cycle": True,
        "tasks": [
            ("Planifier le cycle : but, tâches, responsables", "rituel", 0, "haute", 2, "Choisir un seul but pour le cycle et le noter dans le cycle.", ""),
            ("Point du milieu de cycle", "rituel", 7, "normale", 1, "Ce qui avance, ce qui bloque, ce qu'on retire.", ""),
            ("Tests complets avant la démo", "qualité", 12, "haute", 2, "Paiement, émission de carte, achat en ligne : sur un vrai téléphone.", ""),
            ("Démo de fin de cycle", "rituel", 13, "normale", 1, "Montrer ce qui marche vraiment.", ""),
            ("Rétrospective", "rituel", 14, "normale", 1, "Garder, arrêter, essayer. Une action concrète pour le prochain cycle.", ""),
        ],
    },
    "release": {
        "name": "Mise en production d'une version",
        "icon": "📦",
        "color": "#2F855A",
        "summary": "Tests, vérifications en production (inscription, KYC, paiement, carte), annonce aux clients.",
        "days": 5,
        "tasks": [
            ("Tests automatiques au vert", "qualité", 1, "haute", 1, "", ""),
            ("Note de version : ce qui change pour le client", "doc", 1, "normale", 1, "", ""),
            ("Mise en production", "prod", 2, "haute", 1, "", ""),
            ("Vérifier en production : inscription et KYC", "vérification", 2, "urgente", 1, "", ""),
            ("Vérifier en production : paiement et émission de carte", "vérification", 2, "urgente", 1, "", ""),
            ("Vérifier en production : achat en ligne avec la carte", "vérification", 3, "urgente", 1, "", ""),
            ("Annoncer la nouveauté aux clients (statut WhatsApp)", "communication", 5, "normale", 1, "", ""),
        ],
    },
    "rituels": {
        "name": "Rituels d'équipe",
        "icon": "🔁",
        "color": "#B7791F",
        "summary": "Point du lundi, revue des objectifs, revue sécurité, rétrospective : tâches récurrentes.",
        "days": 30,
        "tasks": [
            ("Point d'équipe du lundi", "rituel", 0, "normale", 1, "Chacun : fait, à faire, bloqué. 20 minutes.", "hebdomadaire"),
            ("Revue des objectifs : points d'étape", "rituel", 4, "normale", 1, "Mettre à jour chaque résultat clé qui n'est pas automatique.", "hebdomadaire"),
            ("Revue sécurité : comptes, jetons, accès", "sécurité", 15, "haute", 1, "Comptes inactifs à désactiver, jetons à faire tourner.", "mensuelle"),
            ("Rétrospective du mois", "rituel", 29, "normale", 1, "", "mensuelle"),
        ],
    },
}


class TemplateApplyIn(BaseModel):
    start_date: Optional[date] = None
    name: str = Field(default="", max_length=80)
    assignee: Optional[str] = Field(default=None, max_length=80)
    # Qui valide les décisions stratégiques du modèle ; l'IA prend le code.
    decider: Optional[str] = Field(default=None, max_length=80)
    ai: bool = True


@router.post(API + "/templates/{key}/apply", status_code=201)
def apply_template(key: str, body: TemplateApplyIn, request: Request):
    """Crée un projet tout prêt (tâches datées, objectif ou cycle) depuis un modèle."""
    template = TEMPLATES.get(key)
    if template is None:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")
    client = _db()
    me = _me(request, client)
    _require(me, "membre")
    team = _team(client)
    names = {m["username"] for m in team}
    if body.assignee and body.assignee not in names:
        raise HTTPException(status_code=400, detail="Responsable inconnu dans l'équipe.")
    if body.decider and (body.decider not in names or body.decider == AI_USER):
        raise HTTPException(status_code=400, detail="La personne qui valide les décisions doit être un membre de l'équipe.")
    decisions = set(template.get("decisions") or [])
    ai_tasks = set(template.get("ai") or []) if body.ai else set()
    start = body.start_date or _today()
    end = start + timedelta(days=template["days"])

    objective_id = None
    if template.get("objective"):
        spec = template["objective"]
        objective = {
            "id": str(uuid.uuid4()), "title": spec["title"], "period": spec["period"], "description": "",
            "owner_username": body.assignee or me.username, "status": "en_cours",
            "start_date": start.isoformat(), "due_date": end.isoformat(), "created_by": me.username,
        }
        _run(client.table(T_OBJECTIVES).insert(objective), "création de l'objectif")
        objective_id = objective["id"]
        krs = [
            {"id": str(uuid.uuid4()), "objective_id": objective_id, "title": kr[0], "unit": kr[1],
             "start_value": kr[2], "current_value": kr[2], "target_value": kr[3], "owner_username": None,
             "position": i, "auto": kr[4] if len(kr) > 4 else ""}
            for i, kr in enumerate(spec["key_results"])
        ]
        _run(client.table(T_KRS).insert(krs), "création des résultats clés")
        if any(k["auto"] for k in krs):
            from app import sales

            sales.refresh_auto_krs(client)

    cycle_id = None
    if template.get("cycle"):
        cycle = {
            "id": str(uuid.uuid4()), "name": body.name.strip() or f"Cycle du {start.strftime('%d/%m')}",
            "goal": "", "start_date": start.isoformat(), "end_date": end.isoformat(), "created_by": me.username,
        }
        _run(client.table(T_CYCLES).insert(cycle), "création du cycle")
        cycle_id = cycle["id"]

    project = {
        "id": str(uuid.uuid4()), "name": (body.name.strip() or template["name"])[:80], "description": template["summary"],
        "color": template["color"], "icon": template["icon"], "status": "actif",
        "owner_username": body.assignee or me.username, "visibility": "equipe", "members": [],
        "start_date": start.isoformat(), "due_date": end.isoformat(), "objective_id": objective_id,
        "created_by": me.username,
    }
    _run(client.table(T_PROJECTS).insert(project), "création du projet")
    base = time.time()
    rows = []
    for i, (title, label, offset, priority, hours, description, recurrence) in enumerate(template["tasks"]):
        labels, assignee = [label], body.assignee or None
        if title in decisions:
            labels, assignee = [label, DECISION_LABEL], body.decider or assignee
        elif title in ai_tasks:
            assignee = AI_USER
        rows.append({
            "id": str(uuid.uuid4()), "project_id": project["id"], "title": title, "description": description,
            "status": "a_faire", "priority": priority, "assignee": assignee,
            "due_date": (start + timedelta(days=offset)).isoformat(), "labels": clean_labels(labels),
            "estimate_hours": float(hours), "spent_hours": 0, "checklist": [], "links": [], "blocked_by": [],
            "watchers": sorted({me.username} | ({assignee} if assignee else set())),
            "recurrence": recurrence, "cycle_id": cycle_id, "position": base + i, "created_by": me.username,
        })
    _run(client.table(T_TASKS).insert(rows), "création des tâches du modèle")
    _log(client, me.username, "template_applied", project_id=project["id"], detail={"template": key, "tasks": len(rows)})
    return {"project": _project_out(project), "tasks": len(rows), "objective_id": objective_id, "cycle_id": cycle_id}
