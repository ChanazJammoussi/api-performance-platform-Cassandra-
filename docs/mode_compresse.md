# Mode « journée compressée » — évaluation accélérée

Référence de conception : [`design-spec.md`](design-spec.md) §5.3 — *« In the demo the "week" is
the compressed seasonality cycle of the load generator »*.

> **Avertissement.** Ce mode sert à **tester la plateforme et obtenir des résultats honnêtes dans
> un temps raisonnable** pour un projet de stage. C'est une évaluation **accélérée et
> synthétique** : elle **ne prouve pas** les performances de Cassandra en production.

## 1. Principe

`k6/load.js` (non modifié) simule une journée de trafic en **2 heures** : montée le matin, pic à
midi, descente le soir, creux la nuit (5 → 50 → 5 utilisateurs simulés).

Cassandra range chaque mesure dans une case `(jour, heure)` de la baseline. Une seule fonction
calcule cette case pour tous les composants : `detection-service/season.py`.

| Mode (`SEASON_MODE`) | Case `(dow, hour_bucket)` | Usage |
|---|---|---|
| `reel` (défaut) | jour PostgreSQL (0 = dimanche) × heure UTC | comportement historique, inchangé |
| `compresse` | `(0, heure simulée 0..23)` | journées de `load.js` |

En mode `compresse` :
- une journée simulée dure **120 minutes réelles** et commence **à chaque heure paire UTC**
  (00:00, 02:00, …) ;
- une heure simulée dure **5 minutes réelles** (exemple : 13:37 UTC → minute 97 du cycle → heure
  simulée 19) ;
- le jour vaut **toujours 0** : `load.js` rejoue la même journée, il n'y a pas de semaine.

Composants qui utilisent `season` : `baseline_job.py` (SQL), `detector.py`, `explainer.py`,
`train_model.py` (`baseline_for`), `validation_checks.py`, `evaluate_layered.py`,
`ml_model.py` (mode inscrit dans le modèle), `scenario-runner/run_evaluation_suite.py` (mode
inscrit dans le manifeste).

## 2. Isolation des données

Rien n'est supprimé ni déplacé :

- **projet Docker séparé** `cassandra_fresh`, avec ses propres volumes neufs (base,
  Prometheus, Grafana, modèles). Les volumes du projet `cassandra` restent intacts. Les noms de
  conteneurs et les ports étant fixes, l'ancienne stack doit être arrêtée **sans `-v`** avant ;
- **ground truth isolée** : `scenario-runner/results/compresse/` (les anciennes campagnes ne sont
  ni lues ni modifiées) ;
- **modèle** : `meta.season` est inscrit à l'entraînement ; un modèle d'un autre mode est
  refusé (`incompatible`) ; un ancien modèle sans mode est considéré comme `reel` ;
- **campagnes** : `manifest.season` est inscrit ; à l'évaluation, un mode absent ou différent,
  ou une baseline contenant des lignes de l'autre mode, rend la campagne **impossible**.

