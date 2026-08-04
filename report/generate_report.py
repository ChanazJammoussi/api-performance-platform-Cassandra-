# -*- coding: utf-8 -*-
"""
generate_report.py — Point d'entree : assemble et produit le rapport PDF Cassandra.

Utilise ReportLab Platypus (BaseDocTemplate + PageTemplate + Frame) pour un controle
fin de la couverture, des en-tetes/pieds de page et de la table des matieres (2 passes).

Usage :
    python generate_report.py
Sortie :
    output/Cassandra_Report.pdf
"""

import os
import sys

# Permet d'importer les modules locaux (styles, components, ...) quel que soit le CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reportlab.lib.units import cm
from reportlab.graphics import renderPDF
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, PageBreak,
    NextPageTemplate)
from reportlab.platypus.tableofcontents import TableOfContents

import styles as S
import components as C
from content import chapters

_ST = S.build_styles()

# Logo entreprise (SVG vectoriel). Chemin relatif au script.
COMPANY_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "talan-logo.svg")


def _draw_svg_fitted(c, path, x, y, max_w, max_h, left_align=False):
    """Dessine un SVG mis a l'echelle pour tenir dans max_w x max_h.

    Rendu 100 % vectoriel via svglib. Best-effort : retourne False si le SVG est
    introuvable ou illisible, pour ne jamais casser la generation du rapport.
    left_align=True colle le dessin au bord gauche au lieu de le centrer.
    """
    try:
        from svglib.svglib import svg2rlg
        drawing = svg2rlg(path)
        if drawing is None or not drawing.width or not drawing.height:
            return False
        scale = min(max_w / drawing.width, max_h / drawing.height)
        scaled_w = drawing.width * scale
        scaled_h = drawing.height * scale
        ox = x if left_align else x + (max_w - scaled_w) / 2
        oy = y + (max_h - scaled_h) / 2
        c.saveState()
        c.translate(ox, oy)
        c.scale(scale, scale)
        renderPDF.draw(drawing, c, 0, 0)
        c.restoreState()
        return True
    except Exception:
        return False

# --------------------------------------------------------------------------- #
# Informations de couverture — A COMPLETER (placeholders editables)           #
# --------------------------------------------------------------------------- #
COVER = {
    "title": "Cassandra",
    "subtitle": "Intelligent Observability and Automated Incident Detection Platform",
    "doc_type": "Rapport de stage d'été",
    "author": "Chanaz Jammoussi",
    "period": "Stage d'été — 08/06/2026 - 07/08/2026",
    "company": "Talan Tunisie",
    "school": "ISIMM - Institut Supérieur d'Informatique et de Mathématiques de Monastir",
    "supervisor_company": "Mme Ines Boukhris",
    "supervisor_school": "Mr Imed Abbessi",
    "academic_year": "Année universitaire 2025 - 2026",
    "repo": "https://github.com/ChanazJammoussi/api-performance-platform-Cassandra-",
    "kpis": [],
}

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "output", "Cassandra_Report.pdf")


# --------------------------------------------------------------------------- #
# Fond de couverture (dessine sur le canvas)                                  #
# --------------------------------------------------------------------------- #

