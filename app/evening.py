"""Tableau du soir : la journée en chiffres, calculée, et le mot de chacun.

Pour une date (Port-au-Prince) :
- cartes du jour et cumul de la campagne, écart avec le rythme des jalons, marge ;
- relances envoyées (messages et appels), réponses (en % des relances),
  closes après relance (en % des réponses) ;
- le détail par personne ;
- le formulaire du soir de chacun : les 3 blocages entendus, les bugs ouverts,
  une note, et les messages WhatsApp restés non lus (règle : zéro le soir).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import followups, sales, timeutil
from app import workspace as ws
from app.db import fetch_all

router = APIRouter()

T_NOTES = "workspace_daily_notes"


def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def _note_out(r: dict) -> dict:
    blockers = r.get("blockers") if isinstance(r.get("blockers"), list) else []
    blockers = [str(b) for b in blockers][:3]
    return {
        "username": str(r.get("username") or ""), "day": ws._date(r.get("day")),
        "blockers": blockers + [""] * (3 - len(blockers)), "bugs": str(r.get("bugs") or ""),
        "note": str(r.get("note") or ""),
        "unread_left": None if r.get("unread_left") is None else int(r.get("unread_left")),
        "updated_at": r.get("updated_at"),
    }


def followup_day_stats(client, iso: str) -> dict[str, dict]:
    """Par personne : relances envoyées, réponses, closes, closes après relance (jour donné)."""
    events = fetch_all(
        lambda: client.table(followups.T_EVENTS).select("followup_id,kind,actor,created_at").eq("day", iso).order("id"),
        "lecture du journal des relances",
    )
    closes = [e for e in events if e.get("kind") == "close"]
    # Une close « après relance » : la fiche avait reçu au moins un message ou un appel avant.
    contacted: dict[str, str] = {}
    ids = sorted({str(e["followup_id"]) for e in closes})
    for i in range(0, len(ids), 150):
        for e in ws._run(client.table(followups.T_EVENTS).select("followup_id,created_at").in_("followup_id", ids[i:i + 150])
                         .in_("kind", list(followups.ATTEMPTS)).order("created_at").limit(1000), "lecture du journal des relances"):
            contacted.setdefault(str(e["followup_id"]), str(e.get("created_at") or ""))
    out: dict[str, dict] = {}
    for e in events:
        who = str(e.get("actor") or "")
        s = out.setdefault(who, {"sent": 0, "replies": 0, "closes": 0, "closes_after": 0})
        kind = e.get("kind")
        if kind in followups.ATTEMPTS:
            s["sent"] += 1
        elif kind == "reponse":
            s["replies"] += 1
        elif kind == "close":
            s["closes"] += 1
            first = contacted.get(str(e.get("followup_id")))
            if first and first <= str(e.get("created_at") or ""):
                s["closes_after"] += 1
    return out


def evening_report(client, day: date) -> dict:
    iso = day.isoformat()
    team = ws._team(client)
    stats = sales.compute_stats(client, day, team=team)
    fcfg = followups.get_followup_settings(client)
    per = followup_day_stats(client, iso)
    notes = {str(r.get("username")): _note_out(r) for r in ws._run(
        client.table(T_NOTES).select("*").eq("day", iso).limit(500), "lecture des notes du soir")}
    cards = {p["username"]: p["cards"] for p in stats["today"]["by_person"] if p["username"]}
    people = []
    for m in team:
        if m.get("is_ai"):
            continue
        s = per.get(m["username"], {"sent": 0, "replies": 0, "closes": 0, "closes_after": 0})
        if m["role"] == "observateur" and not any(s.values()) and not cards.get(m["username"]) and m["username"] not in notes:
            continue
        people.append({
            "username": m["username"], "display_name": m["display_name"], "role": m["role"],
            "cards": cards.get(m["username"], 0), **s,
            "reply_rate": _pct(s["replies"], s["sent"]), "close_rate": _pct(s["closes_after"], s["replies"]),
            "note": notes.get(m["username"]),
        })
    people.sort(key=lambda p: (-p["cards"], -p["sent"], p["display_name"].casefold()))
    sent = sum(s["sent"] for s in per.values())
    replies = sum(s["replies"] for s in per.values())
    closes = sum(s["closes"] for s in per.values())
    closes_after = sum(s["closes_after"] for s in per.values())
    direct = next((p["cards"] for p in stats["today"]["by_person"] if p["username"] is None), 0)
    camp = stats["campaign"]
    return {
        "day": iso,
        "sales": {
            "cards": stats["today"]["cards"], "target": stats["today"]["target"], "direct": direct,
            "margin": stats["today"]["margin"], "cumul": camp["cumul"], "expected": camp["expected"],
            "gap": camp["gap"], "margin_cumul": camp["margin"], "currency": camp["currency"],
            "total_target": camp["target"], "next_milestone": camp["next_milestone"],
        },
        "followups": {
            "sent": sent, "replies": replies, "closes": closes, "closes_after": closes_after,
            "reply_rate": _pct(replies, sent), "close_rate": _pct(closes_after, replies),
            "conversations_target": fcfg["conversations_target"],
        },
        "people": people,
        "unread_total": sum(n["unread_left"] or 0 for n in notes.values()),
    }


@router.get(ws.API + "/evening")
def evening(request: Request, day: str = Query("", max_length=10)):
    client = ws._db()
    me = ws._me(request, client)
    try:
        d = timeutil.parse_day(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date invalide (AAAA-MM-JJ).")
    out = evening_report(client, d)
    mine = next((p["note"] for p in out["people"] if p["username"] == me.username and p["note"]), None)
    out["my_note"] = mine or _note_out({"username": me.username, "day": d.isoformat()})
    return out


class NoteIn(BaseModel):
    day: Optional[date] = None
    blockers: list[str] = Field(default_factory=list, max_length=3)
    bugs: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)
    unread_left: Optional[int] = Field(default=None, ge=0, le=100000)


@router.put(ws.API + "/evening/note")
def put_note(body: NoteIn, request: Request):
    """Son formulaire du soir (aujourd'hui, ou un des 7 derniers jours pour rattraper un oubli)."""
    client = ws._db()
    me = ws._me(request, client)
    ws._require(me, "membre")
    today = timeutil.today()
    day = body.day or today
    if day > today or day < today - timedelta(days=7):
        raise HTTPException(status_code=400, detail="Le formulaire du soir se remplit pour aujourd'hui ou les 7 derniers jours.")
    blockers = [" ".join(b.split())[:300] for b in body.blockers][:3]
    row = {"blockers": blockers, "bugs": body.bugs.strip(), "note": body.note.strip(), "unread_left": body.unread_left}
    existing = ws._run(client.table(T_NOTES).select("id").eq("username", me.username).eq("day", day.isoformat()).limit(1), "lecture de la note")
    if existing:
        rows = ws._run(client.table(T_NOTES).update(row).eq("id", str(existing[0]["id"])), "note du soir")
    else:
        rows = ws._run(client.table(T_NOTES).insert(dict(row, id=str(uuid.uuid4()), username=me.username, day=day.isoformat())), "note du soir")
    return {"note": _note_out(rows[0] if rows else dict(row, username=me.username, day=day.isoformat()))}
