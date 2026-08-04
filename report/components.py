# -*- coding: utf-8 -*-
"""
components.py — Composants Platypus reutilisables (briques de mise en page).

Regroupe les Flowables et fabriques de tableaux communs a tous les chapitres :
bandeaux de titre colores, encadres (definitions / choix techniques / points cles),
tableaux professionnels a en-tete colore et lignes alternees, figures legendees.
"""

from reportlab.platypus import (
    Flowable, Paragraph, Table, TableStyle, Spacer, KeepTogether)
from reportlab.lib.units import mm
from reportlab.lib import colors

import styles as S

_ST = S.build_styles()


# --------------------------------------------------------------------------- #
# Bandeau de titre de chapitre (H1) — Flowable dessine + ancre TOC            #
# --------------------------------------------------------------------------- #

class ChapterBanner(Flowable):
    """Bandeau colore plein largeur portant le numero et le titre du chapitre.

    Sert aussi d'ancre pour la table des matieres : `generate_report.afterFlowable`
    detecte ce Flowable pour notifier la TOC (niveau 0).
    """

    def __init__(self, number, title, color=S.PRIMARY, level=0, in_toc=True):
        super().__init__()
        self.number = number
        self.title = title
        self.color = color
        self.level = level
        self.in_toc = in_toc
        self.width = S.CONTENT_WIDTH
        self.height = 30
        # texte utilise par la TOC
        self.toc_text = f"{number}. {title}" if number else title

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        return availWidth, self.height + 8

    def draw(self):
        c = self.canv
        h = self.height
        # Bande principale
        c.setFillColor(self.color)
        c.roundRect(0, 0, self.width, h, 4, stroke=0, fill=1)
        # Pastille numero
        if self.number:
            c.setFillColor(colors.white)
            c.setFont(S.FONT_B, 15)
            c.drawString(12, h / 2 - 5, str(self.number))
            c.setStrokeColor(S.LIGHT_PINK)
            c.setLineWidth(1)
            c.line(34, 6, 34, h - 6)
            tx = 44
        else:
            tx = 14
        c.setFillColor(colors.white)
        c.setFont(S.FONT_B, 15)
        c.drawString(tx, h / 2 - 5, self.title)


def h2(text):
    return Paragraph(text, _ST["h2"])


def h3(text):
    return Paragraph(text, _ST["h3"])


# --------------------------------------------------------------------------- #
# Numerotation automatique des chapitres et sous-sections                     #
# --------------------------------------------------------------------------- #
# Les numeros ne sont plus codes en dur : ils sont attribues a la construction
# du story, dans l'ordre d'assemblage. Inserer / reordonner un chapitre ne
# desynchronise donc jamais les numeros de section.

_num = {"chap": 0, "sub": 0}


def reset_numbering():
    """Remet les compteurs a zero (a appeler en debut de build_body)."""
    _num["chap"] = 0
    _num["sub"] = 0


def begin_chapter(title, color=S.PRIMARY):
    """Incremente le compteur de chapitre et retourne le bandeau numerote."""
    _num["chap"] += 1
    _num["sub"] = 0
    return ChapterBanner(str(_num["chap"]), title, color)


def subh(title):
    """Sous-section numerotee automatiquement : 'chapitre.sous-section Titre'."""
    _num["sub"] += 1
    return h2("%d.%d %s" % (_num["chap"], _num["sub"], title))


def para(text, style="body"):
    return Paragraph(text, _ST[style])


def lead(text):
    return Paragraph(text, _ST["lead"])


def bullets(items, style="bullet"):
    """Retourne une liste de Paragraphes a puces."""
    out = []
    for it in items:
        out.append(Paragraph(f"&bull;&nbsp;&nbsp;{it}", _ST[style]))
    return out


def spacer(h=6):
    return Spacer(1, h)


# --------------------------------------------------------------------------- #
# Encadres (callouts)                                                         #
# --------------------------------------------------------------------------- #

_CALLOUT_THEME = {
    "def": (S.LIGHT_BLUE, S.SECONDARY, "Definition"),
    "tech": (S.LIGHT_GREEN, S.GREEN, "Choix technique"),
    "key": (S.LIGHT_PINK, S.PRIMARY, "Point cle"),
}


def callout(body, kind="key", title=None):
    """Encadre a barre d'accent laterale (definition / technique / point cle)."""
    bg, accent, default_title = _CALLOUT_THEME.get(kind, _CALLOUT_THEME["key"])
    title = title or default_title
    inner = [
        Paragraph(title.upper(), _ST["callout_title"]),
        Paragraph(body, _ST["callout_body"]),
    ]
    t = Table([[inner]], colWidths=[S.CONTENT_WIDTH])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return KeepTogether([Spacer(1, 4), t, Spacer(1, 8)])


# --------------------------------------------------------------------------- #
# Tableaux professionnels                                                     #
# --------------------------------------------------------------------------- #

def data_table(headers, rows, col_widths=None, header_color=S.PURPLE,
               align_center_cols=None, highlight_last_col=False):
    """Tableau a en-tete colore, lignes alternees et cellules Paragraph.

    - headers : liste de libelles d'en-tete
    - rows    : liste de listes de chaines (contenu)
    - align_center_cols : indices de colonnes centrees
    """
    align_center_cols = align_center_cols or []
    n = len(headers)
    if col_widths is None:
        col_widths = [S.CONTENT_WIDTH / n] * n

    def cell(txt, i, header=False):
        if header:
            return Paragraph(str(txt), _ST["th"])
        st = "td_c" if i in align_center_cols else "td"
        return Paragraph(str(txt), _ST[st])

    data = [[cell(h, i, header=True) for i, h in enumerate(headers)]]
    for r in rows:
        data.append([cell(v, i) for i, v in enumerate(r)])

    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), header_color),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, header_color),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, S.HAIRLINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FB")]),
    ]
    if highlight_last_col:
        style.append(("BACKGROUND", (-1, 1), (-1, -1), S.LIGHT_PINK))
        style.append(("FONTNAME", (-1, 1), (-1, -1), S.FONT_B))
    t.setStyle(TableStyle(style))
    return t


def figure(drawing, caption):
    """Regroupe un schema/graphe et sa legende (jamais coupes)."""
    cap = Paragraph(caption, _ST["caption"])
    return KeepTogether([Spacer(1, 6), drawing, cap])


def kpi_row(items):
    """Bandeau de 3-4 KPI (grande valeur + libelle), couleurs de la charte."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    val_style = ParagraphStyle("kpi_val", parent=_ST["td_c"], fontName=S.FONT_B,
                               fontSize=22, leading=25, textColor=colors.white,
                               alignment=TA_CENTER)
    lab_style = ParagraphStyle("kpi_lab", parent=_ST["td_c"], fontSize=8.5,
                               leading=11, textColor=colors.white, alignment=TA_CENTER)
    palette = [S.PRIMARY, S.SECONDARY, S.PURPLE, S.GREEN]
    cells = []
    for i, (value, label) in enumerate(items):
        col = palette[i % len(palette)]
        inner = Table(
            [[Paragraph(str(value), val_style)],
             [Paragraph(str(label), lab_style)]],
            colWidths=[(S.CONTENT_WIDTH - (len(items) - 1) * 6) / len(items)])
        inner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), col),
            ("TOPPADDING", (0, 0), (-1, 0), 11),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 11),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        cells.append(inner)
    row = Table([cells], colWidths=[(S.CONTENT_WIDTH) / len(items)] * len(items))
    row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return KeepTogether([Spacer(1, 4), row, Spacer(1, 8)])
