# Analyse de devis PDF

Application en ligne de commande pour inventorier, extraire, contrôler et
consolider des lignes de prix de devis de construction français. Les documents
restent locaux, sauf les pages envoyées explicitement à Azure pour OCR et
extraction. L'application n'invente pas les prix manquants et conserve la
provenance des données.

> **Abonnement Azure nécessaire pour l'extraction complète :** l'OCR et
> l'extraction IA des devis utilisent Azure AI Document Intelligence et Azure
> OpenAI, qui doivent être configurés dans un abonnement Azure et peuvent
> entraîner des frais. Sans Azure, l'inventaire local et la consolidation de
> résultats existants restent disponibles, mais l'étape `integrate` ne fait
> qu'un précontrôle et n'extrait pas le contenu du PDF.

## Prérequis

- Python 3.11 ou plus récent.
- Azure CLI et Bicep pour l'étape facultative de déploiement.
- Pour l'extraction distante : des ressources Azure Document Intelligence et
  Azure OpenAI compatibles avec les sorties structurées du client.
- Pour le mode local : OCRmyPDF/Tesseract et les données de langue nécessaires
  au système d'exploitation.

Installation depuis la racine du dépôt :

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Les commandes principales sont `devis-mngt --help` et
`devis-mngt <commande> --help`.

## Démarrage en trois étapes

Chaque étape est lancée séparément. Les étapes 2 et 3 écrivent dans un espace
de travail dédié et ne modifient jamais automatiquement le cumulatif historique
`outputs/compet.xlsx`.

### Étape 1 — Déployer l'infrastructure Azure

Cette étape est facultative si les ressources compatibles existent déjà. Le
projet fournit un modèle Bicep et un exemple de paramètres; il ne provisionne
rien à l'installation.

```bash
test -e infra/main.parameters.json || \
  cp infra/main.parameters.example.json infra/main.parameters.json
```

Renseigner `infra/main.parameters.json` avec l'abonnement, le groupe de
ressources, la région, les noms de ressources disponibles, l'identité Entra
autorisée et un horodatage UTC. Remplacer toutes les valeurs d'exemple,
notamment les UUID nuls. Ne pas versionner ce fichier privé.

Prévisualiser sans déployer :

```bash
devis-mngt infra \
  --parameters infra/main.parameters.json \
  --settings results/azure-settings.json \
  --create-resource-group
```

Si le groupe est absent, cette commande l'indique mais ne fait pas de
prévisualisation complète et ne crée pas le groupe. Après vérification des
changements, des coûts, de la disponibilité du modèle et de la résidence des
données, autoriser explicitement le déploiement :

```bash
devis-mngt infra \
  --parameters infra/main.parameters.json \
  --settings results/azure-settings.json \
  --create-resource-group --apply
```

`--apply` lance un `what-if` avant le déploiement et refuse les changements de
type suppression. Avec `--create-resource-group`, un groupe absent peut être
créé avant ce `what-if`; en cas d'échec du déploiement, il peut rester présent.
Les opérations Azure nécessitent `az login` et les droits de déploiement et
d'attribution des rôles. Le déploiement n'analyse aucun PDF. En cas de succès,
les endpoints et la révision du modèle sont sauvegardés dans le fichier JSON
privé `--settings`, sans clés.

### Étape 2 — Intégrer un PDF

Précontrôle local, sans Azure et sans créer de résultats :

```bash
devis-mngt integrate \
  --input /chemin/vers/devis.pdf \
  --workspace results/nouveau-corpus
```

Ce mode vérifie seulement que le fichier est lisible. **Il ne réalise pas
l'OCR ni l'extraction, et ne l'intègre pas au corpus.**

Pour effectuer l'extraction avec Azure :

```bash
devis-mngt integrate \
  --input /chemin/vers/devis.pdf \
  --workspace results/nouveau-corpus \
  --settings results/azure-settings.json \
  --max-ocr-pages 50 \
  --max-llm-calls 100 \
  --allow-cloud
```

L'option `--allow-cloud` est obligatoire pour autoriser OCR/IA distants. Si
`--settings` n'est pas fourni, les paramètres Azure doivent être chargés dans
l'environnement du shell. `.env.sample` donne la liste des variables; le
programme ne lit pas `.env` automatiquement :

```bash
test -e .env || cp .env.sample .env
# Après avoir renseigné .env :
set -a
source .env
set +a
```

Les deux plafonds sont **par exécution**, pas un budget monétaire ni une limite
cumulative. Ils empêchent de démarrer un appel qui dépasserait le plafond.
Une limite atteinte est signalée comme telle et le PDF apparaît dans le rapport
d'erreurs; relancer l'intégration après ajustement permet de reprendre les
étapes mises en cache. Les appels déjà effectués restent facturables.

Répéter l'étape 2 pour chaque PDF du même corpus, en réutilisant exactement le
même workspace. Chaque source a son cache et son résultat; réintégrer un même
chemin met à jour son résultat. Garder les PDF hors du workspace. Le corpus
actuel, son registre `compet.campaign.json` et son cache ne sont pas utilisés
par ces commandes.

