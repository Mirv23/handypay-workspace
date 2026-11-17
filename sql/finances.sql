-- ═══════════════════════════════════════════════════════════════════
--  HandyPay · Espace d'équipe — module Finances (app/finances.py)
--
--  À exécuter APRÈS sql/schema.sql (il réutilise public.workspace_touch()),
--  dans Supabase → SQL Editor → Run. Ré-exécutable : chaque objet n'est créé
--  que s'il manque ; rien n'est effacé.
--
--  Accès : comme le reste de l'espace, RLS activée ET forcée sur chaque table,
--  AUCUNE policy, aucun droit pour `anon` et `authenticated`. Seul le serveur
--  (clé service_role) lit et écrit ; les droits (responsable, membre ;
--  observateur exclu) sont décidés par le code.
--
--  Les réglages (charges fixes, salaires, dette, réserve, primes, seuil
--  d'accord) vivent dans workspace_settings, clé « finances ».
--  Rien n'est supprimé par l'application : annuler pose `cancelled_at`.
-- ═══════════════════════════════════════════════════════════════════

-- Dépenses réelles. Un membre PROPOSE ; au-dessus du seuil d'accord,
-- `required` liste les personnes payées dont l'accord est attendu (figée à la
-- proposition) ; la dépense passe « validee » quand toutes ont approuvé.
create table if not exists public.workspace_fin_expenses (
    id uuid primary key default gen_random_uuid(),
    day date not null,
    category text not null check (char_length(category) between 1 and 40),
    amount numeric(12,2) not null check (amount > 0 and amount <= 1000000),
    paid_by text not null default '' check (char_length(paid_by) <= 40),
    note text not null default '' check (char_length(note) <= 500),
    status text not null default 'en_attente' check (status in ('en_attente', 'validee', 'refusee')),
    needs_approval boolean not null default false,
    required text[] not null default '{}',
    decided_by text not null default '' check (char_length(decided_by) <= 40),
    decided_at timestamptz,
    refusal_reason text not null default '' check (char_length(refusal_reason) <= 300),
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Un « J'approuve » par personne et par dépense.
create table if not exists public.workspace_fin_approvals (
    id uuid primary key default gen_random_uuid(),
    expense_id uuid not null references public.workspace_fin_expenses(id) on delete cascade,
    username text not null,
    created_at timestamptz not null default now(),
    unique (expense_id, username)
);

-- Versements : salaire ou prime versé à une personne pour un mois (AAAA-MM).
create table if not exists public.workspace_fin_payouts (
    id uuid primary key default gen_random_uuid(),
    username text not null,
    month text not null check (month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    kind text not null default 'salaire' check (kind in ('salaire', 'prime')),
    day date not null,
    amount numeric(12,2) not null check (amount > 0 and amount <= 1000000),
    method text not null check (method in ('moncash', 'natcash', 'handypay', 'especes')),
    note text not null default '' check (char_length(note) <= 300),
    created_by text not null default '',
    cancelled_at timestamptz,
    cancelled_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Remboursements de la dette fournisseur. Solde = solde initial (réglage) −
-- somme des remboursements non annulés.
create table if not exists public.workspace_fin_debt_payments (
    id uuid primary key default gen_random_uuid(),
    day date not null,
    amount numeric(12,2) not null check (amount > 0 and amount <= 10000000),
    method text not null default 'especes' check (method in ('moncash', 'natcash', 'handypay', 'especes')),
    note text not null default '' check (char_length(note) <= 300),
    created_by text not null default '',
    cancelled_at timestamptz,
    cancelled_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Autres revenus (vente de cours…) : ils s'ajoutent à la marge du mois.
create table if not exists public.workspace_fin_incomes (
    id uuid primary key default gen_random_uuid(),
    day date not null,
    label text not null check (char_length(label) between 1 and 80),
    amount numeric(12,2) not null check (amount > 0 and amount <= 10000000),
    note text not null default '' check (char_length(note) <= 300),
    created_by text not null default '',
    cancelled_at timestamptz,
    cancelled_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ─── Index ──────────────────────────────────────────────────────────

create index if not exists workspace_fin_expenses_day_idx on public.workspace_fin_expenses (day desc);
create index if not exists workspace_fin_expenses_pending_idx on public.workspace_fin_expenses (status) where status = 'en_attente';
create index if not exists workspace_fin_approvals_expense_idx on public.workspace_fin_approvals (expense_id);
create index if not exists workspace_fin_payouts_month_idx on public.workspace_fin_payouts (month, username) where cancelled_at is null;
create index if not exists workspace_fin_debt_payments_day_idx on public.workspace_fin_debt_payments (day) where cancelled_at is null;
create index if not exists workspace_fin_incomes_day_idx on public.workspace_fin_incomes (day) where cancelled_at is null;

-- ─── RLS forcée, aucun accès pour anon / authenticated ──────────────

do $$
declare t text;
begin
  foreach t in array array[
    'workspace_fin_expenses', 'workspace_fin_approvals', 'workspace_fin_payouts',
    'workspace_fin_debt_payments', 'workspace_fin_incomes'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
    execute format('revoke all on table public.%I from anon, authenticated', t);
  end loop;
end $$;

-- ─── `updated_at` tenu par la base (fonction créée par schema.sql) ──

do $$
declare t text;
begin
  foreach t in array array[
    'workspace_fin_expenses', 'workspace_fin_payouts', 'workspace_fin_debt_payments', 'workspace_fin_incomes'
  ] loop
    execute format('drop trigger if exists %I on public.%I', t || '_touch', t);
    execute format('create trigger %I before update on public.%I for each row execute function public.workspace_touch()', t || '_touch', t);
  end loop;
end $$;

comment on table public.workspace_fin_expenses is
  'Dépenses réelles de HandyPay (proposées, approuvées, validées ou refusées). Accès service_role uniquement.';
comment on table public.workspace_fin_payouts is
  'Salaires et primes versés, par personne et par mois. Accès service_role uniquement.';
comment on table public.workspace_fin_debt_payments is
  'Remboursements de la dette fournisseur. Accès service_role uniquement.';
