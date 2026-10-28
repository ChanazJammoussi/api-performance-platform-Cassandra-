import http from 'k6/http';
import { sleep, check } from 'k6';

// Charge LEGERE et CONSTANTE : 8 VUs.
// Objectif : garder le gateway (mono-thread) non sature -> p99 nominale stable ~95ms,
// coherente avec les buckets bas de la baseline gelee. Memes endpoints/poids que load.js.
// NB : ne remplace pas load.js (qui a servi a construire la baseline) ; profil dedie campagne.
export const options = {
  scenarios: {
    steady: {
      executor: 'constant-vus',
      vus: 8,
      duration: '4h',
    },
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

const ENDPOINTS = [
  { method: 'GET',  url: `${BASE_URL}/api/orders`,    weight: 40 },
  { method: 'GET',  url: `${BASE_URL}/api/orders/1`,  weight: 30 },
  { method: 'GET',  url: `${BASE_URL}/api/orders/2`,  weight: 15 },
  { method: 'POST', url: `${BASE_URL}/api/payments`,  weight: 15,
    body: JSON.stringify({ order_id: 1 }),
    headers: { 'Content-Type': 'application/json' } },
];

function pickEndpoint() {
  const rand = Math.random() * 100;
  let c = 0;
  for (const ep of ENDPOINTS) { c += ep.weight; if (rand < c) return ep; }
  return ENDPOINTS[0];
}

export default function () {
  const ep = pickEndpoint();
  const res = ep.method === 'POST'
    ? http.post(ep.url, ep.body, { headers: ep.headers })
    : http.get(ep.url);
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(Math.random() * 2 + 0.5);
}
