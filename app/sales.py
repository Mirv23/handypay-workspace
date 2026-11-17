"""Ventes : cartes « closées » par personne, objectif du jour et jalons.

Une vente = une ligne : qui a closé, quel jour (Port-au-Prince), combien de
cartes, pour quel client (facultatif), par quelle source. Origine `manuel`
(bouton « + Carte closée », ou « Closé » sur une fiche de relance) ou
`handypay` (envoyée par l'agent branché sur HandyPay, avec un `external_id`
unique : rejouer le même envoi ne compte jamais deux fois).

Rien n'est effacé : annuler une saisie la marque `cancelled_at` ; les chiffres
ne comptent que les ventes non annulées. Un membre annule sa propre saisie du
jour ; un responsable corrige et annule tout. Un observateur lit.

Les réglages (objectif du jour, objectif total, dates, jalons, marge par carte)
vivent dans `workspace_settings` (clé « sales ») ; un responsable les change.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import date, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import security, timeutil
from app import workspace as ws
from app.db import fetch_all

logger = logging.getLogger("handypay.sales")
router = APIRouter()

T_SALES = "workspace_sales"
SOURCES = ("whatsapp", "appel", "pub", "parrainage", "autre")
SOURCE_LABEL = {"whatsapp": "WhatsApp", "appel": "Appel", "pub": "Publicité", "parrainage": "Parrainage", "autre": "Autre"}
DIRECT = "__direct"  # ventes HandyPay sans vendeur : « Direct (HandyPay) »

DEFAULT_SALES = {
    "daily_target": 100,
    "total_target": 3000,
    "start": "2026-11-16",
    "end": "2026-12-15",
    "margin_per_card": 10,
    "currency": "USD",
    "milestones": [
        {"date": "2026-11-22", "target": 700},
        {"date": "2026-11-29", "target": 1400},
        {"date": "2026-12-06", "target": 2100},
        {"date": "2026-12-15", "target": 3000},
    ],
}


# ═══════════════════════════════════════════════════════════════════
# Réglages (partagés avec les relances)
# ═══════════════════════════════════════════════════════════════════

def get_setting(client, key: str) -> dict:
    rows = ws._run(client.table(ws.T_SETTINGS).select("*").eq("key", key).limit(1), "lecture des réglages")
    value = rows[0].get("value") if rows else None
    return value if isinstance(value, dict) else {}


def put_setting(client, key: str, value: dict, by: str) -> None:
    existing = ws._run(client.table(ws.T_SETTINGS).select("key").eq("key", key).limit(1), "lecture des réglages")
    if existing:
        ws._run(client.table(ws.T_SETTINGS).update({"value": value, "updated_by": by}).eq("key", key), "réglages")
    else:
        ws._run(client.table(ws.T_SETTINGS).insert({"key": key, "value": value, "updated_by": by}), "réglages")


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if low <= v <= high else default


def _day(value: Any, default: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return default


def normalize_sales_settings(raw: dict) -> dict:
    d = DEFAULT_SALES
    start = _day(raw.get("start"), d["start"])
    end = _day(raw.get("end"), d["end"])
    if end < start:
        start, end = d["start"], d["end"]
    try:
        margin = round(float(raw.get("margin_per_card", d["margin_per_card"])), 2)
    except (TypeError, ValueError):
        margin = d["margin_per_card"]
    milestones = []
    for m in raw.get("milestones") if isinstance(raw.get("milestones"), list) else d["milestones"]:
        if not isinstance(m, dict):
            continue
        day = _day(m.get("date"), "")
        target = _int(m.get("target"), -1, 1, 10_000_000)
        if day and target > 0:
            milestones.append({"date": day, "target": target})
    milestones.sort(key=lambda m: m["date"])
    return {
        "daily_target": _int(raw.get("daily_target"), d["daily_target"], 1, 1_000_000),
        "total_target": _int(raw.get("total_target"), d["total_target"], 1, 100_000_000),
        "start": start, "end": end,
        "margin_per_card": margin if 0 <= margin <= 100000 else d["margin_per_card"],
        "currency": str(raw.get("currency") or d["currency"])[:8],
        "milestones": milestones[:20],
    }


def get_sales_settings(client) -> dict:
    return normalize_sales_settings({**DEFAULT_SALES, **get_setting(client, "sales")})


# ═══════════════════════════════════════════════════════════════════
# Lignes et calculs
# ═══════════════════════════════════════════════════════════════════

def sale_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""),
        "username": r.get("username") or None,
        "day": str(r.get("day") or "")[:10],
        "cards": int(r.get("cards") or 0),
        "client_name": str(r.get("client_name") or ""),
        "client_phone": str(r.get("client_phone") or ""),
        "source": r.get("source") if r.get("source") in SOURCES else "autre",
        "followup_id": ws._id_or_none(r.get("followup_id")),
        "note": str(r.get("note") or ""),
        "origin": "handypay" if r.get("origin") == "handypay" else "manuel",
        "external_id": r.get("external_id") or None,
        "created_by": str(r.get("created_by") or ""),
        "cancelled": bool(r.get("cancelled_at")),
        "cancelled_by": str(r.get("cancelled_by") or ""),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def sales_between(client, start: str, end: str) -> list[dict]:
    """Ventes non annulées de `start` à `end` inclus."""
    if end < start:
        return []
    return fetch_all(
        lambda: client.table(T_SALES).select("username,day,cards").gte("day", start).lte("day", end)
        .is_("cancelled_at", "null").order("day").order("id"),
        "lecture des ventes",
    )


def _people(client) -> dict[str, str]:
    return {m["username"]: m["display_name"] for m in ws._team(client) if not m.get("is_ai")}


def _ranking(rows: list[dict], names: dict[str, str], roles: dict[str, str]) -> list[dict]:
    totals: dict[str, int] = {}
    for r in rows:
        key = r.get("username") or DIRECT
        totals[key] = totals.get(key, 0) + int(r.get("cards") or 0)
    # Tout vendeur possible figure au classement, même à zéro (sauf observateurs).
    for name, role in roles.items():
        if role != "observateur":
            totals.setdefault(name, 0)
    out = [{"username": None if k == DIRECT else k,
            "display_name": "Direct (HandyPay)" if k == DIRECT else names.get(k, k),
            "cards": v} for k, v in totals.items()]
    return sorted(out, key=lambda x: (-x["cards"], x["display_name"].casefold()))


def compute_stats(client, day: date, cfg: Optional[dict] = None, team: Optional[list[dict]] = None) -> dict:
    cfg = cfg or get_sales_settings(client)
    team = team if team is not None else ws._team(client)
    names = {m["username"]: m["display_name"] for m in team if not m.get("is_ai")}
    roles = {m["username"]: m["role"] for m in team if not m.get("is_ai")}
    iso = day.isoformat()
    start, end = cfg["start"], cfg["end"]
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    first = min(start, monday.isoformat())
    rows = sales_between(client, first, iso)

    today_rows = [r for r in rows if str(r.get("day"))[:10] == iso]
    week_rows = [r for r in rows if monday.isoformat() <= str(r.get("day"))[:10] <= iso]
    camp_last = min(iso, end)
    camp_rows = [r for r in rows if start <= str(r.get("day"))[:10] <= camp_last]
    today_cards = sum(int(r.get("cards") or 0) for r in today_rows)
    cumul = sum(int(r.get("cards") or 0) for r in camp_rows)

    d_start, d_end = date.fromisoformat(start), date.fromisoformat(end)
    campaign_days = (d_end - d_start).days + 1
    elapsed = max(0, min(campaign_days, (day - d_start).days + 1))
    expected = min(cfg["total_target"], cfg["daily_target"] * elapsed)
    by_day: dict[str, int] = {}
    for r in camp_rows:
        k = str(r.get("day"))[:10]
        by_day[k] = by_day.get(k, 0) + int(r.get("cards") or 0)
    series, running = [], 0
    for i in range(campaign_days):
        k = (d_start + timedelta(days=i)).isoformat()
        past = k <= iso
        running += by_day.get(k, 0) if past else 0
        series.append({"day": k, "cards": by_day.get(k, 0) if past else None, "cumul": running if past else None})

    milestones = []
    for m in cfg["milestones"]:
        at = sum(by_day.get(k, 0) for k in by_day if k <= m["date"])
        milestones.append(dict(m, cumul_at=at if m["date"] <= iso else None,
                               reached=(at >= m["target"]) if m["date"] <= iso else cumul >= m["target"]))
    nxt = next((m for m in milestones if m["date"] >= iso), None)
    next_info = None
    if nxt:
        days_left = (date.fromisoformat(nxt["date"]) - day).days + 1
        remaining = max(0, nxt["target"] - cumul)
        # Ce qui reste à faire chaque jour, AUJOURD'HUI COMPRIS, pour tenir le jalon.
        next_info = dict(nxt, remaining=remaining, days_left=days_left,
                         per_day_needed=math.ceil(remaining / days_left) if days_left > 0 else remaining)
    margin = cfg["margin_per_card"]
    return {
        "day": iso,
        "settings": cfg,
        "today": {
            "cards": today_cards, "target": cfg["daily_target"],
            "pct": round(today_cards / cfg["daily_target"], 4) if cfg["daily_target"] else 0,
            "margin": round(today_cards * margin, 2),
            "by_person": _ranking(today_rows, names, roles),
        },
        "week": {
            "start": monday.isoformat(), "end": sunday.isoformat(),
            "cards": sum(int(r.get("cards") or 0) for r in week_rows),
            "by_person": _ranking(week_rows, names, roles),
        },
        "campaign": {
            "start": start, "end": end, "days": campaign_days, "elapsed": elapsed,
            "cumul": cumul, "target": cfg["total_target"], "expected": expected, "gap": cumul - expected,
            "margin": round(cumul * margin, 2), "currency": cfg["currency"],
            "next_milestone": next_info, "milestones": milestones,
        },
        "series": series,
    }


def refresh_auto_krs(client) -> None:
    """Résultats clés reliés aux ventes : leur valeur suit le cumul de la campagne."""
    try:
        krs = ws._run(client.table(ws.T_KRS).select("*").neq("auto", "").limit(200), "lecture des résultats clés")
        if not krs:
            return
        cfg = get_sales_settings(client)
        last = min(timeutil.today().isoformat(), cfg["end"])
        cumul = sum(int(r.get("cards") or 0) for r in sales_between(client, cfg["start"], last))
        values = {"ventes_cumul": float(cumul), "marge_cumul": round(cumul * cfg["margin_per_card"], 2)}
        for kr in krs:
            value = values.get(kr.get("auto"))
            if value is not None and ws._num(kr.get("current_value")) != value:
                ws._run(client.table(ws.T_KRS).update({"current_value": value}).eq("id", str(kr["id"])), "mise à jour automatique")
    except Exception as exc:  # noqa: BLE001 - secondaire : la vente est déjà enregistrée
        logger.warning("Résultats clés automatiques non mis à jour (%s)", type(exc).__name__)


def record_sale(client, *, username: Optional[str], cards: int, day: date, created_by: str,
                client_name: str = "", client_phone: str = "", source: str = "whatsapp",
                note: str = "", followup_id: Optional[str] = None, origin: str = "manuel",
                external_id: Optional[str] = None) -> dict:
    row = {
        "id": str(uuid.uuid4()), "username": username, "day": day.isoformat(), "cards": int(cards),
        "client_name": " ".join(client_name.split())[:120], "client_phone": client_phone,
        "source": source if source in SOURCES else "autre", "note": note.strip()[:500],
        "followup_id": followup_id, "origin": origin, "external_id": external_id, "created_by": created_by,
    }
    inserted = ws._run(client.table(T_SALES).insert(row), "enregistrement de la vente")
    sale = sale_out(inserted[0] if inserted else row)
    ws._log(client, created_by, "sale_added", detail={"cards": sale["cards"], "username": username or "", "origin": origin})
    refresh_auto_krs(client)
    return sale


def _sale_or_404(client, sale_id: str) -> dict:
    sid = ws._check_uuid(sale_id)
    rows = ws._run(client.table(T_SALES).select("*").eq("id", sid).limit(1), "lecture de la vente")
    if not rows:
        raise HTTPException(status_code=404, detail="Vente introuvable.")
    return rows[0]


def clean_phone_or_400(raw: str) -> str:
    if not str(raw or "").strip():
        return ""
    phone = security.normalize_phone(raw)
    if not phone:
        raise HTTPException(status_code=400, detail="Numéro du client invalide (8 chiffres haïtiens, ou +indicatif).")
    return phone


# ═══════════════════════════════════════════════════════════════════
# Routes (session de l'espace)
# ═══════════════════════════════════════════════════════════════════

Source = Literal["whatsapp", "appel", "pub", "parrainage", "autre"]


class SaleIn(BaseModel):
    cards: int = Field(default=1, ge=1, le=500)
    client_name: str = Field(default="", max_length=120)
    client_phone: str = Field(default="", max_length=30)
    source: Source = "whatsapp"
    note: str = Field(default="", max_length=500)
    followup_id: Optional[str] = Field(default=None, max_length=40)
    # Réservés aux responsables (correction) : vendeur et jour.
    username: Optional[str] = Field(default=None, max_length=40)
    day: Optional[date] = None


class SalePatch(BaseModel):
    cards: Optional[int] = Field(default=None, ge=1, le=500)
    client_name: Optional[str] = Field(default=None, max_length=120)
    client_phone: Optional[str] = Field(default=None, max_length=30)
    source: Optional[Source] = None
    note: Optional[str] = Field(default=None, max_length=500)
    username: Optional[str] = Field(default=None, max_length=40)
    day: Optional[date] = None


def _check_seller(client, username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    if username not in _people(client):
        raise HTTPException(status_code=400, detail="Vendeur inconnu dans l'équipe.")
    return username


@router.get(ws.API + "/sales/stats")
def sales_stats(request: Request, day: str = Query("", max_length=10)):
    client = ws._db()
    ws._me(request, client)
    try:
        d = timeutil.parse_day(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date invalide (AAAA-MM-JJ).")
    return compute_stats(client, d)


@router.get(ws.API + "/sales")
def list_sales(request: Request, day: str = Query("", max_length=10), username: str = Query("", max_length=40)):
    client = ws._db()
    ws._me(request, client)
    try:
        d = timeutil.parse_day(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date invalide (AAAA-MM-JJ).")

    def build():
        q = client.table(T_SALES).select("*").eq("day", d.isoformat())
        if username:
            q = q.eq("username", username)
        return q.order("created_at", desc=True)

    rows = fetch_all(build, "lecture des ventes")
    return {"day": d.isoformat(), "sales": [sale_out(r) for r in rows]}


@router.post(ws.API + "/sales", status_code=201)
def create_sale(body: SaleIn, request: Request):
    """« + Carte closée » : au nom de la personne connectée, aujourd'hui."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    today = timeutil.today()
    seller, day = me.username, today
    if body.username is not None or body.day is not None:
        if me.role != "responsable" and ((body.username and body.username != me.username) or (body.day and body.day != today)):
            raise HTTPException(status_code=403, detail="Seul un responsable enregistre une vente pour quelqu'un d'autre ou un autre jour.")
        seller = _check_seller(client, body.username) if body.username else me.username
        day = body.day or today
    if day > today:
        raise HTTPException(status_code=400, detail="Une vente ne se saisit pas à l'avance.")
    followup_id = None
    if body.followup_id:
        fid = ws._check_uuid(body.followup_id)
        if not ws._run(client.table("workspace_followups").select("id").eq("id", fid).limit(1), "lecture de la fiche"):
            raise HTTPException(status_code=400, detail="Fiche de relance inconnue.")
        followup_id = fid
    sale = record_sale(
        client, username=seller, cards=body.cards, day=day, created_by=me.username,
        client_name=body.client_name, client_phone=clean_phone_or_400(body.client_phone),
        source=body.source, note=body.note, followup_id=followup_id,
    )
    return {"sale": sale}


