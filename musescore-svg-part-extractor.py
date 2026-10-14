#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extraire_portee.py - garder un ou plusieurs instruments dans une partition SVG.

Le fichier d'entrée est un export SVG de partition (MuseScore) : chaque type d'élément graphique est
un chemin <path> avec un attribut class (StaffLines, Note, Stem, InstrumentName, BarLine, ...).

Le script repère les portées et les systèmes, identifie l'instrument de chaque portée (étiquette,
clé, alignement sur un système de référence), puis écrit un SVG qui ne contient que les éléments des
instruments choisis. Les coordonnées ne changent pas : les portées gardées s'affichent exactement comme
dans l'original.

Deux façons de l'utiliser :
    - interface graphique (sans argument, ou double-clic sur le fichier) :
          python3 extraire_portee.py
    - ligne de commande :
          python3 extraire_portee.py partition.svg                 (question interactive)
          python3 extraire_portee.py partition.svg --liste         (liste des instruments)
          python3 extraire_portee.py partition.svg -i 3            (un instrument, sans question)
          python3 extraire_portee.py partition.svg -i 1 3          (plusieurs instruments)
          python3 extraire_portee.py partition.svg --tous          (un fichier par instrument)

Dépendances : Python 3.8 ou plus récent, bibliothèque standard uniquement (Tkinter pour l'interface).
L'aperçu de l'interface demande en plus PyMuPDF (pip install pymupdf), facultatif.
"""

import argparse
import base64
import math
import os
import re
import subprocess
import sys

# --- Parametres (en espaces de portee, sauf mention contraire) -----------------
ECART_MAX_DANS_SYSTEME = 12.0   # au-dela de cet ecart entre portees, on change de systeme
TOLERANCE_CENTRAGE = 0.5        # tolerance de centrage d'une etiquette sur ses portees
TOLERANCE_FORME = 0.3           # tolerance (unites SVG) pour reconnaitre une meme etiquette
MARGE_SYSTEME = 4.0             # marge autour d'un systeme (pour le distinguer de la page)
HAUTEUR_MIN_CLEF = 1.0          # un morceau plus bas qu'un espace est un point, pas une clef
HAUTEUR_CLEF_SOL = 5.5          # hauteur (espaces) a partir de laquelle la clef est une clef de sol
HAUTEUR_CLEF_UT = 3.5           # ... une clef d'ut
HAUTEUR_CLEF_FA = 2.4           # ... une clef de fa

NOM_CLEF = {'G': 'sol', 'C': 'ut', 'F': 'fa', '?': 'inconnue'}
COULEUR_APERCU = '#d7191c'     # couleur des instruments choisis dans l'aperçu
CLASSES_PARTAGEES = ('Text', 'Page', 'MeasureNumber', 'Tempo')   # communes à toute la page


# ==============================================================================
# 1. Lecture de l'attribut d d'un chemin SVG
# ==============================================================================

_NOMBRE = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
_NB_ARGS = {'M': 2, 'L': 2, 'T': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'A': 7}
_SEPARATEURS = ' \t\r\n,'


class _Curseur(object):
    """Lit les nombres et les drapeaux de l'attribut d."""

    def __init__(self, texte):
        self.t = texte
        self.i = 0

    def passer(self):
        while self.i < len(self.t) and self.t[self.i] in _SEPARATEURS:
            self.i += 1

    def nombre_suit(self):
        self.passer()
        return self.i < len(self.t) and self.t[self.i] in '+-.0123456789'

    def nombre(self):
        self.passer()
        m = _NOMBRE.match(self.t, self.i)
        if not m:
            raise ValueError('nombre attendu en position %d' % self.i)
        self.i = m.end()
        return float(m.group(0))

    def drapeau(self):
        self.passer()
        c = self.t[self.i:self.i + 1]
        if c not in ('0', '1'):
            raise ValueError("drapeau d'arc invalide en position %d" % self.i)
        self.i += 1
        return c == '1'


def _lire_arguments(cur, cmd):
    if cmd == 'A':
        rx = cur.nombre()
        ry = cur.nombre()
        rot = cur.nombre()
        grand = cur.drapeau()
        balayage = cur.drapeau()
        ex = cur.nombre()
        ey = cur.nombre()
        return [rx, ry, rot, grand, balayage, ex, ey]
    return [cur.nombre() for _ in range(_NB_ARGS[cmd])]


def _points_arc(x1, y1, rx, ry, phi_deg, grand, balayage, x2, y2, pas=12):
    """Points echantillonnes d'un arc elliptique (conversion de la norme SVG, annexe F.6)."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if bool(grand) == bool(balayage):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2.0
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    t1 = math.atan2(uy, ux)
    dt = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    if not balayage and dt > 0:
        dt -= 2 * math.pi
    elif balayage and dt < 0:
        dt += 2 * math.pi
    pts = []
    for k in range(1, pas + 1):
        t = t1 + dt * k / pas
        pts.append((cx + rx * math.cos(t) * cp - ry * math.sin(t) * sp,
                    cy + rx * math.cos(t) * sp + ry * math.sin(t) * cp))
    return pts


class Contour(object):
    """Un sous-chemin (contour) d'un attribut d.

    brut         : texte source exact du contour (de sa lettre M/m a la suivante)
    depart       : (x, y) absolu du premier point
    relatif      : True si le contour commence par 'm' (coordonnees relatives)
    apres_couple : texte du contour situe apres son premier couple de coordonnees
    segments     : [(commande, [arguments absolus])]
    bbox         : (xmin, ymin, xmax, ymax), courbes echantillonnees
    """

    def __init__(self, debut, depart, relatif, fin_couple):
        self.debut = debut
        self.fin = None
        self.depart = depart
        self.relatif = relatif
        self.fin_couple = fin_couple
        self.points = [depart]
        self.segments = [('M', [depart[0], depart[1]])]
        self.bbox = None
        self.brut = None
        self.apres_couple = None


def lire_contours(d):
    """Decoupe un attribut d en contours."""
    cur = _Curseur(d)
    contours = []
    c = None
    px = py = 0.0
    sx = sy = 0.0
    cmd = None
    ctrl = None
    while True:
        cur.passer()
        if cur.i >= len(d):
            break
        pos = cur.i
        car = d[pos]
        if car.isalpha():
            cmd = car
            cur.i += 1
            if cmd in 'Zz':
                if c is None:
                    raise ValueError('Z sans sous-chemin')
                c.segments.append(('Z', []))
                px, py = sx, sy
                ctrl = None
                continue
            if cmd.upper() not in _NB_ARGS:
                raise ValueError('commande SVG non prise en charge : %r' % cmd)
            args = _lire_arguments(cur, cmd.upper())
        else:
            if cmd is None or cmd in 'Zz' or not cur.nombre_suit():
                raise ValueError('chemin SVG mal formé en position %d' % pos)
            args = _lire_arguments(cur, cmd.upper())
            # repetition implicite : apres M c'est L, apres m c'est l
            if cmd == 'M':
                cmd = 'L'
            elif cmd == 'm':
                cmd = 'l'
        up = cmd.upper()
        rel = cmd.islower()

        if up == 'M':
            x, y = (px + args[0], py + args[1]) if rel else (args[0], args[1])
            if c is not None:
                c.fin = pos
            c = Contour(pos, (x, y), rel, cur.i)
            contours.append(c)
            px, py = sx, sy = x, y
            ctrl = None
            continue

        if c is None:
            raise ValueError('chemin qui ne commence pas par M ou m')

        if up in ('L', 'T'):
            x, y = (px + args[0], py + args[1]) if rel else (args[0], args[1])
            c.points.append((x, y))
            c.segments.append(('L', [x, y]))
            px, py = x, y
            ctrl = None
        elif up == 'H':
            x = px + args[0] if rel else args[0]
            c.points.append((x, py))
            c.segments.append(('L', [x, py]))
            px = x
            ctrl = None
        elif up == 'V':
            y = py + args[0] if rel else args[0]
            c.points.append((px, y))
            c.segments.append(('L', [px, y]))
            py = y
            ctrl = None
        elif up in ('C', 'S', 'Q'):
            if up == 'C':
                if rel:
                    x1, y1 = px + args[0], py + args[1]
                    x2, y2 = px + args[2], py + args[3]
                    x, y = px + args[4], py + args[5]
                else:
                    x1, y1, x2, y2, x, y = args
            elif up == 'S':
                if ctrl is not None:
                    x1, y1 = 2 * px - ctrl[0], 2 * py - ctrl[1]
                else:
                    x1, y1 = px, py
                if rel:
                    x2, y2 = px + args[0], py + args[1]
                    x, y = px + args[2], py + args[3]
                else:
                    x2, y2, x, y = args
            else:  # Q : convertie en cubique pour l'echantillonnage
                if rel:
                    qx, qy = px + args[0], py + args[1]
                    x, y = px + args[2], py + args[3]
                else:
                    qx, qy, x, y = args
                x1, y1 = px + 2.0 / 3 * (qx - px), py + 2.0 / 3 * (qy - py)
                x2, y2 = x + 2.0 / 3 * (qx - x), y + 2.0 / 3 * (qy - y)
            for k in range(1, 9):
                t = k / 8.0
                mt = 1 - t
                c.points.append((
                    mt ** 3 * px + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t ** 3 * x,
                    mt ** 3 * py + 3 * mt * mt * t * y1 + 3 * mt * t * t * y2 + t ** 3 * y))
            c.segments.append(('C', [x1, y1, x2, y2, x, y]))
            ctrl = (x2, y2)
            px, py = x, y
        elif up == 'A':
            rx, ry, rot, grand, balayage, ex, ey = args
            if rel:
                ex, ey = px + ex, py + ey
            c.points.extend(_points_arc(px, py, rx, ry, rot, grand, balayage, ex, ey))
            c.segments.append(('A', [rx, ry, rot, grand, balayage, ex, ey]))
            px, py = ex, ey
            ctrl = None

    for c in contours:
        if c.fin is None:
            c.fin = len(d)
        xs = [p[0] for p in c.points]
        ys = [p[1] for p in c.points]
        c.bbox = (min(xs), min(ys), max(xs), max(ys))
        c.brut = d[c.debut:c.fin].rstrip(_SEPARATEURS)
        c.apres_couple = d[c.fin_couple:c.fin].rstrip(_SEPARATEURS)
    return contours


# ==============================================================================
# 2. Lecture du fichier SVG et structure de la partition
# ==============================================================================

_RE_PATH = re.compile(r'<path\b[^>]*/>', re.S)
_RE_D = re.compile(r'(\sd=")([^"]*)(")')
_RE_CLASSE = re.compile(r'\sclass="([^"]*)"')
_RE_AUTRE_DESSIN = re.compile(r'<(rect|circle|ellipse|line|polyline|polygon|text|use|image)\b')


class Element(object):
    """Un element <path> du SVG : sa balise d'origine, sa classe et ses contours."""

    def __init__(self, balise, classe, contours):
        self.balise = balise
        self.classe = classe
        self.contours = contours


class Partition(object):
    """Un fichier SVG de partition : elements, portees, systemes, etiquettes, clefs, instruments."""

    def __init__(self, texte):
        m_w = re.search(r'<svg[^>]*\swidth="([\d.]+)"', texte)
        m_h = re.search(r'<svg[^>]*\sheight="([\d.]+)"', texte)
        self.largeur_page = float(m_w.group(1)) if m_w else 2976.0
        self.hauteur_page = float(m_h.group(1)) if m_h else 4209.0
        m_r = re.search(r'<svg[^>]*>', texte)
        self.racine = m_r.group(0) if m_r else '<svg>'
        self.morceaux = []   # chaines (texte conserve tel quel) et objets Element, dans l'ordre
        pos = 0
        for m in _RE_PATH.finditer(texte):
            self.morceaux.append(texte[pos:m.start()])
            balise = m.group(0)
            cm = _RE_CLASSE.search(balise)
            dm = _RE_D.search(balise)
            if dm is None:
                raise ValueError('un élément <path> n\'a pas d\'attribut d')
            self.morceaux.append(Element(balise, cm.group(1) if cm else '', lire_contours(dm.group(2))))
            pos = m.end()
        self.morceaux.append(texte[pos:])
        self.elements = [m for m in self.morceaux if isinstance(m, Element)]
        self.avertissements = []
        if len(re.findall(r'<path\b', texte)) != len(self.elements):
            self.avertissements.append("certains <path> ont une forme non prise en charge : "
                                       "ils seront conservés tels quels dans chaque sortie")
        autres = sorted(set(_RE_AUTRE_DESSIN.findall(texte)))
        if autres:
            self.avertissements.append('éléments non filtrés (conservés tels quels) : ' + ', '.join(autres))
        if not self.elements:
            raise ValueError('aucun élément graphique trouvé : ce fichier ne semble pas être un SVG de partition')
        self.par_classe = {}
        for el in self.elements:
            self.par_classe.setdefault(el.classe, []).extend(el.contours)
        self._construire_portees()
        self._demi_trait_portee = self._lire_demi_trait()
        self._construire_systemes()
        self._construire_etiquettes()
        self._construire_clefs()
        self._construire_tetes()
        self._construire_hampes()
        self._construire_instruments()

    # -- utilitaires geometriques --------------------------------------------------

    @staticmethod
    def _boite(groupe):
        return (min(c.bbox[0] for c in groupe), min(c.bbox[1] for c in groupe),
                max(c.bbox[2] for c in groupe), max(c.bbox[3] for c in groupe))

    @staticmethod
    def _regrouper(contours, dx_max, dy_max):
        """Regroupe des contours proches (chevauchement vertical, faible ecart horizontal)."""
        n = len(contours)
        parent = list(range(n))

        def trouver(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            a = contours[i].bbox
            for j in range(i + 1, n):
                b = contours[j].bbox
                dx = max(a[0] - b[2], b[0] - a[2], 0.0)
                dy = max(a[1] - b[3], b[1] - a[3], 0.0)
                if dx <= dx_max and dy <= dy_max:
                    parent[trouver(i)] = trouver(j)
        groupes = {}
        for i in range(n):
            groupes.setdefault(trouver(i), []).append(contours[i])
        return list(groupes.values())

    def portee_la_plus_proche(self, y0, y1):
        """Index de la portee la plus proche de l'intervalle vertical [y0, y1], et sa distance."""
        yc = (y0 + y1) / 2.0
        meilleur = None
        for k, p in enumerate(self.portees):
            d = max(0.0, p['haut'] - y1, y0 - p['bas'])
            recouvrement = min(y1, p['bas']) - max(y0, p['haut'])
            cle = (d, -recouvrement, abs(yc - (p['haut'] + p['bas']) / 2.0))
            if meilleur is None or cle < meilleur[0]:
                meilleur = (cle, k)
        return meilleur[1], meilleur[0][0]

    def portee_de_marquage(self, y0, y1):
        """Portee d'une nuance, d'un hairpin ou d'un texte de portee. Ces marquages sont presque
        toujours places sous leur portee : on privilegie donc la portee situee au-dessus s'ils
        sont a moins de 6 espaces de sa ligne basse ; sinon la portee situee en dessous s'ils
        sont a moins de 2,5 espaces de sa ligne haute ; sinon la portee la plus proche."""
        k, d = self.portee_la_plus_proche(y0, y1)
        if d == 0:
            return k
        au_dessus = [j for j, p in enumerate(self.portees) if p['bas'] <= y0]
        en_dessous = [j for j, p in enumerate(self.portees) if p['haut'] >= y1]
        if au_dessus and y0 - self.portees[au_dessus[-1]]['bas'] <= 6.0 * self.espace:
            return au_dessus[-1]
        if en_dessous and self.portees[en_dessous[0]]['haut'] - y1 <= 2.5 * self.espace:
            return en_dessous[0]
        return k

    def systeme_le_plus_proche(self, y0, y1):
        yc = (y0 + y1) / 2.0
        meilleur = None
        for s, (haut, bas) in enumerate(self.bandes_systemes):
            d = max(0.0, haut - y1, y0 - bas)
            cle = (d, abs(yc - (haut + bas) / 2.0))
            if meilleur is None or cle < meilleur[0]:
                meilleur = (cle, s)
        return meilleur[1]

    # -- portees et systemes -------------------------------------------------------

    def _lire_demi_trait(self):
        """Demi-epaisseur du trait des lignes de portee : une barre de mesure va du bord
        exterieur de la ligne haute au bord exterieur de la ligne basse."""
        for el in self.elements:
            if el.classe == 'StaffLines':
                m = re.search(r'stroke-width="([\d.]+)"', el.balise)
                if m:
                    return float(m.group(1)) / 2.0
        return 0.04 * self.espace

    def _construire_portees(self):
        lignes = []
        for c in self.par_classe.get('StaffLines', []):
            x0, y0, x1, y1 = c.bbox
            lignes.append((round((y0 + y1) / 2.0, 3), round(x0, 2), round(x1, 2)))
        lignes.sort()
        ecarts = [b[0] - a[0] for a, b in zip(lignes, lignes[1:]) if b[0] - a[0] > 0.5]
        if not ecarts:
            raise ValueError('aucune ligne de portée trouvée : ce fichier ne semble pas être une partition')
        self.espace = min(ecarts)    # espacement entre deux lignes d'une meme portee
        groupes = []
        for ligne in lignes:
            if groupes:
                y_prec, xg0, xg1 = groupes[-1][-1]
                if (ligne[0] - y_prec) <= 1.5 * self.espace and \
                        abs(ligne[1] - xg0) < 0.5 and abs(ligne[2] - xg1) < 0.5:
                    groupes[-1].append(ligne)
                    continue
            groupes.append([ligne])
        self.portees = []
        for k, g in enumerate(groupes):
            self.portees.append({'index': k, 'haut': g[0][0], 'bas': g[-1][0],
                                 'nb_lignes': len(g), 'systeme': None,
                                 'etiquette': None, 'clef': None, 'clef_x': float('inf'),
                                 'cle': None})

    def _construire_systemes(self):
        self.systemes = []           # pour chaque systeme : liste des index de portees
        for k, p in enumerate(self.portees):
            if k == 0 or p['haut'] - self.portees[k - 1]['bas'] > ECART_MAX_DANS_SYSTEME * self.espace:
                self.systemes.append([k])
            else:
                self.systemes[-1].append(k)
        for s, indices in enumerate(self.systemes):
            for k in indices:
                self.portees[k]['systeme'] = s
        self.bandes_systemes = []
        for indices in self.systemes:
            haut = self.portees[indices[0]]['haut'] - MARGE_SYSTEME * self.espace
            bas = self.portees[indices[-1]]['bas'] + MARGE_SYSTEME * self.espace
            self.bandes_systemes.append((haut, bas))

    # -- etiquettes d'instrument ---------------------------------------------------

    def _plage_portees(self, yc, systeme):
        """Plage (i, j) de portees du systeme dont le centre est le plus proche de yc (None si
        aucune n'est centree). A centre egal, la plage la plus courte gagne : une portee
        centrale a le meme centre que la plage de trois portees qui l'entoure."""
        indices = self.systemes[systeme]
        meilleur = None
        for a in range(len(indices)):
            for b in range(a, min(a + 4, len(indices))):
                centre = (self.portees[indices[a]]['haut'] + self.portees[indices[b]]['bas']) / 2.0
                d = abs(centre - yc)
                cle = (d > TOLERANCE_CENTRAGE * self.espace, b - a, d)
                if meilleur is None or cle < meilleur[0]:
                    meilleur = (cle, indices[a], indices[b])
        if meilleur[0][0]:
            return None
        return meilleur[1], meilleur[2]

    def _construire_etiquettes(self):
        """Une etiquette (classe InstrumentName) est un mot centre sur une portee ou sur un groupe.
        Deux etiquettes de meme forme sont le meme nom (ex. 'Vln.' dans deux systemes)."""
        self.etiquettes = []
        self.etiquette_de_contour = {}
        self.prototypes = []
        for groupe in self._regrouper(self.par_classe.get('InstrumentName', []), 30.0, 3.0):
            x0, y0, x1, y1 = self._boite(groupe)
            s = self.systeme_le_plus_proche(y0, y1)
            plage = self._plage_portees((y0 + y1) / 2.0, s)
            forme = sorted((round(c.bbox[0] - x0, 4), round(c.bbox[1] - y0, 4),
                            round(c.bbox[2] - c.bbox[0], 4), round(c.bbox[3] - c.bbox[1], 4))
                           for c in groupe)
            idx = len(self.etiquettes)
            proto = self._prototype_de(forme)
            self.etiquettes.append({'systeme': s, 'plage': plage, 'proto': proto})
            for c in groupe:
                self.etiquette_de_contour[id(c)] = idx
            if plage is not None:
                i, j = plage
                for k in range(i, j + 1):
                    self.portees[k]['etiquette'] = (proto, j - i + 1, i)

    def _prototype_de(self, forme):
        for k, ref in enumerate(self.prototypes):
            if _meme_forme(forme, ref):
                return k
        self.prototypes.append(forme)
        return len(self.prototypes) - 1

    # -- clefs ---------------------------------------------------------------------

    def _construire_clefs(self):
        """Clef initiale de chaque portee : la clef la plus a gauche de la portee (les changements
        de clef en cours de ligne sont plus a droite). Sa forme (hauteur) donne son nom."""
        for groupe in self._regrouper(self.par_classe.get('Clef', []), 6.0, 3.0):
            x0, y0, x1, y1 = self._boite(groupe)
            hauteur = (y1 - y0) / self.espace
            if hauteur < HAUTEUR_MIN_CLEF:
                continue
            k, d = self.portee_la_plus_proche(y0, y1)
            if d > 2 * self.espace:
                continue
            if hauteur >= HAUTEUR_CLEF_SOL:
                nom = 'G'
            elif hauteur >= HAUTEUR_CLEF_UT:
                nom = 'C'
            elif hauteur >= HAUTEUR_CLEF_FA:
                nom = 'F'
            else:
                continue
            p = self.portees[k]
            if p['clef'] is None or x0 < p['clef_x']:
                p['clef'] = nom
                p['clef_x'] = x0

    def _construire_tetes(self):
        """Têtes de notes situées dans l'espace entre deux portées. Leur portée se lit dans le sens de la
        hampe, selon la convention de notation : une tête sous la portée du haut a sa hampe vers le haut
        et appartient à cette portée ; une tête au-dessus de la portée du bas a sa hampe vers le bas et
        appartient à celle-ci. Les signes et points qui accompagnent la tête la suivent."""
        self._portee_de_tete = {}
        self._portee_fixee = {}
        sp = self.espace
        notes = self.par_classe.get('Note', [])
        hampes = [c for c in self.par_classe.get('Stem', [])
                  if c.bbox[2] - c.bbox[0] <= 1.0 and c.bbox[3] - c.bbox[1] >= 1.0]
        for n in notes:
            x0, y0, x1, y1 = n.bbox
            hc = (y0 + y1) / 2.0
            au_dessus = [k for k, p in enumerate(self.portees) if p['bas'] <= y0]
            en_dessous = [k for k, p in enumerate(self.portees) if p['haut'] >= y1]
            if not au_dessus or not en_dessous:
                continue
            a, b = au_dessus[-1], en_dessous[0]
            if self.portees[a]['systeme'] != self.portees[b]['systeme']:
                continue                       # espace entre deux systèmes : attribution par proximité
            if not (y0 > self.portees[a]['bas'] and y1 < self.portees[b]['haut']):
                continue                       # tête dans une portée : attribution par proximité
            if any(p['haut'] < y1 and p['bas'] > y0 for p in self.portees):
                continue                       # la tête chevauche une portée : elle en fait partie
            sens = None
            for h in hampes:
                hx0, hy0, hx1, hy1 = h.bbox
                if not (x0 - 4.0 <= hx0 <= x1 + 4.0) or hy1 < y0 - 3.0 or hy0 > y1 + 3.0:
                    continue
                sens = 'bas' if (hy1 - hc) > (hc - hy0) else 'haut'
                break
            # 1. les lignes supplémentaires relient la tête à sa portée : si elles sont d'un seul côté, on suit
            ledger = self.par_classe.get('LedgerLine', [])
            def lignes_vers(k):
                # lignes entre la tête et la portée k : sous la portée a, au-dessus de la portée b
                nb = 0
                for L in ledger:
                    lx0, ly0, lx1, ly1 = L.bbox
                    if lx1 < x0 - 2.0 or lx0 > x1 + 2.0:
                        continue
                    yl = (ly0 + ly1) / 2.0
                    if k == b and y1 < yl < self.portees[b]['haut']:
                        nb += 1
                    if k == a and self.portees[a]['bas'] < yl < y0:
                        nb += 1
                return nb
            ca, cb = lignes_vers(a), lignes_vers(b)
            if ca != cb:
                self._portee_de_tete[id(n)] = a if ca > cb else b
                self._portee_fixee[id(n)] = self._portee_de_tete[id(n)]
                continue
            # 2. sinon, le sens de la hampe (convention : vers le haut sous la portée du haut, vers le bas au-dessus de celle du bas)
            if sens is None:
                continue                       # sans hampe (ronde) : attribution par proximité
            self._portee_de_tete[id(n)] = b if sens == 'bas' else a
            self._portee_fixee[id(n)] = self._portee_de_tete[id(n)]
        # signes et points rattachés à une tête reportée : même portée que la tête
        for classe in ('Accidental', 'NoteDot', 'Articulation', 'LedgerLine'):
            for c in self.par_classe.get(classe, []):
                cx = (c.bbox[0] + c.bbox[2]) / 2.0
                cy = (c.bbox[1] + c.bbox[3]) / 2.0
                meilleur = None
                for n in notes:
                    if id(n) not in self._portee_de_tete:
                        continue
                    nx0, ny0, nx1, ny1 = n.bbox
                    if not (nx0 - 8.0 <= cx <= nx1 + 8.0 and ny0 - 1.5 * sp <= cy <= ny1 + 1.5 * sp):
                        continue
                    d = abs(cy - (ny0 + ny1) / 2.0)
                    if meilleur is None or d < meilleur[0]:
                        meilleur = (d, n)
                if meilleur is not None:
                    self._portee_fixee[id(c)] = self._portee_de_tete[id(meilleur[1])]

    def _construire_hampes(self):
        """Rattache chaque hampe verticale a la portee de la tete de note qu'elle touche par une
        de ses extremites. Une hampe peut descendre loin dans la portee voisine : sa position
        verticale seule ne suffit donc pas. Les hampes sans tete (reliees a une poutre) restent
        rattachees a la portee la plus proche."""
        self._portee_de_hampe = {}
        notes = self.par_classe.get('Note', [])
        for c in self.par_classe.get('Stem', []):
            x0, y0, x1, y1 = c.bbox
            if x1 - x0 > 1.0 or y1 - y0 < 1.0:
                continue
            meilleur = None
            for n in notes:
                nx0, ny0, nx1, ny1 = n.bbox
                if not (nx0 - 3.0 <= x0 <= nx1 + 3.0):
                    continue
                centre = (ny0 + ny1) / 2.0
                for ye in (y0, y1):
                    if ny0 - 2.0 <= ye <= ny1 + 2.0:
                        d = abs(ye - centre)
                        if meilleur is None or d < meilleur[0]:
                            meilleur = (d, n)
            if meilleur is not None:
                n = meilleur[1]
                if id(n) in self._portee_de_tete:
                    k = self._portee_de_tete[id(n)]
                else:
                    k, _ = self.portee_la_plus_proche(n.bbox[1], n.bbox[3])
                self._portee_de_hampe[id(c)] = k

    # -- instruments ---------------------------------------------------------------

    def _proto_portee(self, k):
        e = self.portees[k]['etiquette']
        return None if e is None else e[0]

    def _construire_instruments(self):
        # 1. une etiquette (qui peut couvrir plusieurs portees, ex. le piano) donne une cle : son
        #    nom, le nombre de portees couvertes et la clef de sa premiere portee. Deux etiquettes
        #    identiques dans un meme systeme (ex. deux 'Synth.' en clef de sol) recoivent un numero.
        for s, indices in enumerate(self.systemes):
            groupes = []
            for k in indices:
                e = self.portees[k]['etiquette']
                if e is not None and e[2] == k:
                    groupes.append((k, e[0], e[1]))
            base = {}
            for k, proto, taille in groupes:
                base[k] = 'etiquette%d:%d:%s' % (proto, taille, self.portees[k]['clef'] or '?')
            compte = {}
            for k in base:
                compte[base[k]] = compte.get(base[k], 0) + 1
            vu = {}
            for k, proto, taille in groupes:
                b = base[k]
                vu[b] = vu.get(b, 0) + 1
                cle = b if compte[b] == 1 else '%s#%d' % (b, vu[b])
                for m in range(k, k + taille):
                    self.portees[m]['cle'] = cle
        # 2. systeme de reference : celui qui a le plus de portees (le premier en cas d'egalite)
        ref = max(range(len(self.systemes)), key=lambda s: (len(self.systemes[s]), -s))
        self.systeme_reference = ref
        indices_ref = self.systemes[ref]
        cles_ref = []
        for k in indices_ref:
            if self.portees[k]['cle'] is None:
                self.portees[k]['cle'] = 'inconnu:%d' % k
            cles_ref.append(self.portees[k]['cle'])
        # 3. autres systemes : alignement de leurs portees sur celles du systeme de reference
        refl = [(self._proto_portee(k), self.portees[k]['clef']) for k in indices_ref]
        for s, indices in enumerate(self.systemes):
            if s == ref:
                continue
            cur = [(self._proto_portee(k), self.portees[k]['clef']) for k in indices]
            aligne = dict(_aligner(cur, refl))
            for i, k in enumerate(indices):
                if i in aligne:
                    self.portees[k]['cle'] = cles_ref[aligne[i]]
                elif self.portees[k]['cle'] is None:
                    self.portees[k]['cle'] = 'inconnu:%d' % k
        # 4. liste des instruments : ordre du systeme de reference, puis les nouveaux
        ordre = []
        occurrences = {}
        for k in indices_ref + [k for indices in self.systemes for k in indices]:
            cle = self.portees[k]['cle']
            if cle not in occurrences:
                occurrences[cle] = []
                ordre.append(cle)
            if k not in occurrences[cle]:
                occurrences[cle].append(k)
        self.instruments = []
        for n, cle in enumerate(ordre):
            occ = sorted(occurrences[cle])
            self.instruments.append({
                'numero': n + 1, 'cle': cle, 'portees': occ,
                'systemes': sorted(set(self.portees[k]['systeme'] for k in occ))})

    def _systeme_de_contour(self, classe, c):
        """Système auquel appartient un contour. None pour le titre et le pied de page, qui sont hors
        des systèmes. Un contour rattaché à une portée (tête reportée, hampe, signe) suit cette portée."""
        for rattachement in (self._portee_fixee, self._portee_de_hampe):
            if id(c) in rattachement:
                return self.portees[rattachement[id(c)]]['systeme']
        x0, y0, x1, y1 = c.bbox
        if classe in ('Text', 'Page') and all(not (haut <= y1 and y0 <= bas) for haut, bas in self.bandes_systemes):
            return None
        return self.systeme_le_plus_proche(y0, y1)

    def blocs(self, cles):
        """Un bloc par système contenant une portée gardée : le texte SVG des éléments de ce système
        (portées, notes, étiquettes, barres, numéros de mesure, tempo) et sa hauteur (y0, y1)."""
        gardees, systemes_gardes = self._portees_gardees(cles)
        self._barres = self._preparer_barres(gardees)
        rendus = []
        for morceau in self.morceaux:
            if isinstance(morceau, Element):
                rendus.append((morceau, [self._contour_a_garder(morceau.classe, c, gardees, systemes_gardes)
                                         for c in morceau.contours]))
        blocs = []
        for s in sorted(systemes_gardes):
            sorties, bords = [], []
            for morceau, rs in rendus:
                inclus = [r is not None and self._systeme_de_contour(morceau.classe, c) == s
                          for c, r in zip(morceau.contours, rs)]
                parties = _assembler(morceau, inclus,
                                     lambda i, m=morceau, r=rs: _texte_rendu(m.contours[i], r[i]))
                if parties:
                    sorties.append(_balise_avec_d(morceau.balise, parties))
                for c, r, dedans in zip(morceau.contours, rs, inclus):
                    if not dedans:
                        continue
                    if r == 'tel-quel':
                        bords.append(c.bbox)
                    else:   # barre rognée : l'emprise est celle de la portion conservée
                        bords.extend(x.bbox for x in lire_contours(r))
            if sorties:
                blocs.append({'systeme': s, 'svg': ''.join(sorties),
                              'y0': min(b[1] for b in bords), 'y1': max(b[3] for b in bords)})
        return blocs

    def elements_communs(self):
        """Titre (au-dessus des systèmes) et pied de page (nom du compositeur, centré) de la page.
        Les numéros de page ne sont pas repris : la partie solo les recrée."""
        largeur = self.largeur_page
        entete, pied, bords_e, bords_p = [], [], [], []
        for morceau in self.morceaux:
            if not isinstance(morceau, Element):
                continue
            inc_e, inc_p = [], []
            for c in morceau.contours:
                x0, y0, x1, y1 = c.bbox
                hors = self._systeme_de_contour(morceau.classe, c) is None
                e = morceau.classe == 'Text' and hors
                p = morceau.classe == 'Page' and hors and 0.3 * largeur <= (x0 + x1) / 2.0 <= 0.7 * largeur
                inc_e.append(e)
                inc_p.append(p)
                if e:
                    bords_e.append(y1)
                if p:
                    bords_p.append((y0, y1))
            if any(inc_e):
                entete.append(_balise_avec_d(morceau.balise, _assembler(
                    morceau, inc_e, lambda i, m=morceau: (m.contours[i].brut, True))))
            if any(inc_p):
                pied.append(_balise_avec_d(morceau.balise, _assembler(
                    morceau, inc_p, lambda i, m=morceau: (m.contours[i].brut, True))))
        return {'entete': ''.join(entete), 'entete_bas': max(bords_e) if bords_e else 0.0,
                'pied': ''.join(pied),
                'pied_haut': min(b[0] for b in bords_p) if bords_p else 0.0,
                'pied_bas': max(b[1] for b in bords_p) if bords_p else 0.0}

    def identites(self):
        """Identités d'instrument de la page : clé, prototypes des étiquettes, clef et taille du groupe."""
        vues = {}
        for k, p in enumerate(self.portees):
            if p['cle'] is None:
                continue
            d = vues.get(p['cle'])
            if d is None:
                d = vues[p['cle']] = {'cle': p['cle'], 'protos': set(), 'premiere': k,
                                      'clef': p['clef'] or '?', 'taille': 1}
            if p['etiquette'] is not None:
                d['protos'].add(p['etiquette'][0])
                d['taille'] = p['etiquette'][1]
        return sorted(vues.values(), key=lambda d: d['premiere'])

    def decrire(self, instrument):
        """Texte descriptif d'un instrument, pour le menu."""
        ref = self.systeme_reference
        occ = instrument['portees']
        dans_ref = [k for k in occ if self.portees[k]['systeme'] == ref]
        clefs = []
        for k in occ:
            c = self.portees[k]['clef'] or '?'
            if c not in clefs:
                clefs.append(c)
        if len(clefs) == 1:
            txt_clef = "clé d'ut" if clefs[0] == 'C' else 'clé de %s' % NOM_CLEF[clefs[0]]
        else:
            txt_clef = 'clés de ' + ' et de '.join(NOM_CLEF[c] for c in clefs)
        if dans_ref:
            debut = self.systemes[ref][0]
            a = dans_ref[0] - debut + 1
            b = dans_ref[-1] - debut + 1
            pos = 'portée %d' % a if a == b else 'portées %d à %d' % (a, b)
        else:
            k = occ[0]
            s = self.portees[k]['systeme']
            pos = 'portée %d du système %d' % (k - self.systemes[s][0] + 1, s + 1)
        nb = len(instrument['systemes'])
        presence = 'présent dans %d système%s sur %d' % (nb, '' if nb == 1 else 's', len(self.systemes))
        if any(self.portees[k]['etiquette'] for k in occ):
            return '%s, %s, %s' % (pos, txt_clef, presence)
        return '%s, %s, sans étiquette, %s' % (pos, txt_clef, presence)

    # -- ecriture ------------------------------------------------------------------

    def _portees_gardees(self, cles):
        """Indices des portées à garder, pour une clé d'instrument ou une liste de clés."""
        if isinstance(cles, str):
            cles = [cles]
        choisies = set(cles)
        gardees = set(k for k, p in enumerate(self.portees) if p['cle'] in choisies)
        if not gardees:
            raise ValueError('instrument inconnu')
        return gardees, set(self.portees[k]['systeme'] for k in gardees)

    def ecrire(self, cles):
        """Texte SVG ne contenant que les instruments demandés (une clé ou une liste de clés)."""
        gardees, systemes_gardes = self._portees_gardees(cles)
        self._barres = self._preparer_barres(gardees)
        sortie = []
        for morceau in self.morceaux:
            if not isinstance(morceau, Element):
                sortie.append(morceau)
                continue
            rendus = [self._contour_a_garder(morceau.classe, c, gardees, systemes_gardes)
                      for c in morceau.contours]
            parties = _assembler(morceau, [r is not None for r in rendus],
                                 lambda i: _texte_rendu(morceau.contours[i], rendus[i]))
            if parties:
                sortie.append(_balise_avec_d(morceau.balise, parties))
        return ''.join(sortie)

    def apercu(self, cles):
        """Texte SVG de la page entière : les éléments des instruments demandés sont en rouge,
        le reste est en noir. Sert à reconnaître les instruments dans l'interface."""
        gardees, systemes_gardes = self._portees_gardees(cles)
        self._barres = self._preparer_barres(gardees)
        sortie = []
        for morceau in self.morceaux:
            if not isinstance(morceau, Element):
                sortie.append(morceau)
                continue
            # titre, pied de page, numéros de mesure et tempo sont communs : jamais en rouge
            if morceau.classe in CLASSES_PARTAGEES:
                rendus = [None] * len(morceau.contours)
            else:
                rendus = [self._contour_a_garder(morceau.classe, c, gardees, systemes_gardes)
                          for c in morceau.contours]
            # en noir : ce qui n'est pas gardé ; les barres de mesure restent entières
            if morceau.classe == 'BarLine':
                noirs = [True] * len(rendus)
            else:
                noirs = [r is None for r in rendus]
            parties_noires = _assembler(morceau, noirs, lambda i: (morceau.contours[i].brut, True))
            if parties_noires:
                sortie.append(_balise_avec_d(morceau.balise, parties_noires))
            # en rouge : ce qui est gardé, par-dessus
            parties_rouges = _assembler(morceau, [r is not None for r in rendus],
                                        lambda i: _texte_rendu(morceau.contours[i], rendus[i]))
            if parties_rouges:
                sortie.append(_balise_avec_d(_balise_couleur(morceau.balise, COULEUR_APERCU),
                                             parties_rouges))
        return ''.join(sortie)

    def _contour_a_garder(self, classe, c, gardees, systemes_gardes):
        """'tel-quel' si le contour est garde sans changement, un texte SVG si on le reecrit
        (barre de mesure rognee), None s'il est supprime."""
        x0, y0, x1, y1 = c.bbox
        if id(c) in self._portee_fixee:
            return 'tel-quel' if self._portee_fixee[id(c)] in gardees else None
        if classe == 'StaffLines':
            k, _ = self.portee_la_plus_proche(y0, y1)
            return 'tel-quel' if k in gardees else None
        if classe == 'BarLine':
            return self._barres.get(id(c))
        if classe == 'Bracket':
            # une accolade ne sert que si toutes les portees qu'elle relie sont gardees
            relies = [k for k, p in enumerate(self.portees) if p['haut'] >= y0 - 0.5 and p['bas'] <= y1 + 0.5]
            if relies:
                return 'tel-quel' if all(k in gardees for k in relies) else None
            k, _ = self.portee_la_plus_proche(y0, y1)
            return 'tel-quel' if k in gardees else None
        if classe == 'InstrumentName' and id(c) in self.etiquette_de_contour:
            plage = self.etiquettes[self.etiquette_de_contour[id(c)]]['plage']
            if plage is None:
                return None
            return 'tel-quel' if plage[0] in gardees else None
        if classe in ('MeasureNumber', 'Tempo'):
            s = self.systeme_le_plus_proche(y0, y1)
            return 'tel-quel' if s in systemes_gardes else None
        if classe == 'Stem' and id(c) in self._portee_de_hampe:
            return 'tel-quel' if self._portee_de_hampe[id(c)] in gardees else None
        if classe in ('Dynamic', 'HairpinSegment', 'StaffText'):
            return 'tel-quel' if self.portee_de_marquage(y0, y1) in gardees else None
        if classe in ('Text', 'Page') and \
                all(not (haut <= y1 and y0 <= bas) for haut, bas in self.bandes_systemes):
            return 'tel-quel'          # titre, mentions, pied de page, numero de page : hors systemes
        k, _ = self.portee_la_plus_proche(y0, y1)   # tout le reste suit la portee la plus proche
        return 'tel-quel' if k in gardees else None

    def _preparer_barres(self, gardees):
        """Rendu de chaque barre de mesure verticale pour l'instrument choisi.

        Une barre de mesure peut etre formee de plusieurs contours (un par morceau), tous de meme
        abscisse dans un meme systeme. On les traite ensemble : on calcule la plage couverte par la
        barre, puis on la rogne aux portees gardees. Ainsi la barre reste continue entre les deux
        portees d'un piano. Renvoie {id(contour): 'tel-quel' | texte SVG | None}."""
        rendus = {}
        groupes = {}
        for c in self.par_classe.get('BarLine', []):
            segs = [s for s in c.segments if s[0] in ('M', 'L')]
            if len(segs) != 2 or abs(segs[0][1][0] - segs[1][1][0]) > 1e-6:
                # forme inattendue : on garde le contour s'il touche une portee gardee
                y0, y1 = c.bbox[1], c.bbox[3]
                touche = any(min(y1, p['bas']) > max(y0, p['haut'])
                             for k, p in enumerate(self.portees) if k in gardees)
                rendus[id(c)] = 'tel-quel' if touche else None
                continue
            x = segs[0][1][0]
            ya, yb = sorted([segs[0][1][1], segs[1][1][1]])
            s = self.systeme_le_plus_proche(ya, yb)
            groupes.setdefault((round(x, 3), s), []).append((c, ya, yb))
        for (x, s), membres in groupes.items():
            gardees_sys = [k for k in self.systemes[s] if k in gardees]
            couverture = _fusionner([(ya, yb) for _, ya, yb in membres])
            if not gardees_sys:
                for c, _, _ in membres:
                    rendus[id(c)] = None
                continue
            m = self._demi_trait_portee
            plage = (self.portees[gardees_sys[0]]['haut'] - m, self.portees[gardees_sys[-1]]['bas'] + m)
            morceaux = []
            for a, b in couverture:
                a2, b2 = max(a, plage[0]), min(b, plage[1])
                if b2 > a2 + 1e-9:
                    morceaux.append((a2, b2))
            inchange = len(morceaux) == len(couverture) and all(
                abs(p[0] - q[0]) < 0.05 and abs(p[1] - q[1]) < 0.05 for p, q in zip(morceaux, couverture))
            if inchange:
                for c, _, _ in membres:
                    rendus[id(c)] = 'tel-quel'
                continue
            texte = ''.join('M%s %sV%s' % (_fmt(x), _fmt(a), _fmt(b)) for a, b in morceaux)
            for i, (c, _, _) in enumerate(membres):
                rendus[id(c)] = texte if (i == 0 and texte) else None
        return rendus


def _fusionner(intervalles):
    """Reunit des intervalles [a, b] qui se recouvrent ou se touchent."""
    fusion = []
    for a, b in sorted(intervalles):
        if fusion and a <= fusion[-1][1] + 1e-6:
            fusion[-1][1] = max(fusion[-1][1], b)
        else:
            fusion.append([a, b])
    return [(a, b) for a, b in fusion]


def _texte_rendu(c, rendu):
    """(texte, brut) à écrire pour un contour retenu : brut s'il est repris sans changement."""
    if rendu == 'tel-quel':
        return c.brut, True
    return rendu, False


def _assembler(morceau, inclus, texte_de):
    """Concatène les contours retenus d'un élément. texte_de(i) renvoie (texte, brut).
    Un contour relatif dont le contour précédent n'est pas repris tel quel repart d'un point absolu."""
    parties = []
    precedent_brut = True
    for idx, c in enumerate(morceau.contours):
        if not inclus[idx]:
            precedent_brut = False
            continue
        texte, brut = texte_de(idx)
        if brut and c.relatif and idx > 0 and not precedent_brut:
            texte = _debut_absolu(c)
        parties.append(texte)
        precedent_brut = brut
    return parties


def _balise_avec_d(balise, parties):
    """Balise <path> d'origine dont l'attribut d est remplacé par les parties données."""
    return _RE_D.sub(lambda m: m.group(1) + ''.join(parties) + m.group(3), balise, count=1)


def _balise_couleur(balise, couleur):
    """Balise <path> peinte dans la couleur donnée (trait, ou remplissage pour les glyphes)."""
    if 'fill="none"' in balise:
        style = 'fill:none;stroke:%s' % couleur
    elif 'stroke=' in balise:
        style = 'fill:%s;stroke:%s' % (couleur, couleur)
    else:
        style = 'fill:%s;stroke:none' % couleur
    balise = re.sub(r'\sstyle="[^"]*"', '', balise, count=1)
    return balise.replace('<path ', '<path style="%s" ' % style, 1)


def _debut_absolu(c):
    """Contour relatif dont le contour precedent a ete supprime : on repart d'un point absolu."""
    reste = c.apres_couple.lstrip(_SEPARATEURS)
    if reste and reste[0] in '+-.0123456789':
        reste = 'l' + reste          # apres 'm', les paires implicites sont des 'l' relatifs
    return 'M%s %s%s' % (_fmt(c.depart[0]), _fmt(c.depart[1]), reste)


def _fmt(v):
    s = ('%.4f' % v).rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def _meme_forme(a, b):
    """Deux etiquettes ont la meme forme si chacun de leurs morceaux a un correspondant
    dans l'autre, a la tolerance pres."""
    if len(a) != len(b):
        return False
    libre = [True] * len(b)
    for (ax, ay, aw, ah) in a:
        meilleur = None
        for j, (bx, by, bw, bh) in enumerate(b):
            if not libre[j]:
                continue
            d = max(abs(ax - bx), abs(ay - by), abs(aw - bw), abs(ah - bh))
            if d <= TOLERANCE_FORME and (meilleur is None or d < meilleur[0]):
                meilleur = (d, j)
        if meilleur is None:
            return False
        libre[meilleur[1]] = False
    return True


def _aligner(cur, ref):
    """Alignement global (Needleman-Wunsch) de deux suites de portees.

    cur, ref : listes de (prototype d'etiquette ou None, clef ou None).
    Renvoie les paires (i, j) d'indices alignes, i dans cur, j dans ref.
    Une portee sans etiquette s'aligne sur une portee de meme clef ; deux portees
    etiquetees s'alignent si leurs etiquettes sont identiques, ou si elles ont la meme
    clef (le nom abrege d'un systeme peut differer du nom complet du premier)."""
    NEG = -1e9
    GAP = -1.0

    def score(a, b):
        (pa, ca), (pb, cb) = a, b
        clef_ok = ca is None or cb is None or ca == cb
        if pa is not None and pb is not None:
            if pa == pb:
                return 10.0 if clef_ok else 4.0
            return 1.0 if clef_ok else NEG
        return 4.0 if clef_ok else NEG

    n, m = len(cur), len(ref)
    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    T = [[0] * (m + 1) for _ in range(n + 1)]   # 0 : diagonale, 1 : saut dans cur, 2 : saut dans ref
    for i in range(1, n + 1):
        S[i][0] = S[i - 1][0] + GAP
        T[i][0] = 1
    for j in range(1, m + 1):
        S[0][j] = S[0][j - 1] + GAP
        T[0][j] = 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            opts = [(S[i - 1][j - 1] + score(cur[i - 1], ref[j - 1]), 0),
                    (S[i - 1][j] + GAP, 1),
                    (S[i][j - 1] + GAP, 2)]
            S[i][j], T[i][j] = max(opts, key=lambda o: o[0])
    paires = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            t = T[i][j]
        else:
            t = 1 if i > 0 else 2
        if t == 0:
            if score(cur[i - 1], ref[j - 1]) > NEG / 2:
                paires.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif t == 1:
            i -= 1
        else:
            j -= 1
    paires.reverse()
    return paires


# ==============================================================================
# 2 bis. Partie solo : plusieurs pages, un instrument
# ==============================================================================

DEBUT_PAGE_SUITE = 260.0      # haut de la zone utile des pages suivantes (unités SVG)


def _cle_naturelle(chemin):
    """Clé de tri des noms de fichiers : 2.svg avant 10.svg."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', os.path.basename(chemin))]


def _description_globale(g, nombre_pages):
    if g['clef'] == 'C':
        txt_clef = "clé d'ut"
    else:
        txt_clef = 'clé de %s' % NOM_CLEF.get(g['clef'], 'inconnue')
    if g['taille'] > 1:
        txt_clef += ' (groupe de %d portées)' % g['taille']
    nb = len(g['cles'])
    presence = 'présent dans %d page%s sur %d' % (nb, '' if nb == 1 else 's', nombre_pages)
    if g['etiquette']:
        return '%s, %s' % (txt_clef, presence)
    return '%s, sans étiquette, %s' % (txt_clef, presence)


def instruments_multi(parts):
    """Instruments communs aux pages. Une même étiquette (même forme), avec la même clef et la même
    taille de groupe, désigne le même instrument d'une page à l'autre. Une page ne compte qu'une fois
    par instrument. Renvoie une liste de dicts : numero, description, cles {indice de page: clé}."""
    formes = []

    def canonique(forme):
        for i, f in enumerate(formes):
            if _meme_forme(forme, f):
                return i
        formes.append(forme)
        return len(formes) - 1

    globaux = []
    for fi, part in enumerate(parts):
        for ident in part.identites():
            ids = set(canonique(part.prototypes[p]) for p in ident['protos'])
            cible = None
            if ids:
                for g in globaux:
                    if (fi not in g['cles'] and g['clef'] == ident['clef'] and g['taille'] == ident['taille']
                            and g['formes'] & ids):
                        cible = g
                        break
            if cible is None:
                cible = {'clef': ident['clef'], 'taille': ident['taille'], 'formes': set(),
                         'cles': {}, 'etiquette': bool(ids)}
                globaux.append(cible)
            cible['formes'] |= ids
            cible['cles'][fi] = ident['cle']
    resultat = []
    for n, g in enumerate(globaux, start=1):
        resultat.append({'numero': n, 'clef': g['clef'], 'taille': g['taille'], 'cles': g['cles'],
                         'etiquette': g['etiquette'], 'description': _description_globale(g, len(parts))})
    return resultat


def disposer_blocs(blocs, debut_1, debut, fin, ecart):
    """Place les blocs (systèmes) dans l'ordre, le plus serré possible, sans chevauchement.
    Renvoie une liste de pages ; chaque page est une liste de (bloc, décalage vertical)."""
    pages, curseur = [], 0.0
    for bloc in blocs:
        hauteur = bloc['y1'] - bloc['y0']
        if not pages or curseur + hauteur > fin:
            pages.append([])
            curseur = debut_1 if len(pages) == 1 else debut
            if curseur + hauteur > fin:
                raise ValueError("un système est plus haut que la zone utile d'une page")
        pages[-1].append((bloc, curseur - bloc['y0']))
        curseur += hauteur + ecart
    return pages


def _numero_page(n, ref, communs):
    """Numéro de page, placé comme dans l'original : à gauche sur les pages paires, à droite sur les impaires."""
    y = communs['pied_bas'] if communs['pied'] else ref.hauteur_page - 200
    if n % 2 == 0:
        x, ancre = 344.35, 'start'
    else:
        x, ancre = ref.largeur_page - 142.0, 'end'
    return ('<text x="%.2f" y="%.2f" font-family="Times New Roman, Times, serif" font-size="24" '
            'text-anchor="%s">%d</text>' % (x, y, ancre, n))


def composer_solo(parts, cles_par_fichier):
    """Partie solo : les systèmes de l'instrument choisi, page après page, dans l'ordre des fichiers,
    disposés sur le minimum de pages sans chevauchement.
    cles_par_fichier : {indice de page: [clés d'identités]}. Renvoie (textes SVG des pages, pages, rapport)."""
    ref = parts[0]
    sp = ref.espace
    blocs, absents = [], []
    for fi, part in enumerate(parts):
        cles = cles_par_fichier.get(fi)
        if not cles:
            absents.append(fi)
            continue
        for bloc in part.blocs(cles):
            bloc['fichier'] = fi
            blocs.append(bloc)
    if not blocs:
        raise ValueError("cet instrument n'apparaît dans aucun des fichiers choisis")
    communs = ref.elements_communs()
    debut_1 = communs['entete_bas'] + 2 * sp if communs['entete'] else DEBUT_PAGE_SUITE
    fin = communs['pied_haut'] - 2 * sp if communs['pied'] else ref.hauteur_page - 300
    pages = disposer_blocs(blocs, debut_1, DEBUT_PAGE_SUITE, fin, 2 * sp)
    textes = []
    for n, page in enumerate(pages, start=1):
        parties = [communs['entete']] if n == 1 else []
        parties.append(communs['pied'])
        for bloc, ty in page:
            parties.append('<g transform="translate(0 %.3f)">%s</g>' % (ty, bloc['svg']))
        if n >= 2:
            parties.append(_numero_page(n, ref, communs))
        textes.append(ref.racine + ''.join(parties) + '</svg>')
    return textes, pages, {'blocs': len(blocs), 'absents': absents}


def ecrire_solo(textes, dossier, nom):
    """Écrit une page par fichier SVG : <nom>_page01.svg, <nom>_page02.svg, ..."""
    os.makedirs(dossier, exist_ok=True)
    chemins = []
    for n, texte in enumerate(textes, start=1):
        chemin = os.path.join(dossier, '%s_page%02d.svg' % (nom, n))
        with open(chemin, 'w', encoding='utf-8') as f:
            f.write(texte)
        chemins.append(chemin)
    return chemins


# ==============================================================================
# 3. Ligne de commande
# ==============================================================================

def _pluriel(n, singulier, pluriel):
    return '%d %s' % (n, singulier if n == 1 else pluriel)


def _suffixe(numeros):
    """'instrument3' pour un instrument, 'instruments_1_3' pour plusieurs."""
    numeros = sorted(numeros)
    if len(numeros) == 1:
        return 'instrument%d' % numeros[0]
    return 'instruments_' + '_'.join(str(n) for n in numeros)


def _nom_sortie(chemin_source, numeros, dossier):
    """Nom du fichier produit, à partir du fichier source et des numéros d'instruments."""
    if isinstance(numeros, int):
        numeros = [numeros]
    base = os.path.splitext(os.path.basename(chemin_source))[0]
    return os.path.join(dossier, '%s_%s.svg' % (base, _suffixe(numeros)))


def _ecrire_fichier(chemin, texte):
    with open(chemin, 'w', encoding='utf-8') as f:
        f.write(texte)
    print('Écrit : %s' % chemin)


def _afficher_menu(part):
    print('Instruments détectés (%s, %s) :' % (
        _pluriel(len(part.systemes), 'système', 'systèmes'),
        _pluriel(len(part.portees), 'portée', 'portées')))
    for ins in part.instruments:
        print('  %2d. %s' % (ins['numero'], part.decrire(ins)))


def _analyser_numeros(texte, nombre):
    """Numéros saisis (à partir de 1), séparés par des espaces ou des virgules.
    Renvoie (liste, erreur) ; la liste est vide si la saisie est vide."""
    numeros = []
    for jeton in texte.replace(',', ' ').split():
        if not jeton.isdigit() or not 1 <= int(jeton) <= nombre:
            return [], "numéro incorrect « %s » (de 1 à %d)" % (jeton, nombre)
        if int(jeton) not in numeros:
            numeros.append(int(jeton))
    return numeros, None


def _choisir_au_clavier(part):
    while True:
        try:
            rep = input('Quels instruments voulez-vous garder ? (ex. 3, ou 1 3 ; Entrée pour annuler) : ')
        except EOFError:
            return None
        numeros, erreur = _analyser_numeros(rep, len(part.instruments))
        if erreur:
            print('  Réponse invalide : %s.' % erreur)
            continue
        return numeros or None


def _choisir_solo_au_clavier(nombre):
    while True:
        try:
            rep = input('Quels instruments voulez-vous dans la partie solo ? (ex. 3, ou 1 3 ; Entrée pour annuler) : ')
        except EOFError:
            return None
        numeros, erreur = _analyser_numeros(rep, nombre)
        if erreur:
            print('  Réponse invalide : %s.' % erreur)
            continue
        return numeros or None


def _principal_solo(args):
    parts = []
    try:
        for chemin in args.solo:
            with open(chemin, encoding='utf-8') as f:
                parts.append(Partition(f.read()))
    except (OSError, ValueError) as e:
        print('Erreur : %s' % e, file=sys.stderr)
        return 1
    globaux = instruments_multi(parts)
    print('Instruments communs aux %s :' % _pluriel(len(parts), 'page', 'pages'))
    for g in globaux:
        print('  %2d. %s' % (g['numero'], g['description']))
    if args.liste:
        return 0
    numeros = args.instrument
    if numeros is None:
        numeros = _choisir_solo_au_clavier(len(globaux))
        if numeros is None:
            print("Aucun instrument choisi : rien n'a été écrit.")
            return 0
    hors = [n for n in numeros if not 1 <= n <= len(globaux)]
    if hors:
        print("Erreur : numéro inexistant : %s (de 1 à %d)." % (
            ', '.join(str(n) for n in hors), len(globaux)), file=sys.stderr)
        return 1
    choisis = [g for g in globaux if g['numero'] in numeros]
    cles = {}
    for fi in range(len(parts)):
        c = [g['cles'][fi] for g in choisis if fi in g['cles']]
        if c:
            cles[fi] = c
    try:
        textes, pages, rapport = composer_solo(parts, cles)
    except ValueError as e:
        print('Erreur : %s' % e, file=sys.stderr)
        return 1
    dossier = args.sortie or (os.path.dirname(args.solo[0]) or '.')
    for chemin in ecrire_solo(textes, dossier, 'solo_' + _suffixe(numeros)):
        print('Écrit : %s' % chemin)
    if rapport['absents']:
        print('Attention : instrument absent de %s' % ', '.join(
            os.path.basename(args.solo[i]) for i in rapport['absents']))
    print('%s, %s.' % (_pluriel(len(textes), 'page', 'pages'), _pluriel(rapport['blocs'], 'système', 'systèmes')))
    return 0


def principal(argv=None):
    parser = argparse.ArgumentParser(
        description="Garde un ou plusieurs instruments d'une partition SVG (export MuseScore). "
                    "Sans fichier, ouvre l'interface graphique.")
    parser.add_argument('fichier', nargs='?', help='fichier .svg de la partition')
    parser.add_argument('-i', '--instrument', type=int, nargs='+', metavar='N',
                        help="numéro(s) d'instrument à garder, sans question (ex. -i 3 ou -i 1 3)")
    parser.add_argument('-o', '--sortie', help='fichier de sortie (avec --tous : dossier de sortie)')
    parser.add_argument('--liste', action='store_true', help='afficher la liste des instruments et quitter')
    parser.add_argument('--tous', action='store_true', help='écrire un fichier par instrument')
    parser.add_argument('--solo', nargs='+', metavar='PAGE',
                        help="partie solo : instruments de plusieurs pages (dans l'ordre donné)")
    args = parser.parse_args(argv)
    if args.solo:
        return _principal_solo(args)
    if args.fichier is None:
        return lancer_interface()
    dossier_source = os.path.dirname(args.fichier) or '.'
    try:
        with open(args.fichier, encoding='utf-8') as f:
            texte = f.read()
        part = Partition(texte)
    except (OSError, ValueError) as e:
        print('Erreur : %s' % e, file=sys.stderr)
        return 1
    for avertissement in part.avertissements:
        print('Attention : %s' % avertissement, file=sys.stderr)
    _afficher_menu(part)
    if args.liste:
        return 0
    if args.tous:
        dossier = args.sortie or dossier_source
        os.makedirs(dossier, exist_ok=True)
        for ins in part.instruments:
            _ecrire_fichier(_nom_sortie(args.fichier, ins['numero'], dossier), part.ecrire(ins['cle']))
        return 0
    numeros = args.instrument
    if numeros is None:
        numeros = _choisir_au_clavier(part)
        if numeros is None:
            print("Aucun instrument choisi : rien n'a été écrit.")
            return 0
    hors = [n for n in numeros if not 1 <= n <= len(part.instruments)]
    if hors:
        print("Erreur : numéro inexistant : %s (de 1 à %d)." % (
            ', '.join(str(n) for n in hors), len(part.instruments)), file=sys.stderr)
        return 1
    sortie = args.sortie or _nom_sortie(args.fichier, numeros, dossier_source)
    _ecrire_fichier(sortie, part.ecrire([part.instruments[n - 1]['cle'] for n in numeros]))
    return 0


# ==============================================================================
# 4. Interface graphique (Tkinter, inclus avec Python)
# ==============================================================================

_RENDU_ERREURS = []     # motifs d'échec des moteurs de rendu, pour le message d'aide


def _rendu_png():
    """Fonction de rendu SVG vers PNG pour l'aperçu, ou None si aucun moteur n'est utilisable.
    PyMuPDF est essayé en premier : ses installations ne demandent aucune bibliothèque système.
    cairosvg est proposé en secours."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError as e:
            _RENDU_ERREURS.append('PyMuPDF : %s' % e)
            fitz = None
    if fitz is not None:
        def rendre(bytestring, output_width, background_color='white'):
            doc = fitz.open(stream=bytestring, filetype='svg')
            page = doc[0]
            zoom = output_width / page.rect.width
            return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes('png')
        return rendre
    try:
        from cairosvg import svg2png
        return svg2png
    except Exception as e:
        _RENDU_ERREURS.append('cairosvg : %s' % e)
    return None


def _message_rendu_indisponible():
    detail = ' ; '.join(_RENDU_ERREURS) if _RENDU_ERREURS else 'aucun moteur trouvé'
    return ("Aperçu indisponible : aucun moteur de rendu n'a pu être chargé.\n"
            "Pour l'activer, dans l'invite de commandes :  pip install pymupdf\n"
            "(la sélection et l'enregistrement fonctionnent sans lui).\n"
            "Détail : %s" % detail)


def lancer_interface(fichier=None):
    """Ouvre la fenêtre de l'application. Renvoie 1 si Tkinter ou l'affichage ne sont pas disponibles."""
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        print("L'interface graphique demande Tkinter (inclus avec Python sous Windows et macOS ; "
              "sous Linux, installez le paquet python3-tk). Utilisez la ligne de commande : --help")
        return 1
    try:
        racine = tk.Tk()
    except tk.TclError as e:
        print("Impossible d'ouvrir la fenêtre (%s). Utilisez la ligne de commande : --help" % e)
        return 1
    _Interface(racine, tk, ttk, filedialog, messagebox, fichier)
    racine.mainloop()
    return 0


class _Interface(object):
    """Fenêtre : choix de la partition, des instruments, aperçu, puis enregistrement."""

    LARGEUR_APERCU = 900          # largeur (pixels) de l'aperçu de la page
    COULEUR_ERREUR = '#b00020'
    COULEUR_SUCCES = '#1a7f37'
    CONSIGNE = ("Sélectionnez un ou plusieurs instruments dans la liste :\n"
                "la page s'affichera ici, avec leurs portées en rouge.")

    def __init__(self, racine, tk, ttk, filedialog, messagebox, fichier=None):
        self.racine = racine
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.rendu = _rendu_png()
        self.part = None
        self.chemin = None
        self.image = None
        self.dernier_dossier = None
        self.var_fichier = tk.StringVar(value='Aucune partition choisie')
        self.var_resume = tk.StringVar(value='')
        self.var_dossier = tk.StringVar(value='')
        self.var_nom = tk.StringVar(value='')
        self.var_etat = tk.StringVar(value='Choisissez une partition SVG pour commencer.')
        self._construire()
        racine.title("Extraire une portée d'une partition SVG")
        racine.geometry('1200x740')
        racine.minsize(980, 600)
        if fichier:
            self.charger(fichier)

    # -- construction de la fenêtre --------------------------------------------------

    def _construire(self):
        tk, ttk = self.tk, self.ttk
        self.cahier = ttk.Notebook(self.racine)
        self.cahier.pack(fill='both', expand=True)
        onglet_une = ttk.Frame(self.cahier)
        self.cahier.add(onglet_une, text='Une page')
        cadre = ttk.Frame(onglet_une, padding=10)
        cadre.pack(fill='both', expand=True)

        haut = ttk.Frame(cadre)
        haut.pack(fill='x')
        ttk.Button(haut, text='1. Choisir une partition (.svg)…', command=self.choisir).pack(side='left')
        ttk.Label(haut, textvariable=self.var_fichier).pack(side='left', padx=12)
        ttk.Label(haut, textvariable=self.var_resume).pack(side='right')

        milieu = ttk.Panedwindow(cadre, orient='horizontal')
        milieu.pack(fill='both', expand=True, pady=10)
        gauche = ttk.Frame(milieu)
        droite = ttk.Frame(milieu)
        milieu.add(gauche, weight=1)
        milieu.add(droite, weight=2)

        ttk.Label(gauche, text='2. Choisir le ou les instruments\n'
                               '(Ctrl ou Maj pour en prendre plusieurs)').pack(anchor='w')
        self.arbre = ttk.Treeview(gauche, columns=('numero', 'description'), show='headings',
                                  selectmode='extended', height=18)
        self.arbre.heading('numero', text='N°')
        self.arbre.heading('description', text='Instrument (position, clé, étiquette, présence)')
        self.arbre.column('numero', width=46, anchor='center', stretch=False)
        self.arbre.column('description', width=430, anchor='w')
        defilement = ttk.Scrollbar(gauche, orient='vertical', command=self.arbre.yview)
        self.arbre.configure(yscrollcommand=defilement.set)
        self.arbre.pack(side='left', fill='both', expand=True, pady=(6, 0))
        defilement.pack(side='left', fill='y', pady=(6, 0))
        self.arbre.bind('<<TreeviewSelect>>', lambda event: self.selection_changee())

        ttk.Label(droite, text='Aperçu de la page (en rouge : les instruments choisis)').pack(anchor='w')
        zone = ttk.Frame(droite)
        zone.pack(fill='both', expand=True, pady=(6, 0))
        self.toile = tk.Canvas(zone, background='#e6e6e6', highlightthickness=0)
        vert = ttk.Scrollbar(zone, orient='vertical', command=self.toile.yview)
        horiz = ttk.Scrollbar(zone, orient='horizontal', command=self.toile.xview)
        self.toile.configure(yscrollcommand=vert.set, xscrollcommand=horiz.set)
        self.toile.grid(row=0, column=0, sticky='nsew')
        vert.grid(row=0, column=1, sticky='ns')
        horiz.grid(row=1, column=0, sticky='ew')
        zone.rowconfigure(0, weight=1)
        zone.columnconfigure(0, weight=1)
        self.toile.bind('<Enter>', lambda event: self._activer_molette(True))
        self.toile.bind('<Leave>', lambda event: self._activer_molette(False))
        self._message_apercu(self.CONSIGNE)

        bas = ttk.LabelFrame(cadre, text='3. Enregistrer', padding=8)
        bas.pack(fill='x')
        ttk.Label(bas, text='Dossier :').grid(row=0, column=0, sticky='w')
        ttk.Entry(bas, textvariable=self.var_dossier).grid(row=0, column=1, sticky='we', padx=6)
        ttk.Button(bas, text='Parcourir…', command=self.parcourir).grid(row=0, column=2)
        ttk.Label(bas, text='Nom du fichier :').grid(row=1, column=0, sticky='w', pady=6)
        ttk.Entry(bas, textvariable=self.var_nom).grid(row=1, column=1, sticky='we', padx=6, pady=6)
        bas.columnconfigure(1, weight=1)
        boutons = ttk.Frame(bas)
        boutons.grid(row=2, column=0, columnspan=3, sticky='w')
        self.bouton_selection = ttk.Button(boutons, text='Enregistrer la sélection',
                                           command=self.enregistrer_selection, state='disabled')
        self.bouton_selection.pack(side='left')
        self.bouton_tous = ttk.Button(boutons, text='Enregistrer chaque instrument dans un fichier séparé',
                                      command=self.enregistrer_tous, state='disabled')
        self.bouton_tous.pack(side='left', padx=8)
        self.bouton_ouvrir = ttk.Button(boutons, text='Ouvrir le dossier',
                                        command=self.ouvrir_dossier, state='disabled')
        self.bouton_ouvrir.pack(side='left')

        self.etat = ttk.Label(cadre, textvariable=self.var_etat, anchor='w')
        self.etat.pack(fill='x', pady=(8, 0))
        onglet_solo = ttk.Frame(self.cahier)
        self.cahier.add(onglet_solo, text='Partie solo (plusieurs pages)')
        self.solo = _OngletSolo(onglet_solo, tk, ttk, self.filedialog, self.messagebox, self.rendu)

    def _message_apercu(self, texte):
        self.image = None
        self.toile.delete('all')
        self.toile.configure(scrollregion=(0, 0, 1, 1))
        self.toile.create_text(20, 20, anchor='nw', text=texte, width=600)

    def _activer_molette(self, actif):
        """La molette fait défiler l'aperçu tant que le pointeur est dessus."""
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            if actif:
                self.racine.bind_all(sequence, self._molette)
            else:
                self.racine.unbind_all(sequence)

    def _molette(self, event):
        if event.num == 4:
            pas = -1
        elif event.num == 5:
            pas = 1
        else:
            pas = -1 if event.delta > 0 else 1
        self.toile.yview_scroll(pas * 3, 'units')

    # -- actions ---------------------------------------------------------------------

    def choisir(self):
        chemin = self.filedialog.askopenfilename(
            parent=self.racine, title='Choisir une partition SVG',
            filetypes=[('Partitions SVG', '*.svg'), ('Tous les fichiers', '*.*')])
        if chemin:
            self.charger(chemin)

    def charger(self, chemin):
        try:
            with open(chemin, encoding='utf-8') as f:
                texte = f.read()
            part = Partition(texte)
        except Exception as e:
            self.messagebox.showerror('Fichier non reconnu',
                                      'Ce fichier ne peut pas être utilisé comme partition :\n%s' % e,
                                      parent=self.racine)
            return
        self.part, self.chemin = part, chemin
        self.var_fichier.set(os.path.basename(chemin))
        self.var_resume.set('%s · %s · %s' % (
            _pluriel(len(part.systemes), 'système', 'systèmes'),
            _pluriel(len(part.portees), 'portée', 'portées'),
            _pluriel(len(part.instruments), 'instrument', 'instruments')))
        self.racine.title('%s : extraction de portée' % os.path.basename(chemin))
        self.arbre.delete(*self.arbre.get_children())
        for ins in part.instruments:
            self.arbre.insert('', 'end', iid=str(ins['numero']),
                              values=(ins['numero'], part.decrire(ins)))
        if not self.var_dossier.get():
            self.var_dossier.set(os.path.dirname(os.path.abspath(chemin)))
        self.bouton_tous.configure(state='normal')
        self.bouton_ouvrir.configure(state='disabled')
        self.etat.configure(foreground='black')
        self._message_apercu(self.CONSIGNE)
        if part.avertissements:
            self._erreur('Attention : ' + ' ; '.join(part.avertissements))
        else:
            self.var_etat.set('Choisissez le ou les instruments à extraire.')

    def _numeros(self):
        return sorted(int(i) for i in self.arbre.selection())

    def selection_changee(self):
        numeros = self._numeros()
        if self.part is None or not numeros:
            self.bouton_selection.configure(state='disabled')
            self._message_apercu(self.CONSIGNE)
            return
        self.bouton_selection.configure(state='normal')
        base = os.path.splitext(os.path.basename(self.chemin))[0]
        self.var_nom.set('%s_%s.svg' % (base, _suffixe(numeros)))
        self.apercu(numeros)

    def apercu(self, numeros):
        if self.rendu is None:
            self._message_apercu(_message_rendu_indisponible())
            return
        try:
            cles = [self.part.instruments[n - 1]['cle'] for n in numeros]
            png = self.rendu(bytestring=self.part.apercu(cles).encode('utf-8'),
                             output_width=self.LARGEUR_APERCU, background_color='white')
            self.image = self.tk.PhotoImage(data=base64.b64encode(png).decode('ascii'))
        except Exception as e:
            self._message_apercu("Aperçu impossible : %s" % e)
            return
        self.toile.delete('all')
        self.toile.create_image(0, 0, anchor='nw', image=self.image)
        self.toile.configure(scrollregion=(0, 0, self.image.width(), self.image.height()))
        self.toile.xview_moveto(0)
        self.toile.yview_moveto(0)

    def parcourir(self):
        dossier = self.filedialog.askdirectory(parent=self.racine, title='Dossier de destination',
                                               initialdir=self.var_dossier.get() or os.getcwd())
        if dossier:
            self.var_dossier.set(dossier)

    def _dossier(self):
        dossier = self.var_dossier.get().strip()
        if not dossier:
            self.messagebox.showwarning('Dossier manquant', 'Indiquez le dossier de destination.',
                                        parent=self.racine)
            return None
        try:
            os.makedirs(dossier, exist_ok=True)
        except OSError as e:
            self.messagebox.showerror('Dossier impossible', str(e), parent=self.racine)
            return None
        self.dernier_dossier = dossier
        return dossier

    def enregistrer_selection(self):
        numeros = self._numeros()
        dossier = self._dossier() if numeros else None
        if dossier is None:
            return
        nom = self.var_nom.get().strip() or '%s_%s.svg' % (
            os.path.splitext(os.path.basename(self.chemin))[0], _suffixe(numeros))
        if not nom.lower().endswith('.svg'):
            nom += '.svg'
        chemin = os.path.join(dossier, nom)
        try:
            texte = self.part.ecrire([self.part.instruments[n - 1]['cle'] for n in numeros])
            with open(chemin, 'w', encoding='utf-8') as f:
                f.write(texte)
        except Exception as e:
            self.messagebox.showerror('Enregistrement impossible', str(e), parent=self.racine)
            return
        self._succes('Fichier enregistré : %s' % chemin)

    def enregistrer_tous(self):
        dossier = self._dossier()
        if dossier is None:
            return
        try:
            for ins in self.part.instruments:
                with open(_nom_sortie(self.chemin, ins['numero'], dossier), 'w', encoding='utf-8') as f:
                    f.write(self.part.ecrire(ins['cle']))
        except Exception as e:
            self.messagebox.showerror('Enregistrement impossible', str(e), parent=self.racine)
            return
        self._succes('%s enregistrés dans %s' % (
            _pluriel(len(self.part.instruments), 'fichier', 'fichiers'), dossier))

    def _succes(self, message):
        self.var_etat.set(message)
        self.etat.configure(foreground=self.COULEUR_SUCCES)
        self.bouton_ouvrir.configure(state='normal')

    def _erreur(self, message):
        self.var_etat.set(message)
        self.etat.configure(foreground=self.COULEUR_ERREUR)

    def ouvrir_dossier(self):
        if not self.dernier_dossier:
            return
        try:
            if sys.platform.startswith('win'):
                os.startfile(self.dernier_dossier)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', self.dernier_dossier])
            else:
                subprocess.Popen(['xdg-open', self.dernier_dossier])
        except OSError as e:
            self.messagebox.showerror('Ouverture impossible', str(e), parent=self.racine)


def _dessiner_svg(tk, toile, rendu, svg, largeur):
    """Affiche une page SVG dans un Canvas Tkinter ; renvoie l'image, à garder en référence."""
    png = rendu(bytestring=svg.encode('utf-8'), output_width=largeur, background_color='white')
    image = tk.PhotoImage(data=base64.b64encode(png).decode('ascii'))
    toile.delete('all')
    toile.create_image(0, 0, anchor='nw', image=image)
    toile.configure(scrollregion=(0, 0, image.width(), image.height()))
    toile.xview_moveto(0)
    toile.yview_moveto(0)
    return image


class _OngletSolo(object):
    """Onglet « Partie solo » : plusieurs pages, un instrument, une partition sur le minimum de pages."""

    LARGEUR_APERCU = 800

    def __init__(self, parent, tk, ttk, filedialog, messagebox, rendu):
        self.parent = parent
        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.rendu = rendu
        self.fichiers = []          # chemins des pages, dans l'ordre de la partition
        self.parts = {}             # chemin -> Partition (mise en cache)
        self.globaux = []
        self.par_numero = {}
        self.textes = None          # pages de la partie solo créée
        self.page = 0
        self.image = None
        self.var_dossier = tk.StringVar(value='')
        self.var_nom = tk.StringVar(value='')
        self.var_page = tk.StringVar(value='')
        self.var_etat = tk.StringVar(value="Ajoutez les pages (.svg) de la partition, puis choisissez l'instrument.")
        self._construire()

    # -- construction --------------------------------------------------------------

    def _construire(self):
        tk, ttk = self.tk, self.ttk
        cadre = ttk.Frame(self.parent, padding=10)
        cadre.pack(fill='both', expand=True)

        haut = ttk.Frame(cadre)
        haut.pack(fill='x')
        ttk.Button(haut, text='1. Ajouter des pages (.svg)…', command=self.ajouter).pack(side='left')
        ttk.Button(haut, text='Retirer', command=self.retirer).pack(side='left', padx=4)
        ttk.Button(haut, text='Monter', command=lambda: self.deplacer(-1)).pack(side='left')
        ttk.Button(haut, text='Descendre', command=lambda: self.deplacer(1)).pack(side='left', padx=4)
        ttk.Button(haut, text='Vider', command=self.vider).pack(side='left')
        ttk.Label(haut, text="Pages classées par nom de fichier ; Monter / Descendre pour corriger l'ordre.").pack(
            side='left', padx=12)

        milieu = ttk.Panedwindow(cadre, orient='horizontal')
        milieu.pack(fill='both', expand=True, pady=8)
        gauche = ttk.Frame(milieu)
        droite = ttk.Frame(milieu)
        milieu.add(gauche, weight=1)
        milieu.add(droite, weight=1)

        ttk.Label(gauche, text="Pages, dans l'ordre de la partition").pack(anchor='w')
        zone_liste = ttk.Frame(gauche)
        zone_liste.pack(fill='x')
        self.liste = tk.Listbox(zone_liste, height=6, activestyle='none', exportselection=False)
        defilement_liste = ttk.Scrollbar(zone_liste, orient='vertical', command=self.liste.yview)
        self.liste.configure(yscrollcommand=defilement_liste.set)
        self.liste.pack(side='left', fill='x', expand=True)
        defilement_liste.pack(side='left', fill='y')
        ttk.Label(gauche, text='2. Choisir le ou les instruments à réunir').pack(anchor='w', pady=(8, 0))
        self.arbre = ttk.Treeview(gauche, columns=('numero', 'description'), show='headings',
                                  selectmode='extended', height=12)
        self.arbre.heading('numero', text='N°')
        self.arbre.heading('description', text='Instrument (clé, étiquette, présence)')
        self.arbre.column('numero', width=46, anchor='center', stretch=False)
        self.arbre.column('description', width=440, anchor='w', stretch=True)
        self.arbre.pack(fill='both', expand=True)
        self.arbre.bind('<<TreeviewSelect>>', lambda event: self.instrument_selectionne())

        ttk.Label(droite, text="Aperçu : première page où l'instrument apparaît").pack(anchor='w')
        zone = ttk.Frame(droite)
        zone.pack(fill='both', expand=True, pady=(4, 0))
        self.toile = tk.Canvas(zone, background='#e6e6e6', highlightthickness=0)
        vert = ttk.Scrollbar(zone, orient='vertical', command=self.toile.yview)
        horiz = ttk.Scrollbar(zone, orient='horizontal', command=self.toile.xview)
        self.toile.configure(yscrollcommand=vert.set, xscrollcommand=horiz.set)
        self.toile.grid(row=0, column=0, sticky='nsew')
        vert.grid(row=0, column=1, sticky='ns')
        horiz.grid(row=1, column=0, sticky='ew')
        zone.rowconfigure(0, weight=1)
        zone.columnconfigure(0, weight=1)
        nav = ttk.Frame(droite)
        nav.pack(fill='x', pady=4)
        ttk.Button(nav, text='◀ page précédente', command=lambda: self.feuilleter(-1)).pack(side='left')
        ttk.Label(nav, textvariable=self.var_page).pack(side='left', padx=8)
        ttk.Button(nav, text='page suivante ▶', command=lambda: self.feuilleter(1)).pack(side='left')

        bas = ttk.LabelFrame(cadre, text='3. Créer la partie solo', padding=8)
        bas.pack(fill='x')
        ttk.Label(bas, text='Dossier :').grid(row=0, column=0, sticky='w')
        ttk.Entry(bas, textvariable=self.var_dossier).grid(row=0, column=1, sticky='we', padx=6)
        ttk.Button(bas, text='Parcourir…', command=self.parcourir).grid(row=0, column=2)
        ttk.Label(bas, text='Nom des fichiers :').grid(row=1, column=0, sticky='w', pady=6)
        ttk.Entry(bas, textvariable=self.var_nom).grid(row=1, column=1, sticky='we', padx=6, pady=6)
        bas.columnconfigure(1, weight=1)
        self.bouton_creer = ttk.Button(bas, text='Créer la partie solo', command=self.creer, state='disabled')
        self.bouton_creer.grid(row=2, column=0, columnspan=3, sticky='w')

        self.etat = ttk.Label(cadre, textvariable=self.var_etat, anchor='w')
        self.etat.pack(fill='x', pady=(6, 0))

    # -- pages ---------------------------------------------------------------------

    def ajouter(self):
        chemins = self.filedialog.askopenfilenames(
            parent=self.parent, title='Choisir les pages de la partition',
            filetypes=[('Partitions SVG', '*.svg'), ('Tous les fichiers', '*.*')])
        self.ajouter_chemins(list(chemins))

    def ajouter_chemins(self, chemins):
        for c in chemins:
            if c and c not in self.fichiers:
                self.fichiers.append(c)
        self.fichiers.sort(key=_cle_naturelle)
        self._rafraichir()

    def retirer(self):
        sel = self.liste.curselection()
        if sel:
            del self.fichiers[sel[0]]
            self._rafraichir()

    def deplacer(self, d):
        sel = self.liste.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + d
        if 0 <= j < len(self.fichiers):
            self.fichiers[i], self.fichiers[j] = self.fichiers[j], self.fichiers[i]
            self._rafraichir()
            self.liste.selection_set(j)

    def vider(self):
        self.fichiers = []
        self._rafraichir()

    def _partie(self, chemin):
        if chemin not in self.parts:
            with open(chemin, encoding='utf-8') as f:
                self.parts[chemin] = Partition(f.read())
        return self.parts[chemin]

    def _rafraichir(self):
        valides = []
        for c in self.fichiers:
            try:
                self._partie(c)
                valides.append(c)
            except Exception as e:
                self.messagebox.showerror('Fichier non reconnu', '%s ne peut pas être utilisé :\n%s' % (
                    os.path.basename(c), e), parent=self.parent)
        self.fichiers = valides
        self.liste.delete(0, 'end')
        for c in self.fichiers:
            self.liste.insert('end', os.path.basename(c))
        parts = [self.parts[c] for c in self.fichiers]
        self.globaux = instruments_multi(parts) if parts else []
        self.par_numero = {g['numero']: g for g in self.globaux}
        self.arbre.delete(*self.arbre.get_children())
        for g in self.globaux:
            self.arbre.insert('', 'end', iid=str(g['numero']), values=(g['numero'], g['description']))
        self.textes = None
        self.var_page.set('')
        self.bouton_creer.configure(state='disabled')
        self.var_etat.set('%s. Choisissez le ou les instruments à réunir.' % _pluriel(
            len(self.fichiers), 'page chargée', 'pages chargées') if self.fichiers else
            'Ajoutez les pages (.svg) de la partition.')
        self.etat.configure(foreground='black')

    # -- instruments, aperçu, création ----------------------------------------------

    def _cles(self, fi, numeros):
        return [self.par_numero[n]['cles'][fi] for n in numeros
                if n in self.par_numero and fi in self.par_numero[n]['cles']]

    def instrument_selectionne(self):
        numeros = sorted(int(i) for i in self.arbre.selection())
        if not numeros or not self.fichiers:
            self.bouton_creer.configure(state='disabled')
            return
        self.bouton_creer.configure(state='normal')
        self.var_nom.set('solo_' + _suffixe(numeros))
        self._apercu_premiere_page(numeros)

    def _apercu_premiere_page(self, numeros):
        self.textes = None
        self.var_page.set('')
        for fi, chemin in enumerate(self.fichiers):
            cles = self._cles(fi, numeros)
            if not cles:
                continue
            if self.rendu is None:
                self._message(_message_rendu_indisponible())
                return
            try:
                self.image = _dessiner_svg(self.tk, self.toile, self.rendu,
                                           self._partie(chemin).apercu(cles), self.LARGEUR_APERCU)
                self.var_etat.set('Aperçu : %s' % os.path.basename(chemin))
            except Exception as e:
                self.var_etat.set('Aperçu impossible : %s' % e)
            return

    def _message(self, texte):
        self.image = None
        self.toile.delete('all')
        self.toile.configure(scrollregion=(0, 0, 1, 1))
        self.toile.create_text(20, 20, anchor='nw', text=texte, width=600)

    def creer(self):
        numeros = sorted(int(i) for i in self.arbre.selection())
        if not numeros or not self.fichiers:
            return
        dossier = self.var_dossier.get().strip()
        if not dossier:
            self.messagebox.showwarning('Dossier manquant', 'Indiquez le dossier de destination.',
                                        parent=self.parent)
            return
        cles = {}
        for fi in range(len(self.fichiers)):
            c = self._cles(fi, numeros)
            if c:
                cles[fi] = c
        parts = [self._partie(c) for c in self.fichiers]
        try:
            textes, pages, rapport = composer_solo(parts, cles)
            nom = self.var_nom.get().strip() or 'solo_' + _suffixe(numeros)
            if nom.lower().endswith('.svg'):
                nom = nom[:-4]
            chemins = ecrire_solo(textes, dossier, nom)
        except Exception as e:
            self.messagebox.showerror('Partie solo impossible', str(e), parent=self.parent)
            return
        self.textes = textes
        self.page = 0
        self._afficher_page()
        msg = 'Partie solo créée : %s, %s, dans %s.' % (
            _pluriel(len(chemins), 'page', 'pages'), _pluriel(rapport['blocs'], 'système', 'systèmes'), dossier)
        if rapport['absents']:
            msg += ' Instrument absent de : %s.' % ', '.join(
                os.path.basename(self.fichiers[i]) for i in rapport['absents'])
        self.var_etat.set(msg)
        self.etat.configure(foreground='#1a7f37')

    def _afficher_page(self):
        if self.rendu is not None:
            self.image = _dessiner_svg(self.tk, self.toile, self.rendu, self.textes[self.page], self.LARGEUR_APERCU)
        self.var_page.set('page %d / %d' % (self.page + 1, len(self.textes)))

    def feuilleter(self, d):
        if not self.textes:
            return
        self.page = min(max(0, self.page + d), len(self.textes) - 1)
        self._afficher_page()

    def parcourir(self):
        dossier = self.filedialog.askdirectory(parent=self.parent, title='Dossier de destination',
                                               initialdir=self.var_dossier.get() or os.getcwd())
        if dossier:
            self.var_dossier.set(dossier)


if __name__ == '__main__':
    sys.exit(principal())