def draw_cover(canvas, doc):
    """Fond graphique moderne + contenu de la page de garde (canvas direct)."""
    w, h = S.PAGE_SIZE
    c = canvas
    c.saveState()

    # Fond blanc
    c.setFillColor(S.colors.white)
    c.rect(0, 0, w, h, stroke=0, fill=1)

    # Barre d'accent verticale (bord gauche) : identite visuelle sur fond blanc
    c.setFillColor(S.PRIMARY)
    c.rect(0, 0, 0.35 * cm, h, stroke=0, fill=1)

    # Cercles decoratifs discrets (zone mediane droite), teintes claires de la charte
    c.setFillColor(S.LIGHT_PINK)
    c.setFillAlpha(0.55)
    c.circle(w - 2.6 * cm, h * 0.55, 2.08 * cm, stroke=0, fill=1)
    c.setFillColor(S.LIGHT_GREEN)
    c.setFillAlpha(0.55)
    c.circle(w - 4.5 * cm, h * 0.535, 1.17 * cm, stroke=0, fill=1)
    c.setFillColor(S.LIGHT_BLUE)
    c.setFillAlpha(0.55)
    c.circle(w - 1.5 * cm, h * 0.45, 0.91 * cm, stroke=0, fill=1)
    c.setFillAlpha(1)

    # --- Contenu texte ---
    ml = S.MARGIN_LEFT

    # En-tete academique (meme ligne : ecole gauche, entreprise droite)
    c.setFillColor(S.MUTED)
    c.setFont(S.FONT, 8.5)
    c.drawString(ml, h - 1.5 * cm, COVER["school"])
    c.drawRightString(w - S.MARGIN_RIGHT, h - 1.5 * cm, COVER["company"])

    # Filet juste sous l'en-tete, au-dessus du logo
    filet_y = h - 1.75 * cm
    c.setStrokeColor(S.HAIRLINE)
    c.setLineWidth(0.6)
    c.line(ml, filet_y, w - S.MARGIN_RIGHT, filet_y)

    # === LOGO TALAN — haut droite, grand, sous le filet ===
    logo_h = 3.2 * cm
    logo_w = 7.0 * cm
    logo_x = w - S.MARGIN_RIGHT - logo_w
    logo_y = filet_y - 0.2 * cm - logo_h  # bas du logo
    if not _draw_svg_fitted(c, COMPANY_LOGO, logo_x, logo_y, logo_w, logo_h):
        c.setFillColor(S.MUTED)
        c.setFont(S.FONT_B, 14)
        c.drawString(logo_x, logo_y + logo_h / 2 - 6, "Talan")

    # Kicker (sous le logo)
    c.setFillColor(S.PRIMARY)
    c.setFont(S.FONT_B, 14)
    c.drawCentredString(w / 2, logo_y - 1.9 * cm, "PLATEFORME D'OBSERVABILITÉ INTELLIGENTE")

    # Titre
    title_y = logo_y - 4.4 * cm
    c.setFillColor(S.PURPLE)
    c.setFont(S.FONT_B, 74)
    c.drawCentredString(w / 2, title_y, COVER["title"])

    # Filet d'accent segmente sous le titre (centre)
    seg_w, seg_h, seg_y = 2.4 * cm, 0.14 * cm, title_y - 0.45 * cm
    total_seg_w = 3 * seg_w + 2 * 0.15 * cm
    seg_x0 = w / 2 - total_seg_w / 2
    for i, col in enumerate([S.PRIMARY, S.SECONDARY, S.GREEN]):
        c.setFillColor(col)
        c.rect(seg_x0 + i * (seg_w + 0.15 * cm), seg_y, seg_w, seg_h, stroke=0, fill=1)

    # Sous-titre (2 lignes fixes)
    c.setFillColor(S.INK)
    c.setFont(S.FONT, 18)
    c.drawCentredString(w / 2, title_y - 1.3 * cm, "Intelligent Observability and")
    c.drawCentredString(w / 2, title_y - 2.15 * cm, "Automated Incident Detection Platform")

    # Bloc type de document
    c.setFillColor(S.PURPLE)
    c.setFont(S.FONT_B, 21)
    c.drawString(ml, 9.05 * cm, COVER["doc_type"])
    c.setFillColor(S.MUTED)
    c.setFont(S.FONT, 14)
    c.drawString(ml, 8.15 * cm, COVER["period"])

    # Resume graphique : trois pastilles KPI (fond colore, texte blanc)
    y_kpi = h * 0.38
    chip_w = 4.6 * cm
    gap = 0.5 * cm
    palette = [S.PRIMARY, S.SECONDARY, S.GREEN]
    for i, (val, lab) in enumerate(COVER["kpis"]):
        x = ml + i * (chip_w + gap)
        c.setFillColor(palette[i])
        c.roundRect(x, y_kpi, chip_w, 1.9 * cm, 8, stroke=0, fill=1)
        c.setFillColor(S.colors.white)
        c.setFont(S.FONT_B, 22)
        c.drawCentredString(x + chip_w / 2, y_kpi + 0.85 * cm, val)
        c.setFont(S.FONT, 9)
        c.drawCentredString(x + chip_w / 2, y_kpi + 0.35 * cm, lab)

    # Bloc d'informations (bas de page)
    c.setFillColor(S.INK)
    c.setFont(S.FONT_B, 14)
    y0 = 7.35 * cm
    c.drawString(ml, y0, COVER["author"])
    c.setFont(S.FONT, 13)
    c.setFillColor(S.MUTED)
    lines = [
        COVER["company"],
        COVER["school"],
        "Encadrant entreprise : " + COVER["supervisor_company"],
        "Encadrant académique : " + COVER["supervisor_school"],
        COVER.get("academic_year", ""),
    ]
    for i, ln in enumerate(lines):
        c.drawString(ml, y0 - (i + 1) * 0.65 * cm, ln)

    # Lien depot GitHub
    repo_y = y0 - (len(lines) + 1) * 0.65 * cm
    c.setFillColor(S.SECONDARY)
    c.setFont(S.FONT, 11)
    c.drawString(ml, repo_y, COVER["repo"])

    # Filet de pied
    c.setStrokeColor(S.PRIMARY)
    c.setLineWidth(1)
    c.line(ml, 1.6 * cm, w - S.MARGIN_RIGHT, 1.6 * cm)
    c.setFillColor(S.MUTED)
    c.setFont(S.FONT, 8)
    c.drawString(ml, 1.2 * cm, "Cassandra · Detect · Attribute · Explain")
    c.drawRightString(w - S.MARGIN_RIGHT, 1.2 * cm, "Rapport de stage d'été")

    c.restoreState()


