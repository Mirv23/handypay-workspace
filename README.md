# HandyPay · Espace d'équipe

L'outil de travail de l'équipe HandyPay (Mirv, Michael, Handy, Wydlyn et l'agent IA) pendant la campagne
**100 cartes par jour, 3 000 cartes du 16 novembre au 15 décembre 2026** (jalons : 700 le 22 nov., 1 400 le 29 nov.,
2 100 le 6 déc., 3 000 le 15 déc. ; marge de 10 $ par carte).

Application autonome tirée du module « espace d'équipe » de JaKOB : FastAPI + Jinja + Supabase, une seule page.

## Ce que fait l'application

| Vue | Ce qu'on y fait |
|---|---|
| **Ventes** | Gros bouton « + Carte closée » (avec quantité) ; classement du jour et de la semaine par personne ; progression du jour (x / 100) et du cumul face au jalon ; écart au rythme ; marge ; graphique des ventes par jour ; saisies du jour (un membre annule sa propre saisie du jour, un responsable corrige et annule tout). |
| **Relances** | Fiches clients à relancer. « Ma file du jour » (assignées à moi, à contacter ou relance due), filtres par étape, statut, personne, recherche. Sur chaque fiche : **Ouvrir WhatsApp** (lien `wa.me` avec le message rempli, tentative notée, prochaine relance programmée), **A répondu**, **Closé** (crée la vente à ton nom), **Perdu**, note ou appel. Import CSV avec rapport, répartition équitable, modèles de message, export CSV (responsables). |
| **Relances à grand volume** | **Mode rafale** (une fiche à la fois, message modifiable, touches W / R / C / N / P), **ajout par collage** d'une liste de clients (aperçu, doublons, répartition), **actions groupées**, « Copier le message », modèles en **français et en créole**, objectif de conversations par personne et compteur « reste à traiter ». Détails : `docs/relances.md`. |
| **Finances** | La cascade du mois : charges fixes → salaires de base → dette fournisseur → réserve → primes et croissance. Seuil en cartes, projection, ce qui revient à chacun, dépenses (accord des personnes payées au-dessus de 200 $), versements, dette. Lecture pour toute l'équipe, saisie par les responsables. Détails : `docs/finances.md`. |
| **Téléphone** | Application installable (PWA), barre d'onglets en bas, bouton « + Carte closée » flottant, **mode compteur** plein écran pour filmer (`/compteur`, aucun nom ni numéro). Détails : `docs/mobile.md`. |
| **Tableau du soir** | Pour une date : cartes du jour et cumul, écart au rythme des jalons, marge, relances envoyées (/ 300), réponses en % des relances, closes après relance en % des réponses, détail par personne ; le formulaire du soir de chacun (3 blocages entendus, bugs ouverts, note, messages non lus). |
| Aujourd'hui, Tâches (tableau, liste, calendrier, chronologie), Objectifs, Cycles, Équipe, Documents, Rapports, Corbeille | Les fonctions d'origine : projets, tâches (clé `HP-12`), objectifs et résultats clés (un résultat clé peut suivre les ventes tout seul), cycles, charge de l'équipe, documents, rapports, corbeille, modèles de projet (dont « Campagne 3 000 cartes »), notifications, activité, export CSV. |
| Automatisations | E-mail du matin (ventes d'hier, file de relances, tâches), webhook GitHub, membre « IA » et sa porte à jeton (tâches **et ventes venues de HandyPay**). |

Parcours client suivi : inscrit → KYC validé → achat commencé → a payé → carte émise → carte utilisée → a racheté.
« Aujourd'hui » = la date à **Port-au-Prince** (serveur et page).

### Rôles

- **responsable** : tout, dont les comptes, les réglages (objectifs, jalons, marge, séquence de relance), les modèles de message, la répartition et l'export des fiches ;
- **membre** : crée et modifie tâches, ventes, fiches, formulaire du soir ;
- **observateur** : lit, commente et suit des tâches ; n'écrit ni vente ni relance.

### Comptes

Chaque personne a son compte : identifiant = **nom d'utilisateur ou numéro WhatsApp**. Mot de passe haché en **scrypt
salé** ; session dans un **cookie signé HMAC** (HttpOnly, SameSite=Lax, Secure en production) ; **CSRF** par double
soumission (en-tête `X-CSRF-Token` = cookie `hp_csrf`, lié à la session) sur toute requête qui modifie ;
**8 échecs bloquent l'identifiant 15 minutes**. Le premier responsable se crée en ligne de commande ; ensuite un
responsable ajoute les membres depuis Équipe → « + Membre » : le mot de passe généré s'affiche **une seule fois**, avec un
bouton « Envoyer par WhatsApp » (message pré-rempli). Changer son mot de passe (Équipe → Mon profil) ferme ses autres sessions.

## Arborescence

```
app/
  main.py          application, gardien (session, CSRF, en-têtes de sécurité), journaux sans numéro
  config.py        réglages (variables d'environnement uniquement ; .env lu en local)
  auth.py          comptes, connexion, verrouillage, mot de passe
  security.py      scrypt, cookie signé, CSRF, normalisation des numéros, liens wa.me
  db.py            Supabase en production, faux client local sinon
  localdb.py       faux client Supabase persistant dans .data/ (mode local)
  workspace.py     tâches, projets, objectifs, cycles, documents, équipe, modèles de projet
  sales.py         ventes, réglages, classement, jalons
  followups.py     relances WhatsApp, modèles de message, import, répartition, export
  evening.py       tableau du soir et formulaire du soir
  connect.py       webhook GitHub, porte de l'IA (tâches, ventes, fiches)
  digest.py        e-mail du matin et son planificateur
  email_sender.py  envoi Resend ou SMTP
  finances.py      relevé du mois (cascade), dépenses, versements, dette
  mobile.py        manifest, service worker, icônes, mode compteur
  timeutil.py      fuseau America/Port-au-Prince
  templates/index.html   la page ; templates/compteur.html   le mode compteur
  static/          manifest.webmanifest, sw.js, icons/
sql/schema.sql         schéma de base (RLS forcée) — à exécuter en premier
sql/relances_v2.sql    relances à grand volume — ensuite
sql/finances.sql       finances — en dernier
docs/              relances.md, finances.md, mobile.md
tests/             run_verify.py et les vérifications de bout en bout
tools/make_icons.py    génère les icônes
manage.py          create-owner, reset-password, seed
ia_cli.py          outil en ligne de commande de l'agent IA
Dockerfile, render.yaml, requirements.txt, .env.example
```

## Lancer en local (sans base)

Avec `SUPABASE_URL` vide, l'application range ses données dans `.data/` (fichiers JSON, ignorés par git) grâce à un faux
client qui imite celui de Supabase. Parfait pour essayer et tester ; **pas** pour la production (une seule machine, pas de sauvegarde).

Windows (PowerShell) :

```powershell
cd C:\chemin\vers\workspace
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # facultatif en local
.\.venv\Scripts\python.exe manage.py create-owner --username mirv --name "Mirv" --whatsapp "3412 3456"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

macOS / Linux :

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py create-owner --username mirv --name "Mirv" --whatsapp "3412 3456"
uvicorn app.main:app --reload
```

Ouvre http://127.0.0.1:8000 et connecte-toi avec `mirv` (ou le numéro) et le mot de passe affiché **une seule fois**
par `create-owner`. Si la base est vide, `create-owner` crée aussi l'objectif « 3 000 cartes » (deux résultats clés reliés
aux ventes) et les modèles de message de départ (français et créole). Mot de passe perdu : `python manage.py reset-password --username mirv`.

## Créer la base Supabase

1. Crée un projet sur https://supabase.com (région la plus proche d'Haïti : `us-east`).
2. **SQL Editor → New query** : exécute, **dans cet ordre**, `sql/schema.sql`, puis `sql/relances_v2.sql`, puis
   `sql/finances.sql` (chacun : coller, **Run**). Les scripts sont ré-exécutables.
   Ils créent les tables `workspace_*`, activent **et forcent** la RLS sur chacune, sans aucune policy, et retirent tout
   droit aux rôles `anon` et `authenticated` : seule la clé service du serveur lit et écrit. Sans `relances_v2.sql`, les
   fiches répondent 503 ; sans `finances.sql`, la vue Finances aussi.
3. **Project Settings → API** : copie l'URL du projet (`SUPABASE_URL`) et la clé **service_role** / secrète
   (`SUPABASE_SERVICE_KEY`). Elle ne quitte jamais le serveur ; n'utilise jamais la clé `anon` ici.
4. Lance `python manage.py create-owner …` avec ces deux variables définies : le compte est créé dans Supabase.

## Variables d'environnement

Aucune clé n'est écrite dans le code. Tout passe par l'environnement (voir `.env.example`).

| Variable | Obligatoire | Rôle |
|---|---|---|
| `SESSION_SECRET` | **oui en production** | Secret HMAC des sessions, 32 caractères au moins : `python -c "import secrets; print(secrets.token_urlsafe(48))"`. En local, vide = généré dans `.data/`. |
| `SUPABASE_URL` | production | URL du projet. Vide = mode local. |
| `SUPABASE_SERVICE_KEY` | production | Clé service_role (secrète). |
| `APP_ENV` | non | `local` ou `production` (déduit de `SUPABASE_URL`). |
| `SESSION_MAX_AGE_HOURS` | non | Durée d'une session (168 h). |
| `COOKIE_SECURE` | non | Cookies HTTPS seulement (oui en production). |
| `LOCAL_DATA_DIR` | non | Dossier du mode local (`.data`). |
| `WORKSPACE_PUBLIC_URL` | conseillé | Adresse publique (liens des e-mails, message d'invitation). |
| `WORKSPACE_API_URL` | non | Adresse appelée par GitHub et l'IA (par défaut l'adresse publique). |
| `WORKSPACE_IA_TOKEN` | pour l'IA | Jeton Bearer de l'agent (32 caractères au moins). |
| `WORKSPACE_GITHUB_SECRET` | pour GitHub | Secret du webhook (signature HMAC SHA-256). |
| `WORKSPACE_GITHUB_REPO` | non | `organisation/depot` : n'accepte que ce dépôt. |
| `WORKSPACE_DIGEST_HOUR` | non | Heure d'envoi de l'e-mail du matin, heure d'Haïti (7). |
| `WORKSPACE_DIGEST_DAYS` | non | Jours ISO (`1-7` = tous les jours). |
| `WORKSPACE_DIGEST_INTERVAL_S` | non | Fréquence de vérification (600 s ; 0 = arrêt). |
| `EMAIL_FROM` | pour l'e-mail | Expéditeur, ex. `HandyPay équipe <equipe@ton-domaine>`. |
| `RESEND_API_KEY` | e-mail (au choix) | Envoi par Resend. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS` | e-mail (au choix) | Envoi SMTP (465 = TLS direct). |
| `EMAIL_PROVIDER`, `EMAIL_REPLY_TO` | non | Forcer `resend` / `smtp` ; adresse de réponse. |

## Déploiement

Un `Dockerfile` est fourni (Python 3.12, utilisateur sans privilèges, uvicorn derrière le proxy de l'hébergeur).

**Render** : le fichier `render.yaml` décrit le service (Docker, région Virginie, `/healthz`, secrets générés par Render).
Sinon, à la main : New → Web Service → ton dépôt → Runtime *Docker*. Ajoute les variables (au minimum `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, `SESSION_SECRET`, `WORKSPACE_PUBLIC_URL`), *Health Check Path* `/healthz`. Une instance payante
reste éveillée ; une instance gratuite s'endort : l'e-mail du matin part alors au réveil, une seule fois dans la journée.

