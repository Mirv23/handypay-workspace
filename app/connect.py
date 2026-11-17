"""Espace connecté : GitHub et l'IA écrivent dans l'espace d'équipe HandyPay.

DEUX PORTES, CHACUNE AVEC SON SECRET. Ni GitHub ni l'IA n'ont de session : leurs
routes vivent hors de `/api/ws` (sous `/api/workspace/`) et s'authentifient
elles-mêmes, fail-closed (cf. `is_external_path`, appelé par le middleware de
`main.py`) :
- GitHub signe chaque livraison (HMAC SHA-256, `WORKSPACE_GITHUB_SECRET`) :
  signature absente ou fausse → 401 ; secret non configuré → 503 ;
- l'IA présente `Authorization: Bearer <WORKSPACE_IA_TOKEN>` (32 caractères
  au moins), comparé en temps constant : jeton faux → 401, absent → 503.

LA CLÉ « HP-12 ». Écrite dans un commit, une branche (`ia/hp-12-…`) ou une PR,
elle relie ce travail à la tâche n° 12. Ensuite, la tâche avance seule :
- un premier commit lié fait passer « À faire » à « En cours » ;
- une PR ouverte fait passer « À faire » ou « En cours » à « En revue » ;
- une PR fusionnée dans la branche principale passe la tâche à « Fait » si
  elle la ferme (« ferme HP-12 », « fixes HP-12 », ou une branche nommée pour
  elle) ; un commit poussé sur la branche principale avec un mot de fermeture
  aussi.
Chaque étape entre dans l'historique de la tâche ; rien n'est effacé.

CE QUE L'IA PEUT FAIRE. Lire sa file du matin, créer ses tâches, travailler
sur celles qu'on lui confie (statut, grands points cochés, commentaires,
journal). Sur la tâche d'un autre, elle peut seulement répondre par un
commentaire. Elle ne voit un projet restreint que si on l'y a ajoutée, et ne
gère ni membres ni projets. Son journal technique est réservé aux
responsables ; l'équipe voit les grands points.

VENTES ET FICHES VENUES DE HANDYPAY. L'agent branché sur HandyPay envoie :
- chaque vente (`POST /api/workspace/ia/ventes`, `external_id` OBLIGATOIRE) :
  origine `handypay`, idempotent : rejouer le même `external_id` ne compte
  jamais deux fois ;
- des clients à relancer (`POST /api/workspace/ia/fiches`) : dédoublonnés par
  numéro ; une fiche existante voit seulement son étape avancer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import security
from app import workspace as ws
from app.config import get_settings

logger = logging.getLogger("handypay.connect")
router = APIRouter()

EXTERNAL_PREFIX = "/api/workspace"
WEBHOOK_PATH = EXTERNAL_PREFIX + "/github"
IA_PREFIX = EXTERNAL_PREFIX + "/ia"
AI_PROJECT_NAME = "Chantier de l'IA"

_MIN_TOKEN = 32
_MAX_COMMITS = 30
_MAX_TASKS_PER_EVENT = 20
_MAX_POINTS = 60

# « HP-12 » isolé (pas « XHP-12 », pas « HP-123 » lu comme « HP-12 »).
KEY_RE = re.compile(r"(?<![A-Za-z0-9])HP-(\d{1,7})(?!\d)", re.I)
# Mots qui FERMENT une tâche, en français et en anglais (comme sur GitHub).
CLOSE_RE = re.compile(
    r"\b(?:ferme|fermes|fermer|close|closes|closed|fix|fixes|fixed|r[ée]sout|r[ée]solu|resolve|resolves|resolved|termine)"
    r"\s*:?\s*HP-(\d{1,7})(?!\d)",
    re.I,
)
# Travail de l'IA : la signature de Claude dans le commit, une branche à son
# nom, ou un compte de robot à son nom.
_AI_TRAILER = re.compile(r"^co-authored-by:\s*claude\b", re.I | re.M)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_PR_ACTIONS = {"opened", "reopened", "ready_for_review", "converted_to_draft", "edited", "synchronize", "closed"}

Status = Literal["a_faire", "en_cours", "en_revue", "bloque", "fait"]
Priority = Literal["urgente", "haute", "normale", "basse"]


def is_external_path(path: str) -> bool:
    """Routes qui s'authentifient elles-mêmes (webhook signé, jeton de l'IA)."""
    return path == WEBHOOK_PATH or path == IA_PREFIX or path.startswith(IA_PREFIX + "/")


def _secret() -> str:
    return (os.environ.get("WORKSPACE_GITHUB_SECRET") or "").strip()


def _ia_token() -> str:
    token = (os.environ.get("WORKSPACE_IA_TOKEN") or "").strip()
    # Un jeton trop court se devine : autant considérer l'IA comme non branchée.
    return token if len(token) >= _MIN_TOKEN else ""


def _api_url() -> str:
    return (os.environ.get("WORKSPACE_API_URL") or get_settings().public_url).strip().rstrip("/")


# ═══════════════════════════════════════════════════════════════════
# Lecture des clés et des signatures
# ═══════════════════════════════════════════════════════════════════

def task_numbers(text: str) -> list[int]:
    """Numéros de tâches cités (« HP-12 »), dans l'ordre, sans doublon."""
    seen: list[int] = []
    for m in KEY_RE.finditer(text or ""):
        n = int(m.group(1))
        if n > 0 and n not in seen:
            seen.append(n)
    return seen


