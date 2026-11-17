# Téléphone : application installable, navigation mobile, mode compteur

Les commerciaux travaillent au téléphone (les boutons `wa.me` ouvrent l'application WhatsApp) et Mirv filme chaque
matin le compteur des ventes pour la série « Défi 3 000 cartes ». Cette page décrit ce qui a été fait pour eux.

## Installer l'application

- **Android (Chrome)** : ouvrir l'espace, menu ⋮ → « Installer l'application » (ou « Ajouter à l'écran d'accueil »).
  L'icône « H » verte apparaît ; l'application s'ouvre sur **Ventes**, sans barre d'adresse. Un appui long sur l'icône
  propose trois raccourcis : Carte closée, Ma file de relances, Mode compteur.
- **iPhone (Safari)** : bouton Partager → « Sur l'écran d'accueil ».

Ce qui le permet :

| Fichier | Rôle |
|---|---|
| `app/static/manifest.webmanifest` | Nom, nom court, couleurs (`theme_color` #0E7C66, fond #F5F6F8), `display: standalone`, `start_url` `/#/ventes`, icônes, raccourcis. |
| `app/static/icons/*.png` | `icon-192`, `icon-512` (carré arrondi), `icon-maskable-512` (fond plein, « H » dans la zone sûre), `apple-touch-icon` (180), `favicon-32`. |
| `tools/make_icons.py` | Redessine ces icônes sans dépendance (zlib + struct) : `python tools/make_icons.py`. |
| `app/static/sw.js` | Le service worker. |
| `app/mobile.py` | Les routes publiques ci-dessous, la page `/compteur` et `GET /api/ws/compteur`. |

### Routes publiques (sans session)

`/manifest.webmanifest` (`application/manifest+json`), `/sw.js` (`text/javascript`, `Cache-Control: no-cache`),
`/icons/<nom>.png` (liste fermée de cinq fichiers ; tout autre nom → 404). Elles ne posent aucun cookie et ne contiennent
aucune donnée de l'espace. Elles sont hors de `/api/`, donc le gardien de `main.py` ne leur demande rien.

### Le service worker : la coquille, jamais les données

- Il met en cache **seulement** la coquille statique : la page `/` (du HTML sans aucune donnée), le manifeste, les icônes.
- **Jamais une réponse de `/api/*`** : ces requêtes ne passent même pas par lui (première règle du gestionnaire `fetch`),
  parce qu'elles contiennent des numéros de clients. `/compteur` n'est pas gardé non plus.
- Pages : le réseau d'abord. Sans réseau, `/` sort du cache et l'espace affiche « Réseau injoignable » avec un bouton
  **Réessayer** et le bandeau « Pas de réseau » ; toute autre page reçoit un court message hors ligne (« Pas de réseau »,
  « Réessayer »). Au retour du réseau, l'espace se recharge tout seul.
- Mise à jour : `sw.js` est revalidé à chaque visite ; changer `VERSION` dans `sw.js` vide les anciens caches.
- Servi à la racine, sa portée est déjà `/` : l'en-tête `Service-Worker-Allowed` n'est pas nécessaire.

### En-têtes et CSP

- Page `/` et `/compteur` : la CSP de la page gagne `manifest-src 'self'` et `worker-src 'self'` (déjà couverts par
  `default-src 'self'` : rien n'est ouvert, c'est seulement explicite). Scripts : `script-src 'self' 'nonce-…'`, sans
  `'unsafe-inline'` ; le nonce est tiré pour chaque réponse et posé sur les `<script>` de la page (un script ou un
  attribut `on…=` injecté ne s'exécuterait pas).
- `/sw.js` : `default-src 'none'; connect-src 'self'` (la CSP d'un service worker s'applique à ses propres requêtes ; il
  ne relit que la coquille, sur le même site).
- Manifeste et icônes : la CSP de l'API (`default-src 'none'`), comme tout le reste ; cache public (1 h et 24 h).

## Navigation au téléphone (≤ 768 px)

- **Barre d'onglets en bas** : Ventes, Relances (avec le nombre de fiches dues), Soir, Tâches, **Plus**. Elle fait partie de
  la grille de la page : elle ne recouvre jamais le contenu. Toucher l'onglet déjà ouvert remonte en haut de la vue.
- **Plus** ouvre le reste du menu dans un panneau qui monte du bas (Aujourd'hui, Objectifs, Équipe, projets, Mode compteur,
  Documents, Rapports, Chronologie, Cycles, Corbeille, profil, thème, déconnexion). Fermeture : toucher le fond, Échap, ou
  choisir une vue. C'est la barre latérale du bureau, présentée autrement : un seul menu à maintenir.
- **Bouton flottant « + Carte closée »** sur Ventes (aujourd'hui, pour qui peut saisir) : même action que le gros bouton de
  la vue, avec la quantité choisie (« + 3 cartes closées ») et « Annuler » dans le message.
- En-tête : sur Ventes, Relances et Soir, le titre de la vue remplace la recherche de tâches.
- **Zones tactiles d'au moins 44 px** (boutons, listes, onglets, champs, dates, cases) ; champs en 16 px pour que l'iPhone ne
  zoome pas dessus.
- **Encoches** : `viewport-fit=cover` et `env(safe-area-inset-*)` sur l'en-tête, la barre d'onglets, le panneau, le bouton
  flottant, les fenêtres.
- Lignes de tâches : titre en entier (sur deux lignes au besoin). Calendrier : des repères de couleur dans la grille et la
  liste des échéances du mois dessous. Tiroir d'une tâche : en-tête sur deux lignes, plein écran.
- Contrôle : toutes les vues (Aujourd'hui, Ventes, Relances, Soir, Mes tâches, Tableau, Liste, Calendrier, Chronologie,
  Objectifs, Cycles, Équipe, Documents, Rapports, Corbeille, projet), une fenêtre et le tiroir, à 390 et 360 px : aucun
  débordement horizontal, aucun texte coupé, aucune cible de moins de 44 px.

## Confort

- **Polling** : tâches toutes les 30 s et notifications toutes les 60 s quand la page est visible ; 5 et 10 min quand elle est
  cachée (`visibilitychange`) ; rien hors ligne ; synchronisation immédiate au retour.
- **Pas de saut de mise en page** : la carte « Campagne » de l'accueil garde sa place pendant le chargement, le fil
  « En direct » arrive en dernier, la zone des filtres est cachée d'emblée, le thème choisi s'applique avant le premier rendu.
- **Clavier** : `interactive-widget=resizes-content` (Android) ; la zone visible est suivie (`visualViewport` → `--vvh`) ;
  les fenêtres s'y logent, collées en bas ; le champ actif est ramené à l'écran ; la barre d'onglets et le bouton flottant
  s'effacent pendant la saisie.
- La couleur de la barre d'état suit le thème (clair ou sombre).

## Mode compteur (`/compteur`)

La page que l'on filme chaque matin, pensée pour une vidéo verticale (9:16) : le **cumul** en très grand, « Défi 3 000
cartes », la barre de progression avec les jalons (700, 1 400, 2 100, 3 000), « **x / 100 aujourd'hui** », le **prochain
jalon** (« 700 cartes le dim. 22 nov. · encore 532 · 89 par jour »), « **Jour N / 30** », la date et l'heure de la dernière
mise à jour, un gros bouton **Rafraîchir**.

- Quand le chiffre change : le cumul compte de l'ancienne valeur à la nouvelle, grossit un instant et passe au vert, « +3 »
  s'affiche à la place du titre ; la barre avance. Rien de tout cela si l'appareil demande moins d'animations.
- **Aucun nom ni numéro** : ni client, ni membre de l'équipe (le classement reste dans Ventes).
- **Marge cachée par défaut** ; le bouton « Marge » l'affiche, sans jamais s'en souvenir (on ne la filme pas par mégarde).
- Thème Auto / Sombre / Clair (même réglage que l'espace), plein écran (si le navigateur le permet), écran maintenu allumé
  pendant qu'on filme (Wake Lock), rafraîchi tout seul toutes les 2 min quand la page est visible.
- Réservée aux personnes connectées (observateurs compris) : sans session, `/compteur` renvoie (303) vers la connexion,
  qui y revient ensuite. Les chiffres du moment sont écrits dans la page : rien ne bouge au chargement.
- Accès : « Mode compteur » dans le menu (barre latérale, ou Plus au téléphone), raccourci de l'icône installée.

Conseils pour filmer : thème Sombre, plein écran, luminosité au maximum, mode Ne pas déranger ; ouvrir la page, attendre la
vidéo, puis toucher Rafraîchir à l'image.

### `GET /api/ws/compteur`

Session requise (401 sinon), lecture seule (POST → 405), `Cache-Control: no-store`. Liste blanche de champs, calculée
avec `sales.compute_stats` sans même lire les comptes de l'équipe :

```json
{
  "day": "2026-11-17",
  "campaign": {"start": "2026-11-16", "end": "2026-12-15", "day": 2, "days": 30,
               "cumul": 168, "target": 3000, "expected": 200, "gap": -32},
  "today": {"cards": 72, "target": 100},
  "next_milestone": {"date": "2026-11-22", "target": 700, "remaining": 532, "days_left": 6, "per_day_needed": 89},
  "milestones": [{"date": "2026-11-22", "target": 700, "reached": false}, "…"],
  "margin": {"total": 1680, "today": 720, "currency": "USD"},
  "updated_at": "2026-11-17T12:42:05.123+00:00"
}
```

## Vérifier

```powershell
.venv\Scripts\python.exe tests\run_verify.py --port 8802 --extra tests\verify_mobile.py
```

`tests/verify_mobile.py` contrôle : manifeste et icônes (type, dimensions, maskable opaque, sans session, aucun cookie,
chemins détournés → 404) ; service worker (type MIME, `no-cache`, CSP, la règle `/api/` lue dans le code servi avant tout
cache ou réseau, coquille sans `/api/`, seul `/` réécrit, message hors ligne) ; CSP de la page ; `/compteur` (303 sans
session ou avec un cookie forgé, 200 avec session, en-têtes, marge cachée, aucun nom) ; `GET /api/ws/compteur` (401 sans
session, aucun champ de nom ni de téléphone, mêmes chiffres que les stats, lecture seule, lisible par un observateur).

## Limites connues

- Le clavier virtuel ne s'émule pas dans Chrome sans interface : le comportement (classe `kb-open`, `--vvh`, fenêtres
  collées en bas) est vérifié, pas son rendu sur un vrai téléphone. À essayer sur Android et sur iPhone.
- iOS : pas d'invite d'installation (Partager → Sur l'écran d'accueil), pas de plein écran pour une page (le bouton est
  masqué), Wake Lock seulement depuis iOS 16.4.
- Hors ligne, rien n'est lisible (c'est voulu : aucune donnée n'est gardée sur le téléphone).
