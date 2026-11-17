"""Faux client Supabase pour le mode local (sans base), persistant dans `.data/`.

Il imite le SOUS-ENSEMBLE du client `supabase` réellement employé par l'app :

    client.table(nom)
        .select("*" | "a,b", count="exact")
        .insert(ligne | [lignes]) / .update(champs) / .upsert(lignes, on_conflict="a,b") / .delete()
        .eq .neq .gt .gte .lt .lte .like .ilike .in_ .is_  (et .not_.<filtre>)
        .or_("col.op.valeur,col.op.valeur")
        .order(col, desc=False) .limit(n) .range(debut, fin)
        .execute()  →  objet avec .data (liste) et .count

Il applique aussi ce que fait la base et dont le code dépend : valeurs par
défaut des colonnes, identifiants (uuid, numéros auto-incrémentés), clés
primaires et contraintes d'unicité (une violation lève une exception, comme
PostgREST), `updated_at` tenu à jour, suppressions en cascade ou « set null ».
Une colonne inconnue lève une erreur, comme en production : un oubli dans
`sql/schema.sql` se voit dès les essais en local.

Et il REFUSE ce que PostgREST / PostgreSQL refuseraient (sinon une requête qui
passe en local tombe en 503 le jour de la mise en ligne) :
- une colonne inconnue dans `select`, un filtre, `or_` ou `order` ; un
  opérateur inconnu dans `or_` ;
- NULL dans une colonne `not null`, une valeur du mauvais type (uuid, date,
  horodatage, entier, nombre, booléen, tableau), en écriture comme dans un
  filtre : types et `not null` sont relus dans `sql/*.sql` ;
- `upsert(on_conflict=…)` sans contrainte d'unicité sur ces colonnes ;
- NaN / Infinity (le client `postgrest` ne les envoie pas) ;
- `update` / `delete` sans filtre (Supabase les refuse) ;
- un décalage au-delà du total quand le compte exact est demandé (416).
Il imite aussi le plafond de Supabase : 1 000 lignes au plus par lecture, et
une insertion de plusieurs lignes met NULL (pas la valeur par défaut) dans une
colonne qu'une ligne omet et qu'une autre donne.

Une table = un fichier JSON. Chaque opération prend un verrou (fil d'exécution
et fichier) : le serveur et `manage.py` peuvent tourner en même temps.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional


class LocalAPIError(Exception):
    """Erreur renvoyée comme le ferait PostgREST (contrainte, colonne inconnue…)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid() -> str:
    return str(uuid.uuid4())


NOW = _now
UUID = _uuid


def T(columns: dict, *, pk=("id",), unique=(), identity=(), touch=False, fks=()) -> dict:
    return {"columns": columns, "pk": tuple(pk), "unique": [tuple(u) for u in unique],
            "identity": tuple(identity), "touch": touch, "fks": list(fks)}


