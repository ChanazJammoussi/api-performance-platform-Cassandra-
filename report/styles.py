# -*- coding: utf-8 -*-
"""
styles.py — Identite graphique et styles de paragraphes du rapport Cassandra.

Centralise la charte graphique (palette imposee), la geometrie de page et
l'ensemble des ParagraphStyle utilises par Platypus. Aucun contenu ici : uniquement
la couche presentation, reutilisable par tous les modules.
"""

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm

# --------------------------------------------------------------------------- #
# Palette imposee (charte graphique)                                          #
# --------------------------------------------------------------------------- #
PRIMARY = colors.HexColor("#E04580")      # rose principal
SECONDARY = colors.HexColor("#5581BA")    # bleu
PURPLE = colors.HexColor("#6B367D")       # violet
GREEN = colors.HexColor("#8E9425")        # vert olive
LIGHT_BLUE = colors.HexColor("#D4DFED")   # bleu clair (fonds)
LIGHT_PINK = colors.HexColor("#F7D0DF")   # rose clair (fonds)
LIGHT_GREEN = colors.HexColor("#D9DBB4")  # vert clair (fonds)

# Neutres derives (pour lisibilite du texte et des tableaux)
INK = colors.HexColor("#1E2230")          # texte principal, presque noir
MUTED = colors.HexColor("#5A6072")        # texte secondaire
HAIRLINE = colors.HexColor("#C9CFDC")     # filets fins
PAGE_BG = colors.white

# --------------------------------------------------------------------------- #
# Geometrie de page                                                           #
# --------------------------------------------------------------------------- #
PAGE_SIZE = A4
MARGIN_LEFT = 2.2 * cm
MARGIN_RIGHT = 2.2 * cm
MARGIN_TOP = 2.4 * cm
MARGIN_BOTTOM = 2.2 * cm
CONTENT_WIDTH = PAGE_SIZE[0] - MARGIN_LEFT - MARGIN_RIGHT

# Police : Helvetica est garantie par ReportLab sur toutes les plateformes,
# ce qui rend la generation reproductible sans installation de fontes.
FONT = "Helvetica"
FONT_B = "Helvetica-Bold"
FONT_I = "Helvetica-Oblique"
FONT_MONO = "Courier"


def build_styles():
    """Construit et retourne le dictionnaire de styles du rapport."""
    ss = getSampleStyleSheet()
    styles = {}

    styles["body"] = ParagraphStyle(
        "body", parent=ss["Normal"], fontName=FONT, fontSize=10,
        leading=15, textColor=INK, alignment=TA_JUSTIFY, spaceAfter=6,
    )
    styles["body_left"] = ParagraphStyle(
        "body_left", parent=styles["body"], alignment=TA_LEFT,
    )
    styles["lead"] = ParagraphStyle(
        "lead", parent=styles["body"], fontSize=11, leading=16,
        textColor=MUTED, spaceAfter=10,
    )

    # Titres de chapitre (H1) : rendus via un bandeau colore, donc texte blanc.
    styles["h1"] = ParagraphStyle(
        "h1", parent=ss["Heading1"], fontName=FONT_B, fontSize=17,
        leading=20, textColor=colors.white, spaceBefore=0, spaceAfter=0,
    )
    styles["h1_num"] = ParagraphStyle(
        "h1_num", parent=styles["h1"], fontSize=17, textColor=LIGHT_PINK,
    )
    styles["h2"] = ParagraphStyle(
        "h2", parent=ss["Heading2"], fontName=FONT_B, fontSize=13,
        leading=16, textColor=PURPLE, spaceBefore=14, spaceAfter=6,
    )
    styles["h3"] = ParagraphStyle(
        "h3", parent=ss["Heading3"], fontName=FONT_B, fontSize=11,
        leading=14, textColor=SECONDARY, spaceBefore=10, spaceAfter=4,
    )

    # Puces
    styles["bullet"] = ParagraphStyle(
        "bullet", parent=styles["body"], leftIndent=14, bulletIndent=2,
        spaceAfter=4, alignment=TA_LEFT,
    )

    # Encadres (definitions, choix techniques, points cles)
    styles["callout_title"] = ParagraphStyle(
        "callout_title", parent=styles["body"], fontName=FONT_B, fontSize=10,
        textColor=PURPLE, spaceAfter=3, alignment=TA_LEFT,
    )
    styles["callout_body"] = ParagraphStyle(
        "callout_body", parent=styles["body"], fontSize=9.5, leading=14,
        alignment=TA_LEFT, spaceAfter=0,
    )

    # Legendes de figures / tableaux
    styles["caption"] = ParagraphStyle(
        "caption", parent=styles["body"], fontSize=8.5, leading=11,
        textColor=MUTED, alignment=TA_CENTER, spaceBefore=4, spaceAfter=10,
        fontName=FONT_I,
    )

    # Cellules de tableau
    styles["th"] = ParagraphStyle(
        "th", parent=styles["body"], fontName=FONT_B, fontSize=9,
        textColor=colors.white, alignment=TA_LEFT, leading=12, spaceAfter=0,
    )
    styles["td"] = ParagraphStyle(
        "td", parent=styles["body"], fontSize=9, alignment=TA_LEFT,
        leading=12, spaceAfter=0,
    )
    styles["td_c"] = ParagraphStyle(
        "td_c", parent=styles["td"], alignment=TA_CENTER,
    )
    styles["code"] = ParagraphStyle(
        "code", parent=styles["body"], fontName=FONT_MONO, fontSize=8.5,
        leading=12, textColor=INK, alignment=TA_LEFT, spaceAfter=0,
        backColor=colors.HexColor("#F3F5F9"),
        borderPadding=(6, 6, 6, 6),
    )

    # Styles de la table des matieres
    styles["toc1"] = ParagraphStyle(
        "toc1", parent=styles["body"], fontName=FONT_B, fontSize=11,
        textColor=PURPLE, leading=18, spaceBefore=4,
    )
    styles["toc2"] = ParagraphStyle(
        "toc2", parent=styles["body"], fontSize=10, textColor=INK,
        leading=15, leftIndent=16,
    )

    # Styles de couverture
    styles["cover_title"] = ParagraphStyle(
        "cover_title", parent=ss["Title"], fontName=FONT_B, fontSize=52,
        leading=54, textColor=colors.white, alignment=TA_LEFT, spaceAfter=0,
    )
    styles["cover_sub"] = ParagraphStyle(
        "cover_sub", parent=styles["body"], fontSize=14, leading=19,
        textColor=colors.white, alignment=TA_LEFT,
    )
    styles["cover_meta"] = ParagraphStyle(
        "cover_meta", parent=styles["body"], fontSize=10.5, leading=16,
        textColor=colors.white, alignment=TA_LEFT,
    )
    styles["cover_kicker"] = ParagraphStyle(
        "cover_kicker", parent=styles["body"], fontName=FONT_B, fontSize=12,
        leading=14, textColor=LIGHT_PINK, alignment=TA_LEFT,
    )
    return styles