def closing_numbers(text: str) -> set[int]:
    return {int(n) for n in CLOSE_RE.findall(text or "") if int(n) > 0}


def is_ai_work(message: str, branch: str, login: str) -> bool:
    return (
        bool(_AI_TRAILER.search(message or ""))
        or (branch or "").lower().startswith(("ia/", "claude/"))
        or "claude" in (login or "").lower()
    )


def branch_name(number: int, title: str, ai: bool = False) -> str:
    """Nom de branche conseillé : « hp-12-corriger-le-kyc » (préfixe « ia/ » pour l'IA)."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    slug = ""
    for word in re.findall(r"[a-z0-9]+", plain):
        # Mots entiers seulement : « paiement », jamais « paiem ».
        if len(slug) + (1 if slug else 0) + len(word) > 40:
            break
        slug += ("-" if slug else "") + word
    return ("ia/" if ai else "") + f"hp-{int(number)}" + (f"-{slug}" if slug else "")


def signature_ok(secret: str, raw: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest((header or "").encode("utf-8", "replace"), expected.encode("ascii"))


def _gh_url(value: Any) -> str:
    url = str(value or "")
    return url[:500] if url.startswith("https://github.com/") else ""


# ═══════════════════════════════════════════════════════════════════
# Écritures liées à GitHub
# ═══════════════════════════════════════════════════════════════════

def _tasks_by_number(client, numbers: list[int]) -> dict[int, dict]:
    if not numbers:
        return {}
    rows = ws._run(client.table(ws.T_TASKS).select("*").in_("number", numbers).limit(len(numbers)), "lecture des tâches liées")
    return {int(r["number"]): r for r in rows if r.get("number") is not None and not r.get("archived")}


def _upsert_link(client, task_id: str, kind: str, ref: str, fields: dict) -> bool:
    """Crée ou met à jour le lien (tâche, commit|PR). Renvoie True s'il est nouveau."""
    rows = ws._run(
        client.table(ws.T_GH_LINKS).select("id").eq("task_id", task_id).eq("kind", kind).eq("ref", ref).limit(1),
        "lecture des liens GitHub",
    )
    if rows:
        ws._run(client.table(ws.T_GH_LINKS).update(fields).eq("id", rows[0]["id"]), "mise à jour du lien GitHub")
        return False
    ws._run(client.table(ws.T_GH_LINKS).insert(dict(fields, task_id=task_id, kind=kind, ref=ref)), "lien GitHub")
    return True


def _refresh_summary(client, task_id: str) -> dict:
    """Recalcule le résumé affiché sur la carte : commits, PR qui compte, son état."""
    links = ws._run(client.table(ws.T_GH_LINKS).select("*").eq("task_id", task_id).limit(500), "lecture des liens GitHub")
    prs = sorted(
        (x for x in links if x.get("kind") == "pr" and str(x.get("ref") or "").isdigit()),
        key=lambda x: int(x["ref"]), reverse=True,
    )
    # La PR qui compte : une PR encore ouverte d'abord, sinon la plus récente.
    live = next((x for x in prs if x.get("state") in ("ouverte", "brouillon")), prs[0] if prs else None)
    summary = {
        "commits": sum(1 for x in links if x.get("kind") == "commit"),
        "pr": int(live["ref"]) if live else None,
        "state": str(live.get("state") or "") if live else "",
        "url": _gh_url(live.get("url")) if live else "",
    }
    ws._run(client.table(ws.T_TASKS).update({"github": summary}).eq("id", task_id), "résumé GitHub de la tâche")
    return summary