@router.patch(ws.API + "/sales/{sale_id}")
def update_sale(sale_id: str, body: SalePatch, request: Request):
    """Correction d'une vente : responsable seulement."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    current = _sale_or_404(client, sale_id)
    data = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    for key, value in data.items():
        if key == "username":
            changes[key] = _check_seller(client, value) if value else None
        elif key == "day":
            if value is None or value > timeutil.today():
                raise HTTPException(status_code=400, detail="Jour invalide.")
            changes[key] = value.isoformat()
        elif key == "client_phone":
            changes[key] = clean_phone_or_400(value or "")
        elif key == "client_name":
            changes[key] = " ".join(str(value or "").split())
        elif key == "note":
            changes[key] = str(value or "").strip()
        elif value is not None:
            changes[key] = value
    if not changes:
        return {"sale": sale_out(current)}
    rows = ws._run(client.table(T_SALES).update(changes).eq("id", str(current["id"])), "correction de la vente")
    ws._log(client, me.username, "sale_updated", detail={k: True for k in changes})
    refresh_auto_krs(client)
    return {"sale": sale_out(rows[0] if rows else {**current, **changes})}


@router.delete(ws.API + "/sales/{sale_id}")
def cancel_sale(sale_id: str, request: Request):
    """Annule une saisie (rien n'est effacé) : la sienne du jour, ou toute vente pour un responsable."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    current = _sale_or_404(client, sale_id)
    if current.get("cancelled_at"):
        return {"sale": sale_out(current)}
    if me.role != "responsable":
        mine = me.username in (current.get("username"), current.get("created_by"))
        if not mine or str(current.get("day"))[:10] != timeutil.today().isoformat() or current.get("origin") == "handypay":
            raise HTTPException(status_code=403, detail="Tu peux annuler seulement ta propre saisie du jour. Demande à un responsable.")
    rows = ws._run(client.table(T_SALES).update({"cancelled_at": ws._now(), "cancelled_by": me.username}).eq("id", str(current["id"])), "annulation de la vente")
    ws._log(client, me.username, "sale_cancelled", detail={"cards": int(current.get("cards") or 0), "username": current.get("username") or ""})
    refresh_auto_krs(client)
    return {"sale": sale_out(rows[0] if rows else current)}


class MilestoneIn(BaseModel):
    date: date
    target: int = Field(ge=1, le=10_000_000)


class SalesSettingsIn(BaseModel):
    daily_target: int = Field(ge=1, le=1_000_000)
    total_target: int = Field(ge=1, le=100_000_000)
    start: date
    end: date
    margin_per_card: float = Field(ge=0, le=100000)
    currency: str = Field(default="USD", max_length=8)
    milestones: list[MilestoneIn] = Field(default_factory=list, max_length=20)


@router.put(ws.API + "/settings/sales")
def put_sales_settings(body: SalesSettingsIn, request: Request):
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "responsable")
    if body.end < body.start:
        raise HTTPException(status_code=400, detail="La fin de la campagne ne peut pas précéder son début.")
    value = normalize_sales_settings(body.model_dump(mode="json"))
    put_setting(client, "sales", value, me.username)
    ws._log(client, me.username, "settings_sales", detail={"daily_target": value["daily_target"], "total_target": value["total_target"]})
    refresh_auto_krs(client)
    return {"settings": value}
