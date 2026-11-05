#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert a classic Type1 (.pfb) TeX font to a web OTF the browser can search.

Many documents pull in classic 8-bit TeX fonts (cmsy10, cmmi10, cmr10, the AMS
symbol faces, …) that have no OpenType form. LuaTeX typesets them happily, but
the serializer has no file to hand the browser, so it records filename 'unknown'
and the viewer falls back to drawing each glyph's metric box. Their Type1
outlines ship with every TeX install, though, so we convert each .pfb to a
CFF/OTF in pure fontTools (no FontForge dependency) that the browser can load.

The addressing is the crux. A classic font is 8-bit: each glyph lives in a
numeric slot (0–255) whose meaning is font-specific — slot 0x22 of cmsy10 is
'arrowup', not '"'. We give each slot a target codepoint (build the converted
font's cmap accordingly, and let the caller rewrite each glyph node's `char` to
match — transforms.normalise_legacy_font_addressing):

  * If the slot's glyph *name* has a standard Unicode meaning — through the
    Adobe Glyph List, through the TeX glyph names the AGL does not carry, or
    through one of the systematic TeX size-variant families — the real
    codepoint is used, so the text is selectable and Ctrl-F finds it. Keying on
    the name, not the slot, sidesteps the classic TeX encodings' quirks (cmr's
    slot 0x3C is 'exclamdown', and gets U+00A1, not '<'). See the tables under
    "Glyph name → Unicode" below.
  * Only a name with no standard meaning at all (the pieces of an extensible
    brace, an accent that exists in Unicode solely as a combining mark, a
    CM-specific oddity like 'suppress'), or one whose codepoint another slot of
    the *same* font already claimed, keeps a private-use code, PUA_BASE + slot:
    collision-free, but not searchable. This is the same idea the rest of the
    pipeline uses for awkward glyphs (see transforms.PUA_BASE).

Because every converted font is served as its own @font-face, PUA_BASE+slot never
collides across fonts, and the per-glyph explicit positioning in the viewer means
the browser never ligates or kerns adjacent real-Unicode glyphs (the converted
faces carry no GSUB/kern anyway), so switching a subset to real codepoints cannot
move any ink.

A BMP private-use base is used (U+E000, not the Plane-16 base the glyph-index
normaliser uses) so a font's 256 slots fit in E000..E0FF and a plain format-4
cmap suffices — the converted font is one we build from scratch, so there is no
real glyph at those codepoints to collide with, and no real target may land
there (_acceptable_cp rejects every private-use codepoint, on all three
planes). A real target outside the BMP is fine — fontTools then adds a
format-12 subtable beside the format-4 one.