# Miroir de sql/schema.sql : colonnes et valeurs par défaut.
SCHEMA: dict[str, dict] = {
    "workspace_accounts": T({
        "username": None, "whatsapp": None, "display_name": "", "password_hash": "", "role": "membre",
        "active": True, "session_version": 1, "failed_logins": 0, "first_failed_at": None,
        "locked_until": None, "last_login_at": None, "created_by": "", "created_at": NOW, "updated_at": NOW,
    }, pk=("username",), unique=[("whatsapp",)], touch=True),
    "workspace_members": T({
        "username": None, "title": "", "color": "#0B6FA4", "capacity_hours": 30, "email": None,
        "daily_digest": True, "github_login": None, "added_by": "", "created_at": NOW, "updated_at": NOW,
    }, pk=("username",), touch=True, fks=[("username", "workspace_accounts", "username", "cascade")]),
    "workspace_settings": T({
        "key": None, "value": dict, "updated_by": "", "updated_at": NOW,
    }, pk=("key",), touch=True),
    "workspace_digest_runs": T({
        "day": None, "triggered_by": "", "started_at": NOW, "finished_at": None, "sent": 0,
        "skipped": 0, "failed": 0, "error": "", "detail": list,
    }, pk=("day",)),
    "workspace_objectives": T({
        "id": UUID, "title": None, "description": "", "period": "", "owner_username": None,
        "status": "en_cours", "start_date": None, "due_date": None, "created_by": "",
        "created_at": NOW, "updated_at": NOW,
    }, touch=True),
    "workspace_key_results": T({
        "id": UUID, "objective_id": None, "title": None, "unit": "", "start_value": 0,
        "current_value": 0, "target_value": 0, "owner_username": None, "position": 0, "auto": "",
        "created_at": NOW, "updated_at": NOW,
    }, touch=True, fks=[("objective_id", "workspace_objectives", "id", "cascade")]),
    "workspace_checkins": T({
        "id": UUID, "key_result_id": None, "value": None, "note": "", "author": "", "created_at": NOW,
    }, fks=[("key_result_id", "workspace_key_results", "id", "cascade")]),
    "workspace_cycles": T({
        "id": UUID, "name": None, "goal": "", "start_date": None, "end_date": None, "created_by": "",
        "created_at": NOW, "updated_at": NOW,
    }, touch=True),
    "workspace_projects": T({
        "id": UUID, "name": None, "description": "", "color": "#0B6FA4", "icon": "", "status": "actif",
        "owner_username": "", "visibility": "equipe", "members": list, "start_date": None,
        "due_date": None, "objective_id": None, "created_by": "", "created_at": NOW, "updated_at": NOW,
    }, touch=True, fks=[("objective_id", "workspace_objectives", "id", "set null")]),
    "workspace_tasks": T({
        "id": UUID, "number": None, "project_id": None, "title": None, "description": "",
        "status": "a_faire", "priority": "normale", "assignee": None, "start_date": None,
        "due_date": None, "labels": list, "estimate_hours": None, "spent_hours": 0, "checklist": list,
        "links": list, "blocked_by": list, "watchers": list, "recurrence": "", "recurrence_next": None,
        "cycle_id": None, "key_result_id": None, "position": 0, "completed_at": None, "archived": False,
        "github": dict, "created_by": "", "created_at": NOW, "updated_at": NOW,
    }, unique=[("number",)], identity=("number",), touch=True, fks=[
        ("project_id", "workspace_projects", "id", "cascade"),
        ("cycle_id", "workspace_cycles", "id", "set null"),
        ("key_result_id", "workspace_key_results", "id", "set null"),
    ]),
    "workspace_comments": T({
        "id": UUID, "task_id": None, "author": None, "body": None, "created_at": NOW, "edited_at": None,
    }, fks=[("task_id", "workspace_tasks", "id", "cascade")]),
    "workspace_activity": T({
        "id": None, "project_id": None, "task_id": None, "actor": "", "action": None, "detail": dict,
        "created_at": NOW,
    }, identity=("id",)),
    "workspace_notifications": T({
        "id": UUID, "recipient": None, "kind": None, "actor": "", "text": "", "project_id": None,
        "task_id": None, "read_at": None, "created_at": NOW,
    }),
    "workspace_docs": T({
        "id": UUID, "project_id": None, "title": None, "body": "", "kind": "note", "pinned": False,
        "author": "", "created_at": NOW, "updated_at": NOW,
    }, touch=True, fks=[("project_id", "workspace_projects", "id", "set null")]),
    "workspace_github_links": T({
        "id": UUID, "task_id": None, "kind": None, "ref": None, "title": "", "url": "", "state": "",
        "author": "", "by_ai": False, "created_at": NOW, "updated_at": NOW,
    }, unique=[("task_id", "kind", "ref")], touch=True, fks=[("task_id", "workspace_tasks", "id", "cascade")]),
    "workspace_github_deliveries": T({
        "id": None, "event": "", "received_at": NOW, "linked": 0, "note": "",
    }),
    "workspace_ai_journal": T({
        "id": UUID, "task_id": None, "body": None, "created_at": NOW,
    }, fks=[("task_id", "workspace_tasks", "id", "cascade")]),
    "workspace_message_templates": T({
        "id": UUID, "name": None, "stage": "", "body": None, "active": True, "position": 0,
        "created_by": "", "created_at": NOW, "updated_at": NOW,
        "lang": "fr", "description": "",  # sql/relances_v2.sql
    }, touch=True),
    "workspace_followups": T({
        "id": UUID, "name": "", "phone": None, "stage": "inscrit", "status": "a_contacter",
        "assignee": None, "next_followup": None, "attempts": 0, "first_contact_at": None,
        "last_contact_at": None, "source": "", "notes": "", "sale_id": None, "created_by": "",
        "created_at": NOW, "updated_at": NOW,
        "lang": "fr",  # sql/relances_v2.sql
    }, unique=[("phone",)], touch=True, fks=[("sale_id", "workspace_sales", "id", "set null")]),
    "workspace_followup_events": T({
        "id": UUID, "followup_id": None, "kind": None, "template_id": None, "template_name": "",
        "note": "", "actor": "", "day": None, "created_at": NOW,
    }, fks=[
        ("followup_id", "workspace_followups", "id", "cascade"),
        ("template_id", "workspace_message_templates", "id", "set null"),
    ]),
    "workspace_sales": T({
        "id": UUID, "username": None, "day": None, "cards": 1, "client_name": "", "client_phone": "",
        "source": "whatsapp", "followup_id": None, "note": "", "origin": "manuel", "external_id": None,
        "created_by": "", "cancelled_at": None, "cancelled_by": "", "created_at": NOW, "updated_at": NOW,
    }, unique=[("external_id",)], touch=True, fks=[("followup_id", "workspace_followups", "id", "set null")]),
    "workspace_daily_notes": T({
        "id": UUID, "username": None, "day": None, "blockers": list, "bugs": "", "note": "",
        "unread_left": None, "created_at": NOW, "updated_at": NOW,
    }, unique=[("username", "day")], touch=True),
}

