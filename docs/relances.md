# Relances à grand volume (v2)

L'équipe vise **100 cartes par jour**, soit environ **300 conversations WhatsApp par jour** (150 pour Michael, 100 pour
Mirv), avec plus de 2 000 messages de clients en attente. Règle d'équipe : **zéro message non lu le soir**. La v2 des
relances sert ce volume sans rien automatiser : l'envoi reste fait par un humain, avec un lien `wa.me` (ou un copier-coller
dans WhatsApp Business).

Tout se passe dans **Relances** (et dans le **Tableau du soir** pour le bilan).

## Mise à jour d'une base Supabase

Exécuter `sql/relances_v2.sql` dans le SQL Editor, **après** `sql/schema.sql` (base neuve ou déjà en service). Le script
est ré-exécutable ; il ajoute :

- `workspace_followups.lang` (`fr` ou `ht`, `fr` par défaut) ;
- `workspace_message_templates.lang` et `workspace_message_templates.description` ;
- deux index (fiches traitées un jour donné, file due d'une personne) ;
- le rappel de la RLS forcée, sans policy, sans droit pour `anon` et `authenticated`.

Les objectifs par personne et la langue par défaut vivent dans `workspace_settings` (clé `followups`, en jsonb) : rien à
créer. En local, le faux client (`app/localdb.py`) connaît les nouvelles colonnes.

## Les mots

- **Fiche due** : fiche ouverte (ni closée, ni perdue, ni « pas intéressé ») dont la relance est prévue **aujourd'hui ou
  avant**, ou qui n'a **aucune date** (à contacter, ou sans suite prévue). Une fiche « à contacter » dont la relance est
  programmée plus tard (action groupée) attend cette date. C'est aussi la règle de « Ma file du jour » et de l'e-mail du matin.
- **Fiche traitée aujourd'hui** : fiche qui a reçu aujourd'hui, de n'importe qui, un message, un appel, une réponse, une
  vente, « perdu » ou « pas intéressé ». Une note seule ne compte pas.
- **Reste à traiter aujourd'hui** : fiches dues et pas encore traitées aujourd'hui. Chacun voit le sien dans la navigation
  (à la place de l'ancien badge) et sur l'onglet « Ma file » ; le Tableau du soir le montre par personne et pour l'équipe
  (dont les fiches sans personne). Le soir, il doit être à **0**. Il n'est calculé que pour aujourd'hui : un jour passé
  affiche « – ».
- **Conversation** : une fiche avec au moins un **message, appel, réponse ou vente** de cette personne dans la journée.
  Une fiche compte une fois par personne et par jour, même avec plusieurs événements. « Perdu » seul n'est pas une
  conversation.

## Objectif de conversations par personne

Relances → **Réglages** (responsables) : objectif par jour pour chaque membre (par défaut Michael 150, Mirv 100, les autres
0 = pas d'objectif), objectif d'équipe (300), langue des nouvelles fiches, et la séquence de relance habituelle.

La progression du jour s'affiche en haut de Relances (mes conversations / mon objectif, mon reste à traiter, une ligne
pour l'équipe) et, par personne, dans le Tableau du soir (barre de progression et reste à traiter).

## Mode rafale

Relances → **Mode rafale** (ou `#/relances/rafale`). Une fiche à la fois, sur une grande carte : nom, numéro, étape,
statut, langue (FR / HT), tentatives et date de relance, les 4 derniers événements, puis **le message rempli, visible et
modifiable** avant l'envoi (on peut aussi changer de modèle). En haut : « x traitées aujourd'hui / objectif » et le nombre
restant.

Ordre des fiches (ma file seulement) : d'abord les clients **qui ont répondu**, puis les **relances dues** (la plus ancienne
d'abord), puis les **nouveaux** (les plus anciens d'abord).

| Bouton | Touche | Effet |
|---|---|---|
| Ouvrir WhatsApp | `W` | Ouvre `wa.me` avec le message tel qu'il est dans le champ ; la tentative est notée (relance suivante programmée). La fiche **reste affichée** pour noter la suite. |
| Copier le message | – | Copie le message (WhatsApp Business sur ordinateur, quand `wa.me` ne s'ouvre pas), puis propose **Marquer comme envoyé** (même effet qu'« Ouvrir WhatsApp »). |
| A répondu | `R` | Statut « A répondu », compteur de tentatives à zéro ; la fiche revient dans la file les jours suivants. Fiche suivante. |
| Closé | `C` | Petite fenêtre (nombre de cartes, note) ; `Entrée` enregistre la vente à mon nom. Fiche suivante. |
| Pas de réponse | `N` ou `→` | Si un message ou un appel est déjà noté aujourd'hui sur la fiche : simple note « Pas de réponse ». Sinon, c'est une tentative (le client a été contacté hors de la page) : message noté, relance suivante programmée. Fiche suivante. |
| Perdu | `P` | Statut « Perdu », sans confirmation ; un bouton **Annuler** dans la notification rétablit la fiche et la réaffiche. Fiche suivante. |

Les touches ne font rien quand on écrit dans le message (ni dans aucun champ) : `Échap` sort du champ. « G puis R » (et
les autres raccourcis « G puis … ») restent la navigation. Sur un téléphone de 390 px, les quatre boutons de résultat
restent collés en bas de l'écran ; les rappels de touches sont masqués sur les écrans tactiles.

## Ajout rapide par collage

Relances → **Coller une liste**. Une ligne par client :

```
Jean Pierre, 3456 7801
3712 4455
Marie Claire; +509 3456-7802
Rose Joseph<TAB>50934567803
Paul Henry 3456 7804
```

« nom, numéro » ou le numéro seul ; séparateur `,`, `;` ou tabulation ; un numéro écrit dans la ligne est aussi reconnu.
Une première ligne d'en-tête (« nom ; téléphone ») est ignorée. On choisit **une fois** l'étape, la source (par défaut
« WhatsApp non lus ») et la langue, puis l'assignation : **à moi**, ou **répartir** entre des personnes cochées
(responsables), proportionnellement à leurs objectifs de conversations (à parts égales si l'une n'en a pas).

L'**aperçu** (automatique pendant la saisie) montre les lignes valides (avec la personne prévue), les **doublons**
(répété dans la liste, ou déjà en base avec sa personne) et les **rejets** avec leur raison. Rien n'est écrit avant
« Importer ». Côté serveur, c'est la même logique que l'import CSV (`prepare_rows` + `known_phones` + `import_rows` :
normalisation `+509`, dédoublonnage dans la liste et en base).

## Actions groupées

Relances → **Toutes les fiches** : une case par fiche, « Sélectionner les fiches de la page » ; la barre d'actions apparaît :
assigner (ou retirer la personne), changer le statut, l'étape ou la langue, **programmer la relance** à une date. Les droits
sont ceux d'une fiche seule (membre ou responsable ; l'observateur ne voit pas les cases et l'API répond 403). Un
changement de statut laisse dans le journal de chaque fiche « Statut → … (action groupée) » ; un passage en
Closé / Perdu / Pas intéressé demande confirmation (pas de vente créée, comme en modifiant une fiche).

## Langue du message

Chaque fiche a une langue (`fr` ou `ht`), choisie à la création selon le réglage « Langue des nouvelles fiches » (ou
précisée : fiche, collage, action groupée, boutons FR / HT du mode rafale). Le modèle proposé suit la langue de la fiche :
même langue et même étape, puis même langue tous publics, puis même étape, puis le premier modèle actif. Même règle dans la
page (`tplFor`) et sur le serveur (`followups.pick_template`).

Cinq modèles de départ en **créole haïtien** s'ajoutent aux cinq français (même étapes : premier contact, inscrit sans KYC,
achat commencé, carte jamais utilisée, rachat). Leur description dit « **à relire par l'équipe** » : à faire valider par
quelqu'un dont c'est la langue avant de les envoyer en masse.

Le seed est **idempotent** : le réglage `templates_seeded` garde les clés des modèles déjà posés. Une base d'avant la v2
reçoit seulement les modèles créoles ; aucun modèle existant n'est modifié ; un modèle de même nom et même langue n'est
jamais dupliqué ; un modèle de départ supprimé ne revient pas.

## API (session de l'espace, CSRF sur tout ce qui modifie)

| Route | Rôle | Ce que ça fait |
|---|---|---|
| `GET /api/ws/followups/today` | tous | Conversations, objectif et reste à traiter : `me`, `people`, `team`. |
| `GET /api/ws/followups/next?skip=id,id` | tous | Mode rafale : la prochaine fiche de ma file (avec ses derniers événements, le modèle choisi, le message rempli et le lien `wa.me`), `remaining`, `conversations`, `target`. |
| `POST /api/ws/followups/paste` | membre (répartir : responsable) | `{text, stage, source, lang, assign, usernames, commit}` (`assign` : `me` ou `spread`) : aperçu, puis import. |
| `POST /api/ws/followups/bulk` | membre | `{ids, changes: {assignee, status, stage, next_followup, lang}}` (500 fiches au plus). |
| `POST /api/ws/followups/{id}/whatsapp` | membre | Nouveaux champs : `message` (texte relu) et `via` (`wa` ou `copie`). |
| `POST /api/ws/followups/{id}/event` | membre | Nouveau `kind` : `sans_reponse` (enregistré comme message ou note, voir plus haut). |
| `PUT /api/ws/settings/followups` | responsable | Nouveaux champs facultatifs : `targets` (`{"michael": 150}`), `default_lang`. Absents, ils sont gardés. |
| `GET /api/ws/followups` | tous | Nouveau champ `to_treat` (le badge). |
| `GET /api/ws/evening` | tous | Par personne : `conversations`, `conversations_target`, `to_treat` ; totaux `conversations`, `to_treat`, `unassigned_due`. |

## Données personnelles

Inchangé : les numéros ne sortent que vers des comptes connectés ; l'aperçu du collage les montre à la personne qui colle ;
aucun numéro dans les journaux (le contenu collé n'est jamais journalisé ; le journal d'activité ne garde que des
comptes : ajoutées, doublons, rejetées, nombre de fiches modifiées).

## Vérifier

```powershell
.\.venv\Scripts\python.exe tests\run_verify.py --port 8801 --extra tests\verify_relances.py
```

`tests/verify_relances.py` couvre le collage (aperçu, doublon répété et déjà en base, rejets, import, répartition), les
actions groupées, le mode rafale côté API (fiche suivante, actions qui la font avancer, `skip`), l'objectif et le compteur
de conversations, le reste à traiter (Relances et Tableau du soir), les modèles créoles et la langue du message, le seed
idempotent (y compris sur une base d'avant la v2), le refus pour un observateur et l'absence des numéros collés dans le
journal du serveur.