#### Que signifie « hors ligne » ?

- `integrate` sans `--allow-cloud` fait uniquement le précontrôle local : ce
  n'est pas une analyse ni une intégration complète.
- Les PDF illisibles sont conservés comme erreurs et apparaîtront dans le
  rapport d'erreurs, même si aucun appel Azure n'est effectué.
- Un PDF lisible ne peut pas obtenir les lignes extraites par le moteur
  standard sans Azure. L'extraction locale OCR/LLM de bout en bout n'est pas
  implémentée dans cette commande.
- `consolidate` est hors ligne : il assemble les résultats déjà enregistrés et
  ne refait aucun OCR ou appel IA.
- La commande `run --local-first --local-ocr` existe pour essayer une lecture
  PDF et un OCR locaux avant le chemin Azure de secours. Ce n'est pas un mode
  hors ligne garanti : le traitement peut appeler Azure; ne pas l'utiliser
  pour un traitement déconnecté.

### Étape 3 — Consolider et récupérer les rapports

```bash
devis-mngt consolidate --workspace results/nouveau-corpus
```

Cette étape n'appelle ni Azure ni le réseau. Elle produit une génération
immuable sous `results/nouveau-corpus/reports/`, ainsi qu'un manifeste privé
`results/nouveau-corpus/latest-report.json` contenant les chemins absolus, les
SHA-256 et les compteurs :

- **`prices.xlsx`** : base de prix acceptée, traçabilité, données brutes,
  documents, lignes à vérifier et écarts de couverture OCR.
- **`errors.xlsx`** : documents en erreur, non-devis ou sans lignes, lignes à
  vérifier et lignes OCR tarifées non rapprochées.

Les générations précédentes sont conservées. Il faut relancer l'étape 3 après
de nouvelles intégrations pour actualiser les rapports. Les lignes qui échouent
aux contrôles restent exclues de la base acceptée mais visibles dans l'audit.
Des lignes identiques peuvent être regroupées tout en gardant leurs sources;
les prix différents ne sont pas moyennés ni fusionnés. Le code de sortie est
`0` si une base acceptée existe, `1` si des erreurs documentaires sont présentes,
`2` en cas de configuration, d'usage ou de limite, et `3` si aucune ligne n'est
acceptée. Examiner aussi `À vérifier` même si le code est `0`.

## Autres commandes utiles

Inventaire PDF local, sans appels Azure :

```bash
devis-mngt inventory --input /chemin/vers/un-dossier
```

Analyser un dossier avec la commande générale, après vérification des coûts et
accord explicite :

```bash
devis-mngt run \
  --input /chemin/vers/nouveaux-pdf \
  --output outputs/nouveaux-devis.xlsx \
  --cache .devis-cache/nouveaux-devis \
  --log-dir logs/nouveaux-devis \
  --pdf-only --max-ocr-pages 50 --max-llm-calls 100 --allow-cloud
```

Régénérer un export hors ligne à partir d'un run terminé :

```bash
devis-mngt export \
  --results .devis-cache/nouveaux-devis/results.json \
  --output outputs/nouveaux-devis.xlsx \
  --log-dir logs/nouveaux-devis
```

`run` et `export` ne remplacent pas un classeur de campagne accompagné de son
`*.campaign.json` ou d'un verrou. Ils ne fusionnent pas avec `compet.xlsx`.
Pour reprendre une campagne approuvée, utiliser son manifeste et la commande
`wave` décrite dans [infra/USAGE.txt](infra/USAGE.txt), pas une commande générique
sur tout le dossier `devis/`.

## Données, sécurité et sauvegardes

Les PDF sont ouverts en lecture seule. Les caches peuvent contenir du texte
OCR sensible; les espaces de travail, résultats, rapports et journaux doivent
rester privés. `.gitignore` exclut les données de devis, les fichiers de
résultats, les caches, les sorties Excel et `.env`. Ne pas utiliser `git add -f`
pour contourner ces règles. Le contrôle de publication est :

```bash
python scripts/check_public_files.py --include-untracked
```

Sauvegarder ensemble les originaux, caches, résultats, journaux et sorties
privées hors traitement. Pour le corpus historique, le classeur
`outputs/compet.xlsx` et son registre `outputs/compet.campaign.json` forment une
paire : les conserver ensemble. Un clone Git ne restaure aucune donnée privée.

L'extraction n'est pas une validation métier exhaustive. Vérifier les prix,
les unités, les variantes, les documents en erreur, les lignes `À vérifier` et
les écarts de couverture avant utilisation. Les scores sont des indicateurs de
règles, pas des probabilités calibrées. Aucun rapprochement avec Batiprix ou
BatiChiffrage n'est effectué.

## Tests

Les tests ne font aucun appel Azure et s'exécutent avec :

```bash
python -m unittest discover -s tests -q
```

Pour les détails de déploiement, de reprise du corpus historique, des vagues et
des fichiers à préserver, consulter [infra/USAGE.txt](infra/USAGE.txt).
