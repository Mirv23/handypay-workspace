-- ═══════════════════════════════════════════════════════════════════
--  HandyPay · Espace d'équipe — relances à grand volume (v2)
--
--  À coller dans Supabase → SQL Editor → Run, APRÈS sql/schema.sql (base
--  neuve ou déjà en service). Ré-exécutable : chaque colonne, contrainte et
--  index n'est créé que s'il manque ; rien n'est effacé ni réécrit.
--
--  Ce que la v2 ajoute en base :
--  - la langue d'une fiche (fr ou ht) : le modèle de message proposé la suit ;
--  - la langue et une description pour chaque modèle de message (les modèles
--    créoles de départ sont marqués « à relire par l'équipe ») ;
--  - deux index pour le « reste à traiter aujourd'hui » et le mode rafale.
--
--  Le reste vit dans workspace_settings (jsonb, aucune colonne à créer) :
--  clé « followups » → objectifs de conversations par personne (« targets »)
--  et langue par défaut (« default_lang ») ; clé « templates_seeded » → les
--  modèles de départ déjà posés (ils ne reviennent pas une fois supprimés).
--
--  Accès : inchangé. RLS activée ET forcée, aucune policy, aucun droit pour
--  `anon` et `authenticated` (rappelé ci-dessous) : seul le serveur lit et
--  écrit, avec la clé service_role.
-- ═══════════════════════════════════════════════════════════════════

-- ─── Langue d'une fiche ─────────────────────────────────────────────

alter table public.workspace_followups
    add column if not exists lang text not null default 'fr';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'workspace_followups_lang_check') then
    alter table public.workspace_followups
      add constraint workspace_followups_lang_check check (lang in ('fr', 'ht'));
  end if;
end $$;

-- ─── Langue et description d'un modèle de message ───────────────────

alter table public.workspace_message_templates
    add column if not exists lang text not null default 'fr';
alter table public.workspace_message_templates
    add column if not exists description text not null default '';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'workspace_message_templates_lang_check') then
    alter table public.workspace_message_templates
      add constraint workspace_message_templates_lang_check check (lang in ('fr', 'ht'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'workspace_message_templates_description_check') then
    alter table public.workspace_message_templates
      add constraint workspace_message_templates_description_check check (char_length(description) <= 200);
  end if;
end $$;

-- ─── Index ──────────────────────────────────────────────────────────

-- Fiches traitées un jour donné (reste à traiter, conversations par personne).
create index if not exists workspace_followup_events_day_fo_idx
    on public.workspace_followup_events (day, followup_id);
-- File due d'une personne (mode rafale) : fiches encore ouvertes.
create index if not exists workspace_followups_due_idx
    on public.workspace_followups (assignee, next_followup)
    where status not in ('close', 'perdu', 'pas_interesse');

-- ─── RLS forcée, aucun accès pour anon / authenticated (rappel) ─────

do $$
declare t text;
begin
  foreach t in array array[
    'workspace_followups', 'workspace_message_templates', 'workspace_followup_events', 'workspace_settings'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('alter table public.%I force row level security', t);
    execute format('revoke all on table public.%I from anon, authenticated', t);
  end loop;
end $$;

comment on column public.workspace_followups.lang is
  'Langue de la fiche (fr ou ht) : le modèle de message proposé suit cette langue.';
comment on column public.workspace_message_templates.description is
  'Note pour l''équipe (ex. « à relire par l''équipe » pour les modèles créoles de départ).';