def _actor(team: list[dict], login: str, by_ai: bool) -> str:
    """Au nom de qui écrire : l'IA, le membre qui a ce pseudo GitHub, sinon « github »."""
    if by_ai:
        return ws.AI_USER
    login = (login or "").casefold()
    if login:
        for m in team:
            if (m.get("github_login") or "").casefold() == login:
                return m["username"]
    return "github"


def _move(client, team: list[dict], actor: str, row: dict, status: str) -> None:
    """Change le statut comme le ferait une personne : historique et notifications compris."""
    if status == "fait" and ws.DECISION_LABEL in ws._strs(row.get("labels")):
        # Une décision ne se ferme jamais toute seule : seule la personne qui tranche la valide, dans
        # l'espace. (Le rôle « responsable » prêté à GitHub ci-dessous la laissait fermer une décision
        # sans personne désignée, ou celle de l'auteur du commit.)
        logger.info("Décision HP-%s : GitHub ne la ferme pas", row.get("number"))
        return
    me = ws.Me(actor, "responsable", "github")
    try:
        ws._apply_patch(client, me, team, str(row["id"]), {"status": status})
    except HTTPException as exc:
        logger.warning("Statut de HP-%s non modifié (%s)", row.get("number"), exc.status_code)


def handle_push(client, payload: dict) -> dict:
    if payload.get("deleted"):
        return {"linked": 0, "note": "branche supprimée"}
    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/"):
        return {"linked": 0, "note": "étiquette ignorée"}
    branch = ref[len("refs/heads/"):][:200]
    default = str((payload.get("repository") or {}).get("default_branch") or "main")
    on_default = branch == default
    branch_keys = task_numbers(branch)
    per_task: dict[int, list[dict]] = {}
    closing: set[int] = set()
    for c in [c for c in (payload.get("commits") or []) if isinstance(c, dict)][:_MAX_COMMITS]:
        message = str(c.get("message") or "")[:5000]
        for n in task_numbers(message) + branch_keys:
            per_task.setdefault(n, [])
            if c not in per_task[n]:
                per_task[n].append(c)
        if on_default:
            closing |= closing_numbers(message)
    numbers = list(per_task)[:_MAX_TASKS_PER_EVENT]
    rows = _tasks_by_number(client, numbers)
    team = ws._team(client) if rows else []
    linked = 0
    for n in numbers:
        row = rows.get(n)
        if not row:
            continue
        task_id, new, ai, login = str(row["id"]), 0, False, ""
        for c in per_task[n]:
            sha = str(c.get("id") or "").lower()[:40]
            if not _SHA_RE.match(sha):
                continue
            message = str(c.get("message") or "")
            author = c.get("author") if isinstance(c.get("author"), dict) else {}
            login = str(author.get("username") or "")[:80]
            by_ai = is_ai_work(message, branch, login)
            ai = ai or by_ai
            new += _upsert_link(client, task_id, "commit", sha, {
                "title": message.split("\n", 1)[0].strip()[:200], "url": _gh_url(c.get("url")),
                "author": (login or str(author.get("name") or ""))[:80], "by_ai": by_ai,
            })
        if not new and n not in closing:
            continue
        linked += 1
        _refresh_summary(client, task_id)
        actor = _actor(team, login, ai)
        if new:
            ws._log(client, actor, "github_commits", project_id=row.get("project_id"), task_id=task_id,
                    detail={"count": new, "branch": branch[:80]})
        if n in closing and row.get("status") != "fait":
            _move(client, team, actor, row, "fait")
        elif new and row.get("status") == "a_faire":
            _move(client, team, actor, row, "en_cours")
    return {"linked": linked}


