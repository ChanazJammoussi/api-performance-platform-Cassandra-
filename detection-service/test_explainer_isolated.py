"""
test_explainer_isolated.py -- teste explainer.py sur une alerte FIRING existante
sans toucher au flux temps reel de detector.py.
"""

import json
import logging
import psycopg2
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from explainer import build_context, call_llm, generate_explanation

DB_URL = "postgresql://cassandra:cassandra@localhost:5434/cassandra"

# Endpoint a tester -- adapte si besoin
ENDPOINT_ID = "GET /orders"
SIGNAL_TYPE = "p99_ms"


def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    print("=" * 70)
    print("ETAPE 1 -- build_context()")
    print("=" * 70)
    context = build_context(cur, ENDPOINT_ID, SIGNAL_TYPE, now)
    print(json.dumps(context, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("ETAPE 2 -- call_llm()")
    print("=" * 70)
    explanation = call_llm(context)
    if explanation is None:
        print("call_llm() a retourne None -- le fallback serait utilise")
    else:
        print(json.dumps(explanation, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("ETAPE 3 -- generate_explanation() (orchestration complete + ecriture DB)")
    print("=" * 70)
    final = generate_explanation(cur, ENDPOINT_ID, SIGNAL_TYPE, now)
    conn.commit()
    print(json.dumps(final, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("ETAPE 4 -- verification lecture DB")
    print("=" * 70)
    cur.execute("""
        SELECT explanation FROM alerts
        WHERE endpoint_id = %s AND signal_type = %s
    """, (ENDPOINT_ID, SIGNAL_TYPE))
    row = cur.fetchone()
    if row and row[0]:
        print("Explanation bien presente en base:")
        print(json.dumps(row[0], indent=2, ensure_ascii=False))
    else:
        print("ATTENTION: aucune explanation trouvee en base")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
