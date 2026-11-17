"""E-mail du matin de l'espace HandyPay : chaque personne reçoit sa journée.

Chaque jour (par défaut tous les jours, la campagne ne s'arrête pas le
week-end), à 7 h heure d'Haïti, l'agent écrit à chaque membre qui a une
adresse : ses ventes d'hier et celles de l'équipe face à l'objectif, sa file de
relances du jour, ses tâches en retard, pour aujourd'hui, à venir. Les
responsables reçoivent en plus l'état de l'équipe.

TOUT EST CALCULÉ. Les faits (ventes, fiches, tâches, dates) viennent de la
base ; aucune phrase n'est inventée par un modèle. La phrase d'ouverture nomme
simplement la priorité du jour.

UN SEUL ENVOI PAR JOUR. Le jour est « réservé » dans `workspace_digest_runs`
(clé primaire = date) avant tout envoi : un redémarrage, deux instances ou un
clic pendant le passage automatique n'envoient jamais deux fois. Sans e-mail
configuré, rien n'est réservé : la tournée part dès que la configuration existe.

POURQUOI L'E-MAIL ET PAS WHATSAPP. WhatsApp n'est pas un canal d'envoi
automatique ici (risque de bannissement du numéro sans l'API officielle) :
`send_digest` est le seul point à doubler le jour où l'API WhatsApp Business
Cloud sera branchée (cf. README).

Réglages (facultatifs) :
- `WORKSPACE_DIGEST_HOUR` : heure locale d'envoi (7) ;
- `WORKSPACE_DIGEST_DAYS` : jours ISO, « 1-7 » (tous les jours) par défaut ;
- `WORKSPACE_DIGEST_INTERVAL_S` : fréquence de vérification (600 s), 0 = arrêt ;
- `WORKSPACE_PUBLIC_URL` : adresse de la page.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from app import timeutil
from app import workspace as ws
from app.config import get_settings

logger = logging.getLogger("handypay.digest")
router = APIRouter()

DEFAULT_HOUR = 7
DEFAULT_INTERVAL_S = 600
_SOON_DAYS = 3
_MAX_LINES = 8
_TEST_COOLDOWN_S = 60
_last_test: dict[str, float] = {}

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
_PRIO = {"urgente": 0, "haute": 1, "normale": 2, "basse": 3}


# ═══════════════════════════════════════════════════════════════════
# Réglages et heure locale
# ═══════════════════════════════════════════════════════════════════

def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if low <= value <= high else default


def parse_days(raw: str) -> set[int]:
    """« 1-5 », « 1,3,5 » ou « 1-7 » → jours ISO (1 = lundi). Mal formé : tous les jours."""
    days: set[int] = set()
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"([1-7])(?:-([1-7]))?", part)
        if not m:
            return set(range(1, 8))
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        days.update(range(min(start, end), max(start, end) + 1))
    return days or set(range(1, 8))


def settings() -> dict:
    return {
        "hour": _int_env("WORKSPACE_DIGEST_HOUR", DEFAULT_HOUR, 0, 23),
        "days": sorted(parse_days(os.environ.get("WORKSPACE_DIGEST_DAYS", "1-7"))),
        "interval_s": _int_env("WORKSPACE_DIGEST_INTERVAL_S", DEFAULT_INTERVAL_S, 0, 86400),
        "url": get_settings().public_url,
        "tz": timeutil.TZ_NAME,
    }


def haiti_now(now_utc: Optional[datetime] = None) -> datetime:
    return timeutil.haiti_now(now_utc)


def is_due(local: datetime, cfg: dict) -> bool:
    return local.isoweekday() in cfg["days"] and local.hour >= cfg["hour"]


# ═══════════════════════════════════════════════════════════════════
# Contenu (calculé, jamais inventé)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Digest:
    username: str
    name: str
    email: str
    late: list = field(default_factory=list)
    today: list = field(default_factory=list)
    soon: list = field(default_factory=list)
    done_yesterday: list = field(default_factory=list)
    team_late: list = field(default_factory=list)  # [(nom, nombre)] pour les responsables
    unassigned: list = field(default_factory=list)
    ai_open: int = 0
    ai_done: list = field(default_factory=list)
    # Ventes et relances
    sold_yesterday: int = 0
    team_sold_yesterday: int = 0
    daily_target: int = 0
    cumul: int = 0
    expected: int = 0
    total_target: int = 0
    team_ranking: list = field(default_factory=list)  # [(nom, cartes)] d'hier, pour les responsables
    queue_due: int = 0
    queue_new: int = 0

    @property
    def should_send(self) -> bool:
        return bool(self.late or self.today or self.soon or self.team_late or self.unassigned
                    or self.queue_due or self.sold_yesterday or self.team_sold_yesterday or self.daily_target)


def _order(t: dict) -> tuple:
    return (t.get("due_date") or "9999", _PRIO.get(t.get("priority"), 2), t.get("number") or 0)


def build_digest(member: dict, tasks: list[dict], projects: dict[str, dict], team: list[dict], today: date,
                 extras: Optional[dict] = None) -> Digest:
    """Ce que la personne doit savoir ce matin, tâches visibles d'elle seulement."""
    username, role = member["username"], member.get("role", "membre")
    visible = [
        t for t in tasks
        if not t.get("archived") and t.get("project_id") in projects
        and ws._sees(username, role, projects[t["project_id"]])
    ]
    iso, soon_end = today.isoformat(), (today + timedelta(days=_SOON_DAYS)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    mine = [t for t in visible if t.get("assignee") == username]
    open_ = [t for t in mine if t.get("status") != "fait"]
    d = Digest(username=username, name=member.get("display_name") or username, email=member.get("email") or "")
    d.late = sorted((t for t in open_ if t.get("due_date") and t["due_date"] < iso), key=_order)
    d.today = sorted((t for t in open_ if t.get("due_date") == iso), key=_order)
    d.soon = sorted((t for t in open_ if t.get("due_date") and iso < t["due_date"] <= soon_end), key=_order)
    d.done_yesterday = [t for t in mine if t.get("status") == "fait" and str(t.get("completed_at") or "")[:10] == yesterday]
    if extras:
        d.sold_yesterday = extras["sold"].get(username, 0)
        d.team_sold_yesterday = extras["team_sold"]
        d.daily_target = extras["daily_target"]
        d.cumul, d.expected, d.total_target = extras["cumul"], extras["expected"], extras["total_target"]
        d.queue_due, d.queue_new = extras["queue"].get(username, (0, 0))
    if role == "responsable":
        names = {m["username"]: m.get("display_name") or m["username"] for m in team}
        counts: dict[str, int] = {}
        for t in visible:
            if t.get("status") != "fait" and t.get("due_date") and t["due_date"] < iso and t.get("assignee") and t["assignee"] != username:
                counts[t["assignee"]] = counts.get(t["assignee"], 0) + 1
        d.team_late = sorted(((names.get(u, u), n) for u, n in counts.items()), key=lambda x: (-x[1], x[0]))
        d.unassigned = sorted(
            (t for t in visible if t.get("status") != "fait" and not t.get("assignee") and t.get("due_date") and t["due_date"] <= iso),
            key=_order,
        )
        ai = [t for t in visible if t.get("assignee") == ws.AI_USER]
        d.ai_open = sum(1 for t in ai if t.get("status") != "fait")
        d.ai_done = [t for t in ai if t.get("status") == "fait" and str(t.get("completed_at") or "")[:10] == yesterday]
        if extras:
            d.team_ranking = [(names.get(u, u), n) for u, n in sorted(extras["sold"].items(), key=lambda x: -x[1]) if n]
    return d


def subject_of(d: Digest) -> str:
    parts = []
    if d.queue_due:
        parts.append(f"{d.queue_due} relance{'s' if d.queue_due > 1 else ''}")
    if d.late:
        parts.append(f"{len(d.late)} tâche{'s' if len(d.late) > 1 else ''} en retard")
    if d.today:
        parts.append(f"{len(d.today)} pour aujourd'hui")
    if not parts and d.daily_target:
        parts.append(f"hier {d.team_sold_yesterday}/{d.daily_target} cartes")
    if not parts:
        parts.append("rien d'urgent")
    return "Ta journée HandyPay : " + ", ".join(parts)


def fallback_advice(d: Digest, today: date) -> str:
    """Phrase d'ouverture : la priorité la plus pressante, nommée."""
    if d.queue_due:
        return f"Commence par ta file de relances : {d.queue_due} client{'s' if d.queue_due > 1 else ''} à contacter aujourd'hui."
    if d.late:
        t = d.late[0]
        days = (today - date.fromisoformat(t["due_date"])).days
        return f"Commence par « {t['title']} » : elle est en retard de {days} jour{'s' if days > 1 else ''}."
    if d.today:
        return f"Ta priorité du jour : « {d.today[0]['title']} »."
    if d.soon:
        return f"Rien d'urgent aujourd'hui : avance « {d.soon[0]['title']} »."
    if d.daily_target:
        return f"Objectif de l'équipe aujourd'hui : {d.daily_target} cartes. Bonne journée !"
    return "Rien d'urgent ce matin. Bonne journée !"


def _n(value: int) -> str:
    """1 400 plutôt que 1400 (espace insécable fine)."""
    return f"{int(value):,}".replace(",", " ")


def _due_label(t: dict, today: date) -> str:
    due = date.fromisoformat(t["due_date"])
    gap = (due - today).days
    if gap < 0:
        return f"en retard de {-gap} j"
    if gap == 0:
        return "aujourd'hui"
    if gap == 1:
        return "demain"
    return f"{JOURS[due.weekday()]} {due.day} {MOIS[due.month - 1]}"


def render_html(d: Digest, advice: str, today: date, projects: dict[str, dict], url: str) -> str:
    """E-mail sobre, styles en ligne, tout texte échappé."""
    e = html.escape
    first = e(d.name.split(" ")[0])

    def section(title: str, items: list, color: str) -> str:
        if not items:
            return ""
        rows = []
        for t in items[:_MAX_LINES]:
            project = projects.get(t.get("project_id"), {}).get("name", "")
            rows.append(
                f'<tr><td style="padding:6px 0;border-bottom:1px solid #eef2f6">'
                f'<a href="{e(url)}/#/t/{int(t.get("number") or 0)}" style="color:#14233A;font-weight:600;text-decoration:none">{e(t["title"])}</a>'
                f'<div style="font-size:12px;color:#55657A">{e(project)}</div></td>'
                f'<td style="padding:6px 0 6px 12px;border-bottom:1px solid #eef2f6;text-align:right;white-space:nowrap;font-size:13px;color:{color};font-weight:600">{e(_due_label(t, today))}</td></tr>'
            )
        more = f'<p style="font-size:12px;color:#55657A;margin:4px 0 0">et {len(items) - _MAX_LINES} autre(s)</p>' if len(items) > _MAX_LINES else ""
        return (f'<h3 style="font-size:14px;margin:20px 0 4px;color:{color}">{e(title)} ({len(items)})</h3>'
                f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(rows)}</table>{more}')

    body = [
        f'<p style="font-size:16px;margin:0 0 6px">Bonjour {first},</p>',
        f'<p style="font-size:15px;margin:0 0 4px;color:#14233A">{e(advice)}</p>',
    ]
    if d.daily_target:
        gap = d.cumul - d.expected
        gap_txt = f"{'+' if gap >= 0 else '−'}{_n(abs(gap))} par rapport au rythme des jalons"
        lines = [
            f"<li>Toi hier : <b>{d.sold_yesterday}</b> carte{'s' if d.sold_yesterday > 1 else ''} closée{'s' if d.sold_yesterday > 1 else ''}</li>",
            f"<li>L'équipe hier : <b>{_n(d.team_sold_yesterday)}</b> / {_n(d.daily_target)}</li>",
            f"<li>Cumul de la campagne : <b>{_n(d.cumul)}</b> / {_n(d.total_target)} ({e(gap_txt)})</li>",
        ]
        if d.team_ranking:
            lines.append("<li>Classement d'hier : " + e(", ".join(f"{n} {c}" for n, c in d.team_ranking[:8])) + "</li>")
        body.append('<h3 style="font-size:14px;margin:20px 0 4px;color:#0B6FA4">Ventes</h3>'
                    f'<ul style="margin:0;padding-left:18px;font-size:14px">{"".join(lines)}</ul>')
    if d.queue_due or d.queue_new:
        new_txt = f", dont {d.queue_new} jamais contactée{'s' if d.queue_new > 1 else ''}" if d.queue_new else ""
        body.append('<h3 style="font-size:14px;margin:20px 0 4px;color:#1E7A45">Relances du jour</h3>'
                    f'<p style="font-size:14px;margin:0">{d.queue_due} fiche{"s" if d.queue_due > 1 else ""} dans ta file{new_txt}. '
                    "Règle d'équipe : réponse le jour même, zéro message non lu le soir.</p>")
    body += [
        section("En retard", d.late, "#B42318"),
        section("Pour aujourd'hui", d.today, "#0B6FA4"),
        section(f"Dans les {_SOON_DAYS} prochains jours", d.soon, "#55657A"),
    ]
    if d.done_yesterday:
        body.append(f'<p style="font-size:14px;margin:20px 0 0;color:#1E7A45">Hier, tu as terminé {len(d.done_yesterday)} tâche{"s" if len(d.done_yesterday) > 1 else ""}. Bravo !</p>')
    if d.team_late or d.unassigned:
        lines_t = "".join(f"<li>{e(name)} : {n} en retard</li>" for name, n in d.team_late[:10])
        if d.unassigned:
            lines_t += f"<li>{len(d.unassigned)} tâche(s) à rendre sans responsable</li>"
        body.append(f'<h3 style="font-size:14px;margin:20px 0 4px;color:#14233A">L\'équipe</h3><ul style="margin:0;padding-left:18px;font-size:14px">{lines_t}</ul>')
    if d.ai_open or d.ai_done:
        done = "".join(f"<li>{e(t['title'])}</li>" for t in d.ai_done[:5])
        tail = f' · terminé hier :</p><ul style="margin:4px 0 0;padding-left:18px;font-size:14px">{done}</ul>' if done else ".</p>"
        body.append(
            '<h3 style="font-size:14px;margin:20px 0 4px;color:#7C3AED">L\'IA</h3>'
            f'<p style="font-size:14px;margin:0">{d.ai_open} tâche{"s" if d.ai_open > 1 else ""} en cours{tail}'
        )
    body.append(
        f'<p style="margin:24px 0"><a href="{e(url)}/#/relances" style="background:#0B6FA4;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">Ouvrir ma journée</a></p>'
        '<p style="font-size:12px;color:#8494A8;margin:0">E-mail automatique de l\'espace d\'équipe HandyPay. '
        'Pour ne plus le recevoir : Équipe → Mon profil → « E-mail du matin ».</p>'
    )
    return ('<div style="font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:8px 4px;color:#14233A">'
            + "".join(body) + "</div>")


# ═══════════════════════════════════════════════════════════════════
# Envoi
# ═══════════════════════════════════════════════════════════════════

def email_ready() -> tuple[bool, list[str]]:
    from app import email_sender

    missing = []
    if not email_sender.sender_address():
        missing.append(email_sender.ENV_FROM)
    if not email_sender.provider_name():
        missing.append(f"{email_sender.ENV_RESEND_KEY} ou {email_sender.ENV_SMTP_HOST}")
    return (not missing), missing


def _transport():
    from app import email_sender

    return email_sender.default_transport()


def send_digest(transport, d: Digest, subject: str, body: str) -> tuple[bool, str]:
    from app import email_sender

    try:
        return transport.send(email_sender.Message(to=d.email, subject=subject, html=body))
    except Exception as exc:  # noqa: BLE001 - un destinataire n'arrête pas les autres
        return False, type(exc).__name__


def _extras(client, team: list[dict], today: date) -> dict:
    """Ventes d'hier par personne, cumul, et la file de relances de chacun ce matin."""
    from app import followups, sales

    yesterday = today - timedelta(days=1)
    stats = sales.compute_stats(client, yesterday, team=team)
    sold = {p["username"]: p["cards"] for p in stats["today"]["by_person"] if p["username"]}
    iso = today.isoformat()
    rows = ws._run(
        client.table(followups.T_FOLLOWUPS).select("assignee,status,next_followup")
        .not_.is_("assignee", "null").not_.in_("status", list(followups.CLOSED))
        .or_(f"status.eq.a_contacter,next_followup.lte.{iso}").limit(1000),
        "lecture des relances",
    )
    queue: dict[str, tuple[int, int]] = {}
    for r in rows:
        due, new = queue.get(str(r.get("assignee")), (0, 0))
        queue[str(r.get("assignee"))] = (due + 1, new + (1 if r.get("status") == "a_contacter" else 0))
    camp = stats["campaign"]
    return {"sold": sold, "team_sold": stats["today"]["cards"], "daily_target": stats["today"]["target"],
            "cumul": camp["cumul"], "expected": camp["expected"], "total_target": camp["target"], "queue": queue}


def _load(client):
    team = ws._team(client, with_email=True)
    members = [dict(m) for m in team]
    projects = {p["id"]: p for p in ws._all_projects(client) if p.get("status") != "archive"}
    tasks = [ws._task_out(r) for r in ws._run(client.table(ws.T_TASKS).select("*").eq("archived", False).limit(ws._TASK_LIMIT), "lecture des tâches")]
    return members, team, projects, tasks


def compose(d: Digest, today: date, projects: dict, cfg: dict) -> tuple[str, str, str]:
    advice = fallback_advice(d, today)
    return subject_of(d), render_html(d, advice, today, projects, cfg["url"]), advice


def run_daily(client=None, now: Optional[datetime] = None, *, force: bool = False, triggered_by: str = "agent",
              transport=None) -> dict:
    """Une tournée. Renvoie un bilan ; ne lève jamais (sauf usage manuel)."""
    cfg = settings()
    client = client or ws._svc()
    if client is None:
        return {"ok": False, "reason": "base"}
    local = haiti_now(now)
    day = local.date()
    if not force and not is_due(local, cfg):
        return {"ok": False, "reason": "pas encore l'heure"}
    if transport is None:
        ready, missing = email_ready()
        if not ready:
            # Rien n'est réservé : la tournée partira dès que l'e-mail sera configuré.
            return {"ok": False, "reason": "e-mail non configuré", "error": "E-mail non configuré : " + ", ".join(missing)}
    already = ws._run(client.table(ws.T_DIGEST_RUNS).select("*").eq("day", day.isoformat()).limit(1), "lecture des envois")
    if already:
        return {"ok": False, "reason": "déjà envoyée aujourd'hui", "run": already[0]}
    try:
        client.table(ws.T_DIGEST_RUNS).insert({"day": day.isoformat(), "triggered_by": triggered_by[:80]}).execute()
    except Exception:  # noqa: BLE001 - une autre instance vient de réserver le jour
        return {"ok": False, "reason": "déjà réservée"}

    summary = {"sent": 0, "skipped": 0, "failed": 0, "error": "", "detail": []}
    try:
        transport = transport or _transport()
        members, team, projects, tasks = _load(client)
        extras = _extras(client, team, day)
        for m in members:
            if m.get("is_ai"):
                # L'IA ne lit pas d'e-mail : sa relance est sa file (GET /api/workspace/ia/matin).
                queue = sum(1 for t in tasks if t.get("assignee") == ws.AI_USER and t.get("status") != "fait")
                summary["detail"].append({"username": m["username"], "status": f"file : {queue} tâche(s)"})
                continue
            if not m.get("email") or not m.get("daily_digest", True):
                summary["skipped"] += 1
                summary["detail"].append({"username": m["username"], "status": "sans e-mail" if not m.get("email") else "désactivé"})
                continue
            d = build_digest(m, tasks, projects, team, day, extras)
            if not d.should_send:
                summary["skipped"] += 1
                summary["detail"].append({"username": m["username"], "status": "rien à signaler"})
                continue
            subject, body, _advice = compose(d, day, projects, cfg)
            ok, code = send_digest(transport, d, subject, body)
            summary["sent" if ok else "failed"] += 1
            summary["detail"].append({"username": m["username"], "status": "envoyé" if ok else f"échec {code}"[:60]})
    except Exception as exc:  # noqa: BLE001 - le bilan dit ce qui a manqué
        summary["error"] = str(exc)[:300] if isinstance(exc, RuntimeError) else type(exc).__name__
        logger.warning("E-mail du matin interrompu (%s)", summary["error"])
    try:
        client.table(ws.T_DIGEST_RUNS).update({**summary, "finished_at": ws._now()}).eq("day", day.isoformat()).execute()
    except Exception:  # noqa: BLE001
        logger.warning("Bilan de l'e-mail du matin non enregistré")
    return {"ok": not summary["error"], "day": day.isoformat(), **summary}


def start_scheduler() -> Optional[threading.Thread]:
    """Fil démon : vérifie toutes les `interval_s` secondes s'il est l'heure."""
    interval = settings()["interval_s"]
    if interval <= 0:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                run_daily()
            except Exception:  # noqa: BLE001 - jamais de chute du serveur
                logger.warning("Tick de l'e-mail du matin en échec", exc_info=False)

    thread = threading.Thread(target=_loop, name="handypay-digest", daemon=True)
    thread.start()
    return thread


# ═══════════════════════════════════════════════════════════════════
# Routes (session de l'espace)
# ═══════════════════════════════════════════════════════════════════

def _my_digest(client, me) -> tuple[Digest, dict, date]:
    members, team, projects, tasks = _load(client)
    member = next((m for m in members if m["username"] == me.username), None)
    if member is None:
        member = {"username": me.username, "role": me.role, "display_name": me.username, "email": ""}
    member = dict(member, role=me.role)
    today = haiti_now().date()
    return build_digest(member, tasks, projects, team, today, _extras(client, team, today)), projects, today


@router.get(ws.API + "/digest/status")
def digest_status(request: Request):
    client = ws._db()
    me = ws._me(request, client)
    cfg = settings()
    ready, missing = email_ready()
    team = [m for m in ws._team(client, with_email=True) if not m.get("is_ai")]
    with_email = [m["username"] for m in team if m.get("email")]
    out = {
        "hour": cfg["hour"], "days": cfg["days"], "enabled": cfg["interval_s"] > 0, "ai": False,
        "email_ready": ready, "missing": missing,
        "recipients": sum(1 for m in team if m.get("email") and m.get("daily_digest", True)),
        "without_email": [m["display_name"] for m in team if m["username"] not in with_email] if me.role == "responsable" else [],
        "runs": [],
    }
    if me.role == "responsable":
        runs = ws._run(client.table(ws.T_DIGEST_RUNS).select("*").order("day", desc=True).limit(7), "lecture des envois")
        out["runs"] = [{k: r.get(k) for k in ("day", "triggered_by", "sent", "skipped", "failed", "error", "finished_at")} for r in runs]
    return out


@router.get(ws.API + "/digest/preview")
def digest_preview(request: Request):
    """L'e-mail que la personne recevrait ce matin."""
    client = ws._db()
    me = ws._me(request, client)
    d, projects, today = _my_digest(client, me)
    subject, body, _ = compose(d, today, projects, settings())
    return {"subject": subject, "html": body, "would_send": d.should_send}


@router.post(ws.API + "/digest/test")
def digest_test(request: Request):
    """S'envoyer son e-mail tout de suite, pour voir ce qu'il donne."""
    client = ws._db()
    me = ws._me(request, client)
    last = _last_test.get(me.username, 0.0)
    if time.monotonic() - last < _TEST_COOLDOWN_S:
        raise HTTPException(status_code=429, detail="Un essai par minute : regarde ta boîte de réception.")
    d, projects, today = _my_digest(client, me)
    if not d.email:
        raise HTTPException(status_code=400, detail="Ajoute d'abord ton adresse e-mail dans Mon profil.")
    ready, missing = email_ready()
    if not ready:
        raise HTTPException(status_code=503, detail="E-mail non configuré sur le serveur : " + ", ".join(missing))
    cfg = settings()
    subject, body, _ = compose(d, today, projects, cfg)
    _last_test[me.username] = time.monotonic()
    ok, code = send_digest(_transport(), d, "[Essai] " + subject, body)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Envoi refusé par le fournisseur ({code}).")
    return {"ok": True, "to": d.email}


@router.post(ws.API + "/digest/run")
def digest_run(request: Request):
    """Lancer la tournée du jour maintenant (responsable), une seule fois par jour."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    result = run_daily(client, force=True, triggered_by=me.username)
    if not result.get("ok") and result.get("reason") in ("déjà envoyée aujourd'hui", "déjà réservée"):
        raise HTTPException(status_code=409, detail="L'e-mail de ce matin est déjà parti.")
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return result