# ==== FINANCES ==== (miroir de sql/finances.sql)
SCHEMA.update({
    "workspace_fin_expenses": T({
        "id": UUID, "day": None, "category": None, "amount": None, "paid_by": "", "note": "",
        "status": "en_attente", "needs_approval": False, "required": list, "decided_by": "", "decided_at": None,
        "refusal_reason": "", "created_by": "", "created_at": NOW, "updated_at": NOW,
    }, touch=True),
    "workspace_fin_approvals": T({
        "id": UUID, "expense_id": None, "username": None, "created_at": NOW,
    }, unique=[("expense_id", "username")], fks=[("expense_id", "workspace_fin_expenses", "id", "cascade")]),
    "workspace_fin_payouts": T({
        "id": UUID, "username": None, "month": None, "kind": "salaire", "day": None, "amount": None,
        "method": None, "note": "", "created_by": "", "cancelled_at": None, "cancelled_by": "",
        "created_at": NOW, "updated_at": NOW,
    }, touch=True),
    "workspace_fin_debt_payments": T({
        "id": UUID, "day": None, "amount": None, "method": "especes", "note": "", "created_by": "",
        "cancelled_at": None, "cancelled_by": "", "created_at": NOW, "updated_at": NOW,
    }, touch=True),
    "workspace_fin_incomes": T({
        "id": UUID, "day": None, "label": None, "amount": None, "note": "", "created_by": "",
        "cancelled_at": None, "cancelled_by": "", "created_at": NOW, "updated_at": NOW,
    }, touch=True),
})
# ==== FIN FINANCES ====

# Index inverse des clés étrangères : table parente → [(enfant, colonne, colonne parente, action)].
_CHILDREN: dict[str, list[tuple[str, str, str, str]]] = {}
for _child, _spec in SCHEMA.items():
    for _col, _parent, _pcol, _action in _spec["fks"]:
        _CHILDREN.setdefault(_parent, []).append((_child, _col, _pcol, _action))

# Plafond de lignes d'une lecture (Supabase : « Max rows », 1 000 par défaut).
MAX_ROWS = 1000
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SQL_FILES = ("schema.sql", "relances_v2.sql", "finances.sql")
_SQL_TYPE_RE = r"(text\[\]|uuid\[\]|text|uuid|date|timestamptz|integer|bigint|numeric(?:\(\d+,\s*\d+\))?|boolean|jsonb|double precision)"


