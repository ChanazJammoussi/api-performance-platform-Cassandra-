# -*- coding: utf-8 -*-
"""
charts.py — Graphiques de resultats (histogrammes) du rapport Cassandra.

Construits avec reportlab.graphics.charts pour rester vectoriels. Les valeurs
proviennent de la campagne d'evaluation reelle (docs/results.md, table eval_runs).
"""

from reportlab.graphics.shapes import Drawing, String, Rect, Line
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.lib.units import mm

import styles as S


def _legend(d, x, y, items):
    """Petite legende horizontale (couleur -> libelle)."""
    lg = Legend()
    lg.x = x
    lg.y = y
    lg.deltax = 95
    lg.dxTextSpace = 5
    lg.columnMaximum = 1
    lg.alignment = "right"
    lg.fontName = S.FONT
    lg.fontSize = 8
    lg.colorNamePairs = items
    d.add(lg)


def _title(d, width, text):
    d.add(String(width / 2, d.height - 12, text, fontName=S.FONT_B,
                 fontSize=9.5, fillColor=S.PURPLE, textAnchor="middle"))


def detection_by_fault(width=S.CONTENT_WIDTH):
    """Detection rate par type de faute : static vs layered."""
    d = Drawing(width, 200)
    _title(d, width, "Taux de detection par type de faute (%)")
    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 40
    bc.width = width - 80
    bc.height = 120
    bc.data = [
        [100, 100, 80, 50, 0],    # static
        [100, 100, 100, 50, 0],   # layered
    ]
    bc.categoryAxis.categoryNames = [
        "latency\ncreep", "downstream\nslow", "latency\nstep", "error\nburst", "pool\nshrink"]
    bc.categoryAxis.labels.fontName = S.FONT
    bc.categoryAxis.labels.fontSize = 7.5
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 100
    bc.valueAxis.valueStep = 25
    bc.valueAxis.labels.fontName = S.FONT
    bc.valueAxis.labels.fontSize = 7.5
    bc.bars[0].fillColor = S.SECONDARY
    bc.bars[1].fillColor = S.PRIMARY
    bc.barWidth = 6
    bc.groupSpacing = 14
    bc.barSpacing = 1
    d.add(bc)
    _legend(d, width - 230, 8, [(S.SECONDARY, "Static (Layer 0)"), (S.PRIMARY, "Layered")])
    return d


def global_metrics(width=S.CONTENT_WIDTH):
    """Detection rate global et faux positifs/heure : static vs layered."""
    d = Drawing(width, 200)
    _title(d, width, "Metriques globales : static vs layered")
    bc = VerticalBarChart()
    bc.x = 45
    bc.y = 40
    bc.width = width - 90
    bc.height = 120
    # Detection rate (%) et FP/h ramene a l'echelle *20 pour lisibilite visuelle
    bc.data = [
        [65, 1.13 * 20],
        [71, 1.29 * 20],
    ]
    bc.categoryAxis.categoryNames = ["Detection rate (%)", "Faux positifs / h  (x20)"]
    bc.categoryAxis.labels.fontName = S.FONT
    bc.categoryAxis.labels.fontSize = 8
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 80
    bc.valueAxis.valueStep = 20
    bc.valueAxis.labels.fontName = S.FONT
    bc.valueAxis.labels.fontSize = 7.5
    bc.bars[0].fillColor = S.SECONDARY
    bc.bars[1].fillColor = S.PRIMARY
    bc.barWidth = 16
    bc.groupSpacing = 30
    d.add(bc)
    _legend(d, width - 230, 8, [(S.SECONDARY, "Static (Layer 0)"), (S.PRIMARY, "Layered")])
    return d


def sensitivity_curve(width=S.CONTENT_WIDTH):
    """Sensibilite a la magnitude : error_burst core vs stress."""
    d = Drawing(width, 190)
    _title(d, width, "Sensibilite a la magnitude (taux de detection %)")
    bc = VerticalBarChart()
    bc.x = 45
    bc.y = 35
    bc.width = width - 90
    bc.height = 115
    bc.data = [
        [40, 100],   # core
        [60, 100],   # stress
    ]
    bc.categoryAxis.categoryNames = ["error_burst", "latency_step (layered)"]
    bc.categoryAxis.labels.fontName = S.FONT
    bc.categoryAxis.labels.fontSize = 8
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = 100
    bc.valueAxis.valueStep = 25
    bc.valueAxis.labels.fontName = S.FONT
    bc.valueAxis.labels.fontSize = 7.5
    bc.bars[0].fillColor = S.GREEN
    bc.bars[1].fillColor = S.PURPLE
    bc.barWidth = 16
    bc.groupSpacing = 30
    d.add(bc)
    _legend(d, width - 200, 6, [(S.GREEN, "Magnitude core"), (S.PURPLE, "Magnitude stress")])
    return d
