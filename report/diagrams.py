# -*- coding: utf-8 -*-
"""
diagrams.py — Diagrammes vectoriels (schemas d'architecture) du rapport.

Tous les schemas sont dessines a la main avec reportlab.graphics afin de rester
100 % vectoriels (nets a toute echelle) et de respecter la charte graphique.
Chaque fonction retourne un objet Drawing (un Flowable) pret a inserer dans le story.
"""

from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon, Group
from reportlab.lib.units import mm

import styles as S

# --------------------------------------------------------------------------- #
# Primitives de dessin                                                        #
# --------------------------------------------------------------------------- #

def _wrap(text, max_chars):
    """Coupe un libelle en plusieurs lignes pour tenir dans une boite."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def box(group, x, y, w, h, text, fill, text_color=None, stroke=None,
        font_size=8.5, bold=True, radius=5):
    """Dessine une boite arrondie avec un libelle centre (multi-lignes)."""
    text_color = text_color or S.colors.white
    r = Rect(x, y, w, h, rx=radius, ry=radius, fillColor=fill,
             strokeColor=stroke or fill, strokeWidth=0.8)
    group.add(r)
    lines = _wrap(text, int(w / (font_size * 0.55)))
    font = S.FONT_B if bold else S.FONT
    total_h = len(lines) * (font_size + 2)
    ty = y + h / 2 + total_h / 2 - font_size
    for ln in lines:
        s = String(x + w / 2, ty, ln, fontName=font, fontSize=font_size,
                   fillColor=text_color, textAnchor="middle")
        group.add(s)
        ty -= (font_size + 2)


def arrow(group, x1, y1, x2, y2, color=None, width=1.2, label=None):
    """Fleche simple avec tete triangulaire (orthogonale ou libre)."""
    color = color or S.MUTED
    group.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=width))
    # Tete de fleche
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    size = 5
    p1 = (x2, y2)
    p2 = (x2 - size * math.cos(ang - 0.4), y2 - size * math.sin(ang - 0.4))
    p3 = (x2 - size * math.cos(ang + 0.4), y2 - size * math.sin(ang + 0.4))
    group.add(Polygon([p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]],
                      fillColor=color, strokeColor=color))
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        group.add(String(mx, my + 3, label, fontName=S.FONT, fontSize=7,
                         fillColor=S.MUTED, textAnchor="middle"))


# --------------------------------------------------------------------------- #
# Schema 1 — Pipeline end-to-end (7 etapes)                                   #
# --------------------------------------------------------------------------- #

def pipeline_end_to_end(width=S.CONTENT_WIDTH):
    """Flux global : generation trafic -> ingestion -> ... -> notification."""
    h = 275
    d = Drawing(width, h)
    g = Group()
    d.add(g)

    bw, bh = width * 0.26, 34
    col1 = 4
    col2 = width / 2 - bw / 2
    col3 = width - bw - 4
    # Niveaux verticaux (tous >= 0 pour ne pas deborder sous la legende)
    y1, y2, y3, y4, y5 = 232, 177, 122, 67, 12

    # Ligne 1 : source de trafic
    box(g, col1, y1, bw, bh, "k6 (trafic diurnal) + services demo\ngateway / orders / payments", S.PRIMARY)
    box(g, col3, y1, bw, bh, "Fault Injection API\n(verite-terrain experimentale)", S.PURPLE)

    # Ligne 2 : ingestion
    box(g, col2, y2, bw, bh, "OTel Collector + spanmetrics\n(metriques RED, redaction PII)", S.SECONDARY)

    # Ligne 3 : stockage
    box(g, col2, y3, bw, bh, "Prometheus : metriques RED\nTimescaleDB : features, baseline, alertes, anomalies", S.GREEN, font_size=7.5)

    # Ligne 4 : detection + correlation
    box(g, col1, y4, bw, bh, "Detection en couches\n(static + baseline + iForest)", S.PRIMARY)
    box(g, col3, y4, bw, bh, "Correlation deploiement\n+ DAG de services", S.SECONDARY)

    # Ligne 5 : sortie
    box(g, col2, y5, bw, bh, "Explication LLM -> Slack\n+ Dashboards Grafana", S.PURPLE)

    # Fleches
    arrow(g, col1 + bw / 2, y1, col2 + bw * 0.4, y2 + bh)
    arrow(g, col3 + bw / 2, y1, col2 + bw * 0.6, y2 + bh)
    arrow(g, col2 + bw / 2, y2, col2 + bw / 2, y3 + bh)
    arrow(g, col2 + bw * 0.35, y3, col1 + bw / 2, y4 + bh)
    arrow(g, col2 + bw * 0.65, y3, col3 + bw / 2, y4 + bh)
    arrow(g, col1 + bw / 2, y4, col2 + bw * 0.4, y5 + bh)
    arrow(g, col3 + bw / 2, y4, col2 + bw * 0.6, y5 + bh)
    return d


# --------------------------------------------------------------------------- #
# Schema 2 — Detection en couches                                             #
# --------------------------------------------------------------------------- #

def layered_detection(width=S.CONTENT_WIDTH):
    """Layer 0/1/2, direction gating, score combine, state machine."""
    h = 300
    d = Drawing(width, h)
    g = Group()
    d.add(g)

    bw = width * 0.28
    bh = 40
    left = 2
    mid = width / 2 - bw / 2
    right = width - bw - 2

    box(g, mid, 255, bw, bh, "Features endpoint-relatives\ndeviations baseline, ratios, pente Theil-Sen, rps_delta", S.GREEN, font_size=8)

    box(g, left, 185, bw, bh, "Layer 0 - static\np99 > SLO ?", S.SECONDARY)
    box(g, mid, 185, bw, bh, "Layer 1 - baseline\ndeviation vs bande -> direction", S.PRIMARY)
    box(g, right, 185, bw, bh, "Layer 2 - Isolation Forest\nscore calibre [0,1]", S.PURPLE)

    # Direction gating
    box(g, right, 120, bw, bh, "Direction = degradation ?\ngating de la couche ML", S.PURPLE, font_size=8)

    # Combine
    box(g, mid, 60, bw, bh + 6, "Score combine = baseline + (1-baseline)*ml_gated\nplancher = depassement SLO", S.PRIMARY, font_size=7.6)

    # Decision
    box(g, left, 60, bw, bh, "Anomaly store\nscore, layer, direction, top-3", S.GREEN, font_size=8)
    box(g, mid, 0, bw, bh, "State machine\nOK -> PENDING -> FIRING", S.SECONDARY)

    arrow(g, mid + bw / 2, 255, mid + bw / 2, 185 + bh)
    arrow(g, mid + bw * 0.2, 255, left + bw / 2, 185 + bh)
    arrow(g, mid + bw * 0.8, 255, right + bw / 2, 185 + bh)
    arrow(g, right + bw / 2, 185, right + bw / 2, 120 + bh)
    arrow(g, mid + bw / 2, 185, mid + bw / 2, 60 + bh + 6)
    arrow(g, right + bw / 2, 120, mid + bw * 0.85, 60 + bh + 6)
    arrow(g, mid + bw * 0.2, 60, left + bw / 2, 60 + bh)
    arrow(g, mid + bw / 2, 60, mid + bw / 2, 0 + bh)
    return d


# --------------------------------------------------------------------------- #
# Schema 3 — Machine a etats                                                  #
# --------------------------------------------------------------------------- #

def state_machine(width=S.CONTENT_WIDTH):
    """OK -> PENDING -> FIRING -> RESOLVING -> OK (hysteresis)."""
    h = 120
    d = Drawing(width, h)
    g = Group()
    d.add(g)

    n = 4
    gap = 14
    bw = (width - (n - 1) * gap) / n
    bh = 44
    y = 55
    labels = [("OK", S.GREEN), ("PENDING", S.SECONDARY),
              ("FIRING", S.PRIMARY), ("RESOLVING", S.PURPLE)]
    xs = []
    for i, (lab, col) in enumerate(labels):
        x = i * (bw + gap)
        xs.append(x)
        box(g, x, y, bw, bh, lab, col, font_size=10)

    trans = ["score > seuil\n1 fenetre", "soutenu\nM fenetres", "score < clear"]
    for i in range(n - 1):
        arrow(g, xs[i] + bw, y + bh / 2, xs[i + 1], y + bh / 2)
        g.add(String(xs[i] + bw + gap / 2, y + bh + 6, trans[i].split("\n")[0],
                     fontName=S.FONT, fontSize=6.5, fillColor=S.MUTED, textAnchor="middle"))
        g.add(String(xs[i] + bw + gap / 2, y + bh - 2, trans[i].split("\n")[1] if "\n" in trans[i] else "",
                     fontName=S.FONT, fontSize=6.5, fillColor=S.MUTED, textAnchor="middle"))

    # Retour RESOLVING -> OK (hysteresis down)
    arrow(g, xs[3] + bw / 2, y, xs[0] + bw / 2, y, color=S.GREEN)
    g.add(String(width / 2, 20, "RESOLVING -> OK : clear soutenu R fenetres (hysteresis)",
                 fontName=S.FONT_I, fontSize=7, fillColor=S.MUTED, textAnchor="middle"))
    return d


# --------------------------------------------------------------------------- #
# Schema 4 — Cycle de vie du modele ML                                        #
# --------------------------------------------------------------------------- #

def ml_lifecycle(width=S.CONTENT_WIDTH):
    """Historique -> exclusion fautes -> fit -> sanity gate -> promotion latest."""
    h = 130
    d = Drawing(width, h)
    g = Group()
    d.add(g)
    steps = [
        ("Historique trailing\n(fautes exclues)", S.GREEN),
        ("Fit Isolation Forest\n(global, nightly)", S.PURPLE),
        ("Artefact horodate\n(precedent conserve)", S.SECONDARY),
        ("Sanity gate\n(shift de distribution)", S.PRIMARY),
        ("Pointeur latest\n(charge par le detecteur)", S.GREEN),
    ]
    n = len(steps)
    gap = 10
    bw = (width - (n - 1) * gap) / n
    bh = 52
    y = 45
    xs = []
    for i, (lab, col) in enumerate(steps):
        x = i * (bw + gap)
        xs.append(x)
        box(g, x, y, bw, bh, lab, col, font_size=7.5)
    for i in range(n - 1):
        arrow(g, xs[i] + bw, y + bh / 2, xs[i + 1], y + bh / 2)
    g.add(String(width / 2, 18, "Promotion refusee si la distribution de score derive sur la fenetre de reference",
                 fontName=S.FONT_I, fontSize=7, fillColor=S.MUTED, textAnchor="middle"))
    return d


# --------------------------------------------------------------------------- #
# Schema 5 — DAG de dependances de service                                    #
# --------------------------------------------------------------------------- #

def service_dag(width=S.CONTENT_WIDTH):
    """Graphe de dependances : gateway -> orders / payments -> orders."""
    h = 200
    d = Drawing(width, h)
    g = Group()
    d.add(g)
    bw, bh = width * 0.30, 40

    top = width / 2 - bw / 2
    box(g, top, 145, bw, bh, "GET /api/orders/{id}\n(gateway, user-facing)", S.SECONDARY, font_size=8)

    left = width * 0.06
    box(g, left, 75, bw, bh, "POST /payments\n(payments)", S.PRIMARY, font_size=8)
    right = width - bw - width * 0.06
    box(g, right, 75, bw, bh, "GET /orders/{id}\n(orders)", S.PURPLE, font_size=8)

    box(g, width / 2 - bw / 2, 5, bw, bh, "connection pool\nPostgreSQL", S.GREEN, font_size=8)

    arrow(g, top + bw * 0.35, 145, left + bw * 0.7, 75 + bh, label="cascade")
    arrow(g, top + bw / 2, 145, right + bw / 2, 75 + bh, label="direct")
    arrow(g, left + bw * 0.7, 75, width / 2 - bw * 0.25, 5 + bh)
    arrow(g, right + bw / 2, 75, width / 2 + bw * 0.25, 5 + bh)
    return d


# --------------------------------------------------------------------------- #
# Schema 6 — Organigramme simplifie de l'organisme d'accueil                   #
# --------------------------------------------------------------------------- #

def org_chart(width=S.CONTENT_WIDTH):
    """Positionnement du stage dans l'organisation (Groupe -> filiale -> pole -> equipe)."""
    h = 210
    d = Drawing(width, h)
    g = Group()
    d.add(g)
    bw, bh = width * 0.44, 38
    cx = width / 2 - bw / 2

    box(g, cx, 165, bw, bh, "Groupe Talan\nconseil en innovation & technologie", S.PURPLE, font_size=8.5)
    box(g, cx, 110, bw, bh, "Talan Tunisie\ncentre d'expertise / delivery", S.SECONDARY, font_size=8.5)
    box(g, cx, 55, bw, bh, "Encadrement du stage : Mme Ines Boukhris", S.GREEN, font_size=8.5)
    box(g, cx, 0, bw, bh, "Projet Cassandra", S.PRIMARY, font_size=8.5)

    for y in (165, 110, 55):
        arrow(g, cx + bw / 2, y, cx + bw / 2, y - 17)
    return d