def handle_pull_request(client, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    if action not in _PR_ACTIONS:
        return {"linked": 0, "note": f"action {action[:40]} ignorée"}
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    number = pr.get("number")
    if not isinstance(number, int) or number <= 0:
        return {"linked": 0, "note": "PR sans numéro"}
    title = str(pr.get("title") or "")[:500]
    body = str(pr.get("body") or "")[:5000]
    branch = str((pr.get("head") or {}).get("ref") or "")[:200]
    base = str((pr.get("base") or {}).get("ref") or "")
    default = str((payload.get("repository") or {}).get("default_branch") or "main")
    login = str((pr.get("user") or {}).get("login") or "")[:80]
    merged = bool(pr.get("merged"))
    state = "fusionnee" if merged else "fermee" if pr.get("state") == "closed" else "brouillon" if pr.get("draft") else "ouverte"
    numbers = list(dict.fromkeys(task_numbers(title) + task_numbers(branch) + task_numbers(body)))[:_MAX_TASKS_PER_EVENT]
    # Une branche nommée pour une tâche (« hp-12-… ») la ferme à la fusion.
    closing = closing_numbers(title + "\n" + body) | set(task_numbers(branch))
    by_ai = is_ai_work(body, branch, login)
    rows = _tasks_by_number(client, numbers)
    team = ws._team(client) if rows else []
    actor = _actor(team, login, by_ai)
    linked = 0
    for n in numbers:
        row = rows.get(n)
        if not row:
            continue
        task_id = str(row["id"])
        new = _upsert_link(client, task_id, "pr", str(number), {
            "title": title.split("\n", 1)[0].strip()[:200], "url": _gh_url(pr.get("html_url")),
            "state": state, "author": login, "by_ai": by_ai,
        })
        linked += 1
        _refresh_summary(client, task_id)
        if new or action in ("closed", "reopened", "ready_for_review", "converted_to_draft"):
            ws._log(client, actor, "github_pr", project_id=row.get("project_id"), task_id=task_id,
                    detail={"pr": number, "state": state, "title": ws._short(title)})
        if merged and base == default and n in closing and row.get("status") != "fait":
            _move(client, team, actor, row, "fait")
        elif state == "ouverte" and row.get("status") in ("a_faire", "en_cours"):
            _move(client, team, actor, row, "en_revue")
    return {"linked": linked}


def handle_delivery(client, event: str, delivery: str, payload: dict) -> dict:
    """Traite une livraison GitHub une seule fois (identifiant X-GitHub-Delivery)."""
    delivery = re.sub(r"[^A-Za-z0-9-]", "", delivery or "")[:80] or str(uuid.uuid4())
    if ws._run(client.table(ws.T_GH_DELIVERIES).select("id").eq("id", delivery).limit(1), "lecture des livraisons GitHub"):
        return {"ok": True, "duplicate": True}
    try:
        client.table(ws.T_GH_DELIVERIES).insert({"id": delivery, "event": event[:40]}).execute()
    except Exception:  # noqa: BLE001 - livraison déjà réservée par un autre passage
        return {"ok": True, "duplicate": True}
    try:
        if event == "ping":
            out = {"linked": 0, "note": "connexion établie"}
        elif event == "push":
            out = handle_push(client, payload)
        elif event == "pull_request":
            out = handle_pull_request(client, payload)
        else:
            out = {"linked": 0, "note": f"événement {event[:40]} ignoré"}
    except HTTPException:
        # Panne en cours de route : la réservation saute, pour que GitHub
        # puisse rejouer la livraison (« Redeliver ») et tout relier.
        try:
            client.table(ws.T_GH_DELIVERIES).delete().eq("id", delivery).execute()
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        client.table(ws.T_GH_DELIVERIES).update({"linked": out["linked"], "note": str(out.get("note") or "")[:200]}).eq("id", delivery).execute()
    except Exception:  # noqa: BLE001 - le bilan est secondaire
        pass
    return {"ok": True, **out}


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def github_webhook(request: Request):
    """Point d'arrivée du webhook GitHub (push, pull_request, ping)."""
    secret = _secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Connexion GitHub non configurée (WORKSPACE_GITHUB_SECRET).")
    raw = await request.body()
    if not signature_ok(secret, raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=401, detail="Signature GitHub invalide.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Charge invalide : choisis « application/json » dans le webhook GitHub.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Charge invalide.")
    repo = ws.github_public()["repo"]
    full_name = str((payload.get("repository") or {}).get("full_name") or "")
    if repo and full_name.casefold() != repo.casefold():
        return {"ok": True, "ignored": "autre dépôt"}
    client = ws._db()
    event = request.headers.get("x-github-event", "")
    return await run_in_threadpool(handle_delivery, client, event, request.headers.get("x-github-delivery", ""), payload)


# ═══════════════════════════════════════════════════════════════════
# La porte de l'IA
# ═══════════════════════════════════════════════════════════════════

def _ia(request: Request) -> ws.Me:
    token = _ia_token()
    if not token:
        raise HTTPException(status_code=503, detail="IA non branchée : WORKSPACE_IA_TOKEN manque sur le serveur (32 caractères au moins).")
    header = request.headers.get("authorization", "")
    given = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Jeton de l'IA invalide.")
    return ws.Me(ws.AI_USER, "membre", "ia")


def _ia_task_or_404(client, me: ws.Me, key: str) -> tuple[dict, dict]:
    m = re.fullmatch(r"(?:HP-?)?(\d{1,7})", (key or "").strip(), flags=re.I)
    if not m:
        raise HTTPException(status_code=404, detail="Tâche introuvable (clé attendue : HP-12).")
    rows = ws._run(client.table(ws.T_TASKS).select("*").eq("number", int(m.group(1))).limit(1), "lecture de la tâche")
    if not rows or rows[0].get("archived"):
        raise HTTPException(status_code=404, detail="Tâche introuvable.")
    return ws._task_or_404(client, me, rows[0]["id"])


def _ia_out(t: dict, project_name: str = "") -> dict:
    return {
        "cle": ws.task_key(t["number"]), "numero": t["number"], "titre": t["title"],
        "statut": t["status"], "priorite": t["priority"], "echeance": t["due_date"],
        "projet": project_name, "pour": t["assignee"], "confiee_par": t["created_by"],
        "points": [{"n": i + 1, "texte": c.get("text", ""), "fait": bool(c.get("done"))} for i, c in enumerate(t["checklist"])],
        "description": t["description"], "github": t["github"],
        "branche": branch_name(t["number"], t["title"], ai=True),
    }


def _journal(client, task_id: str, text: str) -> None:
    text = (text or "").strip()[:2000]
    if text:
        ws._run(client.table(ws.T_AI_JOURNAL).insert({"task_id": task_id, "body": text}), "journal de l'IA")


@router.get(IA_PREFIX + "/matin")
def ia_morning(request: Request):
    """La file de l'IA, dans l'ordre où la traiter, et ses messages non lus."""
    me = _ia(request)
    client = ws._db()
    projects = {p["id"]: p for p in ws._visible_projects(client, me) if p["status"] != "archive"}
    rows = ws._run(client.table(ws.T_TASKS).select("*").eq("assignee", ws.AI_USER).eq("archived", False).limit(500), "lecture des tâches de l'IA")
    today = ws._today().isoformat()
    rank = {"urgente": 0, "haute": 1, "normale": 2, "basse": 3}

    def why(t: dict) -> tuple[int, str]:
        if t["due_date"] and t["due_date"] < today:
            return 0, "en retard"
        if t["due_date"] == today:
            return 1, "pour aujourd'hui"
        if t["status"] in ("en_cours", "en_revue"):
            return 2, "en cours" if t["status"] == "en_cours" else "en revue"
        if t["status"] == "bloque":
            return 4, "bloquée"
        return 3, "à faire"

    queue = []
    for r in rows:
        t = ws._task_out(r)
        if t["status"] == "fait" or t["project_id"] not in projects:
            continue
        order, reason = why(t)
        queue.append(((order, t["due_date"] or "9999", rank.get(t["priority"], 2), t["number"]), dict(_ia_out(t, projects[t["project_id"]]["name"]), raison=reason)))
    queue.sort(key=lambda x: x[0])

    notes = ws._run(
        client.table(ws.T_NOTIFS).select("*").eq("recipient", ws.AI_USER).is_("read_at", "null").order("created_at", desc=True).limit(30),
        "lecture des messages de l'IA",
    )
    notes = [n for n in notes if not n.get("project_id") or str(n["project_id"]) in projects]
    numbers: dict[str, int] = {}
    ids = [str(n["task_id"]) for n in notes if n.get("task_id")]
    if ids:
        for r in ws._run(client.table(ws.T_TASKS).select("id,number").in_("id", ids).limit(len(ids)), "lecture des tâches citées"):
            numbers[str(r["id"])] = int(r.get("number") or 0)
    messages = [
        {"texte": str(n.get("text") or ""), "de": str(n.get("actor") or ""), "le": n.get("created_at"),
         "cle": ws.task_key(numbers[str(n["task_id"])]) if n.get("task_id") and str(n["task_id"]) in numbers else ""}
        for n in notes
    ]
    if notes:
        # Lus dès qu'ils sont remis : la file de demain ne les répète pas.
        ws._run(client.table(ws.T_NOTIFS).update({"read_at": ws._now()}).in_("id", [str(n["id"]) for n in notes]), "lecture des messages")
    return {
        "date": today,
        "file": [item for _key, item in queue],
        "messages": messages,
        "projets": sorted(p["name"] for p in projects.values()),
    }


class IaTaskIn(BaseModel):
    titre: str = Field(min_length=1, max_length=200)
    points: list[str] = Field(default_factory=list, max_length=12)
    projet: str = Field(default="", max_length=80)
    priorite: Priority = "normale"
    echeance: Optional[date] = None
    commencer: bool = True
    journal: str = Field(default="", max_length=2000)


def _ai_project(client, me: ws.Me, ref: str) -> dict:
    """Le projet visé (nom ou identifiant), sinon le « Chantier de l'IA », créé une fois."""
    visible = [p for p in ws._visible_projects(client, me) if p["status"] != "archive"]
    ref = (ref or "").strip()
    if ref:
        found = next((p for p in visible if p["id"] == ref or p["name"].casefold() == ref.casefold()), None)
        if not found:
            raise HTTPException(status_code=404, detail="Projet introuvable pour l'IA : " + ", ".join(sorted(p["name"] for p in visible))[:300])
        return found
    own = next((p for p in visible if p["created_by"] == ws.AI_USER), None)
    if own:
        return own
    row = {
        "id": str(uuid.uuid4()), "name": AI_PROJECT_NAME, "color": "#7C3AED", "icon": "✦",
        "description": "Les tâches que l'IA se donne. Chacun voit les grands points ; le détail technique est réservé aux responsables.",
        "status": "actif", "owner_username": ws.AI_USER, "visibility": "equipe", "members": [], "created_by": ws.AI_USER,
    }
    inserted = ws._run(client.table(ws.T_PROJECTS).insert(row), "création du projet de l'IA")
    project = ws._project_out(inserted[0] if inserted else row)
    ws._log(client, ws.AI_USER, "project_created", project_id=project["id"], detail={"name": project["name"]})
    return project


@router.post(IA_PREFIX + "/taches", status_code=201)
def ia_create_task(body: IaTaskIn, request: Request):
    """L'IA se donne une tâche : visible de toute l'équipe, confiée à l'IA."""
    me = _ia(request)
    client = ws._db()
    project = _ai_project(client, me, body.projet)
    title = " ".join(body.titre.split())
    if not title:
        raise HTTPException(status_code=400, detail="Le titre est obligatoire.")
    row = {
        "id": str(uuid.uuid4()), "project_id": project["id"], "title": title, "description": "",
        "status": "en_cours" if body.commencer else "a_faire", "priority": body.priorite,
        "assignee": ws.AI_USER, "due_date": body.echeance.isoformat() if body.echeance else None,
        "checklist": [{"id": uuid.uuid4().hex[:12], "text": " ".join(p.split())[:200], "done": False} for p in body.points if p.strip()],
        "watchers": [ws.AI_USER], "created_by": ws.AI_USER, "position": time.time(),
    }
    inserted = ws._run(client.table(ws.T_TASKS).insert(row), "création de la tâche de l'IA")
    task = ws._task_out(inserted[0] if inserted else row)
    ws._log(client, ws.AI_USER, "task_created", project_id=project["id"], task_id=task["id"], detail={"title": ws._short(title)})
    _journal(client, task["id"], body.journal)
    return {"tache": _ia_out(task, project["name"])}


@router.get(IA_PREFIX + "/taches/{key}")
def ia_get_task(key: str, request: Request):
    me = _ia(request)
    client = ws._db()
    row, project = _ia_task_or_404(client, me, key)
    task = ws._task_out(row)
    comments = ws._run(client.table(ws.T_COMMENTS).select("*").eq("task_id", task["id"]).order("created_at").limit(50), "lecture des commentaires")
    out = _ia_out(task, project["name"])
    out["commentaires"] = [{"de": str(c.get("author") or ""), "texte": str(c.get("body") or ""), "le": c.get("created_at")} for c in comments]
    return {"tache": out}


class IaUpdate(BaseModel):
    statut: Optional[Status] = None
    cocher: list[int] = Field(default_factory=list, max_length=_MAX_POINTS)
    decocher: list[int] = Field(default_factory=list, max_length=_MAX_POINTS)
    ajouter_points: list[str] = Field(default_factory=list, max_length=12)
    commentaire: str = Field(default="", max_length=2000)
    journal: str = Field(default="", max_length=2000)


@router.patch(IA_PREFIX + "/taches/{key}")
def ia_update_task(key: str, body: IaUpdate, request: Request):
    """L'IA avance sur sa tâche : statut, grands points, commentaire, journal."""
    me = _ia(request)
    client = ws._db()
    row, project = _ia_task_or_404(client, me, key)
    task = ws._task_out(row)
    work = bool(body.statut or body.cocher or body.decocher or body.ajouter_points or body.journal.strip())
    if task["assignee"] != ws.AI_USER and work:
        raise HTTPException(status_code=403, detail="Cette tâche n'est pas confiée à l'IA : elle peut seulement y répondre par un commentaire.")
    team = ws._team(client)
    changes: dict[str, Any] = {}
    newly_done: list[str] = []
    if body.cocher or body.decocher or body.ajouter_points:
        points = [dict(i) for i in task["checklist"]]
        for n in body.cocher:
            if 1 <= n <= len(points) and not points[n - 1].get("done"):
                points[n - 1]["done"] = True
                newly_done.append(str(points[n - 1].get("text") or ""))
        for n in body.decocher:
            if 1 <= n <= len(points):
                points[n - 1]["done"] = False
        for text in body.ajouter_points:
            if text.strip() and len(points) < _MAX_POINTS:
                points.append({"id": uuid.uuid4().hex[:12], "text": " ".join(text.split())[:200], "done": False})
        changes["checklist"] = points
    if body.statut:
        changes["status"] = body.statut
    if changes:
        task = ws._apply_patch(client, me, team, task["id"], changes)["task"]
    for text in newly_done:
        # L'historique garde chaque grand point coché, avec sa date.
        ws._log(client, ws.AI_USER, "point_done", project_id=project["id"], task_id=task["id"], detail={"text": ws._short(text)})
    _journal(client, task["id"], body.journal)
    if body.commentaire.strip():
        ws.add_comment_as(client, me, team, row, project, body.commentaire)
    return {"tache": _ia_out(task, project["name"])}


# ═══════════════════════════════════════════════════════════════════
# État des connexions (page de l'espace, session de l'espace)
# ═══════════════════════════════════════════════════════════════════

@router.get(ws.API + "/connexions")
def connections(request: Request):
    """GitHub et l'IA sont-ils branchés ? Les adresses à configurer : responsables seulement."""
    client = ws._db()
    me = ws._me(request, client)
    gh = ws.github_public()
    last = ws._optional(client.table(ws.T_GH_DELIVERIES).select("*").order("received_at", desc=True).limit(1))
    visible = {p["id"] for p in ws._visible_projects(client, me)}
    rows = ws._run(client.table(ws.T_TASKS).select("*").eq("assignee", ws.AI_USER).eq("archived", False).limit(1000), "lecture des tâches de l'IA")
    tasks = [ws._task_out(r) for r in rows if str(r.get("project_id")) in visible]
    week = (ws._today() - timedelta(days=7)).isoformat()
    acted = ws._optional(client.table(ws.T_ACTIVITY).select("*").eq("actor", ws.AI_USER).order("created_at", desc=True).limit(1))
    out = {
        "github": {
            "connected": gh["connected"], "repo": gh["repo"],
            "last": {k: last[0].get(k) for k in ("event", "received_at", "linked", "note")} if last else None,
        },
        "ia": {
            "connected": bool(_ia_token()),
            "open": sum(1 for t in tasks if t["status"] != "fait"),
            "done_week": sum(1 for t in tasks if t["status"] == "fait" and str(t["completed_at"] or "")[:10] >= week),
            "last_action": acted[0].get("created_at") if acted else None,
        },
    }
    if me.role == "responsable":
        out["github"]["webhook_url"] = _api_url() + WEBHOOK_PATH
        out["ia"]["api_url"] = _api_url()
    return out


# ═══════════════════════════════════════════════════════════════════
# Ventes et fiches venues de HandyPay (porte de l'IA)
# ═══════════════════════════════════════════════════════════════════

class IaSaleIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=120)
    cartes: int = Field(default=1, ge=1, le=500)
    membre: str = Field(default="", max_length=40)
    jour: Optional[date] = None
    client_nom: str = Field(default="", max_length=120)
    client_tel: str = Field(default="", max_length=30)
    source: Literal["whatsapp", "appel", "pub", "parrainage", "autre"] = "autre"
    note: str = Field(default="", max_length=500)


