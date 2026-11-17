"""Finances : la cascade du mois, les dépenses réelles, les versements, la dette.

LA CASCADE. Chaque mois, la MARGE (cartes vendues non annulées × marge par
carte, plus les autres revenus saisis) paie, dans cet ordre :
  1. les charges fixes (internet, outils, publicité…) ;
  2. les salaires de base des personnes payées ;
  3. la dette fournisseur : une mensualité, jusqu'à ce que le solde soit à zéro ;
  4. la réserve : un pourcentage de la marge ;
  5. le surplus : une part en primes, partagée à parts égales entre les
     personnes payées ; le reste va à la croissance.
On paie dans l'ordre jusqu'où l'argent va. Ce qui manque aux étapes 1 à 3 est un
ARRIÉRÉ : il s'ajoute à la même étape le mois suivant (avant le mois courant),
donc toujours avant la moindre prime. La réserve n'est pas une dette : ce qui
lui manque n'est pas reporté. Quand l'argent ne suffit pas pour les salaires,
chacun reçoit la même proportion de ce qui lui est dû.

SEUIL en cartes par mois = (charges + salaires + mensualité de la dette)
                           ÷ ((1 − réserve) × marge par carte), arrondi au-dessus.
Avec les réglages de départ : 4 920 ÷ 9 = 547 cartes ; 436 sans dette.

DETTE. Le solde = solde initial − remboursements enregistrés. La mensualité d'un
mois est plafonnée au solde du 1er de ce mois (remboursements datés d'avant).

RIEN N'EST FIGÉ. Le relevé d'un mois se recalcule à chaque lecture depuis les
ventes, les journaux et les réglages actuels (les arriérés se reportent depuis
le « début du suivi »). Changer un réglage change donc aussi les mois passés.

DROITS. Responsables et membres lisent tout le relevé, par transparence ; un
observateur ne voit rien (403). Un membre propose une dépense et donne son
accord ; seuls les responsables changent les réglages, valident ou refusent une
dépense, enregistrent versements, remboursements et autres revenus, et
exportent le relevé. Rien n'est effacé : annuler pose `cancelled_at`. Rien ne
part dans le fil d'activité (lisible des observateurs) : chaque ligne garde qui
l'a saisie, validée ou annulée.
"""

from __future__ import annotations

import csv
import io
import math
import re
import uuid
from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import sales, timeutil
from app import workspace as ws
from app.db import fetch_all

router = APIRouter()

T_EXPENSES = "workspace_fin_expenses"
T_APPROVALS = "workspace_fin_approvals"
T_PAYOUTS = "workspace_fin_payouts"
T_DEBT = "workspace_fin_debt_payments"
T_INCOMES = "workspace_fin_incomes"
SETTINGS_KEY = "finances"

METHODS = {"moncash": "MonCash", "natcash": "NatCash", "handypay": "HandyPay", "especes": "Espèces"}
KINDS = {"salaire": "Salaire", "prime": "Prime"}
EXPENSE_STATUS = {"en_attente": "En attente", "validee": "Validée", "refusee": "Refusée"}
COMPANY = "entreprise"  # « payé par » : la caisse de l'entreprise
MAX_MONTHS = 120        # au-delà, les arriérés plus anciens ne sont plus reportés
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre")
_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

DEFAULT_FIN: dict[str, Any] = {
    "start_month": "2026-11",
    "fixed_charges": [
        {"label": "Internet", "amount": 120, "note": ""},
        {"label": "ChatGPT", "amount": 200, "note": ""},
        {"label": "Claude", "amount": 100, "note": ""},
        {"label": "Publicité", "amount": 1500, "note": "50 $ × 30 jours"},
    ],
    "default_salary": 500,
    "salaries": None,  # None : chaque membre actif (ni IA, ni observateur) au salaire par défaut
    "debt_label": "Fournisseur",
    "debt_initial": 5000,
    "debt_monthly": 1000,
    "reserve_pct": 10,
    "bonus_pct": 50,
    "approval_threshold": 200,
}


# ═══════════════════════════════════════════════════════════════════
# Argent (centimes entiers) et mois
# ═══════════════════════════════════════════════════════════════════

def cents(value: Any) -> int:
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return 0


def usd(c: int) -> float:
    """Centimes → dollars pour le JSON."""
    return round(c / 100, 2)


def fmt_usd(c: int) -> str:
    """« 1 920 $ » : espace insécable pour les milliers et devant le symbole."""
    sign = "−" if c < 0 else ""
    units, rest = divmod(abs(int(c)), 100)
    text = f"{units:,}".replace(",", " ") + (f",{rest:02d}" if rest else "")
    return sign + text + " $"


def fmt_pct(p: float) -> str:
    return (f"{p:g}").replace(".", ",")


def csv_num(c: int) -> str:
    """Montant pour un tableur français : « 1920 » ou « 1920,50 »."""
    units, rest = divmod(abs(int(c)), 100)
    return ("-" if c < 0 else "") + str(units) + (f",{rest:02d}" if rest else "")