# --------------------------------------------------------------------------- #
# Schema 7 — Planning previsionnel (diagramme de Gantt des 6 phases)          #
# --------------------------------------------------------------------------- #

def gantt_phases(width=S.CONTENT_WIDTH):
    """Planning en 6 phases sequentielles avec checkpoints demoables (spec §14)."""
    phases = [
        ("Phase 1 - Demo & ingestion", 10, S.PRIMARY),
        ("Phase 2 - Features, baseline, alerting v0", 10, S.SECONDARY),
        ("Phase 3 - Detection ML & evaluation", 11, S.PURPLE),
        ("Phase 4 - Correlation deploiement", 7, S.GREEN),
        ("Phase 5 - Explications LLM & dashboards", 8, S.PRIMARY),
        ("Phase 6 - Campagne & durcissement", 6, S.SECONDARY),
    ]
    total = sum(p[1] for p in phases)  # ~52 jours
    label_w = width * 0.44
    track_w = width - label_w - 6
    row_h, gap = 20, 8
    top_pad = 16
    h = len(phases) * (row_h + gap) + top_pad + 18
    d = Drawing(width, h)
    g = Group()
    d.add(g)

    # Barres de phase, positionnees cumulativement sur la duree totale
    cursor = 0
    y = h - top_pad - row_h
    for name, dur, col in phases:
        x = label_w + track_w * (cursor / total)
        bw = track_w * (dur / total)
        # libelle
        g.add(String(0, y + row_h / 2 - 3, name, fontName=S.FONT, fontSize=8,
                     fillColor=S.INK, textAnchor="start"))
        # barre
        g.add(Rect(x, y, bw, row_h, rx=3, ry=3, fillColor=col, strokeColor=col))
        g.add(String(x + bw / 2, y + row_h / 2 - 3, "%d j" % dur, fontName=S.FONT_B,
                     fontSize=7.5, fillColor=S.colors.white, textAnchor="middle"))
        cursor += dur
        y -= (row_h + gap)

    # Axe du temps (jours cumules)
    axis_y = y + gap + 2
    g.add(Line(label_w, axis_y, label_w + track_w, axis_y, strokeColor=S.HAIRLINE, strokeWidth=0.8))
    for frac, lab in [(0, "J0"), (0.25, ""), (0.5, "~26 j"), (0.75, ""), (1.0, "~52 j")]:
        xx = label_w + track_w * frac
        g.add(Line(xx, axis_y, xx, axis_y - 3, strokeColor=S.HAIRLINE, strokeWidth=0.8))
        if lab:
            g.add(String(xx, axis_y - 12, lab, fontName=S.FONT, fontSize=7,
                         fillColor=S.MUTED, textAnchor="middle"))
    return d