`docker-compose.compresse.yml` (complément de `docker-compose.yml`, qui n'est pas modifié) :
`SEASON_MODE=compresse` pour `baseline`, `detector`, `trainer`, `model-monitor` ; Slack et Gemini
**désactivés** (valeurs vides) ; `trainer` en **démarrage manuel** (profil `manuel`).

## 3. Détecteur : une mesure, un seul traitement

- Chaque mesure du scraper est complète dès son écriture (fenêtre `rate[5m]` close). Elle est
  traitée **une seule fois** ; la dernière mesure traitée par endpoint est reprise de l'anomaly
  store au redémarrage.
- Une mesure de plus de **180 s** n'est **pas traitée** et l'automate d'alertes **n'avance
  pas** sans nouvelle mesure (métrique `cassandra_detector_endpoints_stale`). Cela corrige le
  défaut constaté à l'audit : une mesure du 04/09 était recalculée à chaque minute.
- La case de baseline vient de **l'heure de la mesure** (comme le rejeu hors ligne) ; les dates
  d'alerte (`opened_at`, `resolved_at`) restent **l'heure réelle de décision**.

## 4. Déroulement prévu (chaque étape demande une autorisation)

1. Vérifier que SQL et Python donnent la même case (requête en lecture seule).
2. Arrêter l'ancienne stack sans `-v`, puis :
   `docker compose -p cassandra_fresh -f docker-compose.yml -f docker-compose.compresse.yml up -d --build`.
3. Essai court de 3 journées : `bash scripts/run_compressed_days.sh 3`.
4. Collecte de 7 journées (14 h réelles) : `bash scripts/run_compressed_days.sh 7`.
5. Verdict : `python baseline_readiness.py --journal <journal>` (dans un conteneur du projet).
6. Entraînement manuel du modèle, une fois.
7. Campagne de pannes **pendant de nouvelles journées `load.js`** (mode `hote`), lancée
   **sous Windows** (Git Bash avec le Python de Windows, pas WSL) :
   `SEASON_MODE=compresse python run_evaluation_suite.py --results-dir results/compresse`.
8. Évaluation : `python evaluate_layered.py --input <campagne>`.

`scripts/run_compressed_days.sh N` lance `load.js` **inchangé**, une fois par heure paire UTC
(retard toléré ≤ 60 s, non cumulatif), et écrit un journal
`scenario-runner/results/compresse/journal_journees.tsv`. Arrêt propre : `touch <journal>.stop`.

### k6 sous Windows (mode `hote`)

Sur la machine de développement, **l'horloge interne de la VM Docker/WSL2 est ralentie**
(mesuré le 25/10/2026 : une attente de 30 s dure 30,4 à 32 s réelles, soit +1,4 à +6,9 %,
avec la source `tsc` comme avec `hyperv_clocksource_tsc_page`). L'heure murale de la VM
reste juste (recalée sur Windows) : les heures simulées des mesures sont donc correctes,
mais **les durées mesurées dans un conteneur sont trop longues**. k6 lancé dans Docker ferait
durer chaque journée de `load.js` environ 125 min au lieu de 120, et manquerait l'heure
paire suivante. L'horloge de Windows, elle, est juste (compteur matériel et heure système
identiques à 0,00 % près).

Le script accepte donc un mode `hote`, qui lance `k6.exe` sous Windows (k6 v2.3.0 portable,
`C:\Users\chana\tools\k6\k6.exe`, archive officielle `grafana/k6` vérifiée par SHA-256) :

```bash
K6_RUNNER=hote K6_BIN=/c/Users/chana/tools/k6/k6.exe \
  bash scripts/run_compressed_days.sh 7 2>&1 | tee -a scenario-runner/results/compresse/run_7_journees.log
```

- `load.js` est inchangé ; il vise `http://localhost:8000` (port publié du gateway) ;
- `--no-usage-report` : k6 n'envoie aucune statistique d'usage ;
- même alignement sur les heures paires, même journal ; `hote` est le mode par défaut,
  `docker` reste disponible (`K6_RUNNER=docker`) mais la campagne compressée le refuse ;
- interruption de la journée en cours : `taskkill //PID <pid_windows>` (fichier `<journal>.k6.pid`).

Le trafic passe par le relais réseau de Docker Desktop (quelques ms pour k6, sans effet sur
les latences mesurées par les services eux-mêmes). **La campagne de pannes devra utiliser le
même chemin** (k6 sous Windows) que la baseline. Le scraper, le détecteur et `baseline_job`
restent dans la VM : leurs cycles s'allongent d'environ 5 % (scraper ≈ 64 s, soit environ
33 mesures par case sur 7 journées), sans effet sur les critères du verdict.