**Railway** : New Project → Deploy from GitHub → le Dockerfile est détecté ; `PORT` est fourni par Railway ; ajoute les
mêmes variables.

Conseils : une seule instance suffit (le blocage d'un compte est en base ; celui d'un identifiant inconnu et la limite par
adresse IP sont en mémoire, par instance). HTTPS obligatoire (cookies `Secure`). Lance `create-owner` une fois, depuis ton
ordinateur avec les variables Supabase, ou depuis le shell de l'hébergeur.

## Brancher l'agent IA de HandyPay

L'agent n'a pas de session : il présente `Authorization: Bearer <WORKSPACE_IA_TOKEN>` (même valeur sur le serveur et chez
l'agent ; jeton faux → 401, absent du serveur → 503).

**Ventes** — à envoyer à chaque carte payée. `external_id` est **obligatoire** (l'identifiant de la commande dans HandyPay) :
rejouer le même envoi ne compte jamais deux fois (`"doublon": true`).

```bash
curl -X POST "$WORKSPACE_API_URL/api/workspace/ia/ventes" \
  -H "Authorization: Bearer $WORKSPACE_IA_TOKEN" -H "Content-Type: application/json" \
  -d '{"external_id":"ord-48213","cartes":1,"membre":"michael","source":"whatsapp"}'
```

Champs : `external_id` (obligatoire), `cartes` (1), `membre` (nom d'utilisateur du vendeur ; vide = « Direct (HandyPay) »),
`jour` (AAAA-MM-JJ, aujourd'hui à Port-au-Prince par défaut), `client_nom`, `client_tel`, `source`
(`whatsapp`, `appel`, `pub`, `parrainage`, `autre`), `note`. Par lots : `POST /api/workspace/ia/ventes/lot` avec
`{"ventes":[…]}` (200 au plus). Chiffres : `GET /api/workspace/ia/ventes/stats`.

**Clients à relancer** — `POST /api/workspace/ia/fiches` avec `{"fiches":[{"nom":"…","telephone":"3412 3456","etape":"achat commencé"}]}` :
un nouveau numéro crée une fiche ; un numéro connu voit seulement son étape avancer.

**Tâches** — file du matin `GET /api/workspace/ia/matin`, création `POST /api/workspace/ia/taches`, suivi
`GET|PATCH /api/workspace/ia/taches/HP-12`. L'outil `ia_cli.py` (bibliothèque standard seule) fait tout cela :

```bash
export WORKSPACE_IA_TOKEN=…  WORKSPACE_API_URL=https://equipe.exemple.com
python ia_cli.py matin
python ia_cli.py vente --id ord-48213 --cartes 1 --membre michael
python ia_cli.py stats
python ia_cli.py terminer HP-12 "Le KYC passe sur Android 9"
```

## Brancher GitHub

1. Sur le serveur : `WORKSPACE_GITHUB_SECRET` (une longue valeur aléatoire), et `WORKSPACE_GITHUB_REPO=organisation/depot`.
2. Sur GitHub : *Settings → Webhooks → Add webhook* ; Payload URL `https://<ton-espace>/api/workspace/github` ;
   type `application/json` ; Secret = la même valeur ; événements *Pushes* et *Pull requests*.
3. Écris `HP-12` dans un commit, une branche (`hp-12-…`) ou une PR : la tâche 12 passe « En cours », puis « En revue »
   quand la PR s'ouvre, puis « Fait » à la fusion (« ferme HP-12 », « fixes HP-12 », ou branche nommée pour elle).
   Une décision (étiquette « décision ») ne se ferme jamais toute seule.

## Données personnelles

- Les numéros des clients ne sortent que vers des comptes connectés ; l'export CSV des fiches est réservé aux responsables.
- Les journaux du serveur ne contiennent jamais de numéro : la partie `?…` des adresses est retirée du journal d'accès et
  toute suite de chiffres qui ressemble à un numéro est masquée ; le code n'écrit jamais de numéro dans les journaux ni dans
  le fil d'activité.
- En base, RLS forcée sans policy : la clé publique `anon` ne lit rien.
- Les liens `wa.me` sont des navigations (nouvel onglet) avec `noopener noreferrer` ; la page n'envoie rien à WhatsApp.

## Relances : comment marche la séquence

« Ouvrir WhatsApp » ouvre `https://wa.me/<numéro>?text=<message rempli>` : la personne relit et appuie sur Envoyer depuis le
téléphone de l'équipe. L'espace note « message envoyé » (avec le modèle), ajoute une tentative et programme la prochaine
relance : par défaut **J+1** après la 1re tentative, **J+3** après la 2e, **J+7** après la 3e, comptés depuis la tentative.
Après **3 tentatives sans réponse**, la fiche passe en **« froid »** : elle revient une dernière fois à J+7, puis c'est à la
personne de décider (perdu, pas intéressé…). « A répondu » remet le compteur à zéro. Séquence, seuil et objectif de
conversations par personne se règlent dans Relances → Réglages (responsables).

Modèles : variables `{prenom}`, `{nom}` (ce qui suit le prénom) et `{moi}` (le prénom de la personne qui envoie). Le modèle de
l'étape de la fiche est proposé en premier.

Import CSV : colonnes `nom, téléphone, étape, source, note` (en-tête facultatif, séparateur `;` ou `,`). Un numéro à 8 chiffres
reçoit `+509`. Le rapport donne les ajoutées, les doublons (numéro déjà connu ou répété dans le fichier) et les lignes rejetées
avec leur raison.

## Étape suivante : l'API officielle WhatsApp Business Cloud (non implémentée)

Aujourd'hui, l'envoi est **humain** (`wa.me`) : aucun envoi automatique non officiel, qui ferait bannir le numéro. Pour
automatiser plus tard, uniquement par l'API officielle de Meta :

1. **Compte** : un Meta Business Manager vérifié, un compte WhatsApp Business (WABA) et un **numéro dédié**. Un numéro
   branché sur l'API ne s'utilise plus dans l'application WhatsApp du téléphone.
2. **Accès** : une application Meta (produit WhatsApp), un *System User* avec un jeton permanent
   (`whatsapp_business_messaging`, `whatsapp_business_management`), l'identifiant du numéro (*Phone number ID*). Variables à
   prévoir : `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`.
3. **Modèles approuvés** : hors de la fenêtre de 24 h qui suit le dernier message du client, on ne peut envoyer que des
   **modèles soumis et approuvés par Meta** (catégories *marketing* ou *utility*, variables `{{1}}`, `{{2}}`). Chaque modèle
   de l'espace devra avoir son équivalent approuvé (nom + langue `fr`). Dans la fenêtre de 24 h, les réponses libres sont permises.
4. **Coût** : Meta facture l'envoi selon la catégorie et le pays du destinataire (historiquement **par conversation de 24 h**,
   et, selon la grille en vigueur depuis 2025, par message de modèle). Vérifie la grille actuelle pour Haïti avant de lancer
   300 conversations par jour.
5. **Consentement** : n'écrire qu'aux clients qui ont accepté d'être contactés par WhatsApp ; prévoir « STOP ».
6. **Dans le code** : `followups.open_whatsapp` gagnerait un chemin « envoyer par l'API » (`POST
   https://graph.facebook.com/<version>/<phone_number_id>/messages` avec le modèle et ses variables) ; un webhook
   `/api/workspace/whatsapp` (signature `X-Hub-Signature-256` vérifiée avec `WHATSAPP_APP_SECRET`) recevrait les messages
   entrants et les statuts : « A répondu » se cocherait seul et le tableau du soir compterait les non-lus réels. L'e-mail du
   matin (`digest.send_digest`) pourrait être doublé d'un message WhatsApp de modèle *utility*.

## Vérifier

Toutes les vérifications de bout en bout, sur une base locale jetable (le `.env` n'est pas utilisé, rien n'est écrit dans
le projet, le serveur s'arrête à la fin) :

```powershell
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe tests\run_verify.py --extra tests\verify_finances.py tests\verify_relances.py tests\verify_mobile.py
```
