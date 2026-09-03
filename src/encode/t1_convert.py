#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert a classic Type1 (.pfb) TeX font to a web OTF, addressed via PUA.

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

  * If the slot's glyph *name* resolves through the Adobe Glyph List to a single
    common, searchable character (an ASCII/Latin/Greek letter, a digit, ordinary
    punctuation), the real Unicode codepoint is used — so the text is selectable
    and Ctrl-F finds it. Keying on the name, not the slot, sidesteps the classic
    TeX encodings' quirks (cmr's slot 0x3C is 'exclamdown', and gets U+00A1, not
    '<').
  * Everything else — math symbols, ligatures, CM-specific glyphs with no standard
    name — keeps a private-use code, PUA_BASE + slot: collision-free, but not
    searchable. This is the same idea the rest of the pipeline uses for awkward
    glyphs (see transforms.PUA_BASE).

Because every converted font is served as its own @font-face, PUA_BASE+slot never
collides across fonts, and the per-glyph explicit positioning in the viewer means
the browser never ligates or kerns adjacent real-Unicode glyphs (the converted
faces carry no GSUB/kern anyway), so switching a subset to real codepoints cannot
move any ink.

A BMP private-use base is used (U+E000, not the Plane-16 base the glyph-index
normaliser uses) so a font's 256 slots fit in E000..E0FF and a plain format-4
cmap suffices — the converted font is one we build from scratch, so there is no
real glyph at those codepoints to collide with.

fontTools is required (t1Lib for parsing, fontBuilder for emitting). Without it,
convert() is unavailable and the caller keeps the metric-box fallback.
"""
import hashlib
import os
import subprocess
from pathlib import Path

try:
    from fontTools import t1Lib, agl
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    _AVAILABLE = True
except ImportError:                                        # pragma: no cover
    _AVAILABLE = False

# BMP private-use area: a classic font has at most 256 slots, which fit in
# E000..E0FF and keep the converted cmap a plain format-4 table.
PUA_BASE = 0xE000

# Codepoints common and unambiguous enough to be worth making searchable: a slot
# whose AGL name lands here gets its real Unicode value, everything else stays in
# the PUA. Ranges are deliberately conservative — letters, digits, ordinary
# punctuation and Greek — and exclude combining marks, controls, and the private
# use area itself, none of which behave well as standalone SVG text.
_SEARCHABLE_RANGES = (
    (0x0020, 0x007E),   # ASCII: space, digits, letters, punctuation
    (0x00A1, 0x00FF),   # Latin-1: accented letters, ¡ ¿ × ÷, …
    (0x0100, 0x017F),   # Latin Extended-A: more accented letters
    (0x0391, 0x03A9),   # Greek capitals
    (0x03B1, 0x03C9),   # Greek smalls
)
# A few General-Punctuation characters TeX text leans on heavily.
_SEARCHABLE_SINGLES = frozenset({
    0x2013, 0x2014,                     # – —  (en/em dash)
    0x2018, 0x2019, 0x201C, 0x201D,     # ' ' " "  (curly quotes)
    0x2020, 0x2021, 0x2026, 0x2122,     # † ‡ …  ™
    # (lax) The f-ligature presentation forms: every classic text face
    # carries ff/fi/fl/ffi/ffl as single glyphs whose AGL names resolve
    # here. NFKC folds them back to their letters, so browser search and a
    # text comparison both read "fi" — a PUA code would split the word.
    0xFB00, 0xFB01, 0xFB02, 0xFB03, 0xFB04,
})


def available() -> bool:
    return _AVAILABLE


def pua_of(slot: int) -> int:
    return PUA_BASE + slot


def _searchable_cp(cp: int) -> bool:
    return (cp in _SEARCHABLE_SINGLES
            or any(lo <= cp <= hi for lo, hi in _SEARCHABLE_RANGES))


def _addressing(slots: dict[int, str]) -> dict[int, int]:
    """Pick a target codepoint for every source slot: {slot -> codepoint}.

    A slot whose glyph name resolves (via the Adobe Glyph List) to one common,
    searchable character maps to that real codepoint; everything else keeps
    PUA_BASE + slot. Real codepoints are claimed first-come (slots ascending), so
    the cmap stays a function — a later slot that would want an already-taken
    codepoint falls back to the PUA. PUA codes (0xE000+) never overlap the real
    ranges (all < 0x2200), so the two schemes cannot collide.
    """
    addressing: dict[int, int] = {}
    claimed: set[int] = set()
    for slot in sorted(slots):
        uni = agl.toUnicode(slots[slot])            # '' when the name is unknown
        cp = ord(uni) if len(uni) == 1 else None    # skip ligatures (multi-char)
        if cp is not None and _searchable_cp(cp) and cp not in claimed:
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
    found = find_outline(name)
    if not found:
        return None
    pfb, enc = found
    try:
        return _convert(pfb, name, Path(out_dir), parse_enc(enc) if enc else None)
    except Exception as e:                                  # noqa: BLE001
        print(f'  t1-convert: {name} skipped ({type(e).__name__}: {e}) — keeps metric boxes')
        return None


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

    ps = f'{name}-ReflowTeXConverted'          # PostScript name: no spaces
    label = f'{name} (ReflowTeX converted)'
    fb = FontBuilder(upm, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({addressing[s]: gname for s, gname in slots.items()})
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
    return served, addressing
