# -*- coding: utf-8 -*-
"""
chapters.py — Contenu editorial du rapport Cassandra.

Chaque fonction retourne une liste de Flowables. `build_body()` assemble
l'ensemble des chapitres (chacun precede d'un saut de page et d'un bandeau titre).
Le texte est fidele au code et aux mesures reelles (docs/design-spec.md,
docs/architecture.md, docs/results.md).
"""

from reportlab.platypus import PageBreak, Spacer

import components as C
import diagrams as D
import charts as CH
import styles as S


# --------------------------------------------------------------------------- #
# Front matter : Resume (FR) + Abstract (EN)                                  #
# --------------------------------------------------------------------------- #

def resume_abstract():
    s = []
    s.append(C.ChapterBanner(None, "Résumé", color=S.PURPLE))
    s.append(C.spacer(6))
    s.append(C.h3("Contexte"))
    s.append(C.para(
        "Les plateformes applicatives modernes sont distribuées et évoluent en continu. "
        "Les approches traditionnelles d'observabilité restent encore largement fondées sur des <b>seuils statiques</b> : une "
        "alerte se déclenche quand une métrique dépasse une valeur fixe. Ce modèle ignore la "
        "<b>saisonnalité</b> du trafic (heures de pointe, jours ouvrés), génère du bruit, et "
        "détecte tard les dégradations progressives. Il n'explique ni la <b>cause</b> ni "
        "l'<b>action à mener</b>."))
    s.append(C.h3("Problème"))
    s.append(C.para(
        "Détecter une dégradation de performance d'API <i>plus tôt</i> et avec <i>moins de faux "
        "positifs</i> qu'un seuillage statique, tout en <b>attribuant</b> l'incident à un "
        "déploiement probable et en l'<b>expliquant</b> en langage naturel &mdash; tout en évitant "
        "une dépendance à des jeux de données historiques annotés, généralement difficiles à obtenir "
        "dans les environnements industriels."))
    s.append(C.h3("Solution"))
    s.append(C.para(
        "<b>Cassandra</b> apprend la signature normale de chaque endpoint via une <b>baseline "
        "saisonnière</b> conditionnée (jour x heure), puis combine cette déviation avec une "
        "<b>détection d'anomalies non supervisée</b> (Isolation Forest) sur des features "
        "<i>endpoint-relatives</i>. Un moteur de <b>corrélation</b> relie chaque dégradation aux "
        "déploiements récents ; une couche <b>LLM</b> produit un résumé d'incident actionnable "
        "envoyée vers Slack. Chaque faute injectée dans le banc d'évaluation est un label de "
        "vérité-terrain, ce qui rend la performance <b>directement mesurable</b>."))
    s.append(C.h3("Résultats"))
    s.append(C.para(
        "Une campagne historique comprenant 31 fenêtres d'injection valides a été utilisée pour l'évaluation. Le détecteur multicouche "
        "atteint un taux de détection de <b>71 %</b>, contre 65 % pour une approche basée "
        "uniquement sur des seuils statiques. L'amélioration atteint <b>+20 points</b> pour les "
        "scénarios d'échelon de latence (latency_step), avec une augmentation limitée du taux de faux positifs "
        "(+0,16 FP/h). Une évaluation qualitative des explications générées par le LLM montre une bonne pertinence "
        "des résumés d'incidents. Un "
        "comparatif supervisé (gradient boosting) obtient une PR-AUC quasi identique, ce qui "
        "<b>justifie le choix non supervisé</b>.<br/><br/>"
        "La plateforme est entièrement reproductible via Docker Compose et validée par une suite "
        "de 105 tests automatisés couvrant les principaux composants développés."))

    s.append(C.spacer(10))
    s.append(C.ChapterBanner(None, "Abstract", color=S.SECONDARY))
    s.append(C.spacer(6))
    s.append(C.para(
        "<b>Cassandra</b> is an observability platform that <b>detects</b>, <b>attributes</b> "
        "and <b>explains</b> API endpoint performance degradation. Rather than static threshold "
        "alerting, it learns the normal behavioral signature of each endpoint &mdash; a "
        "<b>seasonal baseline</b> conditioned on day-of-week and hour &mdash; and combines it "
        "with <b>unsupervised anomaly detection</b> (Isolation Forest) over endpoint-relative "
        "engineered features. A correlation engine links each degradation to recent deployment "
        "events, and an <b>LLM explanation layer</b> generates a plain-language incident summary "
        "delivered to Slack: what degraded, by how much versus the expected baseline, which "
        "deploy is suspected, and what to check first."))
    s.append(C.para(
        "Every injected fault is a <b>ground-truth label</b>, making detection rate, false "
        "positive rate and detection delay directly measurable against a static-threshold "
        "baseline. The layered detector reaches <b>71 %</b> detection versus 65 % for static "
        "thresholding (<b>+20 points on gradual latency-step faults</b>) at a modest false "
        "positive cost. The entire stack is reproducible with a single <font face='Courier'>"
        "docker compose up</font> and validated by 105 automated tests with continuous integration."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 1 — Contexte et problematique                                      #
# --------------------------------------------------------------------------- #

def ch1():
    s = [C.begin_chapter("Problématique technique et enjeux", S.PRIMARY), C.spacer(6)]
    s.append(C.lead(
        "Ce chapitre présente les limites des approches traditionnelles de supervision et la "
        "problématique à laquelle Cassandra cherche à répondre."))
    s.append(C.subh("Un paysage applicatif distribué et mouvant"))
    s.append(C.para(
        "Les applications modernes reposent sur des architectures distribuées dont le comportement "
        "varie selon les services, les périodes de trafic et les évolutions du système. Les "
        "dégradations de performance peuvent être causées par un déploiement, une saturation de "
        "ressources ou une propagation d'incident entre services."))
    s.append(C.subh("Les limites du seuillage statique"))
    s.append(C.para(
        "La supervision basée sur des seuils fixes présente plusieurs limites :"))
    for b in C.bullets([
        "<b>Sensibilité limitée à la saisonnalité</b> : un seuil unique ne reflète pas les "
        "variations normales du trafic.",
        "<b>Détection tardive</b> : les dégradations progressives peuvent être détectées après "
        "l'apparition de l'impact utilisateur.",
        "<b>Manque de contexte</b> : une alerte indique un symptôme sans fournir d'explication "
        "ni de cause probable.",
    ]):
        s.append(b)
    s.append(C.callout(
        "Une <b>baseline saisonnière</b> modélise la valeur <i>attendue</i> d'une métrique pour "
        "un endpoint donné à une heure et un jour donnés, sous forme d'une bande de quantiles "
        "(p10&ndash;p90). Une observation est anormale non pas dans l'absolu, mais "
        "<i>relativement</i> à cette bande.", kind="def", title="Définition — baseline saisonnière"))
    s.append(C.subh("Problématique retenue"))
    s.append(C.para(
        "La thèse du projet est <b>relative</b> et mesurable : détecter <i>plus</i> et <i>plus "
        "tôt</i> que le seuillage statique, avec un compromis maîtrisé sur le taux de faux positifs, "
        "puis <b>attribuer</b> et <b>expliquer</b> l'incident. En l'absence de données historiques d'incidents, "
        "l'approche principale repose sur la <b>détection non supervisée</b>, complétée par une "
        "comparaison avec une approche supervisée afin d'évaluer sa pertinence."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 2 — Objectifs et perimetre                                         #
# --------------------------------------------------------------------------- #

def ch2():
    s = [C.begin_chapter("Objectifs techniques et périmètre", S.SECONDARY), C.spacer(6)]
    s.append(C.subh("Objectifs"))
    for b in C.bullets([
        "Détecter les dégradations de performance par endpoint avec une <b>approche adaptative</b> "
        "et évaluer son apport face au seuillage statique.",
        "<b>Attribuer</b> chaque dégradation à une cause probable au niveau déploiement, via "
        "corrélation temporelle, avec un <b>score d'imputation dans [0, 1]</b> basé sur la "
        "proximité temporelle (corrélation, non causalité certaine).",
        "Générer des explications d'incident <b>actionnables</b> avec un LLM, déclenchées "
        "uniquement sur alerte confirmée (maîtrise du coût).",
        "S'intégrer à une chaîne d'observabilité standard basée sur <b>OpenTelemetry</b>.",
        "Mettre en place une <b>évaluation reproductible</b> à partir de scénarios de fautes "
        "contrôlées.",
    ]):
        s.append(b)
    s.append(C.subh("Hors périmètre"))
    s.append(C.para(
        "Afin de conserver un périmètre adapté à la durée du stage, les éléments suivants ne "
        "sont pas couverts :"))
    for b in C.bullets([
        "Prédiction garantie des incidents ou remplacement d'un système APM complet.",
        "Remédiation automatique des incidents.",
        "Analyse détaillée du code source ou profilage bas niveau.",
        "Gestion multi-tenant et mise à l'échelle horizontale avancée.",
        "Développement d'une interface dédiée, les visualisations étant réalisées avec Grafana.",
    ]):
        s.append(b)
    return s


# --------------------------------------------------------------------------- #
# Chapitre 3 — Architecture generale                                          #
# --------------------------------------------------------------------------- #

def ch3():
    s = [C.begin_chapter("Architecture générale", S.PURPLE), C.spacer(6)]
    s.append(C.lead(
        "La plateforme est un pipeline à sept étapes, du trafic synthétique jusqu'à la "
        "notification expliquée, entièrement orchestré par Docker Compose."))
    s.append(C.figure(D.pipeline_end_to_end(),
                      "Figure 3 — Pipeline end-to-end : génération de trafic, ingestion, "
                      "features/baseline, détection, corrélation, explication, notification."))
    s.append(C.subh("Les sept étapes"))
    s.append(C.para(
        "Ces étapes correspondent aux principaux composants fonctionnels de la plateforme, de la "
        "collecte jusqu'à l'explication de l'incident. Chaque étape correspond à un composant "
        "fonctionnel identifiable et peut être validée séparément, ce qui permet une validation "
        "incrémentale :"))
    s.append(C.data_table(
        ["#", "Étape", "Rôle"],
        [
            ["1", "Génération de trafic", "Microservices démo sous charge k6 diurnal, avec API de fault injection (dev/eval)."],
            ["2", "Ingestion", "OTel Collector + connecteur spanmetrics : métriques RED sur tout le trafic."],
            ["3", "Features & baseline", "Fenêtres par endpoint (p50/p95/p99, 4xx/5xx, rps) + baseline saisonnière."],
            ["4", "Détection", "Détecteur en couches (static + baseline + Isolation Forest) -> score d'anomalie."],
            ["5", "Corrélation", "Rapprochement des dégradations avec les déploiements dans une fenêtre temporelle de corrélation."],
            ["6", "Explication", "Un LLM assemble le contexte en un résumé d'incident lisible."],
            ["7", "Notification", "Alertes structurées vers Slack ; visualisation de l'état et de l'historique via Grafana."],
        ],
        col_widths=[0.6 * S.cm, 4.6 * S.cm, S.CONTENT_WIDTH - 0.6 * S.cm - 4.6 * S.cm],
        align_center_cols=[0]))
    s.append(C.subh("Services et ports"))
    s.append(C.data_table(
        ["Service", "Port (hôte)", "Rôle"],
        [
            ["gateway", "8000", "Point d'entrée, proxy vers orders / payments"],
            ["orders / payments", "8001 / 8002", "Services démo instrumentés OTel + API fault interne"],
            ["otel-collector", "4317/4318/8888", "spanmetrics (RED) + filtrage d'attributs -> Prometheus"],
            ["prometheus", "9090", "Stockage des métriques (remote-write)"],
            ["timescaledb", "5434 -> 5432", "Features, baseline, alertes, anomalies, déploiements"],
            ["detector", "&mdash;", "Détection en couches, state machine, corrélation, LLM"],
            ["trainer", "&mdash;", "Réentraînement nightly de l'Isolation Forest"],
            ["deploy-api", "8090", "Registre des déploiements (POST/GET /deploys)"],
            ["grafana", "3000", "Dashboards santé / évaluation / self-observabilité"],
        ],
        align_center_cols=[1],
        col_widths=[3.3 * S.cm, 3.0 * S.cm, S.CONTENT_WIDTH - 6.3 * S.cm]))
    s.append(C.callout(
        "La détection tourne sur une <b>cadence d'une minute</b>. Le délai entre l'événement et "
        "le message Slack est <b>de l'ordre de 3 minutes</b> dans le détecteur live : 3 cycles "
        "de 60 s sont nécessaires avant FIRING "
        "(<font face='Courier'>PENDING_WINDOWS=2</font>, compteur démarre à 0). "
        "Une règle de <i>watermark</i> exclut la dernière fenêtre incomplète "
        "(ne jamais agir sur des données partielles).", kind="tech"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 4 — Banc de fault injection                                        #
# --------------------------------------------------------------------------- #

def ch4():
    s = [C.begin_chapter("Génération de scénarios de panne et fault injection", S.GREEN), C.spacer(6)]
    s.append(C.lead(
        "Le banc de démonstration n'est pas un artefact secondaire : c'est la source des "
        "scénarios de faute contrôlés servant de vérité-terrain expérimentale et rendant "
        "l'évaluation quantitative possible."))
    s.append(C.subh("Microservices de démonstration"))
    s.append(C.para(
        "Trois services (<font face='Courier'>gateway</font>, <font face='Courier'>orders</font>, "
        "<font face='Courier'>payments</font>) forment une topologie d'appels réaliste "
        "(gateway -&gt; orders, gateway -&gt; payments -&gt; orders), chacun instrumenté OTel avec "
        "des routes templateées (<font face='Courier'>http.route</font> renseigné). Un pool de "
        "connexions PostgreSQL rend la saturation réaliste."))
    s.append(C.subh("Charge et saisonnalité"))
    s.append(C.para(
        "Le générateur k6 produit un trafic en forme de journée compressée (une « journée » "
        "de saisonnalité rejouée en 2 heures, courbe 5&rarr;50 VUs) pour que la baseline ait une "
        "structure à apprendre à l'échelle du développement. Les clés de baseline "
        "(<font face='Courier'>dow</font>, <font face='Courier'>hour_bucket</font>) restent "
        "<i>period-agnostic</i>, donc réutilisables sur un run temps-réel lent."))
    s.append(C.subh("Modes de faute"))
    s.append(C.data_table(
        ["Faute", "Forme", "Simulé"],
        [
            ["latency_creep", "Rampe progressive de latence", "Fuite mémoire, décroissance de cache"],
            ["latency_step", "Latence fixe immédiate", "Mauvais déploiement, régression aval"],
            ["pool_shrink", "Réduction du pool DB", "Saturation sous charge"],
            ["error_burst", "Injection de 5xx", "Panne de dépendance, mauvais chemin de code"],
            ["downstream_slow", "Ralentissement d'une dépendance", "Dégradation en cascade"],
            ["bad_deploy", "Deploy event puis faute différée", "Scénario d'attribution bout-en-bout"],
        ],
        header_color=S.GREEN,
        col_widths=[3.0 * S.cm, 5.2 * S.cm, S.CONTENT_WIDTH - 8.2 * S.cm]))
    s.append(C.subh("Scénario runner et vérité-terrain"))
    s.append(C.para(
        "Un CLI exécute des fichiers de scénario déclaratifs (YAML) &mdash; séquence de fautes "
        "avec timing, magnitude et endpoint cible &mdash; et journalise les fenêtres exactes "
        "d'injection dans un <b>log de vérité-terrain</b> consommé par le pipeline d'évaluation. "
        "Une suite standard d'environ 20 scénarios, couvrant tous les types de faute à plusieurs "
        "magnitudes, est versionnée avec le dépôt."))
    s.append(C.callout(
        "<b>Contrainte de correction :</b> les fautes injectées sont le <i>jeu de test</i>, jamais "
        "des données d'entraînement. Le job d'entraînement exclut les fenêtres d'injection de "
        "l'historique sur lequel il ajuste le modèle. Entraîner sur des fautes injectées serait "
        "une violation de correction.", kind="key"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 5 — Ingestion et metriques                                         #
# --------------------------------------------------------------------------- #

def ch5():
    s = [C.begin_chapter("Ingestion et pipeline de données", S.PRIMARY), C.spacer(6)]
    s.append(C.para(
        "Le Collector OpenTelemetry reçoit les spans OTLP. Le connecteur "
        "<font face='Courier'>spanmetrics</font> génère les métriques RED (Rate, Errors, "
        "Duration), exploitées par le pipeline de détection. Aucun mécanisme d'échantillonnage "
        "n'est configuré dans cette chaîne d'ingestion."))
    s.append(C.subh("Deux décisions résolues en amont"))
    s.append(C.para("<b>Stratégie d'histogramme.</b> Les percentiles issus de buckets grossiers "
        "sont trop bruités pour en dériver des features fiables. Des buckets explicites fins "
        "(16 paliers de 2 ms à 15 s) sont donc utilisés : c'est une <b>exigence de correction</b>, "
        "pas du tuning."))
    s.append(C.para("<b>Contrôle de cardinalité.</b> "
        "<font face='Courier'>endpoint_id = méthode + http.route</font> (templateée), ce qui "
        "évite l'explosion par URL brute. Le scraper n'interroge qu'une liste fixe de six "
        "endpoints déclarés explicitement : seules ces séries sont écrites en base, les autres "
        "métriques Prometheus sont ignorées."))
    s.append(C.subh("Rédaction PII et absence de bus de messages"))
    s.append(C.para(
        "Un filtre d'attributs supprime par denylist les attributs de span porteurs de PII "
        "connus (url, ip, identifiant utilisateur) avant export — aucune donnée de payload "
        "n'est ingérée. Le Collector exporte les métriques RED vers <b>Prometheus</b> "
        "(<font face='Courier'>prometheusremotewrite</font>) ; le scraper lit ensuite Prometheus "
        "et alimente TimescaleDB. <b>Aucun bus de messages</b> n'intervient dans cette chaîne."))
    s.append(C.callout(
        "Les métriques RED (<i>Rate, Errors, Duration</i>) résument la santé d'un service : débit "
        "de requêtes, taux d'erreurs, distribution de latence. C'est le socle minimal, standard "
        "et non propriétaire, sur lequel toute la détection s'appuie.", kind="def",
        title="Définition — métriques RED"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 6 — Features et baseline saisonniere                               #
# --------------------------------------------------------------------------- #

def ch6():
    s = [C.begin_chapter("Features et baseline saisonnière", S.SECONDARY), C.spacer(6)]
    s.append(C.subh("Fenêtres de features"))
    s.append(C.para(
        "Pour chaque endpoint et par minute : p50/p95/p99, taux 4xx et 5xx, RPS. Un agrégat "
        "5 minutes, plus lisse, sert aux features de pente et de ratio. Les features dérivées "
        "sont calculées à la lecture par le détecteur :"))
    for b in C.bullets([
        "<b>Pente de latence</b> : ajustement linéaire robuste (Theil-Sen) sur les K dernières fenêtres.",
        "<b>Ratio p99/p50</b> : signal d'étalement de la distribution.",
        "<b>Deltas court-terme</b> et <b>delta de RPS</b> (pour distinguer une dégradation d'un simple changement de charge).",
        "<b>Déviations de baseline</b> par métrique (voir 6.2).",
    ]):
        s.append(b)
    s.append(C.callout(
        "Adaptation d'implémentation assumée : au lieu des <i>continuous aggregates</i> "
        "TimescaleDB, un <font face='Courier'>scraper</font> interroge Prometheus toutes les 60 s "
        "et alimente l'hypertable <font face='Courier'>endpoint_features</font>. La forme des "
        "données (p50/p95/p99, 4xx/5xx, rps par endpoint et par minute) est identique.",
        kind="tech"))
    s.append(C.subh("Baseline saisonnière conditionnée"))
    s.append(C.para(
        "Pour chaque endpoint et chaque métrique, on maintient des quantiles conditionnels "
        "(p10, p50, p90) indexés sur <font face='Courier'>(jour_semaine, heure)</font> sur une "
        "fenêtre glissante de 14 jours. On en déduit, par (endpoint, métrique, fenêtre) :"))
    for b in C.bullets([
        "<b>expected</b> = p50 conditionnel ; <b>band</b> = intervalle p10&ndash;p90 ;",
        "<b>baseline_deviation</b> = position de l'observation vis-à-vis de la bande, "
        "<b>normalisée</b> (0 dans la bande, distance mise à l'échelle au-delà).",
    ]):
        s.append(b)
    s.append(C.para(
        "<b>Cold start :</b> un bucket (jour x heure) avec trop peu d'échantillons se replie sur "
        "les quantiles toutes-heures de l'endpoint, puis sur les quantiles globaux par métrique, "
        "jusqu'à accumuler assez d'historique. Aucune couche de clustering n'est nécessaire."))
    s.append(C.callout(
        "<b>Hygiène de baseline mesurée :</b> calculée sur des données incluant les fautes, la "
        "p90 gonfle à 1391 ms ; en excluant les injections, elle retombe à 317 ms. Sans cette "
        "exclusion, le <i>direction gating</i> classerait la dégradation en « normal » "
        "et les couches 1/2 se tairaient. C'est pourquoi <font face='Courier'>baseline_job</font> "
        "et <font face='Courier'>train_model</font> excluent les injections.", kind="key"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 7 — Architecture de detection multicouche                          #
# --------------------------------------------------------------------------- #

def ch7():
    s = [C.begin_chapter("Architecture de détection multicouche", S.PURPLE), C.spacer(6)]
    s.append(C.lead(
        "Le cœur du système : trois couches complémentaires, chacune utilisable et démoable "
        "seule, dont la sortie est un score d'anomalie calibré par (endpoint, fenêtre)."))
    s.append(C.figure(D.layered_detection(),
                      "Figure 4 — Pipeline de détection : features endpoint-relatives, couches "
                      "0/1/2, direction gating de la couche ML, score combiné, state machine."))
    s.append(C.subh("Les trois couches"))
    s.append(C.data_table(
        ["Couche", "Principe", "Apport"],
        [
            ["Layer 0 — static", "Seuils SLO classiques (p99 &gt; X sur N fenêtres)", "Baseline de comparaison, chemin d'alerte v0"],
            ["Layer 1 — baseline", "Scoring réglé sur baseline_deviation", "Direction + plancher, zéro entraînement, saisonnier"],
            ["Layer 2 — iForest", "Isolation Forest non supervisé sur le vecteur de features", "Sensibilité au drift multivarié"],
        ],
        header_color=S.PURPLE,
        col_widths=[3.4 * S.cm, 6.2 * S.cm, S.CONTENT_WIDTH - 9.6 * S.cm]))
    s.append(C.subh("Features endpoint-relatives : un modèle global unique"))
    s.append(C.para(
        "L'Isolation Forest est <b>un seul modèle global</b> sur tous les endpoints. Ce n'est "
        "valide que parce que les features sont <b>endpoint-relatives</b> : déviations de baseline, "
        "ratios, pentes, deltas &mdash; jamais des niveaux absolus en ms. Alimenter le modèle avec "
        "un <font face='Courier'>p99_ms</font> brut casserait le modèle global (un endpoint "
        "naturellement lent paraîtrait toujours anormal)."))
    s.append(C.subh("Direction gating"))
    s.append(C.para(
        "La couche 1 calcule une <b>direction</b> (dégradation ou amélioration) à partir de "
        "l'observé vs la bande saisonnière. La couche 2 ne contribue à une alerte <b>que</b> "
        "lorsque la direction est <i>dégradation</i>. On n'alerte jamais sur une performance "
        "anormalement <i>bonne</i>. C'est aussi la mitigation clé contre les faux positifs dus à "
        "un simple changement de forme de trafic."))
    s.append(C.subh("Combinaison et calibration du score"))
    s.append(C.para(
        "Le score final combine les couches 1 et 2, avec la couche 2 <b>gatée par la direction</b> "
        "de la couche 1 :"))
    s.append(C.para(
        "<font face='Courier'>combined = baseline_norm + (1 - baseline_norm) &times; ml_gated</font>, "
        "avec un plancher = dépassement SLO.", style="code"))
    s.append(C.spacer(6))
    s.append(C.para(
        "Le score est calibré sur [0,1] à partir de la distribution du score brut à "
        "l'entraînement (p50 &rarr; 0, p99 &rarr; 1), mise à jour lors de chaque "
        "réentraînement nightly. Le "
        "déclenchement a lieu sur dépassement SLO <b>ou</b> score combiné &ge; <b>0.60</b> "
        "(seuil tuné). Le score, les contributions par feature (top 3) et la provenance de "
        "couche sont écrits dans l'<i>anomaly store</i>."))
    s.append(C.callout(
        "<b>Attribution :</b> les trois principales features contributrices sont exposées. Pour "
        "l'Isolation Forest, via une attribution par profondeur de chemin (path-depth) ; pour la "
        "baseline, via les déviations normalisées brutes. C'est ce qui rend l'alerte "
        "interprétable par l'opérateur et par le LLM.", kind="tech"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 8 — Cycle de vie du modele ML                                      #
# --------------------------------------------------------------------------- #

def ch8():
    s = [C.begin_chapter("Cycle de vie du modèle ML", S.GREEN), C.spacer(6)]
    s.append(C.para(
        "Le modèle n'est pas figé : il est réentraîné chaque nuit et promu seulement s'il passe "
        "un contrôle de sécurité. L'objectif est d'éviter toute dégradation silencieuse."))
    s.append(C.figure(D.ml_lifecycle(),
                      "Figure 5 — Cycle de vie : historique (fautes exclues) -> fit -> artefact "
                      "horodaté -> sanity gate -> pointeur latest."))
    s.append(C.subh("Réentraînement et versionnement"))
    for b in C.bullets([
        "<b>Un modèle global</b>, réajusté <i>nightly</i> sur l'historique trailing, avec "
        "exclusion stricte des fenêtres d'injection.",
        "<b>Artefact horodaté</b> ; l'artefact précédent est conservé (rollback possible).",
        "Un pointeur <font face='Courier'>latest</font> indique au détecteur quel modèle charger.",
    ]):
        s.append(b)
    s.append(C.subh("Sanity gate avant promotion"))
    s.append(C.para(
        "La promotion est <b>gatée</b> par un contrôle de dérive de la distribution de score sur "
        "une <b>fenêtre de référence fixe</b> (test KS entre la distribution des scores du nouveau modèle "
        "et celle de l'ancien modèle, évalués sur cette même fenêtre). Si la nouvelle "
        "distribution dérive au-delà d'un seuil, la promotion est refusée. S'ajoutent des "
        "contrôles de non-régression (recall / faux positifs) et de fraîcheur des données."))
    s.append(C.callout(
        "Message défendable : « détecter le drift &rarr; réentraîner &rarr; valider avant de "
        "promouvoir ». Le pipeline ML est autonome <i>et</i> sûr : aucune mise à jour ne "
        "dégrade le détecteur à l'insu de l'opérateur.", kind="key"))
    s.append(C.subh("Le paramètre « évident » qui est inerte"))
    s.append(C.para(
        "Découverte empirique : le paramètre <font face='Courier'>contamination</font> de "
        "scikit-learn n'est appliqué qu'à <font face='Courier'>predict()</font>, pas à "
        "<font face='Courier'>score_samples()</font> que l'on calibre &mdash; il est donc "
        "<b>inerte</b> ici et garde à sa valeur par défaut (0.02). Le vrai levier est le "
        "<b>seuil du score combiné</b>, dont le F1 est maximal à <b>0.60</b>."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 9 — Machine a etats d'alerte                                       #
# --------------------------------------------------------------------------- #

def ch9():
    s = [C.begin_chapter("Machine à états d'alerte", S.SECONDARY), C.spacer(6)]
    s.append(C.para(
        "Les détections « flottent » (flapping) ; les alertes ne doivent pas. Une "
        "machine à états par <font face='Courier'>(endpoint, signal)</font> introduit de "
        "l'hystérésis à la montée et à la descente."))
    s.append(C.figure(D.state_machine(),
                      "Figure 6 — Machine à états : OK -> PENDING -> FIRING -> RESOLVING -> OK, "
                      "avec hystérésis M/R et clé de déduplication (endpoint_id, signal_type)."))
    s.append(C.subh("Transitions et anti-flapping"))
    for b in C.bullets([
        "<b>OK &rarr; PENDING</b> : score au-dessus du seuil sur 1 fenêtre.",
        "<b>PENDING &rarr; FIRING</b> : soutenu sur M fenêtres consécutives (hystérésis montée). "
        "Dans le détecteur live (<font face='Courier'>detector.py</font>, "
        "<font face='Courier'>PENDING_WINDOWS=2</font>), le compteur démarre à 0 à l'entrée en "
        "PENDING et la condition est <font face='Courier'>pending_count+1 &ge; 2</font> : il faut "
        "<b>3 cycles consécutifs au total</b> (1 pour entrer en PENDING + 2 en PENDING) avant de "
        "basculer en FIRING. L'évaluateur offline (<font face='Courier'>evaluate_layered.py</font>, "
        "<font face='Courier'>M_WINDOWS=2</font>) déclenche dès le 2e cycle consécutif &mdash; les "
        "délais rapportés (ex. 124 s median) sont donc <b>sous-estimés d'environ 60 s</b> par "
        "rapport au comportement live.",
        "<b>FIRING &rarr; RESOLVING</b> : dès le <b>1er cycle</b> dont le score repasse sous le "
        "seuil de déclenchement (0.60) ou dont le dépassement SLO est résolu &mdash; sans seuil "
        "de clear distinct, sans attente supplémentaire.",
        "<b>RESOLVING &rarr; OK</b> : confirmation sur "
        "<font face='Courier'>RESOLVING_WINDOWS=2</font> cycles en RESOLVING "
        "(hystérésis descente). <font face='Courier'>set_resolving()</font> initialise "
        "<font face='Courier'>resolving_count=1</font> à l'entrée ; la condition "
        "<font face='Courier'>resolving_count &ge; 2</font> est vérifiée au cycle suivant, puis "
        "OK au cycle d'après : il faut <b>3 cycles clairs au total</b> pour quitter FIRING "
        "(1 pour entrer en RESOLVING + 2 de confirmation) &mdash; par symétrie exacte avec la montée.",
    ]):
        s.append(b)
    s.append(C.subh("Déduplication et économie de coût"))
    s.append(C.para(
        "Clé de dedup : <font face='Courier'>(endpoint_id, signal_type)</font>. Une alerte FIRING "
        "est <b>mise à jour en place</b> (sévérité, score, attribution) plutôt que renotifiée ; "
        "l'escalade ne renotifie que sur augmentation de sévérité. Surtout, <b>seule la première "
        "entrée en état FIRING</b> (transition PENDING &rarr; FIRING) déclenche le moteur de "
        "corrélation et l'explication LLM &mdash; contrôle du bruit et du coût par construction."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 10 — Correlation deploiement et DAG                                #
# --------------------------------------------------------------------------- #

def ch10():
    s = [C.begin_chapter("Corrélation déploiement et graphe de services", S.PRIMARY), C.spacer(6)]
    s.append(C.subh("Fenêtre causale et score d'imputation"))
    s.append(C.para(
        "Sur une nouvelle alerte FIRING, le moteur cherche les déploiements affectant le service "
        "concerné dans une <b>fenêtre temporelle de corrélation</b> (par défaut 30 min avant l'<i>onset</i>, "
        "c.-à-d. l'instant de la transition PENDING &rarr; FIRING). Le <b>score d'imputation</b> est une fonction "
        "transparente de la proximité temporelle (et, à terme, de priors par type d'événement). "
        "Il est toujours exposé comme <b>suspicion, jamais comme cause certaine</b>."))
    s.append(C.callout(
        "Sur le scénario <font face='Courier'>bad_deploy</font>, l'alerte nomme le <b>bon "
        "commit/déploiement</b> en quelques minutes (score de corrélation ~0.9, fenêtre 30 min). "
        "C'est le scénario d'attribution bout-en-bout du projet.", kind="key"))
    s.append(C.subh("Extension : DAG de dépendances de service"))
    s.append(C.para(
        "La spec corrèle les déploiements mais ne modélise pas les dépendances inter-services. "
        "Or une dégradation du service <font face='Courier'>orders</font> se manifeste aussi sur "
        "<font face='Courier'>gateway</font> et <font face='Courier'>payments</font> qui "
        "l'appellent. Une extension modélise donc le graphe d'appels réel."))
    s.append(C.figure(D.service_dag(),
                      "Figure 7 — DAG de dépendances : un même enfant (GET /orders/{id}) est "
                      "appelé en direct par la gateway et en cascade par payments."))
    s.append(C.para(
        "<b>Pourquoi une table de relations, pas une colonne parent ?</b> Un endpoint peut avoir "
        "plusieurs parents : le vrai graphe est un <b>DAG</b>, pas un arbre. La table "
        "<font face='Courier'>endpoint_relationships</font> stocke des arêtes dirigées "
        "<font face='Courier'>(parent, child)</font> avec un <font face='Courier'>call_type</font> "
        "(<i>direct</i> | <i>cascade</i>). Un trigger anti-cycle (CTE récursive + CHECK) rejette "
        "les cycles au niveau de la base."))
    s.append(C.subh("Traversée bornée et priorisation des candidats (P2)"))
    s.append(C.para(
        "Une requête <font face='Courier'>WITH RECURSIVE</font> bornée par "
        "<font face='Courier'>MAX_GRAPH_DEPTH</font> (défaut 3) remonte les chaînes multi-hop, "
        "avec double protection anti-cycles (UNION SQL + garde runtime). Les candidats de cause "
        "racine sont triés par un score de confiance :"))
    s.append(C.para(
        "<font face='Courier'>confidence = min(0.95, anomaly_score &times; call_type_weight "
        "&times; (1/depth))</font>, avec direct=1.00 et cascade=0.70.", style="code"))
    s.append(C.spacer(6))
    s.append(C.callout(
        "<b>Invariant dur :</b> l'analyse du graphe est strictement <i>post-scoring</i> et "
        "best-effort. Toute exception dans <font face='Courier'>analyze_service_graph()</font> ne "
        "bloque jamais les couches 0/1/2, la state machine ni le chemin d'alerte "
        "(<font face='Courier'>try/except</font> englobant). Ceci reste une corrélation "
        "topologique, pas un remplacement du distributed tracing.", kind="tech"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 11 — Explication LLM                                               #
# --------------------------------------------------------------------------- #

def ch11():
    s = [C.begin_chapter("Génération d'explications d'incidents assistée par LLM", S.PURPLE), C.spacer(6)]
    s.append(C.para(
        "Déclenchée une seule fois par transition <font face='Courier'>PENDING &rarr; FIRING</font>, "
        "la couche d'explication assemble un contexte structuré : endpoint, métrique(s) en "
        "violation, observé vs attendu et magnitude de déviation, top features contributrices, "
        "déploiement(s) suspect(s) avec score d'imputation, et historique récent de l'endpoint."))
    s.append(C.subh("Contrat de sortie et robustesse"))
    s.append(C.para(
        "Le contexte est rendu dans un <b>template de prompt fixe</b> demandant un résumé de deux "
        "phrases, la cause la plus probable cadrée par l'incertitude du score d'imputation, et "
        "deux ou trois <b>premières vérifications concrètes</b>. La sortie suit un contrat JSON "
        "strict <font face='Courier'>(summary, suspected_cause, checks[])</font>, <b>validé avant "
        "usage</b>."))
    s.append(C.callout(
        "En cas d'erreur API ou de JSON malformé, l'alerte <b>bascule sur un template "
        "déterministe</b> : l'alerting ne dépend jamais de la disponibilité du LLM. Priorité 1 du "
        "fallback : si des candidats de cause racine existent (DAG), il produit une cause lisible "
        "sans LLM.", kind="key"))
    s.append(C.subh("Maîtrise du coût et provider"))
    s.append(C.para(
        "Le coût en tokens est borné par construction : les explications ne partent que sur les "
        "transitions d'alerte, jamais par prédiction. L'appel passe par un client "
        "<b>provider-agnostic</b> (contrat JSON strict + fallback déterministe)."))
    s.append(C.callout(
        "Adaptation d'implémentation : le provider retenu est <b>Gemini</b> "
        "(<font face='Courier'>google-genai</font>) plutôt que le client Anthropic de la spec. Le "
        "contrat provider-agnostic (JSON strict, fallback) est inchangé.", kind="tech"))
    s.append(C.subh("Benchmark de sélection du modèle LLM"))
    s.append(C.para(
        "50 appels exécutés (<b>2 modèles &times; 5 scénarios &times; 5 runs</b>, "
        "<font face='Courier'>temperature=0</font>) pour sélectionner le modèle Gemini. "
        "Chaque appel est évalué sur : validité JSON du retour, latence API, score de "
        "qualité sur rubrique (structure / chiffres / actionnabilité / incertitude, max 12), "
        "et erreurs réseau."))
    s.append(C.data_table(
        ["Modèle", "JSON valid.", "Lat. moy.", "Lat. p90", "Score /12", "Erreurs", "Verdict"],
        [
            ["gemini-3.1-flash-lite", "100 %", "1,6 s", "2,6 s", "11,6", "0", "RETENU"],
            ["gemini-3-flash-preview", "76 %", "15,3 s", "97,2 s", "11,4", "4 SSL/DNS", "Exclu"],
        ],
        header_color=S.PURPLE,
        align_center_cols=[1, 2, 3, 4, 5, 6],
        highlight_last_col=True,
        col_widths=[
            3.8 * S.cm, 1.5 * S.cm, 2.0 * S.cm, 1.8 * S.cm,
            1.8 * S.cm, 2.2 * S.cm,
            S.CONTENT_WIDTH - 3.8 * S.cm - 1.5 * S.cm - 2.0 * S.cm
            - 1.8 * S.cm - 1.8 * S.cm - 2.2 * S.cm,
        ]))
    s.append(C.callout(
        "<b>gemini-3.1-flash-lite retenu.</b> Score qualité quasi-identique (11,6 vs 11,4/12), "
        "mais avantage décisif sur JSON validity (100 % vs 76 %), latence (1,6 s vs 15,3 s "
        "moyenne, 2,6 s vs 97,2 s p90), et zéro erreur réseau contre 4 erreurs SSL/DNS.", kind="key"))
    s.append(C.para(
        "Le benchmark présenté ici constitue une évaluation comparative utilisée uniquement pour "
        "la sélection du modèle LLM. Il ne correspond pas à la métrique finale de qualité des "
        "explications présentée dans la section d'évaluation, qui utilise une grille distincte "
        "sur 6 points appliquée aux explications générées lors des scénarios d'incident."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 12 — Methodologie d'evaluation                                     #
# --------------------------------------------------------------------------- #

def ch12():
    s = [C.begin_chapter("Méthodologie d'évaluation", S.SECONDARY), C.spacer(6)]
    s.append(C.para(
        "La revendication centrale est <b>relative</b> : une meilleure couverture de détection "
        "et une amélioration de la détection des dégradations progressives, avec un compromis "
        "maîtrisé sur le taux de faux positifs. Le protocole la rend directement mesurable."))
    s.append(C.subh("Protocole"))
    for b in C.bullets([
        "Exécuter la suite standard (~20 scénarios) avec les détecteurs <b>statique (Layer 0) et "
        "en couches actifs en parallèle</b>.",
        "Rapprocher les alertes des fenêtres d'injection : un <b>vrai positif</b> est une alerte "
        "FIRING sur l'endpoint cible chevauchant (ou dans un délai de grâce) la fenêtre "
        "d'injection ; hors fenêtre, c'est un <b>faux positif</b>.",
        "Répéter à plusieurs magnitudes pour produire des <b>courbes de sensibilité</b>.",
    ]):
        s.append(b)
    s.append(C.subh("Métriques"))
    for b in C.bullets([
        "<b>Taux de détection</b> par type de faute et magnitude.",
        "<b>Faux positifs par heure</b> sous charge sans faute (scénarios quiet).",
        "<b>Délai de détection médian</b> (début d'injection &rarr; FIRING) et lead time vs "
        "seuil statique pour les fautes graduelles.",
        "<b>Précision d'attribution</b> sur bad_deploy : fraction où le déploiement suspect est "
        "bien l'injecté.",
        "<b>Qualité d'explication</b> : petit échantillon noté sur rubrique (exactitude des "
        "chiffres cités, actionnabilité).",
    ]):
        s.append(b)
    s.append(C.callout(
        "L'artefact phare du rapport est le <b>tableau comparatif (layered vs static par type de "
        "faute)</b>. Toutes les métriques sont exposées dans un tableau de bord Grafana "
        "d'évaluation, alimenté par la table <font face='Courier'>eval_runs</font>.", kind="tech"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 13 — Resultats                                                     #
# --------------------------------------------------------------------------- #

def ch13():
    s = [C.begin_chapter("Résultats mesurés", S.GREEN), C.spacer(6)]
    s.append(C.lead(
        "Chiffres issus de la campagne d'évaluation historique comprenant 31 fenêtres d'injection "
        "valides et des outils d'évaluation dédiés."))
    s.append(C.kpi_row([
        ("71 %", "Détection (layered)"),
        ("+20 pts", "latency_step vs static"),
        ("5.00/6", "Qualité LLM"),
        ("105", "Tests automatisés"),
    ]))
    s.append(C.subh("Livrable phare : layered vs static"))
    s.append(C.data_table(
        ["Métrique", "Static (Layer 0)", "Layered (baseline + ML)"],
        [
            ["Détection rate (global)", "65 %", "71 %"],
            ["Faux positifs / heure", "1.13", "1.29"],
            ["Délai de détection médian", "&mdash;", "124 s"],
        ],
        header_color=S.GREEN, align_center_cols=[1, 2], highlight_last_col=True))
    s.append(C.figure(CH.global_metrics(),
                      "Figure 8 — Métriques globales : le layered détecte plus (71 % vs 65 %) "
                      "pour un surcoût de faux positifs modeste (FP/h à l'échelle x20)."))
    s.append(C.subh("Détail par type de faute"))
    s.append(C.data_table(
        ["Type de faute", "Static", "Layered", "Gain"],
        [
            ["latency_creep", "100 %", "100 %", "="],
            ["downstream_slow", "100 %", "100 %", "="],
            ["latency_step", "80 %", "100 %", "+20 pts"],
            ["error_burst", "50 %", "50 %", "= (signal commun)"],
            ["pool_shrink", "0 %", "0 %", "= (faute subtile)"],
        ],
        header_color=S.GREEN, align_center_cols=[1, 2, 3]))
    s.append(C.figure(CH.detection_by_fault(),
                      "Figure 9 — Taux de détection par type de faute : le layered domine sur "
                      "latency_step (certains échelons ne franchissent pas le seuil SLO absolu "
                      "mais dépassent la bande baseline ; le score combiné les détecte)."))
    s.append(C.subh("Courbe de sensibilité"))
    s.append(C.figure(CH.sensitivity_curve(),
                      "Figure 10 — Le taux de détection croît avec la magnitude (forme "
                      "attendue) ; l'avantage layered sur latency_step est stable aux deux intensités."))
    s.append(C.subh("Autres résultats"))
    s.append(C.data_table(
        ["Dimension", "Résultat"],
        [
            ["Attribution déploiement", "bad_deploy nomme le bon commit en minutes (score ~0.9)"],
            ["Qualité des explications LLM", "5.00 / 6 (structure, chiffres, actionnabilité, incertitude) — évaluation indépendante du benchmark de sélection (§14.3)"],
            ["Supervisé vs non-supervisé", "PR-AUC 0.167 (iForest) vs 0.164 (gradient boosting) : pas d'avantage décisif"],
            ["Tuning", "contamination inerte ; seuil de déclenchement 0.60 = F1 max"],
            ["Compression TimescaleDB", "ratio ~6x (append-only, chunks > 7 j)"],
            ["Tests / CI", "105 tests automatisés, CI GitHub Actions verte"],
        ],
        header_color=S.GREEN,
        col_widths=[5.2 * S.cm, S.CONTENT_WIDTH - 5.2 * S.cm]))
    s.append(C.para(
        "La note de 5,00/6 ne doit pas être interprétée comme une conversion du score 11,6/12 "
        "obtenu lors du benchmark de sélection (§14.3). Elle correspond à une évaluation "
        "indépendante appliquée aux explications générées par le modèle retenu dans les scénarios "
        "d'incident utilisés pour l'évaluation finale."))
    s.append(C.callout(
        "Le supervisé ne battant pas le non-supervisé ici (PR-AUC quasi identique), le choix non "
        "supervisé est <b>justifié par la mesure</b> : il évite la dépendance à des labels "
        "d'incidents rares et ne consomme pas le jeu de test.", kind="key"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 14 — Securite et self-observabilite                                #
# --------------------------------------------------------------------------- #

def ch14():
    s = [C.begin_chapter("Sécurité et self-observabilité", S.PRIMARY), C.spacer(6)]
    s.append(C.subh("Sécurité"))
    for b in C.bullets([
        "<b>Rédaction PII</b> par denylist d'attributs au Collector (url, ip, identifiant) : aucune donnée de payload ingérée.",
        "POST /deploys protégé par clé API (X-API-Key) quand la variable <font face='Courier'>DEPLOY_API_KEY</font> est configurée ; non authentifié en mode dev.",
        "API de fault injection bornée au réseau interne, jamais exposée sur l'API plateforme.",
        "Secrets (Slack, clé LLM) via injection d'environnement, jamais dans le dépôt.",
    ]):
        s.append(b)
    s.append(C.subh("Self-observabilité"))
    s.append(C.para(
        "La plateforme se supervise elle-même à travers deux sources distinctes. "
        "Le <b>dashboard Grafana</b> agrège des métriques Prometheus natives du détecteur "
        "(<font face='Courier'>cassandra_detector_*</font> : durée de cycle, alertes par état, "
        "taux LLM, fraîcheur de scrape) et des requêtes SQL sur TimescaleDB (qualité d'alerte : "
        "alertes/jour, durées FIRING, cadence scraper/détecteur). "
        "Une sonde de liveness surveille la disponibilité de l'OTel Collector ; "
        "ses métriques internes (queue, drops) sont collectées par Prometheus "
        "mais ne sont pas encore surfacées dans un panneau dédié. "
        "Le <b>détecteur</b> expose son endpoint "
        "<font face='Courier'>/metrics</font> Prometheus natif "
        "(port 9101), scrappé directement par Prometheus."))
    s.append(C.callout(
        "Le détecteur expose un endpoint <font face='Courier'>/metrics</font> Prometheus natif "
        "(durée de cycle p95 ~47 ms, alertes par état, taux LLM, fraîcheur). La dette de stockage "
        "est maîtrisée par compression et rétention TimescaleDB (~6x).", kind="tech"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre 15 — Limites et travaux futurs                                     #
# --------------------------------------------------------------------------- #

def ch15():
    s = [C.begin_chapter("Limites et travaux futurs", S.SECONDARY), C.spacer(6)]
    s.append(C.subh("Limites assumées"))
    for b in C.bullets([
        "<b>pool_shrink non détecté</b> sur le signal p99 aux magnitudes testées : faute de "
        "saturation subtile. Piste : intégrer <font face='Courier'>pool_wait_ms</font> comme "
        "feature optionnelle.",
        "<b>Baseline compressée</b> : la saisonnalité de démo (journée en 2 h) surestime la "
        "qualité de baseline vs un vrai cycle diurnal (documenté ; clés period-agnostic).",
        "<b>PII en denylist</b> (allowlist recommandée mais casse spanmetrics &mdash; à retravailler).",
        "<b>Cold-start</b> : un endpoint sans baseline neutralise la couche ML (direction "
        "indéterminable).",
    ]):
        s.append(b)
    s.append(C.subh("Travaux futurs"))
    for b in C.bullets([
        "<b>Alerte précoce TTD</b> : extrapolation robuste (Theil-Sen) de la tendance p99 vers le "
        "SLO, déjà implémentée en mode advisory, à durcir et surfacer davantage.",
        "<b>Priors d'imputation par type de déploiement</b> (release &gt; migration &gt; config &gt; "
        "flag) une fois la colonne <font face='Courier'>kind</font> introduite.",
        "<b>Propagation par topologie de traces</b> (distributed tracing) pour dépasser la "
        "corrélation topologique actuelle.",
        "<b>Features de saturation</b> généralisées et allowlist PII compatible spanmetrics.",
    ]):
        s.append(b)
    return s


# --------------------------------------------------------------------------- #
# Chapitre 16 — Conclusion                                                    #
# --------------------------------------------------------------------------- #

def ch16():
    s = [C.begin_chapter("Conclusion", S.PURPLE), C.spacer(6)]
    s.append(C.para(
        "Cassandra démontre qu'une détection <b>saisonnière et non supervisée</b> dépasse le "
        "seuillage statique sur le critère qui compte &mdash; détecter plus et plus tôt les "
        "dégradations progressives &mdash; avec une augmentation limitée des faux positifs "
        "(+0.16 FP/h), correspondant au compromis précision/couverture attendu d'un détecteur "
        "plus sensible, et que ce gain est <b>chiffré</b> (71 % vs 65 %, +20 points sur latency_step)."))
    s.append(C.para(
        "Au-delà de la détection, la plateforme <b>attribue</b> chaque incident à un déploiement "
        "probable (score de suspicion honnête, DAG de services) et l'<b>explique</b> en langage "
        "naturel via un LLM, avec un fallback déterministe garantissant que l'alerting ne dépend "
        "jamais d'un service externe."))
    s.append(C.para(
        "L'ensemble est <b>reproductible</b> (<font face='Courier'>docker compose up</font>), "
        "<b>testé</b> (105 tests, CI verte) et <b>observable</b> (métriques natives, dashboards). "
        "Les limites sont assumées et documentées, et les travaux futurs (TTD durci, priors "
        "d'imputation, propagation par traces) tracent une suite naturelle."))
    s.append(C.spacer(8))
    s.append(C.callout(
        "Apports personnels : conception d'un pipeline d'observabilité de bout en bout, mise en "
        "œuvre d'une détection ML non supervisée <i>interprétable</i>, rigueur d'évaluation "
        "(vérité-terrain, comparatif chiffré, honnêteté sur les limites) et pratiques "
        "d'ingénierie (Docker, CI, self-observabilité, sécurité).", kind="key",
        title="Bilan du stage"))
    return s


# --------------------------------------------------------------------------- #
# References                                                                  #
# --------------------------------------------------------------------------- #

def references():
    s = [C.ChapterBanner(None, "Références", color=S.GREEN), C.spacer(6)]
    for b in C.bullets([
        "OpenTelemetry &mdash; spécification et connecteur <i>spanmetrics</i> (métriques RED).",
        "TimescaleDB &mdash; hypertables, continuous aggregates, compression et rétention.",
        "F. T. Liu, K. M. Ting, Z.-H. Zhou, <i>Isolation Forest</i>, ICDM 2008.",
        "scikit-learn &mdash; <font face='Courier'>IsolationForest</font>, pipelines de preprocessing.",
        "P. K. Sen, <i>Estimates of the regression coefficient based on Kendall's tau</i> "
        "(estimateur Theil-Sen), 1968.",
        "Prometheus &mdash; modèle de données et remote-write. Grafana &mdash; tableaux de bord.",
        "Documentation projet : <font face='Courier'>docs/design-spec.md</font>, "
        "<font face='Courier'>docs/architecture.md</font>, <font face='Courier'>docs/results.md</font>.",
    ]):
        s.append(b)
    return s


# --------------------------------------------------------------------------- #
# Front matter : Remerciements                                                #
# --------------------------------------------------------------------------- #

def remerciements():
    s = [C.ChapterBanner(None, "Remerciements", color=S.PRIMARY), C.spacer(6)]
    s.append(C.para(
        "À l'issue de ce stage, je tiens à exprimer ma sincère gratitude à l'ensemble des "
        "personnes qui ont contribué, de près ou de loin, à la réussite de cette expérience "
        "professionnelle."))
    s.append(C.para(
        "Je remercie chaleureusement <b>Talan Tunisie</b> pour son accueil, ainsi que pour la "
        "confiance qui m'a été accordée en me confiant un sujet à la fois stimulant et "
        "enrichissant."))
    s.append(C.para(
        "J'adresse mes remerciements les plus sincères à mon encadrante en entreprise, "
        "<b>Mme Inès Boukhris</b>, pour sa disponibilité, la pertinence de ses conseils et son "
        "accompagnement attentif tout au long de ce projet."))
    s.append(C.para(
        "Je remercie également mon encadrant académique, <b>M. Imed Abbessi</b>, pour son suivi, "
        "ses orientations et la rigueur apportée dans l'accompagnement de ce travail."))
    s.append(C.para(
        "Enfin, j'exprime ma reconnaissance à l'ensemble de l'équipe pour son esprit d'entraide "
        "et son soutien, ainsi qu'au corps enseignant de l'<b>ISIMM</b> pour la qualité de la "
        "formation dispensée."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre — Presentation de l'organisme d'accueil                            #
# --------------------------------------------------------------------------- #

def org_accueil():
    s = [C.begin_chapter("Présentation de l'organisme d'accueil", S.PURPLE), C.spacer(6)]
    s.append(C.lead(
        "Ce chapitre présente l'entreprise d'accueil, son implantation en Tunisie, ses domaines "
        "d'expertise et le service au sein duquel s'est déroulé le stage."))

    s.append(C.subh("Le groupe Talan"))
    s.append(C.para(
        "Fondé en 2002 par Mehdi Houas, Éric Benamou et Philippe Cassoulat, <b>Talan</b> est un "
        "groupe international de conseil spécialisé dans l'<b>innovation</b> et la "
        "<b>transformation des entreprises par la technologie</b>."))
    s.append(C.para(
        "Le groupe accompagne ses clients dans leurs projets de transformation numérique en "
        "s'appuyant sur plusieurs domaines d'expertise, notamment la data, l'intelligence "
        "artificielle, le cloud, l'ingénierie logicielle et les technologies émergentes."))
    s.append(C.para(
        "Présent dans 21 pays sur les cinq continents, Talan regroupe aujourd'hui plus de "
        "<b>6 000 collaborateurs</b>. Son approche repose sur la combinaison de l'expertise "
        "technologique, de l'innovation et de l'accompagnement des organisations dans leurs "
        "évolutions numériques."))

    s.append(C.subh("Talan en Tunisie"))
    s.append(C.para(
        "<b>Talan Tunisie</b> constitue un centre d'expertise et de delivery du groupe, "
        "participant au développement de solutions technologiques pour des clients nationaux et "
        "internationaux. Les équipes tunisiennes interviennent notamment dans les domaines de la "
        "data, de l'intelligence artificielle, du développement logiciel et des technologies "
        "cloud."))
    s.append(C.para(
        "Grâce à ses compétences techniques et à son intégration dans l'écosystème international "
        "du groupe, <b>Talan Tunisie</b> contribue à la réalisation de projets innovants "
        "combinant expertise métier et nouvelles technologies. Le stage présenté dans ce rapport "
        "s'inscrit dans cette dynamique à travers le développement de la plateforme "
        "<b>Cassandra</b> dédiée à l'observabilité intelligente et à la détection d'incidents."))

    s.append(C.subh("Domaines d'expertise"))
    s.append(C.data_table(
        ["Domaine", "Périmètre"],
        [
            ["Data & Intelligence Artificielle", "Valorisation de la donnée, machine learning, MLOps, observabilité"],
            ["Cloud & DevOps", "Migration cloud, conteneurisation, automatisation, CI/CD"],
            ["Ingénierie logicielle", "Conception et développement d'applications et de plateformes"],
            ["Transformation digitale", "Conseil, cadrage et conduite du changement"],
            ["Cybersécurité", "Sécurisation des systèmes et des données"],
            ["Technologies émergentes", "Innovation appliquée (blockchain, IoT, GenAI)"],
        ],
        header_color=S.PURPLE,
        col_widths=[5.6 * S.cm, S.CONTENT_WIDTH - 5.6 * S.cm]))

    s.append(C.subh("Cadre du stage et positionnement du projet"))
    s.append(C.para(
        "Le stage a été réalisé au sein de <b>Talan Tunisie</b> sous l'encadrement de "
        "<b>Mme Inès Boukhris</b>. Le projet Cassandra s'inscrit dans un environnement "
        "technologique combinant plusieurs domaines d'expertise, notamment l'ingénierie "
        "logicielle, l'observabilité des systèmes distribués et l'intelligence artificielle."))
    s.append(C.para(
        "Ce positionnement correspond aux objectifs du projet, qui consiste à concevoir une "
        "plateforme intelligente capable de collecter et d'analyser des métriques applicatives, "
        "de détecter des dégradations de performance et d'assister l'analyse des incidents à "
        "l'aide de techniques d'apprentissage automatique."))
    s.append(C.figure(D.org_chart(),
                      "Figure 1 — Organigramme simplifié : positionnement du projet Cassandra dans "
                      "l'organisation d'accueil."))
    return s


# --------------------------------------------------------------------------- #
# Chapitre — Contexte du stage et objectifs                                    #
# --------------------------------------------------------------------------- #

def contexte_stage():
    s = [C.begin_chapter("Contexte du stage et objectifs", S.SECONDARY), C.spacer(6)]
    s.append(C.lead(
        "Ce chapitre présente le cadre général du stage, le sujet confié, la problématique "
        "ayant motivé le projet, ainsi que les objectifs techniques et les livrables associés."))

    s.append(C.subh("Cadre du stage"))
    s.append(C.para(
        "Le présent travail a été réalisé dans le cadre d'un <b>stage d'ingénieur</b> d'une durée "
        "de deux mois, du <b>08/06/2026 au 07/08/2026</b>, au sein de <b>Talan Tunisie</b>. "
        "Ce stage s'inscrit dans le cursus de formation d'ingénieur de l'<b>ISIMM</b> et vise à "
        "mettre en application les connaissances acquises à travers la réalisation d'un projet "
        "technologique en environnement professionnel."))

    s.append(C.subh("Sujet et problématique métier"))
    s.append(C.para(
        "Les applications distribuées modernes génèrent un volume important de métriques, et les "
        "incidents de performance ont un coût direct (expérience utilisateur dégradée) et "
        "indirect (temps d'investigation). La supervision traditionnelle par <b>seuils "
        "statiques</b> produit trop d'alertes peu exploitables, détecte tardivement les dérives "
        "progressives et n'apporte ni cause ni piste d'action."))
    s.append(C.para(
        "Le sujet confié consiste à <b>concevoir et réaliser Cassandra</b>, une plateforme "
        "d'observabilité capable de <b>détecter</b>, d'<b>attribuer</b> et d'<b>expliquer</b> les "
        "dégradations de performance des API, et de démontrer son apport de manière <b>mesurable</b> "
        "face au seuillage statique."))

    s.append(C.subh("Objectifs du stage"))
    s.append(C.data_table(
        ["Catégorie", "Objectifs"],
        [
            ["Objectifs généraux",
             "Concevoir une plateforme d'observabilité de bout en bout, détectant et expliquant "
             "les dégradations de performance des API."],
            ["Objectifs spécifiques",
             "Baseline saisonnière, détection en couches (static / baseline / Isolation Forest), "
             "corrélation déploiement, explication LLM, évaluation chiffrée par fault injection."],
            ["Objectifs personnels",
             "Développer les compétences en observabilité, en apprentissage automatique supervisé "
             "et non supervisé, ainsi qu'en pratiques modernes d'ingénierie logicielle "
             "(Docker, tests, CI/CD)."],
        ],
        header_color=S.SECONDARY,
        col_widths=[4.2 * S.cm, S.CONTENT_WIDTH - 4.2 * S.cm]))

    s.append(C.subh("Livrables attendus"))
    for b in C.bullets([
        "Une plateforme <b>reproductible</b> déployable via Docker Compose.",
        "Une <b>évaluation quantitative</b> comparative entre l'approche proposée et un seuillage statique sur un banc d'injection de fautes.",
        "Des <b>tableaux de bord Grafana</b> dédiés au suivi opérationnel, à l'évaluation et à la self-observabilité.",
        "Une <b>documentation technique</b> complète ainsi que le présent rapport.",
    ]):
        s.append(b)
    s.append(C.callout(
        "Enjeu pour l'entreprise : disposer d'un socle d'observabilité intelligente, "
        "interprétable et reproductible, transférable à des services réels via OpenTelemetry, "
        "sans instrumentation propriétaire.", kind="key"))
    return s


# --------------------------------------------------------------------------- #
# Chapitre — Methodologie et organisation du projet                            #
# --------------------------------------------------------------------------- #

def demarche_travail():
    s = [C.begin_chapter("Méthodologie et organisation du projet", S.GREEN), C.spacer(6)]
    s.append(C.lead(
        "Ce chapitre présente la méthodologie adoptée, le planning prévisionnel, ainsi que "
        "l'organisation des différentes phases de développement de la plateforme Cassandra."))

    s.append(C.subh("Méthodologie adoptée"))
    s.append(C.para(
        "Le projet a été conduit selon une approche <b>itérative et incrémentale</b>, inspirée "
        "des pratiques Agile. Le développement a été organisé en plusieurs phases successives, "
        "chacune validée par un livrable fonctionnel. Cette démarche a permis de construire "
        "progressivement la plateforme Cassandra et de réduire les risques liés à l'intégration "
        "des différents composants."))

    s.append(C.subh("Planning prévisionnel"))
    s.append(C.para(
        "Le projet a été découpé en six phases sur environ 52 jours ouvrés, du banc de démo "
        "jusqu'à la campagne d'évaluation finale :"))
    s.append(C.figure(D.gantt_phases(),
                      "Figure 2 — Planning prévisionnel : six phases séquentielles avec un "
                      "livrable démontrable à chaque fin de phase."))

    s.append(C.subh("Organisation en phases"))
    s.append(C.data_table(
        ["Phase", "Durée", "Livrable / checkpoint"],
        [
            ["1 — Démonstrateur et ingestion", "10 j", "Métriques RED visibles pour les scénarios injectés"],
            ["2 — Features, baseline et alerting", "10 j", "Première détection avec génération d'alertes"],
            ["3 — Détection ML et évaluation", "11 j", "Comparaison layered vs seuils statiques"],
            ["4 — Corrélation déploiement", "7 j", "Identification du déploiement associé"],
            ["5 — Explication LLM et dashboards", "8 j", "Démonstration complète du cycle d'incident"],
            ["6 — Campagne finale et durcissement", "6 j", "Résultats consolidés et environnement reproductible"],
        ],
        header_color=S.GREEN, align_center_cols=[1],
        col_widths=[6.0 * S.cm, 1.8 * S.cm, S.CONTENT_WIDTH - 7.8 * S.cm]))

    s.append(C.subh("Environnement et outils de travail"))
    s.append(C.data_table(
        ["Catégorie", "Outils / technologies"],
        [
            ["Langage & frameworks", "Python, FastAPI"],
            ["ML & data", "scikit-learn (Isolation Forest), numpy, pandas"],
            ["Observabilité", "OpenTelemetry, Prometheus, Grafana"],
            ["Stockage", "TimescaleDB (PostgreSQL)"],
            ["Charge & tests", "k6, pytest"],
            ["Conteneurisation", "Docker, Docker Compose"],
            ["Versionning & CI", "Git, GitHub, GitHub Actions"],
            ["LLM & notification", "Gemini (google-genai), Slack (Block Kit)"],
        ],
        header_color=S.GREEN,
        col_widths=[4.6 * S.cm, S.CONTENT_WIDTH - 4.6 * S.cm]))

    s.append(C.subh("Pratiques d'ingénierie et suivi"))
    for b in C.bullets([
        "<b>Gestion de versions et CI :</b> utilisation de Git/GitHub et d'un workflow GitHub Actions pour automatiser les vérifications.",
        "<b>Tests automatisés :</b> une suite de tests couvre les composants principaux du service de détection afin de limiter les régressions.",
        "<b>Reproductibilité :</b> déploiement de l'ensemble de la plateforme via Docker Compose.",
        "<b>Documentation et suivi :</b> maintien de la documentation technique et suivi régulier avec l'encadrante en entreprise.",
    ]):
        s.append(b)
    return s


# --------------------------------------------------------------------------- #
# Assemblage                                                                  #
# --------------------------------------------------------------------------- #

def build_body():
    """Retourne la liste complete des Flowables du corps (hors couverture/TOC).

    La numerotation des chapitres et sous-sections est automatique (voir
    components.begin_chapter / subh) : l'ordre d'assemblage ci-dessous suffit.
    """
    C.reset_numbering()
    body = []
    # Front matter (non numerote)
    body += resume_abstract()
    body.append(PageBreak())
    body += remerciements()

    # Corps : chapitres "stage" puis chapitres techniques, references en fin.
    chapters = [
        org_accueil, contexte_stage, demarche_travail,   # dimension stage
        ch1, ch2, ch3, ch5, ch4, ch6, ch7, ch8, ch9, ch10,
        ch11, ch12, ch13, ch14, ch15, ch16,              # coeur technique
        references,                                       # non numerote
    ]
    for ch in chapters:
        body.append(PageBreak())
        body += ch()
    return body