def _round(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def pct_of(c: int, pct: float) -> int:
    return _round(Fraction(c) * Fraction(str(pct)) / 100)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def check_month(value: str) -> str:
    m = _MONTH_RE.match(str(value or ""))
    if not m or not 2000 <= int(m.group(1)) <= 2100:
        raise HTTPException(status_code=400, detail="Mois invalide (AAAA-MM).")
    return str(value)


def month_bounds(key: str) -> tuple[date, date]:
    y, m = int(key[:4]), int(key[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def shift_month(key: str, n: int) -> str:
    y, m = int(key[:4]), int(key[5:7]) - 1 + n
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def month_span(a: str, b: str) -> list[str]:
    out, k = [], a
    while k <= b:
        out.append(k)
        k = shift_month(k, 1)
    return out


def month_label(key: str) -> str:
    return f"{MOIS[int(key[5:7]) - 1]} {key[:4]}"


# ═══════════════════════════════════════════════════════════════════
# Réglages (clé « finances » de workspace_settings)
# ═══════════════════════════════════════════════════════════════════

def _money(value: Any, default: float, high: float = 10_000_000) -> float:
    try:
        v = round(float(value), 2)
    except (TypeError, ValueError):
        return default
    return v if 0 <= v <= high and math.isfinite(v) else default


def _text(value: Any, high: int) -> str:
    return " ".join(str(value or "").split())[:high]


def normalize_fin_settings(raw: dict) -> dict:
    d = DEFAULT_FIN
    start = str(raw.get("start_month") or "")
    if not _MONTH_RE.match(start):
        start = d["start_month"]
    charges, seen = [], set()
    for c in raw.get("fixed_charges") if isinstance(raw.get("fixed_charges"), list) else d["fixed_charges"]:
        if not isinstance(c, dict):
            continue
        label = _text(c.get("label"), 40)
        if not label or label.casefold() in seen:
            continue
        seen.add(label.casefold())
        charges.append({"label": label, "amount": _money(c.get("amount"), 0, 1_000_000), "note": _text(c.get("note"), 80)})
    salaries = None
    if isinstance(raw.get("salaries"), list):
        salaries, names = [], set()
        for s in raw["salaries"]:
            if not isinstance(s, dict):
                continue
            name = str(s.get("username") or "").strip()
            if name and name not in names and name != ws.AI_USER:
                names.add(name)
                salaries.append({"username": name, "amount": _money(s.get("amount"), 0, 1_000_000)})
    reserve = _money(raw.get("reserve_pct"), d["reserve_pct"], 100)
    return {
        "start_month": start,
        "fixed_charges": charges[:30],
        "default_salary": _money(raw.get("default_salary"), d["default_salary"], 1_000_000),
        "salaries": salaries[:30] if salaries is not None else None,
        "debt_label": _text(raw.get("debt_label"), 60) or d["debt_label"],
        "debt_initial": _money(raw.get("debt_initial"), d["debt_initial"]),
        "debt_monthly": _money(raw.get("debt_monthly"), d["debt_monthly"]),
        "reserve_pct": reserve if reserve < 100 else d["reserve_pct"],
        "bonus_pct": _money(raw.get("bonus_pct"), d["bonus_pct"], 100),
        "approval_threshold": _money(raw.get("approval_threshold"), d["approval_threshold"], 1_000_000),
    }


def get_fin_settings(client) -> dict:
    raw = sales.get_setting(client, SETTINGS_KEY)
    return normalize_fin_settings({**DEFAULT_FIN, **raw})


def payroll(cfg: dict, team: list[dict]) -> list[dict]:
    """Les personnes payées : liste réglée, ou chaque membre actif hors IA et observateurs."""
    names = {m["username"]: m["display_name"] for m in team if not m.get("is_ai")}
    if cfg["salaries"] is None:
        return [{"username": m["username"], "display_name": m["display_name"], "base": cents(cfg["default_salary"])}
                for m in team if not m.get("is_ai") and m.get("role") in ("responsable", "membre")]
    return [{"username": s["username"], "display_name": names.get(s["username"], s["username"]), "base": cents(s["amount"])}
            for s in cfg["salaries"]]


# ═══════════════════════════════════════════════════════════════════
# La cascade (calcul pur, en centimes)
# ═══════════════════════════════════════════════════════════════════

def prorata(pot: int, dues: list[tuple[str, int]]) -> dict[str, int]:
    """Partage `pot` au prorata de ce qui est dû ; les centimes restants vont aux premiers."""
    total = sum(d for _, d in dues)
    if pot >= total:
        return {u: d for u, d in dues}
    if total <= 0:
        return {u: 0 for u, _ in dues}
    out = {u: pot * d // total for u, d in dues}
    left = pot - sum(out.values())
    for u, d in dues:
        if left <= 0:
            break
        if out[u] < d:
            out[u] += 1
            left -= 1
    return out


def run_cascade(margin: int, charges: list[tuple[str, int]], charges_arrears: int,
                people: list[tuple[str, int]], people_arrears: dict[str, int],
                debt_due: int, reserve_pct: float, bonus_pct: float) -> dict:
    """Une cascade mensuelle. `people` = personnes payées (salaire de base) ;
    `people_arrears` peut citer quelqu'un qui n'est plus payé : son arriéré reste dû.
    `debt_due` = mensualité + arriéré de dette, déjà plafonnés au solde."""
    avail = margin

    def take(amount: int) -> int:
        nonlocal avail
        paid = min(avail, max(0, amount))
        avail -= paid
        return paid

    # 1. Charges fixes : l'arriéré d'abord, puis chaque poste dans l'ordre de la liste.
    charge_lines = []
    if charges_arrears > 0:
        charge_lines.append({"label": "Arriéré du mois précédent", "due": charges_arrears, "paid": take(charges_arrears), "arrears": True})
    for label, amount in charges:
        charge_lines.append({"label": label, "due": amount, "paid": take(amount), "arrears": False})
    charges_due = sum(x["due"] for x in charge_lines)
    charges_paid = sum(x["paid"] for x in charge_lines)

    # 2. Salaires de base (+ arriérés) : au prorata quand l'argent manque.
    base = dict(people)
    order = [u for u, _ in people] + sorted(u for u in people_arrears if u not in base and people_arrears[u] > 0)
    dues = [(u, base.get(u, 0) + max(0, people_arrears.get(u, 0))) for u in order]
    salaries_due = sum(d for _, d in dues)
    alloc = prorata(take(salaries_due), dues)
    salary_lines = [{"username": u, "base": base.get(u, 0), "arrears_in": max(0, people_arrears.get(u, 0)),
                     "due": d, "paid": alloc[u], "missing": d - alloc[u], "paid_person": u in base} for u, d in dues]
    salaries_paid = sum(alloc.values())

    # 3. Dette.
    debt_paid = take(debt_due)

    # 4. Réserve : un pourcentage de la marge, sans report.
    reserve_due = pct_of(margin, reserve_pct)
    reserve_paid = take(reserve_due)

    # 5. Surplus : primes à parts égales entre les personnes payées, le reste à la croissance.
    surplus = avail
    n = len(people)
    pool = pct_of(surplus, bonus_pct) if n else 0
    each = pool // n if n else 0
    bonus_total = each * n
    growth = surplus - bonus_total

    arrears_out = {
        "charges": charges_due - charges_paid,
        "people": {x["username"]: x["missing"] for x in salary_lines if x["missing"] > 0},
        "debt": debt_due - debt_paid,
    }
    arrears_out["total"] = arrears_out["charges"] + sum(arrears_out["people"].values()) + arrears_out["debt"]
    return {
        "margin": margin,
        "charges": {"due": charges_due, "paid": charges_paid, "lines": charge_lines},
        "salaries": {"due": salaries_due, "paid": salaries_paid, "lines": salary_lines},
        "debt": {"due": debt_due, "paid": debt_paid},
        "reserve": {"due": reserve_due, "paid": reserve_paid},
        "surplus": {"total": surplus, "bonus_pool": bonus_total, "bonus_each": each, "growth": growth, "people": n},
        "arrears_out": arrears_out,
        "covered": arrears_out["total"] == 0 and reserve_paid == reserve_due,
    }


def threshold_cards(costs: int, per_card: int, reserve_pct: float, other_income: int = 0) -> Optional[int]:
    """Cartes à vendre pour que (1 − réserve) × marge couvre `costs`."""
    net = Fraction(per_card) * (1 - Fraction(str(reserve_pct)) / 100)
    if net <= 0:
        return None
    needed = Fraction(costs) / (1 - Fraction(str(reserve_pct)) / 100) - other_income
    return max(0, math.ceil(needed / per_card))


# ═══════════════════════════════════════════════════════════════════
# Lecture des journaux
# ═══════════════════════════════════════════════════════════════════

def _active(client, table: str, what: str) -> list[dict]:
    return fetch_all(lambda: client.table(table).select("*").is_("cancelled_at", "null").order("day").order("id"), what)


def _payout_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "username": str(r.get("username") or ""), "month": str(r.get("month") or ""),
        "kind": r.get("kind") if r.get("kind") in KINDS else "salaire", "day": ws._date(r.get("day")),
        "amount": usd(cents(r.get("amount"))), "method": r.get("method") if r.get("method") in METHODS else "especes",
        "note": str(r.get("note") or ""), "created_by": str(r.get("created_by") or ""),
        "cancelled": bool(r.get("cancelled_at")), "created_at": r.get("created_at"),
    }


def _debt_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "day": ws._date(r.get("day")), "amount": usd(cents(r.get("amount"))),
        "method": r.get("method") if r.get("method") in METHODS else "especes", "note": str(r.get("note") or ""),
        "created_by": str(r.get("created_by") or ""), "cancelled": bool(r.get("cancelled_at")), "created_at": r.get("created_at"),
    }


def _income_out(r: dict) -> dict:
    return {
        "id": str(r.get("id") or ""), "day": ws._date(r.get("day")), "label": str(r.get("label") or ""),
        "amount": usd(cents(r.get("amount"))), "note": str(r.get("note") or ""),
        "created_by": str(r.get("created_by") or ""), "cancelled": bool(r.get("cancelled_at")), "created_at": r.get("created_at"),
    }


def _expense_out(r: dict, approvals: list[dict], me: Optional[ws.Me] = None) -> dict:
    required = ws._strs(r.get("required"))
    given = sorted(({"username": str(a.get("username")), "at": a.get("created_at")} for a in approvals
                    if str(a.get("expense_id")) == str(r.get("id"))), key=lambda a: str(a["at"] or ""))
    done = {a["username"] for a in given}
    status = r.get("status") if r.get("status") in EXPENSE_STATUS else "en_attente"
    pending = status == "en_attente"
    needs = bool(r.get("needs_approval"))
    out = {
        "id": str(r.get("id") or ""), "day": ws._date(r.get("day")), "category": str(r.get("category") or ""),
        "amount": usd(cents(r.get("amount"))), "paid_by": str(r.get("paid_by") or ""), "note": str(r.get("note") or ""),
        "status": status, "needs_approval": needs, "required": required, "approvals": given,
        "missing": [u for u in required if u not in done] if needs else [],
        "decided_by": str(r.get("decided_by") or ""), "decided_at": r.get("decided_at"),
        "refusal_reason": str(r.get("refusal_reason") or ""), "created_by": str(r.get("created_by") or ""),
        "created_at": r.get("created_at"),
    }
    if me is not None:
        lead = me.role == "responsable"
        out["can_approve"] = pending and needs and me.username in required and me.username not in done
        out["can_validate"] = pending and not needs and lead
        out["can_refuse"] = pending and lead
    return out


def _approvals_for(client, ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 150):
        chunk = ids[i:i + 150]
        # Page par page : Supabase rend 1 000 lignes au plus par lecture.
        out += fetch_all(lambda: client.table(T_APPROVALS).select("expense_id,username,created_at").in_("expense_id", chunk).order("id"),
                         "lecture des accords")
    return out


# ═══════════════════════════════════════════════════════════════════
# Le relevé d'un mois
# ═══════════════════════════════════════════════════════════════════

def _explain(cfg: dict, people: list[dict], debt_balance: int) -> dict[str, str]:
    charges_total = sum(cents(c["amount"]) for c in cfg["fixed_charges"])
    bases = {p["base"] for p in people}
    n = len(people)
    if n and len(bases) == 1:
        sal = f"Ensuite, chaque personne payée reçoit son salaire de base : {n} × {fmt_usd(people[0]['base'])}."
    else:
        sal = f"Ensuite, chaque personne payée reçoit son salaire de base ({fmt_usd(sum(p['base'] for p in people))} en tout)."
    if debt_balance > 0:
        debt = (f"Puis on rend {fmt_usd(cents(cfg['debt_monthly']))} par mois au {cfg['debt_label'].lower()}, "
                f"jusqu'à ce que la dette soit à zéro.")
    else:
        debt = "La dette est remboursée : cette étape ne coûte plus rien."
    return {
        "charges": f"On paie d'abord ce qui fait tourner HandyPay (outils, publicité, internet) : {fmt_usd(charges_total)} par mois.",
        "salaires": sal,
        "dette": debt,
        "reserve": f"On met {fmt_pct(cfg['reserve_pct'])} % de la marge de côté pour les mauvais jours.",
        "surplus": (f"Ce qui reste : {fmt_pct(cfg['bonus_pct'])} % en primes, à parts égales entre les personnes payées ; "
                    f"le reste sert à faire grandir HandyPay."),
    }


def build_statement(client, key: str, me: ws.Me) -> dict:
    cfg = get_fin_settings(client)
    scfg = sales.get_sales_settings(client)
    team = ws._team(client)
    names = {m["username"]: m["display_name"] for m in team}
    people = payroll(cfg, team)
    per_card = cents(scfg["margin_per_card"])
    charges = [(c["label"], cents(c["amount"])) for c in cfg["fixed_charges"]]
    debt_initial, monthly = cents(cfg["debt_initial"]), cents(cfg["debt_monthly"])
    today = timeutil.today()
    current = month_key(today)
    start = cfg["start_month"]

    keys = month_span(start, key) if key >= start else [key]
    keys = keys[-MAX_MONTHS:]
    first, _ = month_bounds(keys[0])
    m_first, m_last = month_bounds(key)

    cards_by: dict[str, int] = {}
    for r in sales.sales_between(client, first.isoformat(), m_last.isoformat()):
        k = str(r.get("day"))[:7]
        cards_by[k] = cards_by.get(k, 0) + int(r.get("cards") or 0)
    incomes = [_income_out(r) for r in _active(client, T_INCOMES, "lecture des autres revenus")]
    income_by: dict[str, int] = {}
    for x in incomes:
        income_by[x["day"][:7]] = income_by.get(x["day"][:7], 0) + cents(x["amount"])
    debt_rows = [_debt_out(r) for r in _active(client, T_DEBT, "lecture des remboursements")]

    def balance_before(day: date) -> int:
        return max(0, debt_initial - sum(cents(x["amount"]) for x in debt_rows if x["day"] < day.isoformat()))

    def month_result(k: str, arrears: dict) -> dict:
        d_first, _ = month_bounds(k)
        margin = cards_by.get(k, 0) * per_card + income_by.get(k, 0)
        bal = balance_before(d_first)
        debt_due = min(monthly + arrears["debt"], bal)
        res = run_cascade(margin, charges, arrears["charges"], [(p["username"], p["base"]) for p in people],
                          arrears["people"], debt_due, cfg["reserve_pct"], cfg["bonus_pct"])
        res["debt"].update(base=min(monthly, bal), balance_start=bal, arrears_in=arrears["debt"])
        return res

    arrears = {"charges": 0, "people": {}, "debt": 0}
    res: dict = {}
    for k in keys:
        arrears_in = arrears
        res = month_result(k, arrears)
        arrears = res["arrears_out"]
    if key < start:
        arrears_in = {"charges": 0, "people": {}, "debt": 0}

    cards = cards_by.get(key, 0)
    other = income_by.get(key, 0)
    base_costs = sum(a for _, a in charges) + sum(p["base"] for p in people)
    debt_base = res["debt"]["base"]
    full_costs = res["charges"]["due"] + res["salaries"]["due"] + res["debt"]["due"]
    seuil = threshold_cards(base_costs + debt_base, per_card, cfg["reserve_pct"])
    seuil_month = threshold_cards(full_costs, per_card, cfg["reserve_pct"], other)
    threshold = {
        "cards": seuil,
        "cards_without_debt": threshold_cards(base_costs, per_card, cfg["reserve_pct"]),
        "cards_month": seuil_month,
        "cards_left": max(0, seuil_month - cards) if seuil_month is not None else None,
        "reached": res["covered"],
        "costs": usd(base_costs + debt_base),
        "costs_with_arrears": usd(full_costs),
    }

    # Projection de fin de mois, au rythme du mois en cours (depuis le début de la
    # campagne de ventes s'il tombe dans ce mois).
    projection = None
    if key == current:
        active = m_first
        try:
            camp = date.fromisoformat(scfg["start"])
            if m_first < camp <= today:
                active = camp
        except ValueError:
            pass
        elapsed = (today - active).days + 1
        left = (m_last - today).days
        p_cards = cards + _round(Fraction(cards, elapsed) * left) if elapsed > 0 else cards
        p_bal = balance_before(m_first)
        p = run_cascade(p_cards * per_card + other, charges, arrears_in["charges"], [(x["username"], x["base"]) for x in people],
                        arrears_in["people"], min(monthly + arrears_in["debt"], p_bal), cfg["reserve_pct"], cfg["bonus_pct"])
        projection = {
            "cards": p_cards, "per_day": round(cards / elapsed, 1) if elapsed > 0 else 0, "since": active.isoformat(),
            "days_left": left, "margin": usd(p["margin"]), "reached": p["covered"], "missing": usd(p["arrears_out"]["total"]),
            "reserve": usd(p["reserve"]["paid"]), "surplus": usd(p["surplus"]["total"]),
            "bonus_each": usd(p["surplus"]["bonus_each"]), "growth": usd(p["surplus"]["growth"]),
            "people": [{"username": x["username"], "total": usd(x["paid"] + (p["surplus"]["bonus_each"] if x["paid_person"] else 0))}
                       for x in p["salaries"]["lines"]],
        }

    # Journaux du mois.
    payouts = [_payout_out(r) for r in fetch_all(
        lambda: client.table(T_PAYOUTS).select("*").eq("month", key).is_("cancelled_at", "null").order("day").order("id"),
        "lecture des versements")]
    exp_rows = fetch_all(lambda: client.table(T_EXPENSES).select("*").order("day", desc=True).order("id"), "lecture des dépenses")
    wanted = [r for r in exp_rows if str(r.get("day"))[:7] == key or r.get("status") == "en_attente"]
    approvals = _approvals_for(client, [str(r["id"]) for r in wanted])
    month_expenses = [_expense_out(r, approvals, me) for r in wanted if str(r.get("day"))[:7] == key]
    pending = [_expense_out(r, approvals, me) for r in wanted if r.get("status") == "en_attente"]
    spent_by: dict[str, int] = {}
    for x in month_expenses:
        if x["status"] == "validee":
            spent_by[x["category"].casefold()] = spent_by.get(x["category"].casefold(), 0) + cents(x["amount"])
    charge_keys = {label.casefold() for label, _ in charges}
    other_spent = sum(v for k, v in spent_by.items() if k not in charge_keys)

    # Par personne : ce que la cascade lui doit pour ce mois, et ce qui a été versé.
    paid_out: dict[str, dict[str, int]] = {}
    for x in payouts:
        slot = paid_out.setdefault(x["username"], {"salaire": 0, "prime": 0})
        slot[x["kind"]] += cents(x["amount"])
    rows = []
    bonus_each = res["surplus"]["bonus_each"]
    listed = set()
    for line in res["salaries"]["lines"]:
        u = line["username"]
        listed.add(u)
        bonus = bonus_each if line["paid_person"] else 0
        got = paid_out.get(u, {"salaire": 0, "prime": 0})
        total = line["paid"] + bonus
        rows.append({
            "username": u, "display_name": names.get(u, u), "paid_person": line["paid_person"],
            "base": usd(line["base"]), "arrears_in": usd(line["arrears_in"]), "salary_due": usd(line["due"]),
            "salary": usd(line["paid"]), "salary_missing": usd(line["missing"]), "bonus": usd(bonus),
            "total_due": usd(total), "paid_out": usd(got["salaire"] + got["prime"]),
            "paid_salary": usd(got["salaire"]), "paid_bonus": usd(got["prime"]),
            "remaining": usd(total - got["salaire"] - got["prime"]),
        })
    for u, got in paid_out.items():
        if u not in listed:
            rows.append({
                "username": u, "display_name": names.get(u, u), "paid_person": False, "base": 0.0, "arrears_in": 0.0,
                "salary_due": 0.0, "salary": 0.0, "salary_missing": 0.0, "bonus": 0.0, "total_due": 0.0,
                "paid_out": usd(got["salaire"] + got["prime"]), "paid_salary": usd(got["salaire"]),
                "paid_bonus": usd(got["prime"]), "remaining": usd(-(got["salaire"] + got["prime"])),
            })

    expl = _explain(cfg, people, res["debt"]["balance_start"])
    charge_lines = [dict(x, due=usd(x["due"]), paid=usd(x["paid"]), missing=usd(x["due"] - x["paid"]),
                         note=next((c["note"] for c in cfg["fixed_charges"] if c["label"] == x["label"]), ""),
                         spent=usd(spent_by.get(x["label"].casefold(), 0)) if not x["arrears"] else None)
                    for x in res["charges"]["lines"]]
    salary_lines = [{"username": x["username"], "display_name": names.get(x["username"], x["username"]),
                     "base": usd(x["base"]), "arrears_in": usd(x["arrears_in"]), "due": usd(x["due"]),
                     "paid": usd(x["paid"]), "missing": usd(x["missing"])} for x in res["salaries"]["lines"]]
    steps = [
        {"key": "charges", "rank": 1, "title": "Charges fixes", "explain": expl["charges"],
         "due": usd(res["charges"]["due"]), "paid": usd(res["charges"]["paid"]),
         "missing": usd(res["charges"]["due"] - res["charges"]["paid"]), "arrears_in": usd(arrears_in["charges"]),
         "lines": charge_lines},
        {"key": "salaires", "rank": 2, "title": "Salaires de base", "explain": expl["salaires"],
         "due": usd(res["salaries"]["due"]), "paid": usd(res["salaries"]["paid"]),
         "missing": usd(res["salaries"]["due"] - res["salaries"]["paid"]),
         "arrears_in": usd(sum(arrears_in["people"].values())), "lines": salary_lines},
        {"key": "dette", "rank": 3, "title": "Dette " + cfg["debt_label"].lower(), "explain": expl["dette"],
         "due": usd(res["debt"]["due"]), "paid": usd(res["debt"]["paid"]),
         "missing": usd(res["debt"]["due"] - res["debt"]["paid"]), "arrears_in": usd(res["debt"]["arrears_in"]),
         "balance_start": usd(res["debt"]["balance_start"])},
        {"key": "reserve", "rank": 4, "title": "Réserve", "explain": expl["reserve"],
         "due": usd(res["reserve"]["due"]), "paid": usd(res["reserve"]["paid"]),
         "missing": usd(res["reserve"]["due"] - res["reserve"]["paid"]), "arrears_in": 0.0},
        {"key": "surplus", "rank": 5, "title": "Surplus : primes et croissance", "explain": expl["surplus"],
         "total": usd(res["surplus"]["total"]), "bonus_pool": usd(res["surplus"]["bonus_pool"]),
         "bonus_each": usd(bonus_each), "growth": usd(res["surplus"]["growth"]), "people": res["surplus"]["people"],
         "growth_spent": usd(other_spent)},
    ]
    all_paid = sum(cents(x["amount"]) for x in debt_rows)
    debt = {
        "label": cfg["debt_label"], "initial": usd(debt_initial), "monthly": usd(monthly),
        "repaid": usd(min(all_paid, debt_initial)), "balance": usd(max(0, debt_initial - all_paid)),
        "balance_start": usd(res["debt"]["balance_start"]),
        "repaid_in_month": usd(sum(cents(x["amount"]) for x in debt_rows if x["day"][:7] == key)),
        "months_left": math.ceil(max(0, debt_initial - all_paid) / monthly) if monthly else None,
        "payments": sorted(debt_rows, key=lambda x: (x["day"], str(x["created_at"] or "")), reverse=True)[:60],
    }
    totals_due = sum(cents(r["total_due"]) for r in rows)
    totals_paid = sum(cents(r["paid_out"]) for r in rows)
    return {
        "month": key, "label": month_label(key), "today": today.isoformat(), "current_month": current,
        "is_current": key == current, "is_past": key < current, "is_future": key > current,
        "before_start": key < start, "start_month": start,
        "can_manage": me.role == "responsable", "me": me.username,
        "settings": cfg, "payroll": [{"username": p["username"], "display_name": p["display_name"], "base": usd(p["base"])} for p in people],
        "margin": {"cards": cards, "per_card": usd(per_card), "cards_margin": usd(cards * per_card),
                   "other_income": usd(other), "total": usd(res["margin"])},
        "steps": steps,
        "reserve": usd(res["reserve"]["paid"]),
        "surplus": {"total": usd(res["surplus"]["total"]), "bonus_pool": usd(res["surplus"]["bonus_pool"]),
                    "bonus_each": usd(bonus_each), "growth": usd(res["surplus"]["growth"]), "people": res["surplus"]["people"]},
        "arrears_in": {"charges": usd(arrears_in["charges"]), "debt": usd(arrears_in["debt"]),
                       "people": {u: usd(v) for u, v in arrears_in["people"].items()},
                       "total": usd(arrears_in["charges"] + arrears_in["debt"] + sum(arrears_in["people"].values()))},
        "arrears_out": {"charges": usd(res["arrears_out"]["charges"]), "debt": usd(res["arrears_out"]["debt"]),
                        "people": {u: usd(v) for u, v in res["arrears_out"]["people"].items()},
                        "total": usd(res["arrears_out"]["total"])},
        "covered": res["covered"],
        "threshold": threshold,
        "projection": projection,
        "people": rows,
        "totals": {"total_due": usd(totals_due), "paid_out": usd(totals_paid), "remaining": usd(totals_due - totals_paid)},
        "expenses": {
            "month": month_expenses, "pending": pending,
            "validated_total": usd(sum(v for v in spent_by.values())), "other_total": usd(other_spent),
        },
        "payouts": payouts,
        "incomes": [x for x in incomes if x["day"][:7] == key],
        "debt": debt,
        "methods": METHODS,
    }


# ═══════════════════════════════════════════════════════════════════
# Droits
# ═══════════════════════════════════════════════════════════════════

def _reader(request: Request, client) -> ws.Me:
    me = ws._me(request, client)
    if not me.at_least("membre"):
        raise HTTPException(status_code=403, detail="Les finances sont réservées aux responsables et aux membres de l'équipe.")
    return me


def _lead(request: Request, client) -> ws.Me:
    me = _reader(request, client)
    ws._require(me, "responsable")
    return me


def _month_param(mois: str) -> str:
    return check_month(mois) if mois else month_key(timeutil.today())


# ═══════════════════════════════════════════════════════════════════
# Routes : relevé, réglages, export
# ═══════════════════════════════════════════════════════════════════

@router.get(ws.API + "/finances")
def get_statement(request: Request, mois: str = Query("", max_length=7)):
    client = ws._db()
    me = _reader(request, client)
    return build_statement(client, _month_param(mois), me)


@router.get(ws.API + "/finances/debt")
def get_debt(request: Request):
    client = ws._db()
    _reader(request, client)
    cfg = get_fin_settings(client)
    rows = [_debt_out(r) for r in _active(client, T_DEBT, "lecture des remboursements")]
    initial = cents(cfg["debt_initial"])
    paid = sum(cents(x["amount"]) for x in rows)
    return {"label": cfg["debt_label"], "initial": usd(initial), "monthly": cfg["debt_monthly"],
            "repaid": usd(min(paid, initial)), "balance": usd(max(0, initial - paid)),
            "payments": sorted(rows, key=lambda x: (x["day"], str(x["created_at"] or "")), reverse=True)}


class ChargeIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    amount: float = Field(ge=0, le=1_000_000)
    note: str = Field(default="", max_length=80)


class SalaryIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    amount: float = Field(ge=0, le=1_000_000)


class FinSettingsIn(BaseModel):
    start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    fixed_charges: list[ChargeIn] = Field(default_factory=list, max_length=30)
    default_salary: float = Field(ge=0, le=1_000_000)
    salaries: Optional[list[SalaryIn]] = Field(default=None, max_length=30)
    debt_label: str = Field(default="Fournisseur", min_length=1, max_length=60)
    debt_initial: float = Field(ge=0, le=10_000_000)
    debt_monthly: float = Field(ge=0, le=10_000_000)
    reserve_pct: float = Field(ge=0, lt=100)
    bonus_pct: float = Field(ge=0, le=100)
    approval_threshold: float = Field(ge=0, le=1_000_000)


@router.get(ws.API + "/settings/finances")
def read_fin_settings(request: Request):
    client = ws._db()
    _reader(request, client)
    return {"settings": get_fin_settings(client)}


@router.put(ws.API + "/settings/finances")
def put_fin_settings(body: FinSettingsIn, request: Request):
    client = ws._db()
    me = _lead(request, client)
    check_month(body.start_month)
    labels = [" ".join(c.label.split()).casefold() for c in body.fixed_charges]
    if len(set(labels)) != len(labels):
        raise HTTPException(status_code=400, detail="Deux charges fixes portent le même nom.")
    if body.salaries is not None:
        known = {m["username"] for m in ws._team(client) if not m.get("is_ai")}
        names = [s.username for s in body.salaries]
        if len(set(names)) != len(names):
            raise HTTPException(status_code=400, detail="Une personne figure deux fois dans les salaires.")
        unknown = [n for n in names if n not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Personne inconnue dans l'équipe : {unknown[0]}.")
    value = normalize_fin_settings(body.model_dump())
    sales.put_setting(client, SETTINGS_KEY, value, me.username)
    return {"settings": value}


@router.get(ws.API + "/finances/export.csv")
def export_statement(request: Request, mois: str = Query("", max_length=7)):
    client = ws._db()
    me = _lead(request, client)
    st = build_statement(client, _month_param(mois), me)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    cell = ws._csv_cell
    m = st["margin"]
    w.writerow(["Relevé HandyPay", cell(st["label"])])
    w.writerow(["Cartes vendues", m["cards"]])
    w.writerow(["Marge par carte ($)", csv_num(cents(m["per_card"]))])
    w.writerow(["Marge des cartes ($)", csv_num(cents(m["cards_margin"]))])
    w.writerow(["Autres revenus ($)", csv_num(cents(m["other_income"]))])
    w.writerow(["Marge du mois ($)", csv_num(cents(m["total"]))])
    w.writerow([])
    w.writerow(["Étape", "Poste", "Dû ($)", "Payé ($)", "Manque ($)"])
    for step in st["steps"][:4]:
        lines = step.get("lines") or []
        if step["key"] == "salaires":
            lines = [{"label": x["display_name"], "due": x["due"], "paid": x["paid"], "missing": x["missing"]} for x in lines]
        for x in lines:
            w.writerow([cell(step["title"]), cell(x["label"]), csv_num(cents(x["due"])), csv_num(cents(x["paid"])), csv_num(cents(x["missing"]))])
        w.writerow([cell(step["title"]), "Total", csv_num(cents(step["due"])), csv_num(cents(step["paid"])), csv_num(cents(step["missing"]))])
    s = st["steps"][4]
    w.writerow([cell(s["title"]), "Surplus", csv_num(cents(s["total"])), "", ""])
    w.writerow([cell(s["title"]), f"Primes ({s['people']} personnes)", csv_num(cents(s["bonus_pool"])), "", ""])
    w.writerow([cell(s["title"]), "Prime par personne", csv_num(cents(s["bonus_each"])), "", ""])
    w.writerow([cell(s["title"]), "Croissance", csv_num(cents(s["growth"])), "", ""])
    w.writerow([])
    w.writerow(["Personne", "Salaire de base ($)", "Arriéré reçu ($)", "Salaire payé par la marge ($)", "Prime ($)",
                "Total dû ($)", "Déjà versé ($)", "Reste à verser ($)"])
    for p in st["people"]:
        w.writerow([cell(p["display_name"]), csv_num(cents(p["base"])), csv_num(cents(p["arrears_in"])), csv_num(cents(p["salary"])),
                    csv_num(cents(p["bonus"])), csv_num(cents(p["total_due"])), csv_num(cents(p["paid_out"])), csv_num(cents(p["remaining"]))])
    w.writerow([])
    th = st["threshold"]
    w.writerow(["Seuil (cartes par mois)", th["cards"] if th["cards"] is not None else ""])
    w.writerow(["Seuil sans dette (cartes par mois)", th["cards_without_debt"] if th["cards_without_debt"] is not None else ""])
    w.writerow(["Seuil de ce mois, arriérés compris (cartes)", th["cards_month"] if th["cards_month"] is not None else ""])
    w.writerow(["Arriéré reporté au mois suivant ($)", csv_num(cents(st["arrears_out"]["total"]))])
    if st["projection"]:
        w.writerow(["Projection fin de mois (cartes)", st["projection"]["cards"]])
        w.writerow(["Projection : prime par personne ($)", csv_num(cents(st["projection"]["bonus_each"]))])
    w.writerow(["Dette : solde restant ($)", csv_num(cents(st["debt"]["balance"]))])
    return Response(
        content="﻿" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="handypay-releve-{st["month"]}.csv"'},
    )


# ═══════════════════════════════════════════════════════════════════
# Routes : dépenses (proposer, approuver, valider, refuser)
# ═══════════════════════════════════════════════════════════════════

class ExpenseIn(BaseModel):
    day: date
    category: str = Field(min_length=1, max_length=40)
    amount: float = Field(ge=0.01, le=1_000_000)
    paid_by: str = Field(default=COMPANY, max_length=40)
    note: str = Field(default="", max_length=500)


class RefuseIn(BaseModel):
    reason: str = Field(default="", max_length=300)


def _expense_or_404(client, expense_id: str) -> dict:
    eid = ws._check_uuid(expense_id)
    rows = ws._run(client.table(T_EXPENSES).select("*").eq("id", eid).limit(1), "lecture de la dépense")
    if not rows:
        raise HTTPException(status_code=404, detail="Dépense introuvable.")
    return rows[0]


def _expense_view(client, row: dict, me: ws.Me) -> dict:
    return _expense_out(row, _approvals_for(client, [str(row["id"])]), me)


@router.post(ws.API + "/finances/expenses", status_code=201)
def propose_expense(body: ExpenseIn, request: Request):
    """Un membre PROPOSE ; au-dessus du seuil, chaque personne payée doit donner son accord."""
    client = ws._db()
    me = _reader(request, client)
    cfg = get_fin_settings(client)
    team = ws._team(client)
    known = {m["username"] for m in team if not m.get("is_ai")}
    paid_by = (body.paid_by or COMPANY).strip()
    if paid_by != COMPANY and paid_by not in known:
        raise HTTPException(status_code=400, detail="« Payé par » : l'entreprise ou une personne de l'équipe.")
    category = " ".join(body.category.split())
    if not category:
        raise HTTPException(status_code=400, detail="Catégorie requise.")
    amount = cents(body.amount)
    # L'accord est demandé aux personnes payées qui ont accès aux finances (pas à un observateur).
    voters = {m["username"] for m in team if not m.get("is_ai") and m.get("role") in ("responsable", "membre")}
    required = [p["username"] for p in payroll(cfg, team) if p["username"] in voters]
    needs = amount > cents(cfg["approval_threshold"]) and bool(required)
    lead = me.role == "responsable"
    status = "en_attente" if needs or not lead else "validee"
    row = {
        "id": str(uuid.uuid4()), "day": body.day.isoformat(), "category": category, "amount": usd(amount),
        "paid_by": paid_by, "note": body.note.strip(), "status": status, "needs_approval": needs,
        "required": required if needs else [], "created_by": me.username,
        "decided_by": me.username if status == "validee" else "", "decided_at": ws._now() if status == "validee" else None,
    }
    inserted = ws._run(client.table(T_EXPENSES).insert(row), "enregistrement de la dépense")
    row = inserted[0] if inserted else row
    if needs and me.username in required:
        # Proposer, c'est déjà être d'accord.
        ws._run(client.table(T_APPROVALS).upsert({"id": str(uuid.uuid4()), "expense_id": row["id"], "username": me.username},
                                                 on_conflict="expense_id,username", ignore_duplicates=True), "accord")
        row = _settle(client, row)
    if needs and row.get("status") == "en_attente":
        # Seules les personnes payées sont prévenues (jamais un observateur).
        who = next((m["display_name"] for m in team if m["username"] == me.username), me.username)
        ws._notify(client, team, required, me.username, "fin_accord",
                   f"{who} propose une dépense de {fmt_usd(amount)} ({category}) : ton accord est demandé dans Finances.")
    return {"expense": _expense_view(client, row, me)}


def _settle(client, row: dict) -> dict:
    """Validée dès que toutes les personnes requises ont donné leur accord."""
    if row.get("status") != "en_attente" or not row.get("needs_approval"):
        return row
    given = {str(a["username"]) for a in _approvals_for(client, [str(row["id"])])}
    if set(ws._strs(row.get("required"))) <= given:
        upd = ws._run(client.table(T_EXPENSES).update({"status": "validee", "decided_by": "accord de tous", "decided_at": ws._now()})
                      .eq("id", str(row["id"])).eq("status", "en_attente"), "validation de la dépense")
        if upd:
            return upd[0]
        return _expense_or_404(client, str(row["id"]))
    return row


@router.post(ws.API + "/finances/expenses/{expense_id}/approve")
def approve_expense(expense_id: str, request: Request):
    """« J'approuve » : une personne payée donne son accord à un achat au-dessus du seuil."""
    client = ws._db()
    me = _reader(request, client)
    row = _expense_or_404(client, expense_id)
    if row.get("status") != "en_attente":
        raise HTTPException(status_code=409, detail="Cette dépense n'attend plus d'accord.")
    if not row.get("needs_approval"):
        raise HTTPException(status_code=409, detail="Cette dépense ne demande pas l'accord de chacun : un responsable la valide.")
    if me.username not in ws._strs(row.get("required")):
        raise HTTPException(status_code=403, detail="Ton accord n'est pas demandé pour cette dépense.")
    ws._run(client.table(T_APPROVALS).upsert({"id": str(uuid.uuid4()), "expense_id": str(row["id"]), "username": me.username},
                                             on_conflict="expense_id,username", ignore_duplicates=True), "accord")
    return {"expense": _expense_view(client, _settle(client, row), me)}


@router.post(ws.API + "/finances/expenses/{expense_id}/validate")
def validate_expense(expense_id: str, request: Request):
    """Un responsable valide une dépense proposée sous le seuil."""
    client = ws._db()
    me = _lead(request, client)
    row = _expense_or_404(client, expense_id)
    if row.get("status") != "en_attente":
        raise HTTPException(status_code=409, detail="Cette dépense est déjà tranchée.")
    if row.get("needs_approval"):
        raise HTTPException(status_code=409, detail="Au-dessus du seuil, il faut l'accord de chaque personne payée.")
    upd = ws._run(client.table(T_EXPENSES).update({"status": "validee", "decided_by": me.username, "decided_at": ws._now()})
                  .eq("id", str(row["id"])).eq("status", "en_attente"), "validation de la dépense")
    return {"expense": _expense_view(client, upd[0] if upd else _expense_or_404(client, expense_id), me)}


@router.post(ws.API + "/finances/expenses/{expense_id}/refuse")
def refuse_expense(expense_id: str, body: RefuseIn, request: Request):
    client = ws._db()
    me = _lead(request, client)
    row = _expense_or_404(client, expense_id)
    if row.get("status") != "en_attente":
        raise HTTPException(status_code=409, detail="Cette dépense est déjà tranchée.")
    upd = ws._run(client.table(T_EXPENSES).update({"status": "refusee", "decided_by": me.username, "decided_at": ws._now(),
                                                    "refusal_reason": body.reason.strip()})
                  .eq("id", str(row["id"])).eq("status", "en_attente"), "refus de la dépense")
    return {"expense": _expense_view(client, upd[0] if upd else _expense_or_404(client, expense_id), me)}


# ═══════════════════════════════════════════════════════════════════
# Routes : versements, remboursements, autres revenus (responsables)
# ═══════════════════════════════════════════════════════════════════

Method = Literal["moncash", "natcash", "handypay", "especes"]


class PayoutIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    kind: Literal["salaire", "prime"] = "salaire"
    day: date
    amount: float = Field(ge=0.01, le=1_000_000)
    method: Method
    note: str = Field(default="", max_length=300)


class DebtPaymentIn(BaseModel):
    day: date
    amount: float = Field(ge=0.01, le=10_000_000)
    method: Method = "especes"
    note: str = Field(default="", max_length=300)


class IncomeIn(BaseModel):
    day: date
    label: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0.01, le=10_000_000)
    note: str = Field(default="", max_length=300)


def _not_future(day: date, what: str) -> None:
    if day > timeutil.today():
        raise HTTPException(status_code=400, detail=f"{what} ne se saisit pas à l'avance.")


def _cancel(client, table: str, row_id: str, me: ws.Me, what: str) -> dict:
    rid = ws._check_uuid(row_id)
    rows = ws._run(client.table(table).select("*").eq("id", rid).limit(1), "lecture")
    if not rows:
        raise HTTPException(status_code=404, detail=f"{what} introuvable.")
    if rows[0].get("cancelled_at"):
        return rows[0]
    upd = ws._run(client.table(table).update({"cancelled_at": ws._now(), "cancelled_by": me.username}).eq("id", rid), "annulation")
    return upd[0] if upd else rows[0]


@router.post(ws.API + "/finances/payouts", status_code=201)
def add_payout(body: PayoutIn, request: Request):
    """Salaire ou prime versé à une personne pour un mois."""
    client = ws._db()
    me = _lead(request, client)
    check_month(body.month)
    _not_future(body.day, "Un versement")
    team = ws._team(client)
    known = {m["username"] for m in team if not m.get("is_ai")} | {p["username"] for p in payroll(get_fin_settings(client), team)}
    if body.username not in known:
        raise HTTPException(status_code=400, detail="Personne inconnue dans l'équipe.")
    row = {"id": str(uuid.uuid4()), "username": body.username, "month": body.month, "kind": body.kind,
           "day": body.day.isoformat(), "amount": usd(cents(body.amount)), "method": body.method,
           "note": body.note.strip(), "created_by": me.username}
    inserted = ws._run(client.table(T_PAYOUTS).insert(row), "enregistrement du versement")
    return {"payout": _payout_out(inserted[0] if inserted else row)}


@router.delete(ws.API + "/finances/payouts/{payout_id}")
def cancel_payout(payout_id: str, request: Request):
    client = ws._db()
    me = _lead(request, client)
    return {"payout": _payout_out(_cancel(client, T_PAYOUTS, payout_id, me, "Versement"))}


@router.post(ws.API + "/finances/debt/payments", status_code=201)
def add_debt_payment(body: DebtPaymentIn, request: Request):
    client = ws._db()
    me = _lead(request, client)
    _not_future(body.day, "Un remboursement")
    cfg = get_fin_settings(client)
    paid = sum(cents(x.get("amount")) for x in _active(client, T_DEBT, "lecture des remboursements"))
    balance = max(0, cents(cfg["debt_initial"]) - paid)
    amount = cents(body.amount)
    if amount > balance:
        raise HTTPException(status_code=400, detail=f"Le remboursement dépasse le solde restant ({fmt_usd(balance)}).")
    row = {"id": str(uuid.uuid4()), "day": body.day.isoformat(), "amount": usd(amount), "method": body.method,
           "note": body.note.strip(), "created_by": me.username}
    inserted = ws._run(client.table(T_DEBT).insert(row), "enregistrement du remboursement")
    return {"payment": _debt_out(inserted[0] if inserted else row), "balance": usd(balance - amount)}


@router.delete(ws.API + "/finances/debt/payments/{payment_id}")
def cancel_debt_payment(payment_id: str, request: Request):
    client = ws._db()
    me = _lead(request, client)
    return {"payment": _debt_out(_cancel(client, T_DEBT, payment_id, me, "Remboursement"))}


@router.post(ws.API + "/finances/incomes", status_code=201)
def add_income(body: IncomeIn, request: Request):
    """Autre revenu du mois (vente de cours…) : il s'ajoute à la marge."""
    client = ws._db()
    me = _lead(request, client)
    _not_future(body.day, "Un revenu")
    label = " ".join(body.label.split())
    if not label:
        raise HTTPException(status_code=400, detail="Libellé requis.")
    row = {"id": str(uuid.uuid4()), "day": body.day.isoformat(), "label": label, "amount": usd(cents(body.amount)),
           "note": body.note.strip(), "created_by": me.username}
    inserted = ws._run(client.table(T_INCOMES).insert(row), "enregistrement du revenu")
    return {"income": _income_out(inserted[0] if inserted else row)}


@router.delete(ws.API + "/finances/incomes/{income_id}")
def cancel_income(income_id: str, request: Request):
    client = ws._db()
    me = _lead(request, client)
    return {"income": _income_out(_cancel(client, T_INCOMES, income_id, me, "Revenu"))}