def _ia_sale_out(s: dict) -> dict:
    return {"id": s["id"], "external_id": s["external_id"], "cartes": s["cards"], "membre": s["username"],
            "jour": s["day"], "origine": s["origin"], "annulee": s["cancelled"]}


def record_handypay_sale(client, body: IaSaleIn) -> tuple[dict, bool]:
    """Enregistre une vente HandyPay une seule fois par `external_id` → (vente, doublon ?)."""
    from app import sales

    ext = body.external_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9._:\-]{1,120}", ext):
        raise HTTPException(status_code=400, detail="external_id invalide (lettres, chiffres, . _ : -).")
    existing = ws._run(client.table(sales.T_SALES).select("*").eq("external_id", ext).limit(1), "lecture de la vente")
    if existing:
        return sales.sale_out(existing[0]), True
    seller = None
    if body.membre.strip():
        name = body.membre.strip().lower()
        if name not in {m["username"] for m in ws._team(client) if not m.get("is_ai")}:
            raise HTTPException(status_code=400, detail="membre inconnu dans l'équipe (nom d'utilisateur attendu).")
        seller = name
    today = ws._today()
    day = body.jour or today
    if day > today:
        raise HTTPException(status_code=400, detail="jour dans le futur.")
    phone = security.normalize_phone(body.client_tel) if body.client_tel.strip() else ""
    try:
        sale = sales.record_sale(
            client, username=seller, cards=body.cartes, day=day, created_by=ws.AI_USER,
            client_name=body.client_nom, client_phone=phone, source=body.source, note=body.note,
            origin="handypay", external_id=ext,
        )
    except HTTPException:
        # Course entre deux envois identiques : la contrainte d'unicité a joué, on relit.
        again = ws._run(client.table(sales.T_SALES).select("*").eq("external_id", ext).limit(1), "lecture de la vente")
        if again:
            return sales.sale_out(again[0]), True
        raise
    return sale, False