# --------------------------------------------------------------------------- #
# En-tete / pied de page des pages de contenu                                 #
# --------------------------------------------------------------------------- #

def draw_content_page(canvas, doc):
    """En-tete discret + pied de page avec numero, sur les pages de contenu."""
    w, h = S.PAGE_SIZE
    c = canvas
    c.saveState()

    # En-tete
    c.setFillColor(S.PURPLE)
    c.setFont(S.FONT_B, 8.5)
    c.drawString(S.MARGIN_LEFT, h - 1.4 * cm, "CASSANDRA")
    c.setFillColor(S.MUTED)
    c.setFont(S.FONT, 8.5)
    c.drawRightString(w - S.MARGIN_RIGHT, h - 1.4 * cm,
                      "Observabilité intelligente & détection d'incidents")
    c.setStrokeColor(S.HAIRLINE)
    c.setLineWidth(0.6)
    c.line(S.MARGIN_LEFT, h - 1.55 * cm, w - S.MARGIN_RIGHT, h - 1.55 * cm)

    # Pied de page
    c.setStrokeColor(S.HAIRLINE)
    c.line(S.MARGIN_LEFT, 1.35 * cm, w - S.MARGIN_RIGHT, 1.35 * cm)
    c.setFillColor(S.MUTED)
    c.setFont(S.FONT, 8)
    c.drawString(S.MARGIN_LEFT, 1.0 * cm, "Rapport de stage · " + COVER["author"])
    # Pastille numero de page
    c.setFillColor(S.PRIMARY)
    c.setFont(S.FONT_B, 8.5)
    c.drawRightString(w - S.MARGIN_RIGHT, 1.0 * cm, "Page %d" % doc.page)

    c.restoreState()


# --------------------------------------------------------------------------- #
# DocTemplate avec notification TOC                                           #
# --------------------------------------------------------------------------- #

class CassandraDoc(BaseDocTemplate):
    """BaseDocTemplate qui alimente la table des matieres via afterFlowable."""

    def afterFlowable(self, flowable):
        if isinstance(flowable, C.ChapterBanner) and flowable.in_toc:
            self.notify("TOCEntry", (flowable.level, flowable.toc_text, self.page))


def build():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    doc = CassandraDoc(
        OUTPUT, pagesize=S.PAGE_SIZE,
        leftMargin=S.MARGIN_LEFT, rightMargin=S.MARGIN_RIGHT,
        topMargin=S.MARGIN_TOP, bottomMargin=S.MARGIN_BOTTOM,
        title="Cassandra - Rapport de stage d'été", author=COVER["author"])

    # Frames
    cover_frame = Frame(0, 0, S.PAGE_SIZE[0], S.PAGE_SIZE[1], id="cover",
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    content_frame = Frame(S.MARGIN_LEFT, S.MARGIN_BOTTOM, S.CONTENT_WIDTH,
                          S.PAGE_SIZE[1] - S.MARGIN_TOP - S.MARGIN_BOTTOM,
                          id="content")

    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=draw_cover),
        PageTemplate(id="content", frames=[content_frame], onPage=draw_content_page),
    ])

    # Table des matieres
    toc = TableOfContents()
    toc.levelStyles = [_ST["toc1"], _ST["toc2"]]

    # --- Construction du story ---
    story = []
    # Page de garde (le fond est dessine par draw_cover ; un Spacer occupe le frame)
    story.append(Spacer(1, 1))
    story.append(NextPageTemplate("content"))
    story.append(PageBreak())

    # Sommaire
    story.append(C.ChapterBanner(None, "Sommaire", color=S.SECONDARY, in_toc=False))
    story.append(Spacer(1, 10))
    story.append(toc)
    story.append(PageBreak())  # le corps commence sur une page propre apres la TOC

    # Corps du rapport
    story += chapters.build_body()

    # 2 passes pour resoudre les numeros de page de la TOC
    doc.multiBuild(story)
    print("PDF genere :", OUTPUT)


if __name__ == "__main__":
    build()
