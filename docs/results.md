# Résultats & messages clés — Cassandra

Synthèse des résultats mesurés, prête pour la soutenance. Chiffres issus de la campagne
d'évaluation (`evaluate_layered.py` → table `eval_runs`, ~20 scénarios × 2 magnitudes,
31 injections) et des outils d'évaluation dédiés.

---

## 1. Livrable phare — détecteur en couches vs seuils statiques (§9.3)

> **La thèse du projet est relative** : détecter *plus* et *plus tôt* que le seuillage statique,
> sans exploser les faux positifs. C'est mesuré.

| Métrique | Static (Layer 0) | **Layered** (baseline + ML) |
|---|---|---|
| **Detection rate** (global) | 65 % | **71 %** |
| Faux positifs / heure | 1.13 | **1.29** |
| Délai de détection médian | — | **124 s** |

**Par type de faute :**

| Type de faute | Static | Layered | Gain |
|---|---|---|---|
| `latency_creep` | 100 % | 100 % | = |
| `downstream_slow` | 100 % | 100 % | = |
| `latency_step` | 80 % | **100 %** | **+20 pts** |
| `error_burst` | 50 % | 50 % | = (signal erreur, commun) |
| `pool_shrink` | 0 % | 0 % | = (faute subtile, non détectée sur p99) |

→ Le layered **domine sur `latency_step`** (dégradations progressives que le seuil rate au début)
pour un surcoût de faux positifs **modeste** (+0.16 FP/h).

## 2. Courbe de sensibilité (magnitude)

| Faute | Core | Stress |
|---|---|---|
| `error_burst` (detection) | 40 % | **60 %** |
| `latency_step` (layered) | 100 % | 100 % |

→ Le taux de détection **croît avec la magnitude** (forme attendue) ; l'avantage layered sur
`latency_step` est **stable** aux deux intensités.

## 3. Autres résultats mesurés

| Dimension | Résultat |
|---|---|
| **Attribution déploiement** | La démo `bad_deploy` nomme le **bon commit/déploiement** en minutes (score de corrélation ~0.9, fenêtre causale 30 min) |
| **Qualité des explications LLM** | **5.00 / 6** (rubrique : structure, chiffres cohérents, actionnabilité, cadrage de l'incertitude) |
| **Supervisé vs non-supervisé** | PR-AUC **0.167** (Isolation Forest) vs **0.164** (gradient boosting) → le supervisé **n'apporte aucun avantage décisif** |
| **Tuning** | `contamination` **inerte** (n'affecte pas `score_samples`) ; seuil de déclenchement **0.60** = F1 max |
| **Alerte précoce TTD** | Extrapolation Theil-Sen vers le SLO (advisory) |
| **Compression TimescaleDB** | ratio **~6×** (append-only, chunks > 7 j) |
| **Tests** | **68** tests unitaires, CI verte |

## 4. Messages clés à défendre devant le jury

1. **« Le layered détecte plus, sans exploser les faux positifs — c'est chiffré. »**
   71 % vs 65 %, +20 pts sur `latency_step`, pour +0.16 FP/h seulement.

2. **« L'hygiène de la baseline est déterminante, et je l'ai mesurée. »**
   Baseline calculée sur des données incluant les fautes → p90 gonflé à **1391 ms** ; en excluant
   les injections → **317 ms**. Sans cette exclusion, le direction gating classe la dégradation en
   « normal » et les couches 1/2 se taisent. → `baseline_job` et `train_model` excluent les injections.

3. **« Le paramètre "évident" (contamination) est inerte ; le vrai levier est le seuil. »**
   Découverte empirique : scikit-learn n'applique `contamination` qu'à `predict()`, pas à
   `score_samples()` que l'on calibre. Le F1 est maximal au **seuil 0.60**.

4. **« Le supervisé ne bat pas le non-supervisé ici — donc le choix non-supervisé est justifié. »**
   PR-AUC quasi identique, alors que le supervisé exige des labels d'incidents rares et consomme
   le jeu de test. Awareness de l'alternative + mesure du gap (spec §8.2).

5. **« Le pipeline ML est autonome et sûr. »**
   Détecter le drift (KS live vs référence) → réentraîner (nightly) → **valider avant de promouvoir**
   (sanity-gate + recall/FP non régressifs + fraîcheur des données). Aucune dégradation silencieuse.

## 5. Robustesse & ingénierie (au-delà des features)

- **Reproductibilité** : `docker compose up` reconstruit toute la plateforme ; `scripts/demo.sh`
  rejoue la démo bout-en-bout.
- **Tests + CI** : 68 tests unitaires (logique pure), GitHub Actions à chaque push.
- **Observabilité de la plateforme** : dashboard self-observabilité + **`/metrics` Prometheus natif**
  du détecteur (durée de cycle p95 ~47 ms, alertes par état, taux LLM, fraîcheur).
- **Dette maîtrisée** : compression + rétention TimescaleDB (~6×), pas de croissance illimitée.
- **Sécurité** : secrets via environnement, audit documenté (`docs/security.md`).

## 6. Limites honnêtes (assumées, à l'oral)

- **`pool_shrink` non détecté** sur le signal p99 aux magnitudes testées — faute de saturation
  subtile ; piste : intégrer les métriques de saturation (`pool_wait_ms`) comme features optionnelles.
- **Baseline compressée** : la saisonnalité de démo (journée en 2 h) surestime la qualité de baseline
  vs un vrai cycle diurnal — documenté, clés `dow`/`hour` period-agnostic.
- **PII en denylist** (allowlist recommandée mais casse spanmetrics — à retravailler).
- **Cold-start** : un endpoint sans baseline neutralise la couche ML (direction indéterminable).

---

*Détails : [`architecture.md`](architecture.md) · [`design-spec.md`](design-spec.md) ·
[`security.md`](security.md). Dashboards : Grafana `http://localhost:3000`.*