@router.post(IA_PREFIX + "/ventes")
def ia_sale(body: IaSaleIn, request: Request):
    """Une vente venue de HandyPay. Rejouer le même `external_id` ne compte pas deux fois."""
    _ia(request)
    client = ws._db()
    sale, duplicate = record_handypay_sale(client, body)
    return {"vente": _ia_sale_out(sale), "doublon": duplicate}


class IaSalesBatch(BaseModel):
    ventes: list[IaSaleIn] = Field(min_length=1, max_length=200)


@router.post(IA_PREFIX + "/ventes/lot")
def ia_sales_batch(body: IaSalesBatch, request: Request):
    _ia(request)
    client = ws._db()
    out, added, duplicates = [], 0, 0
    for item in body.ventes:
        sale, duplicate = record_handypay_sale(client, item)
        added += 0 if duplicate else 1
        duplicates += 1 if duplicate else 0
        out.append(dict(_ia_sale_out(sale), doublon=duplicate))
    return {"ajoutees": added, "doublons": duplicates, "ventes": out}


@router.get(IA_PREFIX + "/ventes/stats")
def ia_sales_stats(request: Request, jour: str = ""):
    """Les chiffres du jour et de la campagne (mêmes calculs que la page)."""
    from app import sales, timeutil

    _ia(request)
    client = ws._db()
    try:
        day = timeutil.parse_day(jour)
    except ValueError:
        raise HTTPException(status_code=400, detail="jour invalide (AAAA-MM-JJ).")
    stats = sales.compute_stats(client, day)
    return {k: stats[k] for k in ("day", "today", "week", "campaign")}


class IaFiche(BaseModel):
    nom: str = Field(default="", max_length=120)
    telephone: str = Field(min_length=1, max_length=30)
    etape: str = Field(default="", max_length=40)
    source: str = Field(default="handypay", max_length=40)
    note: str = Field(default="", max_length=2000)


class IaFichesIn(BaseModel):
    fiches: list[IaFiche] = Field(min_length=1, max_length=2000)


@router.post(IA_PREFIX + "/fiches")
def ia_followups(body: IaFichesIn, request: Request):
    """Clients à relancer venus de HandyPay : nouveaux numéros ajoutés, étape avancée pour les connus."""
    from app import followups

    _ia(request)
    client = ws._db()
    items = [(i + 1, {"name": f.nom, "phone": f.telephone, "stage": f.etape, "source": f.source, "notes": f.note})
             for i, f in enumerate(body.fiches)]
    report = followups.import_rows(client, items, actor=ws.AI_USER, default_source="handypay", advance_stage=True)
    return {"ajoutees": report["added"], "doublons": report["duplicates"], "etapes_avancees": report["updated"],
            "rejetees": report["rejected"], "lignes_rejetees": report["rejected_lines"]}