def read_sql_columns(sql_dir: Path = SQL_DIR) -> dict[str, dict[str, dict]]:
    """Colonnes déclarées dans sql/*.sql : {table: {colonne: {"type", "not_null"}}}.

    Lecture volontairement simple, calée sur la mise en forme de ces fichiers
    (une colonne par ligne). Fichiers absents : {} (rien n'est vérifié).
    """
    tables: dict[str, dict[str, dict]] = {}

    def add(table: str, col: str, typ: str, rest: str) -> None:
        rest = rest.lower()
        tables.setdefault(table, {})[col] = {
            "type": re.sub(r"\(.*\)", "", typ),
            "not_null": "not null" in rest or "primary key" in rest,
        }

    for name in SQL_FILES:
        try:
            text = (sql_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r"create table if not exists public\.(\w+)\s*\((.*?)\n\);", text, re.S):
            for line in m.group(2).splitlines():
                c = re.match(r"\s+(\w+)\s+" + _SQL_TYPE_RE + r"(.*)$", line)
                if c and c.group(1) not in ("check", "unique", "primary", "foreign", "constraint"):
                    add(m.group(1), c.group(1), c.group(2), c.group(3))
        for m in re.finditer(r"alter table public\.(\w+)\s+add column if not exists (\w+)\s+" + _SQL_TYPE_RE + r"([^;]*);", text):
            add(m.group(1), m.group(2), m.group(3), m.group(4))
    return tables


_SQL_COLUMNS = read_sql_columns()
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_TSTZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _valid_scalar(typ: str, value: Any) -> bool:
    """La valeur passerait-elle la conversion de PostgreSQL vers ce type ?"""
    if typ == "jsonb":
        return True
    if isinstance(value, (list, dict)):
        return False
    if typ == "text":
        return True
    if typ == "boolean":
        return isinstance(value, bool) or str(value).lower() in ("true", "false", "t", "f")
    if isinstance(value, bool):
        return False
    text = str(value).strip()
    if typ == "uuid":
        return bool(_UUID_RE.match(text))
    if typ in ("date", "timestamptz"):
        # Une date seule vaut minuit ; un horodatage dans une colonne date garde le jour.
        if not _TSTZ_RE.match(text):
            return False
        try:
            date.fromisoformat(text[:10])
        except ValueError:
            return False
        return True
    if typ in ("integer", "bigint"):
        return isinstance(value, int) or (isinstance(value, str) and bool(_INT_RE.match(text)))
    if typ in ("numeric", "double precision"):
        return isinstance(value, (int, float)) or bool(_NUM_RE.match(text))
    return True


def _check_value(table: str, col: str, value: Any, where: str) -> None:
    spec = _SQL_COLUMNS.get(table, {}).get(col)
    if spec is None or value is None:
        return
    typ = spec["type"]
    if typ.endswith("[]"):
        ok = isinstance(value, list) and all(v is None or _valid_scalar(typ[:-2], v) for v in value)
    else:
        ok = _valid_scalar(typ, value)
    if not ok:
        raise LocalAPIError(f'invalid input syntax for type {typ} ({where} « {table}.{col} ») : {str(value)[:40]!r}')


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _as_dt(text: str) -> Optional[datetime]:
    if not isinstance(text, str) or not _TS_RE.match(text):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce(row_value: Any, value: Any) -> Any:
    """Aligne le type d'une valeur de filtre (souvent une chaîne) sur la colonne."""
    if isinstance(value, str):
        if isinstance(row_value, bool):
            return value.lower() == "true"
        if isinstance(row_value, (int, float)) and not isinstance(row_value, bool):
            try:
                return float(value)
            except ValueError:
                return value
    return value


def _cmp(a: Any, b: Any) -> int:
    b = _coerce(a, b)
    da, db = _as_dt(a), _as_dt(b)
    if da is not None and db is not None:
        a, b = da, db
    try:
        return (a > b) - (a < b)
    except TypeError:
        a, b = str(a), str(b)
        return (a > b) - (a < b)


def _like(pattern: str, value: Any, insensitive: bool) -> bool:
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in str(pattern))
    return re.fullmatch(regex, str(value), flags=re.S | (re.I if insensitive else 0)) is not None


