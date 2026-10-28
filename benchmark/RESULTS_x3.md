# Benchmark Cassandra -- Campagne x3 (protocole gele)

## Configuration figee (reproductibilite)

- **Modele** : `iforest_20260802_132542.joblib`, SHA256 `f2a5e3caec0094935750c7a0558b3fe2b26e1e0e723372fcd66181f3b0a8413d`
- **Baseline** : snapshot `endpoint_baseline_snapshot_20260802_132441.sql` (md5 donnees triees `1b6865c1...`), 1710 buckets, conteneur baseline gele (arrete) pendant train+campagne+eval
- **Seuils** : `FIRE_THRESHOLD=0.60`, `M_WINDOWS=2`, `GRACE=120s`, `PAD=10min`, `SCRAPE_INTERVAL=60s`
- **Evaluation** : `evaluate_layered.py --persist` en conteneur (`cassandra-trainer`), modele charge (`modele ML : charge`) pour les 3 campagnes
- **Trafic nominal** : k6 `load.js` maintenu par `keep_k6_alive.sh` (relance ~2h) sur toute la duree

## Campagnes

| Campagne | run OVERALL | span (h) | DR static | DR layered | FP static | FP layered |
|---|---|---|---|---|---|---|
| campaign_20260802_1457 | rep 1 | 4.73 | 0.700 | 0.700 | 0 | 1 |
| campaign_20260802_1932 | rep 2 | 4.82 | 0.667 | 0.667 | 9 | 10 |
| campaign_20260803_0012 | rep 3 | 4.80 | 0.667 | 0.667 | 2 | 3 |
| **Moyenne (n=3)** | | | **0.678** | **0.678** | | |

- FP/heure moyen : **static 0.76** / **layered 0.97**
- 30 fenetres d'injection par campagne, 6 endpoints

## Detection rate par fault_type (moyenne des 3 campagnes, magnitude=all)

| fault_type | DR static | DR layered | delai static (s) | delai layered (s) |
|---|---|---|---|---|
| downstream_slow | 0.000 | 0.000 | -- | -- |
| error_burst | 0.867 | 0.867 | 140 | 140 |
| latency_creep | 1.000 | 1.000 | 163 | 163 |
| latency_step | 0.767 | 0.767 | 115 | 115 |
| pool_shrink | 0.000 | 0.000 | -- | -- |

## Resultat principal (a defendre dans le rapport)

**Contre une baseline saisonniere mure, la couche ML (Isolation Forest, layer 2) n'apporte
AUCUN gain de detection rate ni de delai sur les 3 campagnes : `DR layered == DR static` et
`delai layered == delai static` pour chaque type de faute et chaque campagne.** Le seul effet
net de la couche layered est un LEGER surcout de faux positifs (FP/h 0.97 vs 0.76, ~+1 FP par
campagne), pour zero gain de rappel.

Lecture : le signal supplementaire (baseline + ML combine) ne franchit jamais le seuil
`FIRE_THRESHOLD=0.60` dans une fenetre de degradation ou le detecteur static ne se declenche
pas deja. Ce resultat est coherent avec l'hypothese posee avant le benchmark : l'apport apparent
du ML dans l'ancien benchmark (baseline degradee) etait en grande partie un artefact de baseline
faible. Resultat honnete et reproductible (modele + baseline figes, 3 repetitions).

**Angles morts detectes** : `downstream_slow` et `pool_shrink` ne sont detectes ni par static ni
par layered (DR 0%) -- limite reelle de la detection sur les features scorees, a documenter.

## Fichiers

- `run_campaign_x3.log` -- log complet des 3 campagnes
- `eval_runs_x3_<stamp>.csv` -- export brut de la table eval_runs (par fault_type x magnitude + OVERALL)
- `endpoint_baseline_snapshot_20260802_132441.sql` -- baseline gelee
- `iforest_20260802_132542.joblib` -- modele gele
- `results/campaign_2026080*/` -- ground_truth + manifest de chaque campagne
