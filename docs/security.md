# Passe sécurité (spec §12)

Audit de la surface de sécurité du banc Cassandra. Statut : ✅ conforme · ⚠️ écart /
durcissement recommandé · 🔒 dev-only (à renforcer avant toute exposition).

| Domaine | Statut | Détail |
|---|---|---|
| Secrets hors repo | ✅ | Aucun secret en dur. `SLACK_WEBHOOK_URL` / `GEMINI_API_KEY` via `os.getenv`. `.env` gitignoré, `.env.example` = placeholders. |
| Redaction PII collector | ⚠️ | Denylist en place ; le spec préconise une allowlist (voir ci-dessous). |
| Fault API hors surface plateforme | ✅ | Le gateway (`:8000`) ne proxifie pas `/faults` : l'API fault n'est pas sur l'API publique. |
| Fault API non tracée | ⚠️ | Les routes `/faults/*` apparaissent dans les traces/métriques (§4.3 : ne doivent pas être tracées). |
| Auth API scoped keys | 🔒 | `deploy_api.py` (`:8090`) non authentifié (dev). |
| Credentials par défaut | 🔒 | DB `cassandra/cassandra`, Grafana `admin/admin` (dev). |

## 1. Secrets ✅
- `detection-service/notifier.py` : `SLACK_WEBHOOK_URL = os.getenv(...)` — absent → l'alerting
  logue et continue.
- `detection-service/explainer.py` : `GEMINI_API_KEY` via l'environnement — absent → fallback
  template déterministe.
- `.env` gitignoré ; `.env.example` ne contient que des placeholders.
- Vérifié : aucune occurrence de webhook Slack, clé `AIza…`/`sk-…` ou clé API en dur dans les
  fichiers suivis.

## 2. Redaction PII (collector) ⚠️
État : **denylist** dans `otel-collector/config.yaml` (`attributes/pii_redaction`) qui supprime
`http.url`, `http.target`, `net.peer.ip`, `enduser.id`, `user.id`.

Le spec §5.1/§12 préconise une **allowlist** (ne conserver que les attributs autorisés) : un nouvel
attribut porteur de PII ne peut alors pas fuiter par défaut.

**Tentative allowlist (`redaction` processor, `allow_all_keys: false`) → revertée** : ce processor
supprime aussi les attributs de **ressource** (dont `service.name`), ce qui casse les dimensions
spanmetrics et le label `service_name` (0 série produite). Validé empiriquement puis annulé.

**Recommandation** : réintroduire l'allowlist en préservant explicitement les clés de ressource
(`service.name`, `service.namespace`) **et** les clés de dimension
(`http.method`, `http.route`, `http.status_code`), puis **valider le pipeline** (présence de
`calls_total{span_kind="SPAN_KIND_SERVER"}` avec `http_route`) avant promotion.

## 3. Fault injection API ⚠️🔒
- **Hors surface plateforme** ✅ : le gateway ne route pas `/faults` ; l'API n'est atteignable que
  sur les ports directs des services démo (`orders:8001`, `payments:8002`).
- **Ports publiés en dev** 🔒 : `docker-compose.yml` publie `8001:8001` / `8002:8002` pour le
  scenario runner. En production, ne pas publier ces ports (réseau interne uniquement).
- **Routes tracées** ⚠️ : `/faults/*` apparaît comme `http_route` dans les métriques. Spec §4.3 :
  l'API fault ne doit pas être tracée comme trafic user. **Recommandation** : ajouter au collector
  un `filter` processor supprimant les spans dont `http.route` commence par `/faults/`
  (à valider comme au point 2 pour ne pas casser le pipeline).

## 4. Authentification API 🔒
Le spec §12 demande des **clés API scoped** (clé CI dédiée pour l'endpoint de déploiements).
`deploy_api.py` est actuellement non authentifié (dev, port interne). **Recommandation** : ajouter
une auth par clé API (header) avant toute exposition non-locale ; clé dédiée pour `POST /deploys`.

## 5. Credentials par défaut 🔒
DB (`cassandra/cassandra`) et Grafana (`admin/admin`) utilisent des identifiants par défaut de
développement, définis dans `docker-compose.yml`. **À surcharger via l'environnement** en dehors du
poste de dev.

## Transport
Remote-write Prometheus (`tls.insecure: true`) et connexions DB sans SSL : acceptable **à
l'intérieur du réseau Docker** ; aucune de ces liaisons n'est exposée publiquement.