def _match_one(row: dict, col: str, op: str, value: Any) -> Optional[bool]:
    """Vrai, faux, ou None (« inconnu » au sens SQL : comparaison avec NULL)."""
    current = row.get(col)
    if op == "is":
        v = str(value).lower()
        if v == "null":
            return current is None
        if v in ("true", "false"):
            return current is (v == "true")
        raise LocalAPIError(f"is.{value} non pris en charge")
    if current is None:
        return None
    if op == "eq":
        return _cmp(current, value) == 0
    if op == "neq":
        return _cmp(current, value) != 0
    if op == "gt":
        return _cmp(current, value) > 0
    if op == "gte":
        return _cmp(current, value) >= 0
    if op == "lt":
        return _cmp(current, value) < 0
    if op == "lte":
        return _cmp(current, value) <= 0
    if op == "in":
        return any(_cmp(current, v) == 0 for v in value)
    if op == "like":
        return _like(value, current, False)
    if op == "ilike":
        return _like(value, current, True)
    raise LocalAPIError(f"opérateur {op} non pris en charge")


def _parse_or(expr: str) -> list[tuple[bool, str, str, Any]]:
    """`a.eq.x,b.lte.y,c.is.null,d.in.(1,2)` → filtres (sans imbrication)."""
    parts, depth, cur = [], 0, ""
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    out = []
    for part in parts:
        bits = part.strip().split(".", 2)
        neg = False
        if len(bits) == 3 and bits[1] == "not":
            neg = True
            col, rest = bits[0], bits[2]
            op, _, value = rest.partition(".")
        elif len(bits) == 3:
            col, op, value = bits
        else:
            raise LocalAPIError(f"filtre or invalide : {part[:40]}")
        if op not in _OPS:
            raise LocalAPIError(f"filtre or : opérateur inconnu « {op} » (PGRST100)")
        if op == "in":
            value = [v.strip().strip('"') for v in value.strip("()").split(",") if v.strip()]
        out.append((neg, col, op, value))
    return out


_OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "in", "is", "like", "ilike")


class Result:
    def __init__(self, data: list, count: Optional[int] = None):
        self.data = data
        self.count = count


class _Not:
    def __init__(self, query: "Query"):
        self._q = query

    def __getattr__(self, name: str):
        method = getattr(self._q, name)

        def negated(*args, **kwargs):
            self._q._negate = True
            return method(*args, **kwargs)

        return negated