**Contrôle avant chaque scénario de la campagne** (`check_processes()` de
`run_evaluation_suite.py`) : en mode `compresse`, il exige exactement **un** `k6.exe` sous
Windows avec la commande du mode `hote` (`run --quiet --no-usage-report <dépôt>/k6/load.js`)
et **aucun** conteneur k6 (reconnu par son nom, son image ou son exécutable). Lancée depuis
WSL ou Linux, la suite ne voit pas les processus Windows et refuse de démarrer. Ce contrôle
repose sur des **indices** : il ne vérifie ni la cible du trafic (`BASE_URL`), ni le processus
parent, ni la continuité du trafic pendant un scénario, ni les sources qu'il ne sait pas
reconnaître (k6 sous WSL, autre machine, autre outil de charge).

## 5. Quand la baseline est-elle prête ?

`baseline_readiness.py` (lecture seule) répond **PRÊTE** seulement si **tous** les critères
sont remplis :

| Critère | Seuil |
|---|---|
| Journées k6 | ≥ 7 consécutives, code de sortie 0, démarrage ≤ 60 s après l'heure paire |
| Continuité des mesures | tous les endpoints, aucun trou > 180 s |
| Pannes | aucune ground truth pendant la collecte |
| Cases | 6 endpoints × 6 métriques × 24 heures simulées = **864/864**, toutes avec `dow = 0` |
| Mesures par case | ≥ 25 (≈ 33 attendues : le scraper tourne toutes les ≈ 62,6 s, soit 4 à 5 mesures par heure simulée et par journée) |
| Bandes | valides pour latences et trafic ; bandes nulles des taux d'erreur signalées sans bloquer |
| Forme du trafic | corrélation ≥ 0,9 entre le rps médian par heure simulée et le profil de `load.js` |

Il faut **au moins 3 journées** pour atteindre le minimum de 10 mesures par case de
`baseline_job.py`.

## 6. Limites à présenter dans le rapport

1. **Évaluation accélérée et synthétique** : un seul profil k6, trois services de
   démonstration, une journée de 2 heures. Les résultats valident le **fonctionnement** et la
   **comparaison des couches S/B/C/N dans les mêmes conditions** ; ils ne mesurent **pas** les
   performances en production.
2. **Le temps du trafic est compressé, pas celui des pannes** (90 à 120 s). Une panne dure
   presque une demi-heure simulée : le rapport entre vitesse du trafic normal et vitesse des
   pannes n'est pas réaliste.
3. **Montées de charge rapides** (5 → 50 utilisateurs en 20 minutes réelles) : elles peuvent
   ressembler à une dégradation progressive (`latency_creep`) et rendre ce type de panne plus
   difficile à distinguer.
4. **Pas de saisonnalité hebdomadaire** : toutes les journées sont identiques.
5. **Lissage de 5 minutes** (`rate[5m]`) = une heure simulée entière : la courbe est en retard
   d'environ une demi-heure simulée, de la même façon pour la baseline et la détection ; les
   bandes sont plus larges aux changements de pente de `load.js` (heures simulées 4, 12, 16, 22).
6. **Délai de détection** : en direct, il est mesuré à l'heure réelle de décision ; dans le
   rejeu hors ligne, à l'heure de la mesure (écart ≤ ~60 s).
7. **Détecteur** : seule la mesure la plus récente de chaque endpoint est examinée à chaque
   cycle ; si deux mesures arrivent entre deux cycles (rare), la plus ancienne n'est pas traitée
   en direct, alors que le rejeu la traite. Une alerte en cours reste figée tant qu'aucune
   mesure fraîche n'arrive.
8. **Horloge de l'environnement** : la VM Docker/WSL2 de la machine de développement mesure
   les durées environ 5 % trop longues (variable). k6 tourne sous Windows pour garder des
   journées de 120 min exactes ; les services restent dans la VM (cycles du scraper ≈ 64 s).

Phrase proposée pour le rapport : *« Évaluation sur des journées simulées de 2 heures (horloge
saisonnière compressée, prévue par la spécification §5.3) : les résultats valident le
fonctionnement de bout en bout et le comportement relatif des couches S, B, C et N ; ils ne
constituent pas une mesure de performance en conditions réelles. »*
