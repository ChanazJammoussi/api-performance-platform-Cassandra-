import http from 'k6/http';
import { sleep, check } from 'k6';

// un "jour" compressé en 2 heures
// le trafic monte le matin, pic à midi, descend le soir
const CYCLE_DURATION = 120; // minutes

function diurnalVUs(t) {
    // t = minutes écoulées depuis le début (0 à 120)
    // simule une courbe sinusoïdale : min 5 VUs, max 50 VUs
    const normalized = t / CYCLE_DURATION; // 0 à 1
    const sinValue = Math.sin(normalized * Math.PI); // 0 -> 1 -> 0
    return Math.floor(5 + sinValue * 45);
}

export const options = {
    scenarios: {
        diurnal_load: {
            executor: 'ramping-vus',
            startVUs: 5,
            stages: [
                { duration: '20m', target: 25 },  // matin : montée
                { duration: '40m', target: 50 },  // midi : pic
                { duration: '20m', target: 50 },  // après-midi : maintien
                { duration: '30m', target: 15 },  // soir : descente
                { duration: '10m', target: 5 },   // nuit : creux
            ],
        },
    },
    thresholds: {
        http_req_duration: ['p(95)<2000'],
        http_req_failed: ['rate<0.1'],
    },
};

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

const ENDPOINTS = [
    { method: 'GET', url: `${BASE_URL}/api/orders`,      weight: 40 },
    { method: 'GET', url: `${BASE_URL}/api/orders/1`,    weight: 30 },
    { method: 'GET', url: `${BASE_URL}/api/orders/2`,    weight: 15 },
    { method: 'POST', url: `${BASE_URL}/api/payments`,   weight: 15,
      body: JSON.stringify({ order_id: 1 }),
      headers: { 'Content-Type': 'application/json' } },
];

function pickEndpoint() {
    const rand = Math.random() * 100;
    let cumulative = 0;
    for (const ep of ENDPOINTS) {
        cumulative += ep.weight;
        if (rand < cumulative) return ep;
    }
    return ENDPOINTS[0];
}

export default function () {
    const ep = pickEndpoint();

    let res;
    if (ep.method === 'POST') {
        res = http.post(ep.url, ep.body, { headers: ep.headers });
    } else {
        res = http.get(ep.url);
    }

    check(res, {
        'status 200': (r) => r.status === 200,
    });

    sleep(Math.random() * 2 + 0.5);
}