class Query:
    def __init__(self, store: "LocalStore", table: str):
        if table not in SCHEMA:
            raise LocalAPIError(f'relation "public.{table}" does not exist')
        self._store, self._table = store, table
        self._op = "select"
        self._columns: Optional[list[str]] = None
        self._payload: list[dict] = []
        self._filters: list = []
        self._orders: list[tuple[str, bool]] = []
        self._limit: Optional[int] = None
        self._offset = 0
        self._count: Optional[str] = None
        self._on_conflict: tuple[str, ...] = ()
        self._ignore_duplicates = False
        self._negate = False

    # ── opérations ──────────────────────────────────────────────
    def _known(self, col: str) -> str:
        if col not in SCHEMA[self._table]["columns"]:
            raise LocalAPIError(f"column {self._table}.{col} does not exist (42703)")
        return col

    def select(self, *columns: str, count: Optional[str] = None, **_kw) -> "Query":
        cols = ",".join(columns or ("*",))
        names = [c.strip() for c in cols.split(",") if c.strip()]
        self._columns = None if "*" in names else [self._known(c) for c in names]
        self._count = str(count).split(".")[-1].lower() if count else None
        return self

    @staticmethod
    def _rows(json_: Any) -> list[dict]:
        """Lignes à écrire, comme les envoie le client `postgrest`.

        NaN et Infinity sont refusés (le client lève une erreur avant l'envoi).
        Plusieurs lignes : le client ajoute `?columns=` (toutes les clés vues) et
        PostgREST met NULL, pas la valeur par défaut, dans une colonne qu'une
        ligne omet.
        """
        rows = [_jsonable(r) for r in (json_ if isinstance(json_, list) else [json_])]
        json.dumps(rows, allow_nan=False)
        if isinstance(json_, list) and len(rows) > 1:
            keys = {k for r in rows for k in r}
            rows = [{k: r.get(k) for k in keys} for r in rows]
        return rows

    def insert(self, json_: Any, **_kw) -> "Query":
        self._op = "insert"
        self._payload = self._rows(json_)
        return self

    def update(self, json_: dict, **_kw) -> "Query":
        self._op = "update"
        self._payload = self._rows(json_)
        return self

    def upsert(self, json_: Any, on_conflict: str = "", ignore_duplicates: bool = False, **_kw) -> "Query":
        self._op = "upsert"
        self._payload = self._rows(json_)
        self._on_conflict = tuple(c.strip() for c in on_conflict.split(",") if c.strip())
        self._ignore_duplicates = ignore_duplicates
        spec = SCHEMA[self._table]
        if self._on_conflict and set(self._on_conflict) not in [set(k) for k in [spec["pk"]] + spec["unique"]]:
            raise LocalAPIError("there is no unique or exclusion constraint matching the ON CONFLICT specification (42P10)")
        return self

    def delete(self, **_kw) -> "Query":
        self._op = "delete"
        return self

    # ── filtres ─────────────────────────────────────────────────
    @property
    def not_(self) -> _Not:
        return _Not(self)

    def _add(self, col: str, op: str, value: Any) -> "Query":
        self._check_filter(col, op, value)
        self._filters.append((self._negate, col, op, _jsonable(value)))
        self._negate = False
        return self

    def _check_filter(self, col: str, op: str, value: Any) -> None:
        """Colonne connue et valeur convertible vers son type (sinon PostgREST répond 400)."""
        self._known(col)
        if op in ("is", "like", "ilike"):
            return
        values = value if op == "in" else [value]
        for v in values:
            if v is not None and not isinstance(v, bool):
                v = str(v)  # le client envoie tout sous forme de texte
            _check_value(self._table, col, v, "filtre sur")

    def eq(self, col, value):
        return self._add(col, "eq", value)

    def neq(self, col, value):
        return self._add(col, "neq", value)

    def gt(self, col, value):
        return self._add(col, "gt", value)

    def gte(self, col, value):
        return self._add(col, "gte", value)

    def lt(self, col, value):
        return self._add(col, "lt", value)

    def lte(self, col, value):
        return self._add(col, "lte", value)

    def like(self, col, value):
        return self._add(col, "like", value)

    def ilike(self, col, value):
        return self._add(col, "ilike", value)

    def in_(self, col, values):
        return self._add(col, "in", list(values))

    def is_(self, col, value):
        return self._add(col, "is", value if value is not None else "null")

    def or_(self, expr: str, **_kw) -> "Query":
        parsed = _parse_or(expr)
        for _neg, col, op, value in parsed:
            self._check_filter(col, op, value)
        self._filters.append(("or", parsed))
        return self

    def order(self, col: str, desc: bool = False, **_kw) -> "Query":
        self._orders.append((self._known(col), bool(desc)))
        return self

    def limit(self, n: int, **_kw) -> "Query":
        self._limit = int(n)
        return self

    def range(self, start: int, end: int, **_kw) -> "Query":
        self._offset, self._limit = int(start), int(end) - int(start) + 1
        return self

    def offset(self, n: int) -> "Query":
        self._offset = int(n)
        return self

    # ── exécution ───────────────────────────────────────────────
    def _matches(self, row: dict) -> bool:
        for f in self._filters:
            if f[0] == "or":
                hit = False
                for neg, c, o, v in f[1]:
                    r = _match_one(row, c, o, v)
                    if r is not None and r != neg:
                        hit = True
                        break
                if not hit:
                    return False
                continue
            neg, col, op, value = f
            result = _match_one(row, col, op, value)
            if result is None:
                return False
            if result == neg:
                return False
        return True

    def _project(self, row: dict) -> dict:
        if self._columns is None:
            return copy.deepcopy(row)
        return {c: copy.deepcopy(row.get(c)) for c in self._columns}

    def execute(self) -> Result:
        with self._store.locked():
            return getattr(self, "_do_" + self._op)()

    def _check_columns(self, row: dict) -> None:
        cols = SCHEMA[self._table]["columns"]
        unknown = [k for k in row if k not in cols]
        if unknown:
            raise LocalAPIError(f"Could not find the '{unknown[0]}' column of '{self._table}' in the schema cache")

    def _check_row(self, row: dict, given: dict) -> None:
        """Types des valeurs écrites, et NOT NULL (colonnes de sql/*.sql)."""
        for col, value in given.items():
            _check_value(self._table, col, value, "écriture dans")
        for col, spec in _SQL_COLUMNS.get(self._table, {}).items():
            if spec["not_null"] and col in row and row[col] is None:
                raise LocalAPIError(f'null value in column "{col}" of relation "{self._table}" violates not-null constraint (23502)')

    def _do_select(self) -> Result:
        rows = [r for r in self._store.rows(self._table) if self._matches(r)]
        for col, desc in reversed(self._orders):
            present = [r for r in rows if r.get(col) is not None]
            missing = [r for r in rows if r.get(col) is None]
            present.sort(key=lambda r: _SortKey(r.get(col)), reverse=desc)
            # PostgreSQL : NULL en dernier en ordre croissant, en premier en décroissant.
            rows = missing + present if desc else present + missing
        total = len(rows)
        if self._count and self._offset > total:
            # PostgREST : compte exact demandé et décalage au-delà du total → 416.
            raise LocalAPIError(f"Requested range not satisfiable : offset {self._offset}, {total} lignes (PGRST103)")
        rows = rows[self._offset:]
        rows = rows[: MAX_ROWS if self._limit is None else min(self._limit, MAX_ROWS)]
        return Result([self._project(r) for r in rows], total if self._count else None)

    def _do_insert(self) -> Result:
        spec = SCHEMA[self._table]
        rows = self._store.rows(self._table)
        new = []
        for given in self._payload:
            self._check_columns(given)
            row = {}
            for col, default in spec["columns"].items():
                if col in given:
                    row[col] = given[col]
                elif col in spec["identity"]:
                    row[col] = self._store.next_id(self._table, col)
                else:
                    row[col] = default() if callable(default) else copy.deepcopy(default)
            for col in spec["identity"]:
                if col in given and isinstance(given[col], int):
                    self._store.bump_id(self._table, col, given[col])
            self._check_row(row, given)
            self._check_unique(row, rows + new)
            new.append(row)
        rows.extend(new)
        self._store.save(self._table)
        return Result([copy.deepcopy(r) for r in new])

    def _check_unique(self, row: dict, others: list[dict], skip: Optional[dict] = None) -> None:
        spec = SCHEMA[self._table]
        for key in [spec["pk"]] + spec["unique"]:
            values = tuple(row.get(c) for c in key)
            if any(v is None for v in values):
                if key == spec["pk"]:
                    raise LocalAPIError(f'null value in column "{key[0]}" of relation "{self._table}"')
                continue
            for other in others:
                if other is skip or other is row:
                    continue
                if tuple(other.get(c) for c in key) == values:
                    raise LocalAPIError(
                        f'duplicate key value violates unique constraint "{self._table}_{"_".join(key)}_key"'
                    )

    def _require_filter(self) -> None:
        # Supabase (pg-safeupdate) : « UPDATE/DELETE requires a WHERE clause ».
        if not self._filters:
            raise LocalAPIError(f"{self._op.upper()} requires a WHERE clause (21000)")

    def _do_update(self) -> Result:
        spec = SCHEMA[self._table]
        changes = self._payload[0]
        self._check_columns(changes)
        self._require_filter()
        self._check_row(changes, changes)
        rows = self._store.rows(self._table)
        out = []
        for row in rows:
            if not self._matches(row):
                continue
            before = dict(row)
            row.update(copy.deepcopy(changes))
            if spec["touch"] and "updated_at" not in changes:
                row["updated_at"] = _now()
            try:
                self._check_unique(row, rows, skip=row)
            except LocalAPIError:
                row.clear()
                row.update(before)
                raise
            out.append(copy.deepcopy(row))
        if out:
            self._store.save(self._table)
        return Result(out)

    def _do_upsert(self) -> Result:
        spec = SCHEMA[self._table]
        key = self._on_conflict or spec["pk"]
        rows = self._store.rows(self._table)
        out = []
        for given in self._payload:
            self._check_columns(given)
            values = tuple(given.get(c) for c in key)
            existing = next((r for r in rows if tuple(r.get(c) for c in key) == values), None)
            if existing is not None:
                if self._ignore_duplicates:
                    continue
                self._check_row(given, given)
                existing.update(copy.deepcopy(given))
                if spec["touch"] and "updated_at" not in given:
                    existing["updated_at"] = _now()
                out.append(copy.deepcopy(existing))
            else:
                sub = Query(self._store, self._table).insert(given)
                out.extend(sub._do_insert().data)
                rows = self._store.rows(self._table)
        self._store.save(self._table)
        return Result(out)

    def _do_delete(self) -> Result:
        self._require_filter()
        rows = self._store.rows(self._table)
        gone = [r for r in rows if self._matches(r)]
        if not gone:
            return Result([])
        keep = [r for r in rows if not any(r is g for g in gone)]
        self._store.replace(self._table, keep)
        self._cascade(self._table, gone)
        return Result([copy.deepcopy(r) for r in gone])

    def _cascade(self, table: str, gone: list[dict]) -> None:
        for child, col, pcol, action in _CHILDREN.get(table, []):
            ids = {g.get(pcol) for g in gone}
            rows = self._store.rows(child)
            hit = [r for r in rows if r.get(col) in ids]
            if not hit:
                continue
            if action == "cascade":
                self._store.replace(child, [r for r in rows if r.get(col) not in ids])
                self._cascade(child, hit)
            else:
                for r in hit:
                    r[col] = None
                self._store.save(child)


