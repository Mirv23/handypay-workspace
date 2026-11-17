-- ═══════════════════════════════════════════════════════════════════
--  HandyPay · Espace d'équipe — schéma complet pour une base Supabase NEUVE
--
--  À coller dans Supabase → SQL Editor → Run. Ré-exécutable : chaque objet
--  n'est créé que s'il manque ; rien n'est effacé.
--
--  Accès : RLS activée ET forcée sur chaque table, AUCUNE policy, et aucun
--  droit pour les rôles `anon` et `authenticated`. Seul le serveur (clé
--  service_role, derrière la session de l'espace) lit et écrit. Les droits
--  (responsable, membre, observateur ; projets restreints) sont décidés par
--  le code (app/workspace.py) à partir de workspace_accounts.
--
--  Données personnelles : les numéros de téléphone des clients vivent dans
--  workspace_followups et workspace_sales. Ne crée JAMAIS de policy qui les
--  ouvrirait à la clé publique (anon).
-- ═══════════════════════════════════════════════════════════════════

create extension if not exists pgcrypto;  -- gen_random_uuid()

-- ─── Comptes et membres ─────────────────────────────────────────────

-- Un compte = une personne. Connexion par `username` ou par `whatsapp`.
-- `password_hash` : scrypt salé (jamais le mot de passe). `session_version`
-- augmente quand le mot de passe change : les sessions ouvertes tombent.
create table if not exists public.workspace_accounts (
    username text primary key check (username ~ '^[a-z0-9][a-z0-9_.\-]{1,39}$'),
    whatsapp text unique check (whatsapp is null or whatsapp ~ '^\+[0-9]{8,15}$'),
    display_name text not null default '' check (char_length(display_name) <= 60),
    password_hash text not null default '' check (char_length(password_hash) <= 300),
    role text not null default 'membre' check (role in ('responsable', 'membre', 'observateur')),
    active boolean not null default true,
    session_version integer not null default 1,
    failed_logins integer not null default 0,
    first_failed_at timestamptz,
    locked_until timestamptz,
    last_login_at timestamptz,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- La fiche d'équipe d'un compte : poste, couleur, disponibilité, adresse de
-- l'e-mail du matin, pseudo GitHub.
create table if not exists public.workspace_members (
    username text primary key references public.workspace_accounts(username) on delete cascade,
    title text not null default '' check (char_length(title) <= 60),
    color text not null default '#0B6FA4' check (color ~ '^#[0-9A-Fa-f]{6}$'),
    capacity_hours numeric(5,1) not null default 30 check (capacity_hours between 0 and 80),
    email text check (email is null or char_length(email) <= 254),
    daily_digest boolean not null default true,
    github_login text check (github_login is null or github_login ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}$'),
    added_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Réglages modifiables par un responsable : « sales » (objectifs, dates,
-- jalons, marge), « followups » (séquence de relance), etc.
create table if not exists public.workspace_settings (
    key text primary key check (char_length(key) between 1 and 60),
    value jsonb not null default '{}'::jsonb,
    updated_by text not null default '',
    updated_at timestamptz not null default now()
);

-- Une ligne par jour d'e-mail du matin : la clé primaire empêche deux envois
-- le même jour (redémarrage, deux instances, clic pendant le passage auto).
create table if not exists public.workspace_digest_runs (
    day date primary key,
    triggered_by text not null default '',
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    sent integer not null default 0,
    skipped integer not null default 0,
    failed integer not null default 0,
    error text not null default '' check (char_length(error) <= 300),
    detail jsonb not null default '[]'::jsonb
);

-- ─── Objectifs, cycles, projets, tâches ─────────────────────────────

create table if not exists public.workspace_objectives (
    id uuid primary key default gen_random_uuid(),
    title text not null check (char_length(title) between 1 and 160),
    description text not null default '' check (char_length(description) <= 4000),
    period text not null default '' check (char_length(period) <= 40),
    owner_username text,
    status text not null default 'en_cours' check (status in ('en_cours', 'atteint', 'abandonne')),
    start_date date,
    due_date date,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- `auto` : le résultat clé suit tout seul les ventes (cumul des cartes ou de
-- la marge sur la campagne) ; vide = points d'étape à la main.
create table if not exists public.workspace_key_results (
    id uuid primary key default gen_random_uuid(),
    objective_id uuid not null references public.workspace_objectives(id) on delete cascade,
    title text not null check (char_length(title) between 1 and 160),
    unit text not null default '' check (char_length(unit) <= 20),
    start_value numeric(14,2) not null default 0,
    current_value numeric(14,2) not null default 0,
    target_value numeric(14,2) not null default 0,
    owner_username text,
    position integer not null default 0,
    auto text not null default '' check (auto in ('', 'ventes_cumul', 'marge_cumul')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.workspace_checkins (
    id uuid primary key default gen_random_uuid(),
    key_result_id uuid not null references public.workspace_key_results(id) on delete cascade,
    value numeric(14,2) not null,
    note text not null default '' check (char_length(note) <= 500),
    author text not null default '',
    created_at timestamptz not null default now()
);

create table if not exists public.workspace_cycles (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 1 and 80),
    goal text not null default '' check (char_length(goal) <= 500),
    start_date date not null,
    end_date date not null,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (end_date >= start_date)
);

-- `restreint` : visible des seuls membres listés, de son responsable et des
-- responsables de l'espace.
create table if not exists public.workspace_projects (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 1 and 80),
    description text not null default '' check (char_length(description) <= 4000),
    color text not null default '#0B6FA4' check (color ~ '^#[0-9A-Fa-f]{6}$'),
    icon text not null default '' check (char_length(icon) <= 8),
    status text not null default 'actif' check (status in ('actif', 'pause', 'termine', 'archive')),
    owner_username text not null default '',
    visibility text not null default 'equipe' check (visibility in ('equipe', 'restreint')),
    members text[] not null default '{}',
    start_date date,
    due_date date,
    objective_id uuid references public.workspace_objectives(id) on delete set null,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- `number` donne la clé courte « HP-42 » (commits, branches, PR, à l'oral).
create table if not exists public.workspace_tasks (
    id uuid primary key default gen_random_uuid(),
    number bigint generated by default as identity,
    project_id uuid not null references public.workspace_projects(id) on delete cascade,
    title text not null check (char_length(title) between 1 and 200),
    description text not null default '' check (char_length(description) <= 20000),
    status text not null default 'a_faire' check (status in ('a_faire', 'en_cours', 'en_revue', 'bloque', 'fait')),
    priority text not null default 'normale' check (priority in ('urgente', 'haute', 'normale', 'basse')),
    assignee text,
    start_date date,
    due_date date,
    labels text[] not null default '{}',
    estimate_hours numeric(6,1) check (estimate_hours is null or estimate_hours between 0 and 999),
    spent_hours numeric(7,1) not null default 0 check (spent_hours between 0 and 9999),
    checklist jsonb not null default '[]'::jsonb,
    links jsonb not null default '[]'::jsonb,
    blocked_by uuid[] not null default '{}',
    watchers text[] not null default '{}',
    recurrence text not null default '' check (recurrence in ('', 'quotidienne', 'hebdomadaire', 'mensuelle')),
    recurrence_next uuid,
    cycle_id uuid references public.workspace_cycles(id) on delete set null,
    key_result_id uuid references public.workspace_key_results(id) on delete set null,
    position double precision not null default 0,
    completed_at timestamptz,
    archived boolean not null default false,
    github jsonb not null default '{}'::jsonb,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.workspace_comments (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.workspace_tasks(id) on delete cascade,
    author text not null,
    body text not null check (char_length(body) between 1 and 5000),
    created_at timestamptz not null default now(),
    edited_at timestamptz
);

-- Journal d'activité : qui a changé quoi (jamais de numéro de client ici).
create table if not exists public.workspace_activity (
    id bigint generated by default as identity primary key,
    project_id uuid,
    task_id uuid,
    actor text not null default '',
    action text not null check (char_length(action) <= 40),
    detail jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.workspace_notifications (
    id uuid primary key default gen_random_uuid(),
    recipient text not null,
    kind text not null check (char_length(kind) <= 30),
    actor text not null default '',
    text text not null default '' check (char_length(text) <= 300),
    project_id uuid,
    task_id uuid,
    read_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists public.workspace_docs (
    id uuid primary key default gen_random_uuid(),
    project_id uuid references public.workspace_projects(id) on delete set null,
    title text not null check (char_length(title) between 1 and 160),
    body text not null default '' check (char_length(body) <= 100000),
    kind text not null default 'note' check (kind in ('note', 'reunion', 'decision', 'guide')),
    pinned boolean not null default false,
    author text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ─── GitHub et l'IA (app/connect.py) ────────────────────────────────

create table if not exists public.workspace_github_links (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.workspace_tasks(id) on delete cascade,
    kind text not null check (kind in ('commit', 'pr')),
    ref text not null check (char_length(ref) between 1 and 64),
    title text not null default '' check (char_length(title) <= 200),
    url text not null default '' check (char_length(url) <= 500),
    state text not null default '' check (state in ('', 'ouverte', 'brouillon', 'fusionnee', 'fermee')),
    author text not null default '' check (char_length(author) <= 80),
    by_ai boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (task_id, kind, ref)
);

create table if not exists public.workspace_github_deliveries (
    id text primary key check (char_length(id) between 1 and 80),
    event text not null default '' check (char_length(event) <= 40),
    received_at timestamptz not null default now(),
    linked integer not null default 0,
    note text not null default '' check (char_length(note) <= 200)
);

create table if not exists public.workspace_ai_journal (
    id uuid primary key default gen_random_uuid(),
    task_id uuid not null references public.workspace_tasks(id) on delete cascade,
    body text not null check (char_length(body) between 1 and 2000),
    created_at timestamptz not null default now()
);

-- ─── Relances WhatsApp ──────────────────────────────────────────────

-- Modèles de message ; `stage` vide = toutes les étapes.
create table if not exists public.workspace_message_templates (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 1 and 80),
    stage text not null default '' check (stage in ('', 'inscrit', 'kyc', 'achat_commence', 'a_paye', 'carte_emise', 'carte_utilisee', 'a_rachete')),
    body text not null check (char_length(body) between 1 and 1000),
    active boolean not null default true,
    position integer not null default 0,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Une fiche = un client à relancer ; le téléphone (normalisé +509…) est unique.
create table if not exists public.workspace_followups (
    id uuid primary key default gen_random_uuid(),
    name text not null default '' check (char_length(name) <= 120),
    phone text not null unique check (phone ~ '^\+[0-9]{8,15}$'),
    stage text not null default 'inscrit' check (stage in ('inscrit', 'kyc', 'achat_commence', 'a_paye', 'carte_emise', 'carte_utilisee', 'a_rachete')),
    status text not null default 'a_contacter' check (status in ('a_contacter', 'contacte', 'a_repondu', 'close', 'perdu', 'pas_interesse', 'froid')),
    assignee text,
    next_followup date,
    attempts integer not null default 0 check (attempts >= 0),
    first_contact_at timestamptz,
    last_contact_at timestamptz,
    source text not null default '' check (char_length(source) <= 40),
    notes text not null default '' check (char_length(notes) <= 2000),
    sale_id uuid,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ─── Ventes ─────────────────────────────────────────────────────────

-- Une vente = des cartes closées par une personne, un jour (Port-au-Prince).
-- `username` vide : vente HandyPay sans vendeur (« direct »). `external_id`
-- unique : une vente envoyée deux fois par HandyPay ne compte qu'une fois.
-- Annuler ne supprime rien : `cancelled_at` est posé.
create table if not exists public.workspace_sales (
    id uuid primary key default gen_random_uuid(),
    username text,
    day date not null,
    cards integer not null default 1 check (cards between 1 and 500),
    client_name text not null default '' check (char_length(client_name) <= 120),
    client_phone text not null default '' check (client_phone = '' or client_phone ~ '^\+[0-9]{8,15}$'),
    source text not null default 'whatsapp' check (source in ('whatsapp', 'appel', 'pub', 'parrainage', 'autre')),
    followup_id uuid references public.workspace_followups(id) on delete set null,
    note text not null default '' check (char_length(note) <= 500),
    origin text not null default 'manuel' check (origin in ('manuel', 'handypay')),
    external_id text unique check (external_id is null or char_length(external_id) between 1 and 120),
    created_by text not null default '',
    cancelled_at timestamptz,
    cancelled_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (origin = 'manuel' or external_id is not null)
);

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'workspace_followups_sale_id_fkey') then
    alter table public.workspace_followups
      add constraint workspace_followups_sale_id_fkey
      foreign key (sale_id) references public.workspace_sales(id) on delete set null;
  end if;
end $$;

-- Journal d'une fiche : message envoyé, appel, réponse, closé, note… `day`
-- (Port-au-Prince) sert le tableau du soir.
create table if not exists public.workspace_followup_events (
    id uuid primary key default gen_random_uuid(),
    followup_id uuid not null references public.workspace_followups(id) on delete cascade,
    kind text not null check (kind in ('message', 'appel', 'reponse', 'close', 'note', 'perdu', 'pas_interesse')),
    template_id uuid references public.workspace_message_templates(id) on delete set null,
    template_name text not null default '' check (char_length(template_name) <= 80),
    note text not null default '' check (char_length(note) <= 1000),
    actor text not null default '',
    day date not null,
    created_at timestamptz not null default now()
);

-- ─── Tableau du soir ────────────────────────────────────────────────

create table if not exists public.workspace_daily_notes (
    id uuid primary key default gen_random_uuid(),
    username text not null,
    day date not null,
    blockers jsonb not null default '[]'::jsonb,
    bugs text not null default '' check (char_length(bugs) <= 2000),
    note text not null default '' check (char_length(note) <= 2000),
    unread_left integer check (unread_left is null or unread_left >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (username, day)
);

-- ─── Index ──────────────────────────────────────────────────────────

create unique index if not exists workspace_tasks_number_idx on public.workspace_tasks (number);
create index if not exists workspace_tasks_project_idx on public.workspace_tasks (project_id);
create index if not exists workspace_tasks_updated_idx on public.workspace_tasks (updated_at);
create index if not exists workspace_tasks_assignee_idx on public.workspace_tasks (assignee) where not archived;
create index if not exists workspace_comments_task_idx on public.workspace_comments (task_id, created_at);
create index if not exists workspace_activity_created_idx on public.workspace_activity (created_at desc);
create index if not exists workspace_activity_task_idx on public.workspace_activity (task_id, created_at desc);
create index if not exists workspace_notifications_recipient_idx on public.workspace_notifications (recipient, created_at desc);
create index if not exists workspace_key_results_objective_idx on public.workspace_key_results (objective_id, position);
create index if not exists workspace_checkins_kr_idx on public.workspace_checkins (key_result_id, created_at);
create index if not exists workspace_docs_project_idx on public.workspace_docs (project_id, updated_at desc);
create index if not exists workspace_github_links_task_idx on public.workspace_github_links (task_id, created_at desc);
create index if not exists workspace_github_deliveries_received_idx on public.workspace_github_deliveries (received_at desc);
create index if not exists workspace_ai_journal_task_idx on public.workspace_ai_journal (task_id, created_at);
create index if not exists workspace_sales_day_idx on public.workspace_sales (day) where cancelled_at is null;
create index if not exists workspace_sales_user_day_idx on public.workspace_sales (username, day);
create index if not exists workspace_followups_queue_idx on public.workspace_followups (assignee, status, next_followup);
create index if not exists workspace_followups_created_idx on public.workspace_followups (created_at desc);
create index if not exists workspace_followups_stage_idx on public.workspace_followups (stage, status);
create index if not exists workspace_followup_events_fo_idx on public.workspace_followup_events (followup_id, created_at desc);
create index if not exists workspace_followup_events_day_idx on public.workspace_followup_events (day, actor);
create index if not exists workspace_daily_notes_day_idx on public.workspace_daily_notes (day);

-- ─── RLS forcée, aucun accès pour anon / authenticated ──────────────

do $$
declare t text;
begin
  foreach t in array array[
    'workspace_accounts', 'workspace_members', 'workspace_settings', 'workspace_digest_runs',
    'workspace_objectives', 'workspace_key_results', 'workspace_checkins', 'workspace_cycles',
    'workspace_projects', 'workspace_tasks', 'workspace_comments', 'workspace_activity',
    'workspace_notifications', 'workspace_docs', 'workspace_github_links', 'workspace_github_deliveries',
    'workspace_ai_journal', 'workspace_message_templates', 'workspace_followups', 'workspace_sales',
    'workspace_followup_events', 'workspace_daily_notes'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
    execute format('revoke all on table public.%I from anon, authenticated', t);
  end loop;
end $$;

-- ─── `updated_at` tenu par la base ──────────────────────────────────
-- La page relit les tâches changées depuis son dernier passage (toutes les
-- 30 s) en s'appuyant dessus.

create or replace function public.workspace_touch()
returns trigger language plpgsql
set search_path = public
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

do $$
declare t text;
begin
  foreach t in array array[
    'workspace_accounts', 'workspace_members', 'workspace_settings', 'workspace_objectives',
    'workspace_key_results', 'workspace_cycles', 'workspace_projects', 'workspace_tasks',
    'workspace_docs', 'workspace_github_links', 'workspace_message_templates', 'workspace_followups',
    'workspace_sales', 'workspace_daily_notes'
  ] loop
    execute format('drop trigger if exists %I on public.%I', t || '_touch', t);
    execute format('create trigger %I before update on public.%I for each row execute function public.workspace_touch()', t || '_touch', t);
  end loop;
end $$;

comment on table public.workspace_accounts is
  'Comptes de l''espace d''équipe HandyPay. Empreintes scrypt, jamais de mot de passe. Accès service_role uniquement.';
comment on table public.workspace_followups is
  'Clients à relancer (données personnelles : téléphone). Accès service_role uniquement.';
comment on table public.workspace_sales is
  'Cartes closées par personne et par jour. external_id unique pour les ventes envoyées par HandyPay.';
