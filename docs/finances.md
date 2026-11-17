# Finances

La vue **Finances** montre, mois par mois, comment la marge des cartes paie l'entreprise et l'équipe.
Elle est lisible par les **responsables** et les **membres** (par transparence) ; un **observateur** n'y a pas accès.

Code : `app/finances.py` (calculs et routes), bloc `/* ==== FINANCES ==== */` de `app/templates/index.html` (la vue),
`sql/finances.sql` (tables), `tests/verify_finances.py` (vérifications).

## La cascade du mois

La **marge** du mois = cartes vendues (ventes non annulées du mois) × marge par carte (réglage des ventes, 10 $)
\+ les **autres revenus** saisis (vente de cours…). Elle paie, dans cet ordre :

| Étape | Ce qu'elle paie | Par défaut |
|---|---|---|
| 1. Charges fixes | outils, publicité, internet | Internet 120 $, ChatGPT 200 $, Claude 100 $, Publicité 1 500 $ (50 $ × 30 jours) = 1 920 $ |
| 2. Salaires de base | le salaire de base de chaque personne payée | 500 $ pour chaque membre actif (ni l'IA, ni un observateur) |
| 3. Dette fournisseur | une mensualité, jusqu'à ce que le solde soit à zéro | 1 000 $ par mois sur 5 000 $ |
| 4. Réserve | un pourcentage de la marge, mis de côté | 10 % |
| 5. Surplus | ce qui reste : une part en primes, à parts égales entre les personnes payées ; le reste pour la croissance | 50 % en primes |

**Mois sous le seuil.** On paie dans l'ordre jusqu'où l'argent va. Ce qui manque aux étapes 1 à 3 devient un
**arriéré** : le mois suivant, il s'ajoute à la même étape, avant le mois courant, donc toujours avant la moindre prime.
Si un arriéré ne peut pas être rattrapé, il continue d'être reporté. Quand l'argent ne suffit pas pour les salaires,
chacun reçoit la même proportion de ce qui lui est dû. La réserve n'est pas une dette : ce qui lui manque n'est pas reporté.

**Seuil** (cartes par mois) = (charges + salaires + mensualité de la dette) ÷ ((1 − réserve) × marge par carte),
arrondi au-dessus. Par défaut : 4 920 ÷ 9 = **547 cartes** ; **436** une fois la dette remboursée. La vue montre aussi
le seuil « de ce mois-ci », qui tient compte des arriérés et des autres revenus.

**Projection de fin de mois** (mois en cours seulement) : cartes vendues + rythme × jours restants, où le rythme =
cartes vendues ÷ jours écoulés (aujourd'hui compris). Les jours comptent depuis le 1er, ou depuis le début de la
campagne de ventes s'il tombe dans le mois (16 novembre 2026). La cascade est recalculée sur cette projection.

**Contrôle avec les valeurs par défaut** (4 personnes payées) :

| Cartes | Marge | Réserve | Surplus | Prime par personne | Total par personne |
|---|---|---|---|---|---|
| 1 000 | 10 000 $ | 1 000 $ | 4 080 $ | 510 $ | 1 010 $ |
| 3 000 | 30 000 $ | 3 000 $ | 22 080 $ | 2 760 $ | 3 260 $ |

Les primes se partagent au centime : les centimes qui ne se divisent pas vont à la croissance.

## Le relevé, par personne

Pour chaque personne : **salaire de base** (ce que la marge en couvre, arriéré compris), **prime**, **total dû**
(salaire couvert + prime), **déjà versé** (versements enregistrés pour ce mois) et **reste à verser**.
Ce qui manque au salaire part en arriéré sur le mois suivant.

## Les journaux

- **Dépenses réelles** (date, catégorie, montant, payé par, note). Tout membre peut **proposer** une dépense.
  - Jusqu'au seuil d'accord (200 $) : un responsable la **valide** (la sienne est validée tout de suite).
  - Au-dessus : chaque personne payée (responsable ou membre) doit cliquer **« J'approuve »** ; proposer compte comme
    son propre accord. La liste des accords attendus est figée au moment de la proposition. La dépense est
    **validée** quand tout le monde a approuvé ; un responsable ne peut pas passer outre, mais il peut la **refuser**.
  - Les personnes dont l'accord est attendu reçoivent une notification.
  - Dans le relevé, une dépense validée dont la catégorie porte le nom d'une charge fixe s'affiche à côté de cette
    charge (« dépensé ») ; les autres (matériel, transport…) sont signalées comme prises sur la croissance.
    Les dépenses réelles ne changent pas le calcul de la cascade.
- **Versements** : salaire ou prime versé à une personne pour un mois (date, montant, moyen : MonCash, NatCash,
  HandyPay ou espèces). Responsables seulement.
- **Dette** : journal des remboursements ; **solde = solde initial − remboursements**. La mensualité d'un mois est
  plafonnée au solde du 1er de ce mois (remboursements datés d'avant). On ne peut pas rembourser plus que le solde.
- **Autres revenus** : responsables seulement ; ils s'ajoutent à la marge du mois de leur date.

Rien n'est effacé : annuler un versement, un remboursement ou un revenu pose `cancelled_at` (et `cancelled_by`).
Les actions financières ne vont **pas** dans le fil d'activité de l'espace, que les observateurs lisent : chaque ligne
garde qui l'a saisie, validée, refusée ou annulée.

## Droits

| | Responsable | Membre | Observateur |
|---|---|---|---|
| Lire le relevé, les dépenses, la dette, les réglages | oui | oui | 403 |
| Proposer une dépense, « J'approuve » | oui | oui | 403 |
| Valider ou refuser une dépense | oui | non | 403 |
| Versements, remboursements, autres revenus | oui | non | 403 |
| Changer les réglages, exporter le relevé (CSV) | oui | non | 403 |

Comme partout dans l'espace, toute requête qui modifie exige l'en-tête CSRF.

## Réglages (responsables)

Rangés dans `workspace_settings`, clé `finances` : début du suivi (mois à partir duquel les arriérés se reportent),
charges fixes (liste), personnes payées et salaire de base de chacune, dette (créancier, solde initial, mensualité),
réserve (%), part du surplus en primes (%), seuil d'accord ($). Tant que la liste des personnes payées n'a jamais été
enregistrée, elle suit l'équipe : chaque membre actif hors IA et observateurs, à 500 $.

**Rien n'est figé** : le relevé d'un mois se recalcule à chaque lecture avec les réglages actuels. Changer un salaire
ou une charge change donc aussi les mois passés (et leurs arriérés).

## API

Toutes sous `/api/ws`, avec la session de l'espace.

| Méthode | Chemin | Qui |
|---|---|---|
| GET | `/finances?mois=AAAA-MM` (mois en cours par défaut) | responsable, membre |
| GET | `/finances/debt` | responsable, membre |
| GET, PUT | `/settings/finances` | lecture : responsable, membre ; écriture : responsable |
| GET | `/finances/export.csv?mois=AAAA-MM` | responsable |
| POST | `/finances/expenses` | responsable, membre |
| POST | `/finances/expenses/{id}/approve` | personne dont l'accord est attendu |
| POST | `/finances/expenses/{id}/validate`, `/finances/expenses/{id}/refuse` | responsable |
| POST, DELETE | `/finances/payouts`, `/finances/payouts/{id}` | responsable |
| POST, DELETE | `/finances/debt/payments`, `/finances/debt/payments/{id}` | responsable |
| POST, DELETE | `/finances/incomes`, `/finances/incomes/{id}` | responsable |

L'export CSV (séparateur `;`, encodage UTF-8 avec BOM pour Excel) écrit les montants comme des nombres
(`1920`, `745,12`) pour que le tableur puisse les additionner.

## Base de données

Exécuter `sql/finances.sql` **après** `sql/schema.sql` (il réutilise la fonction `workspace_touch()`). Il crée
`workspace_fin_expenses`, `workspace_fin_approvals`, `workspace_fin_payouts`, `workspace_fin_debt_payments` et
`workspace_fin_incomes`, avec la RLS activée et forcée, sans policy, et aucun droit pour `anon` et `authenticated`.
En local, `app/localdb.py` connaît les mêmes tables.

## Vérifier

```powershell
.\.venv\Scripts\python.exe tests\run_verify.py --port 8803 --extra tests\verify_finances.py
```