class _SortKey:
    __slots__ = ("v",)

    def __init__(self, v: Any):
        self.v = v

    def __lt__(self, other: "_SortKey") -> bool:
        return _cmp(self.v, other.v) < 0


class LocalStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, tuple[tuple, list]] = {}
        self._meta: Optional[dict] = None
        self._meta_stamp: tuple = ()
        self._rlock = threading.RLock()
        self._depth = 0

    @contextmanager
    def locked(self):
        with self._rlock:
            self._depth += 1
            try:
                if self._depth == 1:
                    with _file_lock(self.dir / ".lock"):
                        yield
                else:
                    yield
            finally:
                self._depth -= 1

    def _path(self, table: str) -> Path:
        return self.dir / f"{table}.json"

    @staticmethod
    def _stamp(path: Path) -> tuple:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return ()

    def rows(self, table: str) -> list[dict]:
        path = self._path(table)
        stamp = self._stamp(path)
        cached = self._cache.get(table)
        if cached and cached[0] == stamp:
            return cached[1]
        data: list = []
        if stamp:
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "[]")
            except (OSError, ValueError) as exc:
                raise LocalAPIError(f"lecture de {path.name} impossible ({type(exc).__name__})")
        self._cache[table] = (stamp, data)
        return data

    def replace(self, table: str, rows: list[dict]) -> None:
        self._cache[table] = (self._cache.get(table, ((),))[0], rows)
        self.save(table)

    def save(self, table: str) -> None:
        rows = self._cache[table][1]
        path = self._path(table)
        _atomic_write(path, json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
        self._cache[table] = (self._stamp(path), rows)

    def _load_meta(self) -> dict:
        path = self.dir / "_meta.json"
        stamp = self._stamp(path)
        if self._meta is None or stamp != self._meta_stamp:
            try:
                self._meta = json.loads(path.read_text(encoding="utf-8")) if stamp else {}
            except (OSError, ValueError):
                self._meta = {}
            self._meta_stamp = stamp
        return self._meta

    def _save_meta(self) -> None:
        path = self.dir / "_meta.json"
        _atomic_write(path, json.dumps(self._meta))
        self._meta_stamp = self._stamp(path)

    def next_id(self, table: str, col: str) -> int:
        meta = self._load_meta()
        key = f"{table}.{col}"
        if key not in meta:
            meta[key] = max([int(r.get(col) or 0) for r in self.rows(table)] + [0])
        meta[key] += 1
        self._save_meta()
        return meta[key]

    def bump_id(self, table: str, col: str, value: int) -> None:
        meta = self._load_meta()
        key = f"{table}.{col}"
        if value > meta.get(key, 0):
            meta[key] = value
            self._save_meta()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows : un antivirus ou un lecteur garde parfois le fichier un instant.
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


@contextmanager
def _file_lock(path: Path):
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


class LocalClient:
    """Le point d'entrée : `LocalClient(dossier).table("workspace_tasks")…`."""

    def __init__(self, data_dir: Path):
        self.store = LocalStore(data_dir)

    def table(self, name: str) -> Query:
        return Query(self.store, name)

    from_ = table