fontTools is required (t1Lib for parsing, fontBuilder for emitting). Without it,
convert() is unavailable and the caller keeps the metric-box fallback.
"""
import hashlib
import json
import os
import subprocess
import unicodedata
from pathlib import Path

try:
    from fontTools import t1Lib, agl
    from fontTools.encodings.StandardEncoding import StandardEncoding
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.transformPen import TransformPen
    _AVAILABLE = True
except ImportError:                                        # pragma: no cover
    _AVAILABLE = False

# BMP private-use area: a classic font has at most 256 slots, which fit in
# E000..E0FF and keep the converted cmap a plain format-4 table.
PUA_BASE = 0xE000


# ── Glyph name → Unicode ─────────────────────────────────────────────────────
# (lax) The old rule made a slot searchable only when its AGL codepoint fell in
# a short allowlist of ranges (ASCII, Latin-1/Ext-A, Greek, a few dashes and
# quotes). That allowlist was written for text faces, so every math face lost:
# cmsy10's 'minus' (slot 0x00) copied as U+E000, 'element' as U+E032, msbm10's
# 'greaterorequalslant' as U+E03E. The rule is now the other way round — a name
# with a standard Unicode meaning gets that codepoint, and only a name with no
# meaning (or one whose meaning is already taken in this font) keeps the PUA.
#
# The names come from three places, consulted in this order:
#
#   1. _TEX_GLYPH_UNICODE below — the TeX-specific names the Adobe Glyph List
#      does not carry, plus the handful it carries with the wrong meaning for
#      TeX. It follows lcdf-typetools' texglyphlist.txt (the table pdfTeX and
#      dvips use to build ToUnicode maps, shipped with every TeX Live), with
#      the deviations noted against the entries.
#   2. fontTools' AGL (agl.toUnicode) — letters, digits, punctuation, the
#      accented Latin repertoire, the f-ligature presentation forms.
#   3. _variant_codepoint — the systematic TeX name families no glyph list
#      spells out one by one: the four delimiter sizes (parenleftbig,
#      parenleftBig, parenleftbigg, parenleftBigg), the two operator sizes
#      (summationtext, summationdisplay), the oldstyle digits, and the wide
#      accents. Unicode has one character for all sizes, so these resolve to
#      the same codepoint and only the first slot to ask can have it.
#
# A codepoint has to survive _acceptable_cp: no combining mark or format
# character (a lone one does not behave as standalone SVG text, and the glyph
# normaliser deliberately pushes those to the PUA), nothing in the private-use
# area itself (the AGL sends many TeX names into Adobe's corporate-use block —
# 'dotlessj' to U+F6BE, 'parenlefttp' to U+F8EB — which would be exactly the
# garbage this is fixing, and the plane-15/16 bases the other normalisers use).
# Non-BMP targets are allowed: the BOONDOX script faces name their glyphs
# 'u1D4CD' and friends after the Mathematical Alphanumeric Symbols they draw,
# and fontTools adds the format-12 subtable those need by itself.

def _acceptable_cp(cp: int) -> bool:
    """Is `cp` a codepoint a converted font may address a slot by?"""
    if not 0x0020 <= cp <= 0x10FFFF:
        return False
    if 0xD800 <= cp <= 0xDFFF:                  # surrogates
        return False
    if 0xE000 <= cp <= 0xF8FF:                  # BMP PUA, incl. Adobe corporate use
        return False
    if cp >= 0xF0000:                           # the two private-use planes
        return False
    # Combining marks, controls, format characters, unassigned: none of them
    # stand alone in a text run.
    return unicodedata.category(chr(cp)) not in (
        'Mn', 'Mc', 'Me', 'Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp')


# TeX glyph names and their Unicode meaning, after texglyphlist.txt. Only names
# the AGL misses or gets wrong for TeX are listed; everything else comes from
# the AGL itself. Deliberate deviations from texglyphlist's first choice:
#   Delta/Omega      — it prefers U+2206 INCREMENT and U+2126 OHM SIGN; neither
#                      folds back to the Greek letter under a browser search
#                      (U+2206 has no decomposition at all), so the letters win.
#   Digamma          — its U+1D7CB is outside the BMP; the listed alternate
#                      U+03DD is the same character in the BMP.
#   circlecopyrt     — its U+20DD is a combining enclosing circle; the listed
#                      alternate U+25CB WHITE CIRCLE stands alone.
# Names it maps only to a multi-character string or to an invalid codepoint
# (the 'Xsmall' ligatures, 'cwm', 'negationslash', 'vector', the \not-composed
# relations) are absent on purpose: one slot cannot address two characters, and
# a lone combining mark is not text. Those keep the PUA.
_TEX_GLYPH_UNICODE: dict[str, int] = {
    'Dbar': 0x0110,                   # LATIN CAPITAL LETTER D WITH STROKE
    'Delta': 0x0394,                  # GREEK CAPITAL LETTER DELTA
    'Digamma': 0x03DD,                # GREEK SMALL LETTER DIGAMMA
    'Finv': 0x2132,                   # TURNED CAPITAL F
    'Gmir': 0x2141,                   # TURNED SANS-SERIF CAPITAL G
    'Ifractur': 0x2111,               # BLACK-LETTER CAPITAL I
    'Ng': 0x014A,                     # LATIN CAPITAL LETTER ENG
    'Omega': 0x03A9,                  # GREEK CAPITAL LETTER OMEGA
    'Omegainv': 0x2127,               # INVERTED OHM SIGN
    'Rfractur': 0x211C,               # BLACK-LETTER CAPITAL R
    'Yen': 0x00A5,                    # YEN SIGN
    'angbracketleft': 0x27E8,         # MATHEMATICAL LEFT ANGLE BRACKET
    'angbracketright': 0x27E9,        # MATHEMATICAL RIGHT ANGLE BRACKET
    'anticlockwise': 0x27F2,          # ANTICLOCKWISE GAPPED CIRCLE ARROW
    'approxorequal': 0x224A,          # ALMOST EQUAL OR EQUAL TO
    'archleftdown': 0x21B6,           # ANTICLOCKWISE TOP SEMICIRCLE ARROW
    'archrightdown': 0x21B7,          # CLOCKWISE TOP SEMICIRCLE ARROW
    'arrowbothv': 0x2195,             # UP DOWN ARROW
    'arrowbt': 0x2193,                # DOWNWARDS ARROW
    'arrowdblbothv': 0x21D5,          # UP DOWN DOUBLE ARROW
    'arrowdblbt': 0x21D3,             # DOWNWARDS DOUBLE ARROW
    'arrowdbltp': 0x21D1,             # UPWARDS DOUBLE ARROW
    'arrowhookleft': 0x21A9,          # LEFTWARDS ARROW WITH HOOK
    'arrowhookright': 0x21AA,         # RIGHTWARDS ARROW WITH HOOK
    'arrowleftbothalf': 0x21BD,       # LEFTWARDS HARPOON WITH BARB DOWNWARDS
    'arrowlefttophalf': 0x21BC,       # LEFTWARDS HARPOON WITH BARB UPWARDS
    'arrownortheast': 0x2197,         # NORTH EAST ARROW
    'arrownorthwest': 0x2196,         # NORTH WEST ARROW
    'arrowparrleftright': 0x21C6,     # LEFTWARDS ARROW OVER RIGHTWARDS ARROW
    'arrowparrrightleft': 0x21C4,     # RIGHTWARDS ARROW OVER LEFTWARDS ARROW
    'arrowrightbothalf': 0x21C1,      # RIGHTWARDS HARPOON WITH BARB DOWNWARDS
    'arrowrighttophalf': 0x21C0,      # RIGHTWARDS HARPOON WITH BARB UPWARDS
    'arrowsoutheast': 0x2198,         # SOUTH EAST ARROW
    'arrowsouthwest': 0x2199,         # SOUTH WEST ARROW
    'arrowtailleft': 0x21A2,          # LEFTWARDS ARROW WITH TAIL
    'arrowtailright': 0x21A3,         # RIGHTWARDS ARROW WITH TAIL
    'arrowtp': 0x2191,                # UPWARDS ARROW
    'arrowtripleleft': 0x21DA,        # LEFTWARDS TRIPLE ARROW
    'arrowtripleright': 0x21DB,       # RIGHTWARDS TRIPLE ARROW
    'arrowvertex': 0x23D0,            # VERTICAL LINE EXTENSION
    'asteriskcentered': 0x2217,       # ASTERISK OPERATOR
    'bardbl': 0x2225,                 # PARALLEL TO
    'barshort': 0x2223,               # DIVIDES
    'beth': 0x2136,                   # BET SYMBOL
    'between': 0x226C,                # BETWEEN
    'bigtriangledown': 0x25BD,        # WHITE DOWN-POINTING TRIANGLE
    'bigtriangleup': 0x25B3,          # WHITE UP-POINTING TRIANGLE
    'binampersand': 0x0026,           # AMPERSAND
    'bindnasrepma': 0x214B,           # TURNED AMPERSAND
    'boxast': 0x29C6,                 # SQUARED ASTERISK
    'boxbar': 0x25EB,                 # WHITE SQUARE WITH VERTICAL BISECTING LINE
    'boxbslash': 0x29C5,              # SQUARED FALLING DIAGONAL SLASH
    'boxcircle': 0x29C7,              # SQUARED SMALL CIRCLE
    'boxdot': 0x22A1,                 # SQUARED DOT OPERATOR
    'boxempty': 0x25A1,               # WHITE SQUARE
    'boxslash': 0x29C4,               # SQUARED RISING DIAGONAL SLASH
    'braceex': 0x23AA,                # CURLY BRACKET EXTENSION
    'braceleftbt': 0x23A9,            # LEFT CURLY BRACKET LOWER HOOK
    'braceleftmid': 0x23A8,           # LEFT CURLY BRACKET MIDDLE PIECE
    'bracelefttp': 0x23A7,            # LEFT CURLY BRACKET UPPER HOOK
    'bracerightbt': 0x23AD,           # RIGHT CURLY BRACKET LOWER HOOK
    'bracerightmid': 0x23AC,          # RIGHT CURLY BRACKET MIDDLE PIECE
    'bracerighttp': 0x23AB,           # RIGHT CURLY BRACKET UPPER HOOK
    'bracketleftbt': 0x23A3,          # LEFT SQUARE BRACKET LOWER CORNER
    'bracketleftex': 0x23A2,          # LEFT SQUARE BRACKET EXTENSION
    'bracketlefttp': 0x23A1,          # LEFT SQUARE BRACKET UPPER CORNER
    'bracketrightbt': 0x23A6,         # RIGHT SQUARE BRACKET LOWER CORNER
    'bracketrightex': 0x23A5,         # RIGHT SQUARE BRACKET EXTENSION
    'bracketrighttp': 0x23A4,         # RIGHT SQUARE BRACKET UPPER CORNER
    'ceilingleft': 0x2308,            # LEFT CEILING
    'ceilingright': 0x2309,           # RIGHT CEILING
    'check': 0x2713,                  # CHECK MARK
    'circleR': 0x00AE,                # REGISTERED SIGN
    'circleS': 0x24C8,                # CIRCLED LATIN CAPITAL LETTER S
    'circleasterisk': 0x229B,         # CIRCLED ASTERISK OPERATOR
    'circlecopyrt': 0x25CB,           # WHITE CIRCLE
    'circledivide': 0x2298,           # CIRCLED DIVISION SLASH
    'circledot': 0x2299,              # CIRCLED DOT OPERATOR
    'circleequal': 0x229C,            # CIRCLED EQUALS
    'circleminus': 0x2296,            # CIRCLED MINUS
    'circlering': 0x229A,             # CIRCLED RING OPERATOR
    'clockwise': 0x27F3,              # CLOCKWISE GAPPED CIRCLE ARROW
    'complement': 0x2201,             # COMPLEMENT
    'coproduct': 0x2A3F,              # AMALGAMATION OR COPRODUCT
    'curlyleft': 0x21AB,              # LEFTWARDS ARROW WITH LOOP
    'curlyright': 0x21AC,             # RIGHTWARDS ARROW WITH LOOP
    'daleth': 0x2138,                 # DALET SYMBOL
    'dbar': 0x0111,                   # LATIN SMALL LETTER D WITH STROKE
    'dblarrowdwn': 0x21CA,            # DOWNWARDS PAIRED ARROWS
    'dblarrowheadleft': 0x219E,       # LEFTWARDS TWO HEADED ARROW
    'dblarrowheadright': 0x21A0,      # RIGHTWARDS TWO HEADED ARROW
    'dblarrowup': 0x21C8,             # UPWARDS PAIRED ARROWS
    'dblbracketleft': 0x27E6,         # MATHEMATICAL LEFT WHITE SQUARE BRACKET
    'dblbracketright': 0x27E7,        # MATHEMATICAL RIGHT WHITE SQUARE BRACKET
    'defines': 0x225C,                # DELTA EQUAL TO
    'diamond': 0x2662,                # WHITE DIAMOND SUIT
    'diamondmath': 0x22C4,            # DIAMOND OPERATOR
    'diamondsolid': 0x2666,           # BLACK DIAMOND SUIT
    'difference': 0x224F,             # DIFFERENCE BETWEEN
    'dividemultiply': 0x22C7,         # DIVISION TIMES
    'dotlessj': 0x0237,               # LATIN SMALL LETTER DOTLESS J
    'dotplus': 0x2214,                # DOT PLUS
    'downfall': 0x22CE,               # CURLY LOGICAL OR
    'downslope': 0x29F9,              # BIG REVERSE SOLIDUS
    'epsilon1': 0x03F5,               # GREEK LUNATE EPSILON SYMBOL
    'epsiloninv': 0x03F6,             # GREEK REVERSED LUNATE EPSILON SYMBOL
    'equaldotleftright': 0x2252,      # APPROXIMATELY EQUAL TO OR THE IMAGE OF
    'equaldotrightleft': 0x2253,      # IMAGE OF OR APPROXIMATELY EQUAL TO
    'equalorfollows': 0x22DF,         # EQUAL TO OR SUCCEEDS
    'equalorgreater': 0x2A96,         # SLANTED EQUAL TO OR GREATER-THAN
    'equalorless': 0x2A95,            # SLANTED EQUAL TO OR LESS-THAN
    'equalorprecedes': 0x22DE,        # EQUAL TO OR PRECEDES
    'equalorsimilar': 0x2242,         # MINUS TILDE
    'equalsdots': 0x2251,             # GEOMETRICALLY EQUAL TO
    'equivasymptotic': 0x224D,        # EQUIVALENT TO
    'flat': 0x266D,                   # MUSIC FLAT SIGN
    'floorleft': 0x230A,              # LEFT FLOOR
    'floorright': 0x230B,             # RIGHT FLOOR
    'follownotdbleqv': 0x2ABA,        # SUCCEEDS ABOVE NOT ALMOST EQUAL TO
    'follownotslnteql': 0x2AB6,       # SUCCEEDS ABOVE NOT EQUAL TO
    'followornoteqvlnt': 0x22E9,      # SUCCEEDS BUT NOT EQUIVALENT TO
    'follows': 0x227B,                # SUCCEEDS
    'followsequal': 0x2AB0,           # SUCCEEDS ABOVE SINGLE-LINE EQUALS SIGN
    'followsorcurly': 0x227D,         # SUCCEEDS OR EQUAL TO
    'followsorequal': 0x227F,         # SUCCEEDS OR EQUIVALENT TO
    'forces': 0x22A9,                 # FORCES
    'forcesbar': 0x22AA,              # TRIPLE VERTICAL BAR RIGHT TURNSTILE
    'fork': 0x22D4,                   # PITCHFORK
    'frown': 0x2322,                  # FROWN
    'geomequivalent': 0x224E,         # GEOMETRICALLY EQUIVALENT TO
    'greaterdbleqlless': 0x2A8C,      # GREATER-THAN ABOVE DOUBLE-LINE EQUAL ABOVE LESS-THAN
    'greaterdblequal': 0x2267,        # GREATER-THAN OVER EQUAL TO
    'greaterdot': 0x22D7,             # GREATER-THAN WITH DOT
    'greaterlessequal': 0x22DB,       # GREATER-THAN EQUAL TO OR LESS-THAN
    'greatermuch': 0x226B,            # MUCH GREATER-THAN
    'greaternotdblequal': 0x2A8A,     # GREATER-THAN AND NOT APPROXIMATE
    'greaternotequal': 0x2A88,        # GREATER-THAN AND SINGLE-LINE NOT EQUAL TO
    'greaterorapproxeql': 0x2A86,     # GREATER-THAN OR APPROXIMATE
    'greaterorequalslant': 0x2A7E,    # GREATER-THAN OR SLANTED EQUAL TO
    'greaterornotdbleql': 0x2269,     # GREATER-THAN BUT NOT EQUAL TO
    'greaterornotequal': 0x2269,      # GREATER-THAN BUT NOT EQUAL TO
    'greaterornotsimilar': 0x22E7,    # GREATER-THAN BUT NOT EQUIVALENT TO
    'greaterorsimilar': 0x2273,       # GREATER-THAN OR EQUIVALENT TO
    'harpoondownleft': 0x21C3,        # DOWNWARDS HARPOON WITH BARB LEFTWARDS
    'harpoondownright': 0x21C2,       # DOWNWARDS HARPOON WITH BARB RIGHTWARDS
    'harpoonleftright': 0x21CC,       # RIGHTWARDS HARPOON OVER LEFTWARDS HARPOON
    'harpoonrightleft': 0x21CB,       # LEFTWARDS HARPOON OVER RIGHTWARDS HARPOON
    'harpoonupleft': 0x21BF,          # UPWARDS HARPOON WITH BARB LEFTWARDS
    'harpoonupright': 0x21BE,         # UPWARDS HARPOON WITH BARB RIGHTWARDS
    'hat': 0x02C6,                    # MODIFIER LETTER CIRCUMFLEX ACCENT
    'heart': 0x2661,                  # WHITE HEART SUIT
    'hyphenchar': 0x002D,             # HYPHEN-MINUS
    'integerdivide': 0x2216,          # SET MINUS
    'intercal': 0x22BA,               # INTERCALATE
    'interleave': 0x2AF4,             # TRIPLE VERTICAL BAR BINARY RELATION
    'interrobang': 0x203D,            # INTERROBANG
    'interrobangdown': 0x2E18,        # INVERTED INTERROBANG
    'intersectiondbl': 0x22D2,        # DOUBLE INTERSECTION
    'intersectionsq': 0x2293,         # SQUARE CAP
    'latticetop': 0x22A4,             # DOWN TACK
    'leftarrowtriangle': 0x21FD,      # LEFTWARDS OPEN-HEADED ARROW
    'leftrightarrowtriangle': 0x21FF, # LEFT RIGHT OPEN-HEADED ARROW
    'leftslice': 0x2AA6,              # LESS-THAN CLOSED BY CURVE
    'lessdbleqlgreater': 0x2A8B,      # LESS-THAN ABOVE DOUBLE-LINE EQUAL ABOVE GREATER-THAN
    'lessdblequal': 0x2266,           # LESS-THAN OVER EQUAL TO
    'lessdot': 0x22D6,                # LESS-THAN WITH DOT
    'lessequalgreater': 0x22DA,       # LESS-THAN EQUAL TO OR GREATER-THAN
    'lessmuch': 0x226A,               # MUCH LESS-THAN
    'lessnotdblequal': 0x2A89,        # LESS-THAN AND NOT APPROXIMATE
    'lessnotequal': 0x2A87,           # LESS-THAN AND SINGLE-LINE NOT EQUAL TO
    'lessorapproxeql': 0x2A85,        # LESS-THAN OR APPROXIMATE
    'lessorequalslant': 0x2A7D,       # LESS-THAN OR SLANTED EQUAL TO
    'lessornotdbleql': 0x2268,        # LESS-THAN BUT NOT EQUAL TO
    'lessornotequal': 0x2268,         # LESS-THAN BUT NOT EQUAL TO
    'lessornotsimilar': 0x22E6,       # LESS-THAN BUT NOT EQUIVALENT TO
    'lessorsimilar': 0x2272,          # LESS-THAN OR EQUIVALENT TO
    'lightning': 0x21AF,              # DOWNWARDS ZIGZAG ARROW
    'llbracket': 0x27E6,              # MATHEMATICAL LEFT WHITE SQUARE BRACKET
    'llparenthesis': 0x2985,          # LEFT WHITE PARENTHESIS
    'longst': 0xFB05,                 # LATIN SMALL LIGATURE LONG S T
    'lscript': 0x2113,                # SCRIPT SMALL L
    'maltesecross': 0x2720,           # MALTESE CROSS
    'mapsto': 0x21A6,                 # RIGHTWARDS ARROW FROM BAR
    'measuredangle': 0x2221,          # MEASURED ANGLE
    'multicloseleft': 0x22C9,         # LEFT NORMAL FACTOR SEMIDIRECT PRODUCT
    'multicloseright': 0x22CA,        # RIGHT NORMAL FACTOR SEMIDIRECT PRODUCT
    'multimap': 0x22B8,               # MULTIMAP
    'multiopenleft': 0x22CB,          # LEFT SEMIDIRECT PRODUCT
    'multiopenright': 0x22CC,         # RIGHT SEMIDIRECT PRODUCT
    'nand': 0x22BC,                   # NAND
    'natural': 0x266E,                # MUSIC NATURAL SIGN
    'ng': 0x014B,                     # LATIN SMALL LETTER ENG
    'notapproxequal': 0x2247,         # NEITHER APPROXIMATELY NOR ACTUALLY EQUAL TO
    'notarrowboth': 0x21AE,           # LEFT RIGHT ARROW WITH STROKE
    'notarrowleft': 0x219A,           # LEFTWARDS ARROW WITH STROKE
    'notarrowright': 0x219B,          # RIGHTWARDS ARROW WITH STROKE
    'notbar': 0x2224,                 # DOES NOT DIVIDE
    'notdblarrowboth': 0x21CE,        # LEFT RIGHT DOUBLE ARROW WITH STROKE
    'notdblarrowleft': 0x21CD,        # LEFTWARDS DOUBLE ARROW WITH STROKE
    'notdblarrowright': 0x21CF,       # RIGHTWARDS DOUBLE ARROW WITH STROKE
    'notexistential': 0x2204,         # THERE DOES NOT EXIST
    'notfollows': 0x2281,             # DOES NOT SUCCEED
    'notfollowsoreql': 0x22E1,        # DOES NOT SUCCEED OR EQUAL
    'notforces': 0x22AE,              # DOES NOT FORCE
    'notforcesextra': 0x22AF,         # NEGATED DOUBLE VERTICAL BAR DOUBLE RIGHT TURNSTILE
    'notgreaterequal': 0x2271,        # NEITHER GREATER-THAN NOR EQUAL TO
    'notlessequal': 0x2270,           # NEITHER LESS-THAN NOR EQUAL TO
    'notprecedesoreql': 0x22E0,       # DOES NOT PRECEDE OR EQUAL
    'notsatisfies': 0x22AD,           # NOT TRUE
    'notshortbar': 0x2224,            # DOES NOT DIVIDE
    'notshortparallel': 0x2226,       # NOT PARALLEL TO
    'notsimilar': 0x2241,             # NOT TILDE
    'notsubseteql': 0x2288,           # NEITHER A SUBSET OF NOR EQUAL TO
    'notsubsetoreql': 0x228A,         # SUBSET OF WITH NOT EQUAL TO
    'notsuperseteql': 0x2289,         # NEITHER A SUPERSET OF NOR EQUAL TO
    'notsupersetoreql': 0x228B,       # SUPERSET OF WITH NOT EQUAL TO
    'nottriangeqlleft': 0x22EC,       # NOT NORMAL SUBGROUP OF OR EQUAL TO
    'nottriangeqlright': 0x22ED,      # DOES NOT CONTAIN AS NORMAL SUBGROUP OR EQUAL
    'nottriangleleft': 0x22EA,        # NOT NORMAL SUBGROUP OF
    'nottriangleright': 0x22EB,       # DOES NOT CONTAIN AS NORMAL SUBGROUP
    'notturnstile': 0x22AC,           # DOES NOT PROVE
    'ntrianglelefteqslant': 0x22EC,   # NOT NORMAL SUBGROUP OF OR EQUAL TO
    'ntrianglerighteqslant': 0x22ED,  # DOES NOT CONTAIN AS NORMAL SUBGROUP OR EQUAL
    'obar': 0x29B6,                   # CIRCLED VERTICAL BAR
    'obslash': 0x29B8,                # CIRCLED REVERSE SOLIDUS
    'ogreaterthan': 0x29C1,           # CIRCLED GREATER-THAN
    'olessthan': 0x29C0,              # CIRCLED LESS-THAN
    'orunderscore': 0x22BB,           # XOR
    'owner': 0x220B,                  # CONTAINS AS MEMBER
    'parallelshort': 0x2225,          # PARALLEL TO
    'parenleftbt': 0x239D,            # LEFT PARENTHESIS LOWER HOOK
    'parenleftex': 0x239C,            # LEFT PARENTHESIS EXTENSION
    'parenlefttp': 0x239B,            # LEFT PARENTHESIS UPPER HOOK
    'parenrightbt': 0x23A0,           # RIGHT PARENTHESIS LOWER HOOK
    'parenrightex': 0x239F,           # RIGHT PARENTHESIS EXTENSION
    'parenrighttp': 0x239E,           # RIGHT PARENTHESIS UPPER HOOK
    'perpcorrespond': 0x2A5E,         # LOGICAL AND WITH DOUBLE OVERBAR
    'pertenthousand': 0x2031,         # PER TEN THOUSAND SIGN
    'phi': 0x03D5,                    # GREEK PHI SYMBOL
    'phi1': 0x03C6,                   # GREEK SMALL LETTER PHI
    'pi1': 0x03D6,                    # GREEK PI SYMBOL
    'planckover2pi': 0x210F,          # PLANCK CONSTANT OVER TWO PI
    'planckover2pi1': 0x210F,         # PLANCK CONSTANT OVER TWO PI
    'precedenotdbleqv': 0x2AB9,       # PRECEDES ABOVE NOT ALMOST EQUAL TO
    'precedenotslnteql': 0x2AB5,      # PRECEDES ABOVE NOT EQUAL TO
    'precedeornoteqvlnt': 0x22E8,     # PRECEDES BUT NOT EQUIVALENT TO
    'precedesequal': 0x2AAF,          # PRECEDES ABOVE SINGLE-LINE EQUALS SIGN
    'precedesorcurly': 0x227C,        # PRECEDES OR EQUAL TO
    'precedesorequal': 0x227E,        # PRECEDES OR EQUIVALENT TO
    'prime': 0x2032,                  # PRIME
    'primereverse': 0x2035,           # REVERSED PRIME
    'punctdash': 0x2014,              # EM DASH
    'radicalbt': 0x23B7,              # RADICAL SYMBOL BOTTOM
    'rangedash': 0x2013,              # EN DASH
    'revasymptequal': 0x22CD,         # REVERSED TILDE EQUALS
    'revsimilar': 0x223D,             # REVERSED TILDE
    'rho1': 0x03F1,                   # GREEK RHO SYMBOL
    'rightanglene': 0x231D,           # TOP RIGHT CORNER
    'rightanglenw': 0x231C,           # TOP LEFT CORNER
    'rightanglese': 0x231F,           # BOTTOM RIGHT CORNER
    'rightanglesw': 0x231E,           # BOTTOM LEFT CORNER
    'rightarrowtriangle': 0x21FE,     # RIGHTWARDS OPEN-HEADED ARROW
    'rightslice': 0x2AA7,             # GREATER-THAN CLOSED BY CURVE
    'ringinequal': 0x2256,            # RING IN EQUAL TO
    'rrbracket': 0x27E7,              # MATHEMATICAL RIGHT WHITE SQUARE BRACKET
    'rrparenthesis': 0x2986,          # RIGHT WHITE PARENTHESIS
    'satisfies': 0x22A8,              # TRUE
    'sharp': 0x266F,                  # MUSIC SHARP SIGN
    'shiftleft': 0x21B0,              # UPWARDS ARROW WITH TIP LEFTWARDS
    'shiftright': 0x21B1,             # UPWARDS ARROW WITH TIP RIGHTWARDS
    'shortdownarrow': 0x2193,         # DOWNWARDS ARROW
    'shortleftarrow': 0x2190,         # LEFTWARDS ARROW
    'shortrightarrow': 0x2192,        # RIGHTWARDS ARROW
    'shortuparrow': 0x2191,           # UPWARDS ARROW
    'similarequal': 0x2243,           # ASYMPTOTICALLY EQUAL TO
    'slurabove': 0x2322,              # FROWN
    'slurbelow': 0x2323,              # SMILE
    'smile': 0x2323,                  # SMILE
    'sphericalangle': 0x2222,         # SPHERICAL ANGLE
    'square': 0x25A1,                 # WHITE SQUARE
    'squaredot': 0x22A1,              # SQUARED DOT OPERATOR
    'squareimage': 0x228F,            # SQUARE IMAGE OF
    'squareminus': 0x229F,            # SQUARED MINUS
    'squaremultiply': 0x22A0,         # SQUARED TIMES
    'squareoriginal': 0x2290,         # SQUARE ORIGINAL OF
    'squareplus': 0x229E,             # SQUARED PLUS
    'squaresmallsolid': 0x25AA,       # BLACK SMALL SQUARE
    'squaresolid': 0x25A0,            # BLACK SQUARE
    'squiggleleftright': 0x21AD,      # LEFT RIGHT WAVE ARROW
    'squiggleright': 0x21DD,          # RIGHTWARDS SQUIGGLE ARROW
    'sslash': 0x2AFD,                 # DOUBLE SOLIDUS OPERATOR
    'st': 0xFB06,                     # LATIN SMALL LIGATURE ST
    'star': 0x22C6,                   # STAR OPERATOR
    'subsetdbl': 0x22D0,              # DOUBLE SUBSET
    'subsetdblequal': 0x2AC5,         # SUBSET OF ABOVE EQUALS SIGN
    'subsetnoteql': 0x228A,           # SUBSET OF WITH NOT EQUAL TO
    'subsetornotdbleql': 0x2ACB,      # SUBSET OF ABOVE NOT EQUAL TO
    'subsetplus': 0x2ABF,             # SUBSET WITH PLUS SIGN BELOW
    'subsetsqequal': 0x2291,          # SQUARE IMAGE OF OR EQUAL TO
    'supersetdbl': 0x22D1,            # DOUBLE SUPERSET
    'supersetdblequal': 0x2AC6,       # SUPERSET OF ABOVE EQUALS SIGN
    'supersetnoteql': 0x228B,         # SUPERSET OF WITH NOT EQUAL TO
    'supersetornotdbleql': 0x2ACC,    # SUPERSET OF ABOVE NOT EQUAL TO
    'supersetsqequal': 0x2292,        # SQUARE ORIGINAL OF OR EQUAL TO
    'supsetplus': 0x2AC0,             # SUPERSET WITH PLUS SIGN BELOW
    'talloblong': 0x2AFE,             # WHITE VERTICAL BAR
    'tie': 0x2040,                    # CHARACTER TIE
    'triangle': 0x25B3,               # WHITE UP-POINTING TRIANGLE
    'triangledownsld': 0x25BC,        # BLACK DOWN-POINTING TRIANGLE
    'triangleinv': 0x25BD,            # WHITE DOWN-POINTING TRIANGLE
    'triangleleft': 0x25C1,           # WHITE LEFT-POINTING TRIANGLE
    'trianglelefteqslant': 0x22B4,    # NORMAL SUBGROUP OF OR EQUAL TO
    'triangleleftequal': 0x22B4,      # NORMAL SUBGROUP OF OR EQUAL TO
    'triangleleftsld': 0x25C0,        # BLACK LEFT-POINTING TRIANGLE
    'triangleright': 0x25B7,          # WHITE RIGHT-POINTING TRIANGLE
    'trianglerighteqslant': 0x22B5,   # CONTAINS AS NORMAL SUBGROUP OR EQUAL TO
    'trianglerightequal': 0x22B5,     # CONTAINS AS NORMAL SUBGROUP OR EQUAL TO
    'trianglerightsld': 0x25B6,       # BLACK RIGHT-POINTING TRIANGLE
    'trianglesolid': 0x25B2,          # BLACK UP-POINTING TRIANGLE
    'turnstileleft': 0x22A2,          # RIGHT TACK
    'turnstileright': 0x22A3,         # LEFT TACK
    'uniondbl': 0x22D3,               # DOUBLE UNION
    'unionmulti': 0x228E,             # MULTISET UNION
    'unionsq': 0x2294,                # SQUARE CUP
    'uprise': 0x22CF,                 # CURLY LOGICAL AND
    'upslope': 0x29F8,                # BIG SOLIDUS
    'vextenddouble': 0x2225,          # PARALLEL TO
    'vextendsingle': 0x2223,          # DIVIDES
    'visiblespace': 0x2423,           # OPEN BOX
    'visualspace': 0x2423,            # OPEN BOX
    'wreathproduct': 0x2240,          # WREATH PRODUCT
}

# The n-ary operators. cmex10 draws each in an inline ('…text') and a display
# ('…display') size, under a name whose stem is the *binary* operator: the stem
# of 'uniontext' is 'union' (U+222A UNION), but the character drawn is U+22C3
# N-ARY UNION. Resolving the stem through the tables above would quietly hand
# every big operator its small cousin's codepoint, so the stems are listed here
# with the n-ary meaning and this table is consulted only for the two size
# suffixes.
_NARY_OPERATOR: dict[str, int] = {
    'summation': 0x2211,        # N-ARY SUMMATION
    'product': 0x220F,          # N-ARY PRODUCT
    'coproduct': 0x2210,        # N-ARY COPRODUCT  (the stem alone is \amalg)
    'integral': 0x222B,         # INTEGRAL
    'contintegral': 0x222E,     # CONTOUR INTEGRAL
    'union': 0x22C3,            # N-ARY UNION
    'intersection': 0x22C2,     # N-ARY INTERSECTION
    'unionmulti': 0x2A04,       # N-ARY UNION OPERATOR WITH PLUS
    'unionsq': 0x2A06,          # N-ARY SQUARE UNION OPERATOR
    'logicaland': 0x22C0,       # N-ARY LOGICAL AND
    'logicalor': 0x22C1,        # N-ARY LOGICAL OR
    'circleplus': 0x2A01,       # N-ARY CIRCLED PLUS OPERATOR
    'circlemultiply': 0x2A02,   # N-ARY CIRCLED TIMES OPERATOR
    'circledot': 0x2A00,        # N-ARY CIRCLED DOT OPERATOR
}

# Name suffixes that mark a *size* of some other glyph. Unicode has one
# character per delimiter and per operator, whatever the size TeX drew, so all
# of these resolve to their stem's codepoint and the collision rule in
# _addressing decides which slot actually gets it (the smallest, since it sits
# at the lowest slot in every TeX math encoding).
_VARIANT_SUFFIXES = ('Bigg', 'bigg', 'Big', 'big',    # \Bigg \bigg \Big \big
                     'display', 'text',               # the two operator sizes
                     'oldstyle',                      # zerooldstyle → '0'
                     'widest', 'wider', 'wide',       # hatwide, tildewidest, …
                     'small')                         # Asmall → 'a' (see below)


def available() -> bool:
    return _AVAILABLE


def pua_of(slot: int) -> int:
    return PUA_BASE + slot


def _variant_codepoint(name: str) -> int | None:
    """The codepoint of the glyph `name` is a size variant of, or None.

    'parenleftBigg' → '(' ; 'summationdisplay' → the n-ary sum ; 'sixoldstyle'
    → '6' ; 'tildewidest' → the small tilde. Stripping strictly shortens the
    name, so the recursion through _name_codepoint terminates.

    'small' is the odd one out: a small-capital 'A' is not the character 'A' but
    the character 'a' — a text face's lowercase set, drawn as capitals — which
    is what pdfTeX's own ToUnicode maps it to, and what makes \textsc copy back
    as the word the author typed.
    """
    for suffix in _VARIANT_SUFFIXES:
        if not name.endswith(suffix) or len(name) == len(suffix):
            continue
        stem = name[:-len(suffix)]
        cp = _NARY_OPERATOR.get(stem) if suffix in ('display', 'text') else None
        if cp is None:
            cp = _name_codepoint(stem)
        if cp is not None and suffix == 'small':
            cp = ord(chr(cp).lower()) if chr(cp).isupper() else cp
        if cp is not None:
            return cp
    return None


def _name_codepoint(name: str) -> int | None:
    """The Unicode codepoint a PostScript glyph name stands for, or None when
    the name has no standard meaning (or only an unusable one — see
    _acceptable_cp)."""
    cp = _TEX_GLYPH_UNICODE.get(name)
    if cp is not None and _acceptable_cp(cp):
        return cp
    uni = agl.toUnicode(name)                   # '' when the name is unknown
    cp = ord(uni) if len(uni) == 1 else None    # skip ligatures (multi-char)
    if cp is not None and _acceptable_cp(cp):
        return cp
    # An unusable AGL answer is not the end of it: the AGL sends the oldstyle
    # digits into Adobe's corporate-use block ('zerooldstyle' → U+F730), and
    # they are perfectly good digits once the variant rule strips the suffix.
    cp = _variant_codepoint(name)
    return cp if cp is not None and _acceptable_cp(cp) else None


def _claim_rank(name: str) -> int:
    """Claim order within a font: 0 normally, 1 for a name that should yield.

    Only the inline size of an n-ary operator yields. cmex10 carries both sizes
    of every big operator, one codepoint has to go to one of them, and the
    inline slot is the lower one — so plain slot order would hand U+2211 to
    'summationtext' and leave the sum of every *displayed* equation on a
    private code. A big operator is overwhelmingly seen in a display, so the
    display size is served and the inline one keeps the PUA.
    """
    if name.endswith('text') and name[:-4] in _NARY_OPERATOR:
        return 1
    return 0


def _addressing(slots: dict[int, str]) -> dict[int, int]:
    """Pick a target codepoint for every source slot: {slot -> codepoint}.

    A slot whose glyph *name* has a standard Unicode meaning maps to that real
    codepoint, so the text is selectable and Ctrl-F finds it; everything else
    keeps PUA_BASE + slot. Keying on the name, not the slot, sidesteps the
    classic TeX encodings' quirks (cmr's slot 0x3C is 'exclamdown', and gets
    U+00A1, not '<').

    Real codepoints are claimed first-come (slots ascending), so the cmap stays
    a function — a later slot that would want an already-taken codepoint falls
    back to the PUA. That is what keeps the delimiter sizes honest: cmex10's
    four \\big variants of '(' all resolve to U+0028, the smallest one holds the
    lowest slot and takes it, and the other three keep their private codes.
    Every classic encoding also aliases some glyphs into the high half of the
    vector (cmsy10 names slot 0xA1 'minus' as well as slot 0x00); the alias
    loses the same way. _claim_rank names the one family where slot order is
    the wrong tie-break. PUA codes (0xE000+) are excluded from every real
    target by _acceptable_cp, so the two schemes cannot collide.
    """
    addressing: dict[int, int] = {}
    claimed: set[int] = set()
    for slot in sorted(slots, key=lambda s: (_claim_rank(slots[s]), s)):
        cp = _name_codepoint(slots[slot])
        if cp is not None and cp not in claimed:
            addressing[slot] = cp
            claimed.add(cp)
        else:
            addressing[slot] = pua_of(slot)
    return addressing


def _kpsewhich(filename: str) -> str | None:
    """kpsewhich's answer for one file, or None — not found, or no kpsewhich
    on this host at all (the lax encode host has no TeX tree)."""
    try:
        r = subprocess.run(['kpsewhich', filename], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout.strip() or None


_MAP_CACHE: dict[str, str] | None = None


def _map_entry(name: str) -> str | None:
    """The pdftex.map line for a TeX font name, or None. The map is located
    with kpsewhich and read once per process; the first line per name wins,
    as it does for the engines."""
    global _MAP_CACHE
    if _MAP_CACHE is None:
        _MAP_CACHE = {}
        path = _kpsewhich('pdftex.map')
        if path:
            try:
                with open(path, encoding='latin-1') as fh:
                    for line in fh:
                        if not line.strip() or line[0] in '%#':
                            continue
                        _MAP_CACHE.setdefault(line.split(None, 1)[0], line.rstrip('\n'))
            except OSError:
                pass
    return _MAP_CACHE.get(name)


def _map_line_files(line: str) -> tuple[str | None, str | None]:
    """The (outline, encoding vector) file names a pdftex.map line references
    through its `<file` tokens (`<<file` and `<[file` carry dvips inclusion
    hints and name the same files); None for a leg the line does not have."""
    pfb = enc = None
    for token in line.split():
        if not token.startswith('<'):
            continue
        filename = token.lstrip('<')
        if filename.startswith('['):
            filename = filename[1:]
        if filename.endswith('.enc'):
            enc = filename
        elif filename.endswith(('.pfb', '.pfa')):
            pfb = filename
    return pfb, enc


def find_outline(name: str) -> tuple[str, str | None] | None:
    """Locate a font's Type1 outline and, when the TeX side re-encodes it,
    the encoding vector it is used through: (pfb path, enc path or None), or
    None when the face has no outline to convert.

    (lax) With REFLOWTEX_PFB_DIR set (read per call), that directory is the
    *only* source — `<name>.pfb`, plus `<name>.enc` beside it when the
    exporter resolved a re-encoding. The lax trusted path exports both from
    its pinned TeX image under the TeX name, because the encode host has no
    TeX tree, and a host tree must never silently substitute for a missed
    export. Otherwise the font's pdftex.map entry decides, exactly as it
    does for pdfTeX and LuaTeX: `ec-lmr10` is `lmr10.pfb` re-encoded through
    `lm-ec.enc`, and reading the outline's built-in encoding instead would
    address every slot above ASCII wrongly — so a re-encoded face whose
    vector cannot be found yields None (metric boxes, never wrong glyphs).
    A name without a map entry falls back to `<name>.pfb` as stock. A host
    without kpsewhich yields None (the caller keeps the metric-box
    fallback), never an uncaught crash.
    """
    local = os.environ.get('REFLOWTEX_PFB_DIR')
    if local:
        pfb = Path(local) / f'{name}.pfb'
        if not pfb.is_file():
            return None
        enc = Path(local) / f'{name}.enc'
        return str(pfb), (str(enc) if enc.is_file() else None)
    line = _map_entry(name)
    if line is not None:
        pfb_name, enc_name = _map_line_files(line)
        pfb = _kpsewhich(pfb_name) if pfb_name else None
        if pfb:
            if enc_name is None:
                return pfb, None
            enc = _kpsewhich(enc_name)
            return (pfb, enc) if enc else None
    pfb = _kpsewhich(f'{name}.pfb')
    return (pfb, None) if pfb else None


def find_pfb(name: str) -> str | None:
    """The outline path alone — see find_outline."""
    found = find_outline(name)
    return found[0] if found else None


def parse_enc(path: str) -> list[str]:
    """The 256 glyph names of a dvips/pdftex `.enc` vector — `/Name [ /g0 …
    /g255 ] def`, `%` comments stripped — in slot order."""
    body = []
    for line in Path(path).read_text(encoding='latin-1').splitlines():
        cut = line.find('%')
        body.append(line if cut < 0 else line[:cut])
    joined = '\n'.join(body)
    start, end = joined.find('['), joined.rfind(']')
    if start < 0 or end < start:
        raise ValueError(f'{path}: no encoding vector')
    names = [token[1:] for token in joined[start + 1:end].split() if token.startswith('/')]
    if len(names) != 256:
        raise ValueError(f'{path}: {len(names)} encoding entries, not 256')
    return names


def convert(name: str, out_dir: Path) -> tuple[str, dict[int, int]] | None:
    """Convert <name>'s outline → a content-hashed OTF in out_dir.

    Returns (served_filename, {source slot -> target codepoint}) or None when the
    font cannot be converted — it has no Type1 outline (a genuinely bitmap-only
    face) or fontTools' Type1 parser rejects it. Either way the caller keeps the
    metric-box fallback for that font, so one bad font never fails a build.
    """
    if not _AVAILABLE:
        return None
    try:
        composition = find_composition(name)
        if composition:
            return _compose(composition, name, Path(out_dir))
        found = find_outline(name)
        if not found:
            return None
        pfb, enc = found
        return _convert(pfb, name, Path(out_dir), parse_enc(enc) if enc else None)
    except Exception as e:                                  # noqa: BLE001
        print(f'  t1-convert: {name} skipped ({type(e).__name__}: {e}) — keeps metric boxes')
        return None


def find_composition(name: str) -> Path | None:
    """(lax) The composition the lax exporter wrote for a virtual font, when
    there is one. See _compose."""
    local = os.environ.get('REFLOWTEX_PFB_DIR')
    if not local:
        return None
    path = Path(local) / f'{name}.compose.json'
    return path if path.is_file() else None


def _source(name: str, directory: Path):
    """(lax) One real font a virtual font draws from: its glyphs, its
    code -> glyph name vector, its scale to 1000 units per em."""
    t1 = t1Lib.T1Font(str(directory / f'{name}.pfb'), encoding='latin-1')
    t1.parse()
    enc = directory / f'{name}.enc'
    encoding = parse_enc(str(enc)) if enc.is_file() else t1.font['Encoding']
    if encoding == 'StandardEncoding':
        encoding = StandardEncoding
    return t1.getGlyphSet(), encoding, 1000 * t1.font['FontMatrix'][0]


def _compose(composition: Path, name: str, out_dir: Path) -> tuple[str, dict[int, int]]:
    """(lax) Build one OTF for a virtual font from the real fonts it maps in.

    A virtual font is a program per slot, and mathpazo's zplmr7m shows why no
    single outline can stand in for one: its alpha is `a` in PazoMath-Italic,
    its Latin comes from Palatino, its remaining Greek from cmmi10 scaled to
    1.042. The exporter has read the program into `{"sources": [{"name",
    "at"}], "slots": {"<slot>": [source index, code, dx, dy]}}`, offsets in
    design-size units, and each slot is drawn from its own source. A slot
    whose code names no glyph is left out and keeps its metric box.
    """
    spec = json.loads(composition.read_text(encoding='utf-8'))
    directory = composition.parent
    sources = [_source(source['name'], directory) for source in spec['sources']]
    upm = 1000
    named: dict[int, str] = {}
    charstrings, metrics = {}, {}
    pen = T2CharStringPen(0, None)
    charstrings['.notdef'], metrics['.notdef'] = pen.getCharString(), (0, 0)
    for key, (index, code, dx, dy) in spec['slots'].items():
        slot = int(key)
        glyphs, encoding, unit = sources[index]
        gname = encoding[code]
        if gname == '.notdef' or gname not in glyphs:
            continue
        scale = unit * spec['sources'][index]['at']
        pen = T2CharStringPen(0, None)
        glyph = glyphs[gname]
        glyph.draw(TransformPen(pen, (scale, 0, 0, scale, dx * upm, dy * upm)))
        internal = f'slot{slot}'
        named[slot] = gname
        charstrings[internal] = pen.getCharString()
        metrics[internal] = (round((getattr(glyph, 'width', 0) or 0) * scale), 0)
    if not named:
        raise ValueError('no slot resolved to a glyph')
    addressing = _addressing(named)
    order = ['.notdef'] + [f'slot{slot}' for slot in sorted(named)]
    cmap = {addressing[slot]: f'slot{slot}' for slot in named}
    return _emit(name, out_dir, upm, order, cmap, charstrings, metrics), addressing


def _convert(pfb: str, name: str, out_dir: Path,
             encoding: list[str] | None = None) -> tuple[str, dict[int, int]]:
    # latin-1, not the default ascii: some faces (e.g. eurosym's fey*) carry a
    # non-ASCII byte in their PostScript header that the ascii decoder rejects.
    t1 = t1Lib.T1Font(pfb, encoding='latin-1')
    t1.parse()
    if encoding is None:
        encoding = t1.font['Encoding']         # 256 entries: slot -> glyph name
    glyphs = t1.getGlyphSet()
    upm = round(1 / t1.font['FontMatrix'][0])  # units per em (1000 for CM/AMS)

    # slot -> glyph name, keeping only slots that name a glyph the font actually
    # has (the encoding vector pads unused slots with '.notdef').
    slots = {i: gname for i, gname in enumerate(encoding)
             if gname and gname != '.notdef' and gname in glyphs}
    # slot -> target codepoint (real Unicode where common & searchable, else PUA).
    addressing = _addressing(slots)
    order = ['.notdef'] + sorted(set(slots.values()))

    charstrings, metrics = {}, {}
    for gname in order:
        pen = T2CharStringPen(0, None)
        advance = 0
        if gname in glyphs:
            g = glyphs[gname]
            g.draw(pen)                        # Type1 charstring -> Type2 (CFF)
            advance = int(getattr(g, 'width', 0) or 0)
        charstrings[gname] = pen.getCharString()
        metrics[gname] = (advance, 0)          # (advance, lsb); lsb unused here

    return _emit(name, out_dir, upm, order, {addressing[s]: gname for s, gname in slots.items()},
                 charstrings, metrics), addressing


def _emit(name: str, out_dir: Path, upm: int, order: list[str], cmap: dict[int, str],
          charstrings: dict, metrics: dict) -> str:
    """Write the OTF under a content-hashed name, and return that name."""
    ps = f'{name}-ReflowTeXConverted'          # PostScript name: no spaces
    label = f'{name} (ReflowTeX converted)'
    fb = FontBuilder(upm, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupCFF(ps, {'FullName': label}, charstrings, {})
    fb.setupHorizontalMetrics(metrics)
    # The viewer positions every glyph explicitly from the node's own metrics, so
    # these vertical metrics are not load-bearing; sane values keep the face valid.
    fb.setupHorizontalHeader(ascent=upm, descent=0)
    fb.setupNameTable({'familyName': label, 'styleName': 'Regular', 'psName': ps})
    fb.setupOS2(sTypoAscender=upm, sTypoDescender=0, usWinAscent=upm, usWinDescent=0)
    fb.setupPost()
    # Fixed head timestamps → byte-reproducible output → stable content hash, so an
    # unchanged font keeps the same served filename across rebuilds. A plausible
    # 1904-epoch value (not 0) keeps fontTools from warning it looks like a low date.
    fb.font['head'].created = fb.font['head'].modified = 3_153_600_000

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f'.{name}.building.otf'
    fb.save(str(tmp))
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()[:8]
    served = f'{name}.reflowtex-{digest}.otf'
    tmp.replace(out_dir / served)
    return served
