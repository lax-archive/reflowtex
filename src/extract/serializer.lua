-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Experiment 07: JSON export with math support
--
-- Extends experiment 04's serializer with:
--   - hlist/vlist nodes now recurse into children and include `shift`
--   - math open/close nodes are included with their `surround` field
--   - a `fonts` table maps font ID → {name, size_sp, filename}
--     so the renderer can load the right OTF file at the right size

-- ── JSON encoder (unchanged from experiment 04) ───────────────────────────

local function json_encode(val)
    local t = type(val)
    if t == "nil" then
        return "null"
    elseif t == "boolean" then
        return val and "true" or "false"
    elseif t == "number" then
        return tostring(val)
    elseif t == "string" then
        return '"' ..
            val:gsub('\\', '\\\\')
               :gsub('"',  '\\"')
               :gsub('\n', '\\n')
               :gsub('\r', '\\r')
               :gsub('\t', '\\t')
            .. '"'
    elseif t == "table" then
        local count = 0
        for _ in pairs(val) do count = count + 1 end
        local is_array = (count == #val)
        if is_array then
            local parts = {}
            for _, v in ipairs(val) do parts[#parts + 1] = json_encode(v) end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local keys = {}
            for k in pairs(val) do keys[#keys + 1] = k end
            table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
            local parts = {}
            for _, k in ipairs(keys) do
                parts[#parts + 1] = json_encode(tostring(k)) .. ":" .. json_encode(val[k])
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return '"[' .. t .. ']"'
    end
end

-- ── Font table ────────────────────────────────────────────────────────────
-- Collected lazily as glyphs are encountered.

local used_fonts = {}

local function note_font(font_id)
    if used_fonts[font_id] then return end
    local f = font.getfont(font_id)
    if not f then return end
    -- Extract just the filename from the full path.
    local filename = (f.filename or ""):match("([^/]+)$") or "unknown"
    used_fonts[font_id] = {
        name     = f.fullname or f.name or "unknown",
        size_sp  = f.size or 0,
        filename = filename,
    }
end

-- ── Glyph text ────────────────────────────────────────────────────────────
-- lax: a glyph's codepoint is not always its text. An OpenType face keeps
-- its ligatures (T_h, f_t, f_f_i), small caps (m.sc) and old-style figures
-- (seven.taboldstyle) at private-use codepoints when Unicode has no slot for
-- them (Libertine's cmap places them at U+E0xx), and even a ligature with a
-- Unicode slot (U+FB03) reads as three letters. luaotfload knows the text:
-- `characters[c].unicode` is a codepoint, or a table of codepoints for a
-- ligature the loader analysed, and `descriptions[c].name` is the glyph
-- name, whose AGL-style form (`T_h`, `uni0041.sc`, `seven.oldstyle`)
-- decomposes. The text is recorded beside the glyph — `char` stays what it
-- is, so rendering is untouched — and only when it differs from the
-- codepoint's own text, so the wire form grows nowhere else.
local AGL_NAMES = {
    space = 0x20, exclam = 0x21, quotedbl = 0x22, numbersign = 0x23,
    dollar = 0x24, percent = 0x25, ampersand = 0x26, quotesingle = 0x27,
    quoteright = 0x2019, quoteleft = 0x2018, parenleft = 0x28,
    parenright = 0x29, asterisk = 0x2A, plus = 0x2B, comma = 0x2C,
    hyphen = 0x2D, period = 0x2E, slash = 0x2F, zero = 0x30, one = 0x31,
    two = 0x32, three = 0x33, four = 0x34, five = 0x35, six = 0x36,
    seven = 0x37, eight = 0x38, nine = 0x39, colon = 0x3A, semicolon = 0x3B,
    less = 0x3C, equal = 0x3D, greater = 0x3E, question = 0x3F, at = 0x40,
    bracketleft = 0x5B, backslash = 0x5C, bracketright = 0x5D,
    asciicircum = 0x5E, underscore = 0x5F, grave = 0x60, braceleft = 0x7B,
    bar = 0x7C, braceright = 0x7D, asciitilde = 0x7E, germandbls = 0xDF,
    ae = 0xE6, AE = 0xC6, oe = 0x153, OE = 0x152, oslash = 0xF8,
    Oslash = 0xD8, eth = 0xF0, Eth = 0xD0, thorn = 0xFE, Thorn = 0xDE,
    dotlessi = 0x131, dotlessj = 0x237, lslash = 0x142, Lslash = 0x141,
    endash = 0x2013, emdash = 0x2014, fi = 0xFB01, fl = 0xFB02,
    ff = 0xFB00, ffi = 0xFB03, ffl = 0xFB04, longs = 0x17F,
}
for c = 0x41, 0x5A do AGL_NAMES[string.char(c)] = c end
for c = 0x61, 0x7A do AGL_NAMES[string.char(c)] = c end
-- Accented letters by their AGL names: <letter><accent> (eacute, Ccedilla),
-- emitted as the base letter plus the combining mark, which the oracle's
-- NFD fold strips exactly as it strips the precomposed form's.
local AGL_ACCENTS = {
    grave = 0x300, acute = 0x301, circumflex = 0x302, tilde = 0x303,
    macron = 0x304, breve = 0x306, dotaccent = 0x307, dieresis = 0x308,
    ring = 0x30A, hungarumlaut = 0x30B, caron = 0x30C, cedilla = 0x327,
    ogonek = 0x328,
}
local SMALLCAP_SUFFIX = { sc = true, smcp = true, c2sc = true, scmp = true }

local function safe_char(cp)
    if type(cp) ~= "number" or cp < 0 or cp > 0x10FFFF then return nil end
    return utf8.char(cp)
end
local function is_pua(cp)
    return (cp >= 0xE000 and cp <= 0xF8FF) or cp >= 0xF0000
end

-- One AGL component (no suffix, no underscore) to text, or nil.
local function agl_component(name)
    local cp = AGL_NAMES[name]
    if cp then return utf8.char(cp) end
    local hex = name:match("^uni(%x%x%x%x)$") or name:match("^u(%x%x%x%x%x?%x?)$")
    if hex then
        cp = tonumber(hex, 16)
        if cp and not is_pua(cp) then return safe_char(cp) end
        return nil
    end
    local base, accent = name:match("^([A-Za-z])(%a+)$")
    if base and AGL_ACCENTS[accent] then
        return base .. utf8.char(AGL_ACCENTS[accent])
    end
    return nil
end

-- A glyph name to text: strip one `.suffix`, split on `_`, decompose each
-- part; nil unless every part is known. Small-cap variants read as
-- lowercase text.
local function text_from_name(name)
    if type(name) ~= "string" or name == "" or name == ".notdef" then return nil end
    local stem, suffix = name:match("^([^.]+)%.(.+)$")
    if not stem then stem = name end
    local parts = {}
    for part in stem:gmatch("[^_]+") do
        local t = agl_component(part)
        if not t then return nil end
        parts[#parts + 1] = t
    end
    if #parts == 0 then return nil end
    local text = table.concat(parts)
    if suffix and SMALLCAP_SUFFIX[suffix:match("^(%a+)")] then
        text = text:lower()
    end
    return text
end

local text_cache = {}   -- font id → { char → text or false }

local function glyph_text(font_id, char)
    local per_font = text_cache[font_id]
    if not per_font then per_font = {}; text_cache[font_id] = per_font end
    local cached = per_font[char]
    if cached ~= nil then return cached or nil end
    local text = nil
    local f = font.getfont(font_id)
    local fmt = f and f.format
    if f and (fmt == "opentype" or fmt == "truetype") then
        local cinfo = f.characters and f.characters[char]
        local desc  = f.descriptions and f.descriptions[char]
        local uni = cinfo and cinfo.unicode
        if uni == nil and desc then uni = desc.unicode end
        if type(uni) == "table" then
            local parts = {}
            for _, cp in ipairs(uni) do
                local ch = type(cp) == "number" and not is_pua(cp) and safe_char(cp)
                if not ch then parts = nil; break end
                parts[#parts + 1] = ch
            end
            if parts and #parts > 0 then text = table.concat(parts) end
        elseif type(uni) == "number" and uni ~= char and not is_pua(uni) then
            text = safe_char(uni)
        end
        if not text then
            text = text_from_name(desc and desc.name)
        end
        -- A small-cap glyph whose codepoint route yielded a capital (a
        -- c2sc variant carrying its capital's unicode) still reads lowercase.
        local dname = desc and desc.name
        if text and type(dname) == "string" then
            local suffix = dname:match("%.(%a+)")
            if suffix and SMALLCAP_SUFFIX[suffix] then text = text:lower() end
        end
        if text and text == safe_char(char) then text = nil end
    end
    per_font[char] = text or false
    return text
end

-- ── Colour tracking ───────────────────────────────────────────────────────
-- xcolor's \color emits pdf_colorstack whatsits (push/pop/set with a PDF
-- colour operator string). We simulate the stack during the document-order
-- walk and annotate glyph/rule nodes with the resolved colour as a CSS hex
-- string. nil means "default text colour" (black), which the renderer maps
-- to currentColor so dark mode keeps working; #000000 is normalised to nil.

local function parse_pdf_color(data)
    if not data or data == "" then return nil end
    local function hex(v) return string.format("%02x", math.floor(math.min(1, tonumber(v)) * 255 + 0.5)) end
    local r, g, b = data:match("^%s*([%d%.]+)%s+([%d%.]+)%s+([%d%.]+)%s+rg")
    if r then
        local col = "#" .. hex(r) .. hex(g) .. hex(b)
        return col ~= "#000000" and col or nil
    end
    local gray = data:match("^%s*([%d%.]+)%s+g")
    if gray then
        local h = hex(gray)
        local col = "#" .. h .. h .. h
        return col ~= "#000000" and col or nil
    end
    local c, m, y, k = data:match("^%s*([%d%.]+)%s+([%d%.]+)%s+([%d%.]+)%s+([%d%.]+)%s+k")
    if c then
        k = tonumber(k)
        local function chan(ink) return string.format("%02x", math.floor((1 - math.min(1, tonumber(ink) + k)) * 255 + 0.5)) end
        local col = "#" .. chan(c) .. chan(m) .. chan(y)
        return col ~= "#000000" and col or nil
    end
    return nil
end

-- Stack state is file-level: it persists across paragraphs, which are
-- captured in document order, so colours opened in one paragraph carry into
-- the next exactly as they do in the PDF.
local color_stack   = {}
local current_color = nil

local function handle_colorstack(n)
    if n.command == 1 then          -- push
        color_stack[#color_stack + 1] = current_color or false
        current_color = parse_pdf_color(n.data)
    elseif n.command == 2 then      -- pop
        local top = color_stack[#color_stack]
        if #color_stack > 0 then color_stack[#color_stack] = nil end
        current_color = top or nil
    elseif n.command == 0 then      -- set
        current_color = parse_pdf_color(n.data)
    end
end

-- ── Pictures ──────────────────────────────────────────────────────────────
-- An externalised tikzpicture arrives as a rule node of subtype 2 (an image)
-- carrying TeX's own width/height/depth. The node cannot name its own file,
-- so template.tex stamps attribute 902 on it and records the id → filename
-- here; the two are joined when the node is serialized. prebuild.py then
-- converts each PDF to SVG.

local PIC_ATTR    = 902
local CITE_ATTR   = 903   -- glyphs of a citation number \lrcite{...}
local CITETGT_ATTR = 904  -- glyphs of a bibliography label [n]
-- lax: laxreflow.sty stamps the kernel's \@outputbox with this attribute
-- just before \@outputpage assembles the page around it (Serializer.mark_body),
-- so the shipout walk can find the body positively instead of by anatomy.
local BODY_ATTR   = 905
local RULE_IMAGE  = 2
local picture_files = {}
-- lax: the page of a multi-page file a picture shows (\includegraphics
-- [page=N]{figures.pdf} — one PDF holding every figure of a paper, addressed
-- by page), recorded beside the file by laxreflow.sty's \Gin@setfile hook;
-- the template's tikz capture passes none, and an unpaged picture carries no
-- page field.
local picture_pages = {}

-- lax: \laxmark (laxreflow.sty, the archive's injection package) lowers each
-- lax marker to a user_defined whatsit with this user_id and a string value
-- "b:<n>" / "e:<n>". A marker is an exact content-stream position; ids never
-- enter the node list (the submission's mark table maps numbers back).
local LAX_MARK_ID = 90210
local function lax_marker(n)
    if n.user_id ~= LAX_MARK_ID then return nil end
    local side, num = tostring(n.value):match("^([be]):(%d+)$")
    if not side then return nil end
    return side, tonumber(num)
end
-- lax: the stand-in note_inserts leaves in the vertical list for a footnote
-- reference that has no paragraph to sit in (see "Footnote ordinals"): a
-- user_defined whatsit with this user_id and the footnote's ordinal as its
-- number value.
local LAX_FOOTNOTE_REF_ID = 90211
-- lax: laxreflow.sty's record of a section heading's skip — a user_defined
-- whatsit with this user_id, written below the glue \@startsection's
-- \addvspace left above the heading, its value that glue's width in sp
-- (\lastskip as \@sect began). See "Heading skips" in the walk.
local LAX_HEADING_SKIP_ID = 90212

Serializer = Serializer or {}
function Serializer.note_picture(id, file, page)
    picture_files[id] = file
    -- lax: a positive integer or nothing; the caller's value is TeX's
    -- \number of the key, but only a number joins the node.
    local n = tonumber(page)
    if n and n >= 1 and n == math.floor(n) then
        picture_pages[id] = math.floor(n)
    else
        picture_pages[id] = nil
    end
end
function Serializer.mark_body(boxnum)
    local b = tex.box[boxnum]
    if b and node.type(b.id) == "vlist" then node.set_attribute(b, BODY_ATTR, 1) end
end

-- ── Transforms ────────────────────────────────────────────────────────────
-- Rotation is not a node property: \rotatebox (and \reflectbox, \scalebox)
-- leave the box tree alone and emit three whatsits around the content —
-- pdf_save, pdf_setmatrix, pdf_restore — while adjusting the *enclosing* box
-- to the transformed bounding box. So the metrics are already right without
-- us; only the drawing needs the matrix. Dropping these whatsits is silent
-- and looks like a layout bug: the content renders unrotated in a box sized
-- for the rotated version.
--
-- pdf_setmatrix's `data` is "a b c d" and applies about the current point:
-- the backend emits translate(x,y) . [a b c d] . translate(-x,-y). The pair
-- is nested and well-formed, so it is folded here into one `transform` node
-- with the content as children, which keeps the concept whole in the schema
-- and leaves the renderer a single node type to handle.
-- node.whatsits() maps subtype number -> name, so it is inverted here. The
-- numbers are looked up rather than hardcoded because they are an internal
-- LuaTeX enum; a wrong constant silently strips the whatsit and un-rotates
-- the content rather than failing.
local WH = {}
for subtype, name in pairs(node.whatsits()) do WH[name] = subtype end
local WH_SETMATRIX = assert(WH.pdf_setmatrix, "no pdf_setmatrix whatsit subtype")
local WH_SAVE      = assert(WH.pdf_save,      "no pdf_save whatsit subtype")
local WH_RESTORE   = assert(WH.pdf_restore,   "no pdf_restore whatsit subtype")
local WH_DEST       = assert(WH.pdf_dest,       "no pdf_dest whatsit subtype")
local WH_START_LINK = assert(WH.pdf_start_link, "no pdf_start_link whatsit subtype")
local WH_END_LINK   = assert(WH.pdf_end_link,   "no pdf_end_link whatsit subtype")

-- lax: hyperref's links. A destination is a whatsit carrying its name, and a
-- link is a start/end pair around its text, so both reach the node list as
-- zero-size positions the viewer can anchor and outline. An internal link's
-- action names its destination; hyperref writes a URL as a raw action
-- (`user`, the PDF dictionary as text), so the URL is read out of that, with
-- the PDF string escapes undone. Anything else (a file link, a named action)
-- has no target here, and its text stays plain text.
local ACTION_GOTO, ACTION_USER = 1, 3
local function link_target(n)
    local action = n.action
    if not action then return nil end
    if action.action_type == ACTION_GOTO and action.named_id == 1 then
        return "#" .. tostring(action.action_id)
    end
    if action.action_type == ACTION_USER and action.data then
        local uri = tostring(action.data):match("/URI%((.*)%)")
        if uri then return (uri:gsub("\\(.)", "%1")) end
    end
    return nil
end
local function lax_dest(n)
    if n.subtype == WH_DEST and n.named_id == 1 then return tostring(n.dest_id) end
    return nil
end

local function parse_matrix(data)
    if not data then return nil end
    local a, b, c, d = data:match("^%s*(%-?[%d%.]+)%s+(%-?[%d%.]+)%s+(%-?[%d%.]+)%s+(%-?[%d%.]+)")
    if not a then return nil end
    return { a = tonumber(a), b = tonumber(b), c = tonumber(c), d = tonumber(d) }
end

-- ── Footnote ordinals ─────────────────────────────────────────────────────
-- lax: a footnote is numbered here by encounter order — k for the k-th
-- insert the serializer meets — and that k ties its two halves together
-- for a viewer that sets footnotes beside their references: the
-- `footnote_ref` node (or stream item) at the reference point, and
-- `footnote = k` on each paragraph of the footnote's text (note_inserts).
-- An in-text \footnote is met twice: as an `ins` node in the paragraph
-- being captured (pre_linebreak time, still at its reference point) and
-- again in the contribution list once the line breaker has migrated it out
-- behind its line. Node identity is not relied on across the two; the
-- insert's vertical list is, through the paragraph indices its lines were
-- stamped with by their own pre_linebreak fire (the footnote text is broken
-- before the insert joins the enclosing paragraph), so the first of them
-- keys the ordinal. An insert with no stamped paragraph (\footnote{} —
-- nothing to place) gets no ordinal and no ref.
--
-- Each captured node is stamped with its paragraph index (PARA_ATTR, see
-- "Paragraph capture") so the shipout walk can recognise the resulting
-- lines; the same stamp is read here.
local PARA_ATTR = 900
local footnote_ordinals = {}   -- first paragraph index of an insert → k
local footnote_count    = 0

local function collect_paras(head, out, seen)
    for n in node.traverse(head) do
        local v = node.get_attribute(n, PARA_ATTR)
        if v and not seen[v] then seen[v] = true; out[#out + 1] = v end
        local t = node.type(n.id)
        if t == "hlist" or t == "vlist" then collect_paras(n.head, out, seen) end
    end
    return out
end

-- The insert's ordinal (nil for a paragraphless one), its paragraph
-- indices, and whether this call assigned the ordinal — true exactly when
-- the insert was never met at capture time, i.e. it was appended in
-- vertical mode (\thanks, \footnotetext between paragraphs).
local function footnote_ordinal(ins)
    local paras = collect_paras(ins.head, {}, {})
    local first = paras[1]
    if not first then return nil, paras, false end
    local k = footnote_ordinals[first]
    if k then return k, paras, false end
    footnote_count = footnote_count + 1
    footnote_ordinals[first] = footnote_count
    return footnote_count, paras, true
end

-- ── Node serializer ───────────────────────────────────────────────────────

local function serialize_nodelist(head)
    local result = {}
    -- `cur` is the list being appended to: a pdf_save opens a frame that
    -- collects nodes until its pdf_restore, at which point the frame becomes
    -- a transform node (if a matrix was set) or is spliced back in flat (if
    -- not — plenty of saves carry no matrix at all).
    local cur = result
    local frames = {}

    for n in node.traverse(head) do
        local t = node.type(n.id)

        if t == "local_par" then
            -- skip

        elseif t == "whatsit" and n.subtype == WH_SAVE then
            local f = { parent = cur, list = {} }
            frames[#frames + 1] = f
            cur = f.list

        elseif t == "whatsit" and n.subtype == WH_SETMATRIX then
            local f = frames[#frames]
            if f then f.matrix = parse_matrix(n.data) end

        elseif t == "whatsit" and n.subtype == WH_RESTORE then
            local f = frames[#frames]
            if f then
                frames[#frames] = nil
                cur = f.parent
                if f.matrix then
                    local m = f.matrix
                    cur[#cur + 1] = {
                        type = "transform",
                        m_a = m.a, m_b = m.b, m_c = m.c, m_d = m.d,
                        children = f.list,
                    }
                else
                    for _, item in ipairs(f.list) do cur[#cur + 1] = item end
                end
            end

        elseif t == "whatsit" and lax_marker(n) then
            -- lax: a marker inside a paragraph (horizontal mode) is an
            -- exact position in the captured node list.
            local side, num = lax_marker(n)
            cur[#cur + 1] = { type = "marker", side = side, n = num }

        elseif t == "whatsit" and lax_dest(n) then
            cur[#cur + 1] = { type = "dest", target = lax_dest(n) }

        elseif t == "whatsit" and n.subtype == WH_START_LINK then
            local target = link_target(n)
            if target then cur[#cur + 1] = { type = "link", side = "b", target = target } end

        elseif t == "whatsit" and n.subtype == WH_END_LINK then
            cur[#cur + 1] = { type = "link", side = "e" }

        elseif t == "whatsit" and n.stack ~= nil and n.command ~= nil then
            -- pdf_colorstack whatsit: update colour state; nothing to emit
            -- (the resolved colour is baked into glyph/rule nodes).
            handle_colorstack(n)

        elseif t == "glyph" then
            note_font(n.font)
            local fdata = font.getfont(n.font)
            local cinfo = fdata and fdata.characters and fdata.characters[n.char]
            cur[#cur + 1] = {
                type   = "glyph",
                char   = n.char,
                text   = glyph_text(n.font, n.char),   -- lax: see "Glyph text"
                gindex = cinfo and cinfo.index or nil,
                font   = n.font,
                width  = n.width,
                height = n.height,
                depth  = n.depth,
                color  = current_color,
                -- Citation wiring: `cite` on a \lrcite number, `citetarget` on a
                -- bibliography [n] label. The browser links the two.
                cite       = node.get_attribute(n, CITE_ATTR),
                citetarget = node.get_attribute(n, CITETGT_ATTR),
            }

        elseif t == "glue" then
            local g = {
                type         = "glue",
                width        = n.width,
                stretch      = n.stretch,
                shrink       = n.shrink,
                stretch_order = n.stretch_order,
                shrink_order  = n.shrink_order,
                subtype      = n.subtype,
            }
            -- Leader glue (\leaders, \cleaders, \xleaders, \gleaders) carries a
            -- box that TeX tiles across the glue's *set* width instead of
            -- leaving blank space. Extensible arrows are built this way:
            -- \xrightarrow is an arrow tail, a \cleaders run of en-dashes, and
            -- an arrowhead — so dropping the leader silently deletes the middle
            -- of every arrow and leaves the two ends floating apart.
            if n.leader then
                g.leader = serialize_nodelist(n.leader)[1]
            end
            cur[#cur + 1] = g

        elseif t == "kern" then
            cur[#cur + 1] = {
                type    = "kern",
                kern    = n.kern,
                subtype = n.subtype,
            }

        elseif t == "penalty" then
            cur[#cur + 1] = {
                type    = "penalty",
                penalty = n.penalty,
            }

        elseif t == "rule" and n.subtype == RULE_IMAGE then
            -- An externalised picture. Keep TeX's metrics — they are what lets
            -- the picture behave as an ordinary box everywhere — and carry the
            -- source file across for prebuild.py to turn into SVG (lax: with
            -- the page of it, when the include named one).
            local id = node.get_attribute(n, PIC_ATTR)
            cur[#cur + 1] = {
                type   = "picture",
                width  = n.width,
                height = n.height,
                depth  = n.depth,
                file   = id and picture_files[id] or nil,
                page   = id and picture_pages[id] or nil,
            }

        elseif t == "rule" then
            -- width = -1073741824 sp (-16384pt) is TeX's sentinel meaning
            -- "fill to the width of the enclosing box" (a running rule).
            -- We preserve the raw value; the renderer handles the sentinel.
            cur[#cur + 1] = {
                type   = "rule",
                width  = n.width,
                height = n.height,
                depth  = n.depth,
                color  = current_color,
            }

        elseif t == "disc" then
            local function sub(h) return h and serialize_nodelist(h) or {} end
            cur[#cur + 1] = {
                type    = "disc",
                subtype = n.subtype,
                pre     = sub(n.pre),
                post    = sub(n.post),
                replace = sub(n.replace),
            }

        elseif t == "hlist" or t == "vlist" then
            -- Recurse into children; include shift for superscript/subscript boxes.
            -- glue_set/sign/order are TeX's pre-computed box glue setting: the PDF
            -- backend uses these directly rather than recomputing from children.
            cur[#cur + 1] = {
                type       = t,
                subtype    = n.subtype,
                width      = n.width,
                height     = n.height,
                depth      = n.depth,
                shift      = n.shift,
                glue_set   = n.glue_set,
                glue_sign  = n.glue_sign,
                glue_order = n.glue_order,
                children   = n.head and serialize_nodelist(n.head) or {},
            }

        elseif t == "math" then
            -- Marks the start (subtype=0) and end (subtype=1) of inline math.
            -- `surround` is a thin space on each side for some formula types.
            cur[#cur + 1] = {
                type     = "math",
                subtype  = n.subtype,
                surround = n.surround,
            }

        elseif t == "ins" then
            -- lax: a footnote's insert, still at its reference point in the
            -- paragraph being captured (see "Footnote ordinals"): no ink, no
            -- width, only which footnote. A paragraphless insert stays the
            -- bare node it always was (the encode strips it).
            local k = footnote_ordinal(n)
            if k then
                cur[#cur + 1] = { type = "footnote_ref", n = k }
            else
                cur[#cur + 1] = { type = t }
            end

        else
            cur[#cur + 1] = { type = t }
        end
    end

    -- A save whose restore lands in a different list would strand its content
    -- inside an unclosed frame, i.e. drop it from the output. Nothing emits
    -- that today, but losing content silently is the wrong failure: splice the
    -- frames back untransformed so the text still renders.
    for i = #frames, 1, -1 do
        local f = frames[i]
        for _, item in ipairs(f.list) do f.parent[#f.parent + 1] = item end
    end
    return result
end

-- ── Paragraph capture ─────────────────────────────────────────────────────
-- Every pre_linebreak fire is captured *unbroken* so the browser can re-break
-- it. Note there is no groupcode filter: text preceding a display fires with
-- groupcode="math_shift" (TeX interrupts the paragraph, sets the part so far,
-- appends the display, then resumes), and dropping those fires would silently
-- discard the text around every display.
--
-- Each captured node is stamped with its paragraph index so the shipout walk
-- can recognise the resulting lines. Displays never pass through this
-- callback, so they stay unstamped — that is exactly how the walk tells text
-- apart from math.

local all_paragraphs = {}

local function stamp(head, idx)
    for n in node.traverse(head) do
        node.set_attribute(n, PARA_ATTR, idx)
        local t = node.type(n.id)
        if t == "hlist" or t == "vlist" then stamp(n.head, idx)
        elseif t == "disc" then
            stamp(n.pre, idx); stamp(n.post, idx); stamp(n.replace, idx)
        end
    end
end

local function find_para(n)
    local v = node.get_attribute(n, PARA_ATTR)
    if v then return v end
    local t = node.type(n.id)
    if t == "hlist" or t == "vlist" then
        for c in node.traverse(n.head) do
            local r = find_para(c)
            if r then return r end
        end
    end
    return nil
end

-- List indentation never appears in the node list. LaTeX's list environments
-- leave \leftskip at zero and indent via \parshape instead, whose first pair
-- is {indent, linewidth} — so an item at depth 1 reports {25pt, 320pt} of a
-- 345pt \hsize. The indent must be recorded here, while the paragraph is
-- being broken, and is an absolute typographic measure (\leftmargin), so the
-- renderer keeps it fixed and narrows the text column rather than scaling it.
-- Without it, item text starts at x=0 and the label — which hangs a fixed
-- distance to the *left* of the text — lands at negative x and is clipped.
-- The band this paragraph occupies: {indent, width}. Doubles as the band any
-- display inside it inherits (see the \displaywidth note below).
--
-- lax: a finite \leftskip indents the same way. It is furniture the line
-- breaker adds, so it never reaches the captured list either, while what a
-- paragraph does to undo it does: LaTeX's table of contents sets \leftskip to
-- the number's width and opens with \hskip -\leftskip, hanging the number left
-- of the text, so without the indent every section number lands at negative x.
-- \centering's fill has no width and changes nothing.
local function para_band()
    local ok, left = pcall(tex.getglue, "leftskip")
    left = ok and left or 0
    local has, ps = pcall(function() return tex.parshape end)
    if has and type(ps) == "table" and ps[1] and ps[1][1] and ps[1][2] then
        return ps[1][1] + left, ps[1][2] - left
    end
    return left, (tex.hsize or 0) - left
end

-- TeX's interline spacing parameters, active when this paragraph is broken. The
-- renderer re-breaks the paragraph but reproduces TeX's baseline-to-baseline rule
-- from these: advance = (baselineskip - prevDepth - height >= lineskiplimit)
-- ? baselineskip : prevDepth + height + lineskip. baselineskip and lineskip are
-- glue parameters (read their width); lineskiplimit is a dimen.
local function param_dimen(name)
    local ok, v = pcall(function() return tex[name] end)
    if not ok or v == nil then return 0 end
    if type(v) == "number" then return v end     -- dimen (lineskiplimit)
    return v.width or 0                            -- glue spec (baselineskip, lineskip)
end

-- Paragraph alignment, read from the \leftskip/\rightskip in force as it is
-- broken. LaTeX's alignment commands set these to \@flushglue (0pt plus 1fil):
-- \centering sets both, \raggedright only \rightskip, \raggedleft only
-- \leftskip. So an infinite stretch order on each side names the alignment;
-- neither (the normal case) is justified and returns nil, which the renderer
-- reads as "inherit the block alignment", keeping the wire form small.
local function glue_stretch_order(name)
    local ok, _, _, _, sto = pcall(tex.getglue, name)
    if ok and sto then return sto end
    return 0
end
local function para_align()
    local ls = glue_stretch_order("leftskip")  > 0
    local rs = glue_stretch_order("rightskip") > 0
    if ls and rs then return "center" end
    if rs then return "left"  end   -- ragged right: text flush left
    if ls then return "right" end   -- ragged left:  text flush right
    return nil                      -- justified (default)
end

local function capture_paragraph(head, groupcode)
    local idx = #all_paragraphs + 1
    local indent, width = para_band()
    all_paragraphs[idx] = {
        index  = idx,
        indent = indent,
        width  = width,
        baselineskip  = param_dimen("baselineskip"),
        lineskip      = param_dimen("lineskip"),
        lineskiplimit = param_dimen("lineskiplimit"),
        align  = para_align(),
        nodes  = serialize_nodelist(head),
    }
    stamp(head, idx)
    return head
end

-- ── Shipout walk: the ordered content stream ──────────────────────────────
-- Text is re-breakable and comes from the captures above; displays are rigid
-- and must be taken in their finished form. Only the page walk knows the
-- document order of the two, so it produces a stream of references:
--
--   {kind="paragraph", para=N}   → render all_paragraphs[N], re-broken
--   {kind="display",   box=...}  → render the box as-is (see below)
--   {kind="vspace",    amount=sp}
--
-- A display box is emitted as an ordinary serialized hlist, so the renderer
-- draws it through the same hlist path it already uses for inline boxes:
-- TeX's own glue setting is replayed rather than recomputed, which keeps
-- \hfill, \rlap, \mathclap and tabskips faithful to the PDF. TeX's centering
-- lives in the box's shift; the renderer ignores it and re-centres at the
-- reader's width.
--
-- Subtypes we key on (hlist): line=1, box=2, alignment=4, equation=6. Every
-- other hlist subtype met in a vertical list (unknown=0, the math-internal
-- ones never are) is treated as a plain box.
local HL_LINE, HL_BOX, HL_ALIGNMENT, HL_EQUATION = 1, 2, 4, 6
-- Vertical glue subtype 0 is userskip: the glue \vskip/\vspace/\addvspace insert,
-- and the before/after skips \@startsection puts around a section heading. That
-- is *explicit* spacing the author asked for, and it is preserved. Every other
-- subtype here (baselineskip=2, lineskip=1, parskip=3) is interline leading the
-- renderer re-derives per line, so it is dropped between text paragraphs.
local GLUE_USERSKIP = 0
-- The glue TeX appends to every paragraph before breaking it (0pt plus 1fil by
-- default). The renderer keys the paragraph's end on this subtype, so a
-- paragraph synthesized from a box (below) is given one too.
local GLUE_PARFILLSKIP = 15
-- TeX's "running dimension" sentinel: a rule dimension of -2^30 sp means "as
-- wide/tall as the enclosing box" (an \hrule's width, a \vrule's height).
local RUNNING = -1073741824
-- LuaTeX's glue orders are fi=1, fil=2, fill=3, filll=4 (one more than TeX82).
local ORDER_FIL = 2

-- A display's box width is not what it occupies. \[..\] packs at natural
-- width and is centred by its shift, while amsmath centres an alignment by
-- baking glue *inside* the row (so an align* row can report 204.78pt while
-- its ink sits at 140.22..204.78 of a 345pt \displaywidth). The invariant
-- across both is \displaywidth: the display occupies the band
-- [\displayindent, \displayindent+\displaywidth], and TeX has already placed
-- the ink correctly within it. So we record that band and let the renderer
-- centre *it*, preserving everything TeX decided inside.
--
-- \displaywidth itself is long gone by shipout, but it does not need to be
-- captured: TeX derives it from the enclosing paragraph's shape (TeX82 §1145
-- — with no \parshape and no \hangindent, \displaywidth = \hsize and
-- \displayindent = 0; otherwise both come from \parshape). Since every
-- paragraph already records its \parshape band, a display simply inherits the
-- band of the paragraph it interrupts.
--
-- Reading the value from append_to_vlist_filter would be the obvious
-- alternative and is a trap: returning a box from that callback makes the
-- callback responsible for the interline glue, so hooking it silently drops
-- every baselineskip TeX would have inserted — it changes the document rather
-- than observing it.

-- `content` is the stream being appended to. It is the body stream except
-- while a footnote's vertical list is walked (see note_inserts), when it is
-- swapped for the endnote stream; every emitter below reads it through this
-- upvalue so the same walk serves both.
local content   = {}
local seen_para = {}

-- amsmath leaves an empty paragraph behind after an alignment (a zero-content
-- fire that exists only to close the display group). It has no ink, so it
-- would render as an empty line; skip such paragraphs entirely.
-- lax: was has_glyphs, and gating body emission on glyphs alone silently
-- dropped every paragraph whose only ink is an externalised picture (a
-- standalone tikzpicture between blank lines is exactly that: an hlist
-- holding one picture rule, no glyph at any depth). Body content is
-- anything bearing glyphs, pictures, OR rules with area — the
-- `\rule{3cm}{1cm}` figure placeholder, a `\noindent\rule{\textwidth}{0.4pt}`
-- separator; a strut (`\rule{0pt}{2ex}`) has no width and is not ink. A
-- capture with none still takes the marker-hoist branch below.
local function rule_has_area(n)
    return (n.height or 0) > 0 and ((n.width or 0) > 0 or n.width == RUNNING)
end
local function has_ink(nodes)
    for _, n in ipairs(nodes) do
        if n.type == "glyph" or n.type == "picture" then return true end
        if n.type == "rule" and rule_has_area(n) then return true end
        if n.children and has_ink(n.children) then return true end
        if n.replace and has_ink(n.replace) then return true end
    end
    return false
end

local function emit_vspace(sp)
    if sp and sp ~= 0 and #content > 0 then
        content[#content + 1] = { kind = "vspace", amount = sp }
    end
end

-- ── Heading skips ─────────────────────────────────────────────────────────
-- lax: a section heading that opens a page had its \@startsection
-- beforeskip discarded at the page top with the furniture, and the walk,
-- which discards the furniture too, welded the heading — or the paragraph
-- a run-in \paragraph heading opens — to the paragraph before it. The
-- skip is not guessed back: laxreflow.sty records the glue TeX left above
-- the heading (LAX_HEADING_SKIP_ID, \lastskip right after \addvspace, so
-- already merged with a preceding display's below skip or a list's
-- \topsep as \addvspace merges them) in a whatsit below that glue, which
-- survives the break the glue does not. This is the most recent such
-- record not yet answered by a line or display. Module-level rather than
-- a `pending` field: a break can fall at the glue right after the whatsit
-- (the same page as at \@secpenalty, chosen only on an awful_bad tie), so
-- the whatsit may end one page and its heading open the next.
local heading_skip = nil

-- lax: markers (and footnote refs, the vertical-mode kind) must be
-- transparent to the spacing logic below, so the "what did the stream just
-- emit" questions skip over trailing marker items.
local function last_flow()
    for i = #content, 1, -1 do
        local k = content[i].kind
        if k ~= "marker" and k ~= "footnote_ref" then return content[i] end
    end
    return nil
end

-- lax: an inkless capture (or box) is skipped, but a marker whatsit inside it
-- (e.g. `\end{equation}` directly followed by the end marker and then a blank
-- line — the resumed paragraph holds only the whatsit) must not vanish with
-- it: hoist its markers into the stream at this position.
local function hoist_markers(nodes)
    for _, it in ipairs(nodes) do
        if it.type == "marker" then
            content[#content + 1] = { kind = "marker", side = it.side, n = it.n }
        elseif it.type == "footnote_ref" then
            content[#content + 1] = { kind = "footnote_ref", n = it.n }
        elseif it.type == "dest" then
            content[#content + 1] = { kind = "dest_item", target = it.target }
        end
        if it.children then hoist_markers(it.children) end
        if it.replace then hoist_markers(it.replace) end
    end
end

-- The band of the most recent paragraph; displays inherit it.
local cur_band = { indent = 0, width = 0 }
-- The interline parameters of the most recent real paragraph the walk met. A
-- paragraph synthesized from a box at shipout time cannot read them itself:
-- \@outputpage zeroes \baselineskip/\lineskip/\lineskiplimit around the page
-- box, so the values in force during the walk are the page's, not the text's.
local cur_leading = nil

-- ── Footnotes → endnotes ──────────────────────────────────────────────────
-- TeX floats every \insert to the bottom of the page (or column) it lands on,
-- so a page walk meets the footnote lines *after* every body line of that
-- page, with the \footnoterule before them. In a pageless flow that scatters
-- footnotes at arbitrary points of the text. The web presentation chosen here
-- is endnotes: footnotes are kept out of the body stream where TeX put them
-- and appended at the very end, in document order, behind a rule display.
-- (lax: a viewer that sets footnote k beside its reference instead has what
-- it needs — the `footnote_ref` node or stream item at the reference point
-- and `footnote = k` on the footnote's paragraphs, see "Footnote ordinals";
-- the endnote emission stays as the fallback for narrow widths.)
--
-- Recognition happens where the \insert is still a node: buildpage_filter
-- fires each time TeX is about to move contributions onto the page, and at
-- that moment the contribution list holds every `ins` node appended since —
-- the ones the line breaker migrated out of a paragraph's lines (a \footnote
-- in text) and the ones \footnotetext appended in vertical mode (\thanks,
-- which \maketitle runs between paragraphs). The insert's vertical list is
-- complete by then, its lines stamped by their own pre_linebreak fire, so its
-- paragraph indices are recorded in footnote_paras and the list is walked
-- into the endnote stream right away. The page walk later meets those lines
-- at a page bottom and skips them, and drops the \footnoterule with the
-- \skip\footins above it.
local footnote_paras   = {}     -- paragraph index → true
local footnote_content = {}     -- the endnote stream, walked per insert
local footnote_pending = { sp = 0, explicit = 0, body_seen = true }
-- What separates the endnotes from the body: TeX's own \footnoterule (the
-- first one the page walk drops — its width is the class's, .4\columnwidth
-- for the standard classes) with \skip\footins above it and the rule's own
-- trailing kern below.
local endnote_sep = nil

-- Does a footnote line follow this node (through glue/kern/penalty/whatsit
-- only)? True exactly for the \footnoterule at a page bottom.
local function footnote_line_follows(n)
    local c = n.next
    while c do
        local t = node.type(c.id)
        if t == "hlist" then
            local p = find_para(c)
            return p ~= nil and footnote_paras[p] == true
        elseif t ~= "glue" and t ~= "kern" and t ~= "penalty" and t ~= "whatsit" then
            return false
        end
        c = c.next
    end
    return false
end

-- ── Emitters ──────────────────────────────────────────────────────────────
-- The spacing rules around a paragraph and a display, shared by every branch
-- of the walk that produces one (real lines, synthesized boxes, equations,
-- vertical rules).

local function emit_paragraph(p, pending)
    seen_para[p] = true
    local lf = last_flow()   -- lax: look through marker items
    if not pending.body_seen then
        -- First body line on this page: the glue above it is page
        -- furniture — the top margin, the running header, and \headsep —
        -- which TeX discards at the top of a page. We shipout-walk whole
        -- pages and concatenate them, so discard it here too; otherwise
        -- it surfaces as a ~40pt gap at every page break in the reflowed
        -- output (16pt top margin + 25pt \headsep, say).
        pending.body_seen = true
        -- One exception: if the previous page ended with a display, its
        -- \belowdisplayskip was discarded at the break along with the
        -- furniture. In the reflowed (pageless) output the display and
        -- this paragraph are adjacent, so restore that skip — TeX's own
        -- parameter, not a guessed constant — or they abut too tightly.
        -- lax: the same rule for a heading's skip (see "Heading skips");
        -- the recorded glue already includes a display's below skip when
        -- the heading followed one, so it answers both.
        if heading_skip then
            emit_vspace(heading_skip)
        elseif lf and lf.kind == "display" then
            emit_vspace(lf.below_skip or 0)
        end
    elseif lf and lf.kind == "display" then
        -- Abutting a display, keep TeX's full glue: above/belowdisplayskip
        -- carries the display's spacing.
        emit_vspace(pending.sp)
    else
        -- Between text paragraphs keep only explicit vspace: baselineskip
        -- leading is re-derived per line, but a \vspace or a section's
        -- before/after skip must survive.
        emit_vspace(pending.explicit)
    end
    content[#content + 1] = { kind = "paragraph", para = p }
    pending.sp = 0
    pending.explicit = 0
    heading_skip = nil
end

-- `above_skip` is the \abovedisplayskip to restore when the display opens a
-- page after a paragraph (an equation's); a vertical rule has none.
local function emit_display(item, above_skip, pending)
    local lf = last_flow()   -- lax: look through marker items
    if pending.body_seen then
        emit_vspace(pending.sp)
    elseif lf and lf.kind == "paragraph" then
        -- Display first on a page after a paragraph on the previous page:
        -- its \abovedisplayskip was discarded with the furniture at the
        -- break. Restore it (the symmetric case to belowdisplayskip above).
        emit_vspace(above_skip)
    elseif lf and lf.kind == "display" then
        -- Two displays split by a page break (no glyph-bearing paragraph
        -- between them, so they are adjacent in the stream). In continuous
        -- flow TeX separates them by \belowdisplayskip + \abovedisplayskip;
        -- both were discarded at the break (one as the page-bottom breakpoint
        -- glue, the other as top-of-page furniture), so without this the two
        -- displays abut. Restore the pair so the join matches unbroken flow.
        emit_vspace((lf.below_skip or 0) + above_skip)
    end
    pending.body_seen = true
    pending.sp = 0
    pending.explicit = 0
    heading_skip = nil
    content[#content + 1] = item
end

local function display_box(n, children)
    return {
        type       = "hlist",
        subtype    = n.subtype,
        width      = n.width,
        height     = n.height,
        depth      = n.depth,
        glue_set   = n.glue_set,
        glue_sign  = n.glue_sign,
        glue_order = n.glue_order,
        children   = children,
    }
end

-- A vertical rule (\hrule, a class's title rule, a \footnoterule outside the
-- footnote block) as a display: a box of the rule's width holding the rule,
-- with no display skips of its own — the glue TeX put around it is what
-- `pending` carries. A running width is resolved here to the enclosing
-- vertical list's width (TeX's own meaning of it), and the rule node gets
-- that width explicitly because the renderer's hlist path paints a
-- running-width rule at zero width.
local function emit_rule_display(n, list_width, pending)
    local w = n.width == RUNNING and list_width or n.width
    local d = n.depth == RUNNING and 0 or n.depth
    local rule = { type = "rule", width = w, height = n.height, depth = d, color = current_color }
    emit_display({
        kind = "display",
        below_skip     = 0,
        display_width  = cur_band.width > 0 and cur_band.width or w,
        display_indent = cur_band.indent,
        display_shift  = cur_band.indent,
        box = {
            type = "hlist", subtype = HL_BOX,
            width = w, height = n.height, depth = d,
            glue_set = 0, glue_sign = 0, glue_order = 0,
            children = { rule },
        },
    }, 0, pending)
end

-- ── Boxes in a vertical list ──────────────────────────────────────────────
-- An \hbox appended in vertical mode is not a paragraph line: LaTeX's
-- \@makecaption sets a short caption as \hbox to\hsize{\hfil …\hfil},
-- \centerline is \hbox to\hsize{\hss …\hss}, several classes build their
-- title block from boxes, and the two-column page itself is an \hbox holding
-- the column vlists. Four shapes are told apart, in this order:
--
--   * a box holding vertical lists and no ink beside them — columns, or
--     side-by-side \parbox/\vbox pairs — is walked column by column, each
--     vlist in turn, so the stream keeps reading order (left column, then
--     right);
--   * a box holding, at any depth through further plain boxes, a line
--     stamped with a paragraph (or an equation/alignment) is a *frame around
--     content*, not content of its own — an output routine that re-wraps a
--     line to decorate it. Its children are walked with the page walk's
--     line and display semantics (the paragraph emitted once at its first
--     line, displays as displays), and every other inky box inside the
--     frame — a number, a margin mark — is dropped, never synthesized
--     (holds_line / walk_frame);
--   * a box whose ink lies wholly outside its own horizontal extent is a
--     *margin decoration* and is dropped (ink_inside). lineno's
--     \MakeLineNo does not wrap the line: it \unvbox'es it and appends,
--     as a sibling in the vertical list, \hb@xt@\z@{\makeLineNumber} — a
--     box of zero width and height whose number is \hss-ed into the left
--     margin (or \hskip\columnwidth-ed into the right one, with [switch]
--     alternating per page) — after a \kern of minus the line's depth. A
--     \llap/\rlap in vertical mode has the same shape. Ink is located with
--     TeX's own glue setting, so an \hfil-centred caption, a \centerline,
--     and an overfull box keep their ink inside [0, width] and stay content;
--     the box's own height and depth go to the pending glue, because TeX
--     advanced by them (for lineno's box that cancels the kern exactly);
--   * any other box with ink becomes a *synthesized paragraph*: its
--     serialized children, closed by a \parfillskip, entered in
--     all_paragraphs with the box as its band and its alignment read from
--     the box's own fill glue, then emitted like a real paragraph. It is
--     never stamped on any TeX node, so find_para cannot return it and the
--     walk cannot meet it twice.

-- The vlists a box holds, looked for through nested plain boxes: LaTeX's
-- two-column page is \hb@xt@\textwidth{\hb@xt@\columnwidth{\box\@leftcolumn
-- \hss}\hfil\vrule\hfil\hb@xt@\columnwidth{\box\@outputbox\hss}}, so the
-- columns sit one box down.
local function column_vlists(head, out)
    for c in node.traverse(head) do
        local t = node.type(c.id)
        if t == "vlist" then
            out[#out + 1] = c
        elseif t == "hlist" and c.subtype ~= HL_LINE and c.subtype ~= HL_EQUATION
               and c.subtype ~= HL_ALIGNMENT then
            column_vlists(c.head, out)
        end
    end
    return out
end

-- Ink outside the vlists of a box (TeX-node level: glyphs, pictures, rules
-- with area). The column separator is a \vrule of \columnseprule width and
-- running height, which counts as ink only when the class gives it a width.
local function ink_beside_vlists(head)
    for c in node.traverse(head) do
        local t = node.type(c.id)
        if t == "glyph" then return true end
        if t == "rule" and rule_has_area(c) then return true end
        if t == "disc" and ink_beside_vlists(c.replace) then return true end
        if t == "hlist" and ink_beside_vlists(c.head) then return true end
    end
    return false
end

-- Does this box frame a line? True when a paragraph-stamped line hlist, or
-- an equation/alignment hlist, sits in it at any depth through plain boxes
-- (vlists are not looked into: a box holding vlists is the column shape).
local function holds_line(head)
    for c in node.traverse(head) do
        if node.type(c.id) == "hlist" then
            if c.subtype == HL_LINE then
                if find_para(c) then return true end
            elseif c.subtype == HL_EQUATION or c.subtype == HL_ALIGNMENT then
                return true
            elseif holds_line(c.head) then
                return true
            end
        end
    end
    return false
end

-- Where a box's ink is. Walks the box with each child's set position
-- (node.rangedimensions applies the box's glue setting to the run before
-- it; in a vlist a child hlist sits at its shift), and records in `st`
-- whether any ink was met (`ink`) and whether any of it overlaps the open
-- interval (0, w) of the outermost box's own width (`inside`). `x0` is the
-- offset of this box within the outermost one.
local function ink_inside(box, x0, w, st)
    local vertical = node.type(box.id) == "vlist"
    local function note(x, cw)
        st.ink = true
        if x + cw > 0 and x < w then st.inside = true end
    end
    for c in node.traverse(box.head) do
        local t = node.type(c.id)
        local x = x0
        if vertical then
            if t == "hlist" or t == "vlist" then x = x0 + (c.shift or 0) end
        elseif c ~= box.head then
            x = x0 + node.rangedimensions(box, box.head, c)
        end
        if t == "glyph" then
            note(x, c.width or 0)
        elseif t == "rule" then
            if rule_has_area(c) then
                local cw = c.width
                if cw == RUNNING then x = x0; cw = box.width or 0 end
                note(x, cw)
            end
        elseif t == "disc" then
            if c.replace then note(x, (node.dimensions(c.replace)) or 0) end
        elseif t == "hlist" or t == "vlist" then
            ink_inside(c, x, w, st)
        end
        if st.inside then return st end
    end
    return st
end
local function margin_decoration(n)
    local st = ink_inside(n, 0, n.width or 0, { ink = false, inside = false })
    return st.ink and not st.inside
end

-- Alignment of a box from its own fill glue, the way \centering/\raggedright
-- express it through \leftskip/\rightskip: a fill on both sides is centred,
-- a trailing one only is flush left, a leading one only flush right, none
-- is the block default. (The fills stay in the paragraph and centre it in
-- the renderer regardless; `align` is what a fill-less line falls back to.)
local function box_align(nodes)
    local function is_fill(n) return n.type == "glue" and (n.stretch_order or 0) > 0 end
    local first, last
    for i = 1, #nodes do
        local n = nodes[i]
        if n.type ~= "kern" and n.type ~= "penalty" and n.type ~= "whatsit"
           and n.type ~= "marker" and n.type ~= "footnote_ref" and n.type ~= "local_par" then
            first = first or n
            last = n
        end
    end
    local ls = first and is_fill(first)
    local rs = last and last ~= first and is_fill(last)
    if ls and rs then return "center" end
    if rs then return "left" end
    if ls then return "right" end
    return nil
end

-- \@makecaption keeps the caption text in a natural-width \sbox inside the
-- \hbox to\hsize; as a rigid box it could not be re-broken for a reader
-- narrower than it. A nested box that TeX packed at natural width, unshifted
-- and with ink, is splice-flattened: its children advance the pen exactly as
-- the box did, and become breakable. A box holding a rule directly
-- (\rule{3cm}{1cm} is \hbox{\vrule ...}) stays rigid: the renderer measures a
-- bare rule in a paragraph as zero-size, the box carries the metrics.
local function has_direct_rule(nodes)
    for _, c in ipairs(nodes) do if c.type == "rule" then return true end end
    return false
end
local function flatten_boxes(nodes)
    local out = {}
    for _, n in ipairs(nodes) do
        if n.type == "hlist" and (n.subtype == HL_BOX or n.subtype == 0)
           and (n.glue_sign or 0) == 0 and (n.shift or 0) == 0
           and has_ink(n.children) and not has_direct_rule(n.children) then
            for _, c in ipairs(flatten_boxes(n.children)) do out[#out + 1] = c end
        else
            out[#out + 1] = n
        end
    end
    return out
end

local function leading_for_synthesized()
    if cur_leading then return cur_leading end
    for i = #all_paragraphs, 1, -1 do
        local p = all_paragraphs[i]
        if not p.synthesized and not footnote_paras[i] then
            return { baselineskip = p.baselineskip, lineskip = p.lineskip, lineskiplimit = p.lineskiplimit }
        end
    end
    return { baselineskip = 0, lineskip = 0, lineskiplimit = 0 }
end

local function synthesize_paragraph(n, pending)
    local nodes = serialize_nodelist(n.head)
    if not has_ink(nodes) then
        hoist_markers(nodes)
        return
    end
    nodes = flatten_boxes(nodes)
    local align = box_align(nodes)   -- from the box's own glue, before the parfillskip joins
    nodes[#nodes + 1] = {
        type = "glue", subtype = GLUE_PARFILLSKIP,
        width = 0, stretch = 65536, stretch_order = ORDER_FIL, shrink = 0, shrink_order = 0,
    }
    local lead = leading_for_synthesized()
    local idx = #all_paragraphs + 1
    all_paragraphs[idx] = {
        index  = idx,
        indent = n.shift or 0,
        width  = n.width,
        baselineskip  = lead.baselineskip,
        lineskip      = lead.lineskip,
        lineskiplimit = lead.lineskiplimit,
        align  = align,
        nodes  = nodes,
        synthesized = true,   -- bookkeeping only; not a schema field
    }
    -- cur_band is left alone: a box has no paragraph shape, and a
    -- natural-width \hbox would narrow the band every later display inherits.
    emit_paragraph(idx, pending)
end

-- ── The walk ──────────────────────────────────────────────────────────────
-- `list_width` is the width of the vertical list being walked (what a
-- running-width rule in it spans).
local walk_page

local function walk_columns(cols, pending)
    -- Each column after the first is walked as a page: a column break is a
    -- page break to TeX — the glue at the bottom of one column and the
    -- \topskip furniture at the top of the next were both discarded — so the
    -- top-of-page rules apply (furniture dropped, a display's below skip
    -- restored). The first column continues the enclosing list: the glue
    -- between whatever precedes the box (a two-column title block) and the
    -- column's first line is real spacing. After the last column the walk
    -- continues with no pending glue and the body counted as seen if any
    -- column emitted something.
    local had_body = pending.body_seen
    for i, col in ipairs(cols) do
        if i > 1 then
            pending.body_seen = false
            pending.sp = 0
            pending.explicit = 0
        end
        walk_page(col.head, pending, col.width)
        had_body = had_body or pending.body_seen
    end
    pending.body_seen = had_body
    pending.sp = 0
    pending.explicit = 0
end

-- A line box: emits its paragraph at its first line (a footnote line at a
-- page bottom is already in the endnote stream).
local function walk_line(n, pending)
    local p = find_para(n)
    if p and footnote_paras[p] then
        -- A footnote line at the page bottom: already in the endnote
        -- stream. The glue that came with the footnote block is not
        -- body spacing, except what TeX put right under the rule,
        -- which the endnote separator reproduces.
        if endnote_sep and not endnote_sep.below then endnote_sep.below = pending.sp end
        pending.sp = 0
        pending.explicit = 0
        heading_skip = nil
    else
        if p then
            local para = all_paragraphs[p]
            cur_band.indent = para.indent
            cur_band.width  = para.width
            cur_leading = { baselineskip = para.baselineskip, lineskip = para.lineskip,
                            lineskiplimit = para.lineskiplimit }
        end
        if p and not seen_para[p] and has_ink(all_paragraphs[p].nodes) then
            emit_paragraph(p, pending)
        elseif p and not seen_para[p] then
            seen_para[p] = true
            hoist_markers(all_paragraphs[p].nodes)
        end
        pending.sp = 0
        pending.explicit = 0
        heading_skip = nil
    end
end

local function walk_display(n, pending)
    emit_display({
        kind = "display",
        -- \belowdisplayskip in force here, so a page break that discards it
        -- (see emit_paragraph) can restore TeX's own value.
        below_skip = param_dimen("belowdisplayskip"),
        -- The band this display occupies, and where TeX put the box
        -- inside it. display_shift is carried out here rather than on
        -- the box because in a vertical list shift means a horizontal
        -- displacement, whereas the renderer reaches the box through
        -- its hlist path, where shift means a vertical offset —
        -- passing it through would push displays down the page.
        display_width  = cur_band.width > 0 and cur_band.width or n.width,
        display_indent = cur_band.indent,
        display_shift  = n.shift or 0,
        box  = display_box(n, n.head and serialize_nodelist(n.head) or {}),
    }, param_dimen("abovedisplayskip"), pending)
end

-- A plain box (see "Boxes in a vertical list"). `in_frame` is true for a
-- box met inside a frame, where an inky box that is neither columns nor a
-- frame itself is decoration and is dropped rather than synthesized. A
-- dropped box's markers are hoisted so no marker vanishes with it.
local walk_frame
local function walk_box(n, pending, in_frame)
    local cols = column_vlists(n.head, {})
    if #cols > 0 and not ink_beside_vlists(n.head) then
        walk_columns(cols, pending)
    elseif holds_line(n.head) then
        walk_frame(n.head, pending)
    elseif in_frame or margin_decoration(n) then
        hoist_markers(serialize_nodelist(n.head))
        if not in_frame then
            -- The box took this much of the vertical list; keep the
            -- advance as pending glue (lineno's kern -depth + box of that
            -- depth nets zero, as in the PDF).
            pending.sp = (pending.sp or 0) + (n.height or 0) + (n.depth or 0)
        end
    else
        synthesize_paragraph(n, pending)
    end
end

-- The children of a frame: lines and displays as on a page, plain boxes
-- through walk_box in frame mode, a vlist walked as a column. A frame is
-- horizontal, so its glue and kerns are not vertical spacing and are
-- ignored; bare glyphs and rules in it are decoration.
walk_frame = function(head, pending)
    for n in node.traverse(head) do
        local t = node.type(n.id)
        if t == "hlist" and n.subtype == HL_LINE then
            walk_line(n, pending)
        elseif t == "hlist" and (n.subtype == HL_EQUATION or n.subtype == HL_ALIGNMENT) then
            walk_display(n, pending)
        elseif t == "hlist" then
            walk_box(n, pending, true)
        elseif t == "vlist" then
            walk_page(n.head, pending, n.width)
        elseif t == "whatsit" and lax_marker(n) then
            local side, num = lax_marker(n)
            content[#content + 1] = { kind = "marker", side = side, n = num }
        elseif t == "whatsit" and lax_dest(n) then
            content[#content + 1] = { kind = "dest_item", target = lax_dest(n) }
        end
    end
end

walk_page = function(head, pending, list_width)
    for n in node.traverse(head) do
        local t = node.type(n.id)
        if t == "hlist" and n.subtype == HL_LINE then
            walk_line(n, pending)
        elseif t == "hlist" and (n.subtype == HL_EQUATION or n.subtype == HL_ALIGNMENT) then
            walk_display(n, pending)
        elseif t == "hlist" then
            walk_box(n, pending, false)
        elseif t == "vlist" then
            walk_page(n.head, pending, n.width)
        elseif t == "rule" then
            if rule_has_area(n) then
                if footnote_line_follows(n) then
                    -- The \footnoterule: it goes to the end with the footnotes
                    -- it heads (the first one seen is the separator used
                    -- there); the \skip\footins above it is not body spacing.
                    if not endnote_sep then
                        endnote_sep = { width = n.width, height = n.height,
                                        depth = n.depth == RUNNING and 0 or n.depth }
                    end
                    pending.sp = 0
                    pending.explicit = 0
                else
                    emit_rule_display(n, list_width, pending)
                end
            end
        elseif t == "glue" then
            pending.sp = (pending.sp or 0) + (n.width or 0)
            if n.subtype == GLUE_USERSKIP then
                pending.explicit = (pending.explicit or 0) + (n.width or 0)
            end
        elseif t == "kern" then
            pending.sp = (pending.sp or 0) + (n.kern or 0)
        elseif t == "whatsit" and lax_marker(n) then
            -- lax: a vertical-mode marker sits in the page's vertical
            -- list between line boxes / displays; stock code has no branch for
            -- whatsits here and would silently drop it. Emitted in place, and
            -- deliberately not touching `pending`, so glue accounting around
            -- the marker is unchanged.
            local side, num = lax_marker(n)
            content[#content + 1] = { kind = "marker", side = side, n = num }
        elseif t == "whatsit" and lax_dest(n) then
            -- lax: most of hyperref's destinations sit here: a section's and
            -- a theorem's anchor are set in vertical mode. In place, as a
            -- marker is, `pending` untouched.
            content[#content + 1] = { kind = "dest_item", target = lax_dest(n) }
        elseif t == "whatsit" and n.user_id == LAX_FOOTNOTE_REF_ID then
            -- lax: a vertical-mode footnote's reference point, left here by
            -- note_inserts in place of the insert the page builder removed.
            -- Emitted like a marker: in place, `pending` untouched.
            content[#content + 1] = { kind = "footnote_ref", n = n.value }
        elseif t == "whatsit" and n.user_id == LAX_HEADING_SKIP_ID then
            -- lax: the glue above a heading, as laxreflow.sty measured it
            -- (see "Heading skips"); `pending` untouched — in unbroken flow
            -- the glue itself is walked, and this is only read at a page top.
            heading_skip = tonumber(n.value)
        end
    end
end

-- The \skip\footins in force, read through the insert's class number (the
-- `ins` node's subtype), so no LaTeX register number is assumed.
local footnote_skip = nil
local function note_inserts(extrainfo)
    local head = tex.lists.contrib_head
    if not head then return end
    for n in node.traverse(head) do
        if node.type(n.id) == "ins" and n.head then
            local k, paras, first_sight = footnote_ordinal(n)
            local fresh = false
            for _, p in ipairs(paras) do
                if not footnote_paras[p] then fresh = true end
            end
            if fresh then
                if first_sight then
                    -- lax: an insert no capture met was appended in vertical
                    -- mode (\thanks, \footnotetext between paragraphs) and
                    -- has no paragraph to hold its reference. The page
                    -- builder is about to remove the `ins` node itself, so
                    -- a stand-in whatsit goes in front of it (insertion
                    -- before never moves the list's tail) for the walk to
                    -- emit as a `footnote_ref` stream item. Layout-neutral:
                    -- zero-size, non-discardable exactly as the insert
                    -- beside it, no effect on \prevdepth or breakpoints.
                    local w = node.new("whatsit", "user_defined")
                    w.user_id = LAX_FOOTNOTE_REF_ID
                    w.type    = 100      -- a number value
                    w.value   = k
                    local new_head = node.insert_before(head, n, w)
                    if new_head ~= head then
                        tex.lists.contrib_head = new_head
                        head = new_head
                    end
                end
                if not footnote_skip then
                    local ok, w = pcall(tex.getglue, n.subtype)
                    footnote_skip = ok and w or 0
                end
                -- Walk the insert's list into the endnote stream now; the
                -- band and leading state belong to the body walk and are
                -- kept out of its way.
                local saved_content = content
                local saved_band = { indent = cur_band.indent, width = cur_band.width }
                local saved_leading = cur_leading
                content = footnote_content
                walk_page(n.head, footnote_pending, all_paragraphs[paras[1]].width)
                -- Marked only now: the walk above must see these lines as
                -- ordinary paragraphs; the page walk later must not. The
                -- paragraph entry carries the footnote's ordinal too (a
                -- schema field, Paragraph.footnote): the stream relocates
                -- these to the end while the PDF keeps them at page
                -- bottoms, so a consumer comparing the two needs to know
                -- which paragraphs moved, and a viewer setting footnote k
                -- beside its `footnote_ref` needs to know which paragraphs
                -- are k.
                for _, p in ipairs(paras) do
                    footnote_paras[p] = true
                    all_paragraphs[p].footnote = k
                end
                content = saved_content
                cur_band.indent, cur_band.width = saved_band.indent, saved_band.width
                cur_leading = saved_leading
            end
        end
    end
end

Serializer = Serializer or {}   -- note_picture already added a table above; do not clobber it

-- LaTeX's page (\@outputpage, ltoutput) is
--   \vbox{ \@begindvi \vskip\topmargin
--          \moveright\@themargin\vbox{ \vbox to\headheight{\vfil\hbox to\textwidth{\@thehead}}
--                                      \vskip\headsep
--                                      \box\@outputbox
--                                      \baselineskip\footskip
--                                      \hbox to\textwidth{\@thefoot} } }
-- The running head and the foot carry ink (a page number, a running title)
-- that must not become paragraphs now that boxes in a vertical list are
-- content, so the body is located rather than the whole page walked: in each
-- top-level vlist (the frame), the first vlist is the head box when it is
-- packed to \headheight, hlists are the foot, and the glue between them is
-- \headsep/\footskip — page furniture, never body spacing. Every other
-- vlist there (the \@outputbox, normally exactly one) is walked. A page with
-- no frame vlist at all — a class with its own output routine — is walked
-- whole, as before; its furniture then surfaces as content.
--
-- lax: that anatomy is a heuristic (a head box of a changed \headheight, a
-- class that builds its page differently). When laxreflow.sty has stamped
-- the \@outputbox (BODY_ATTR, set on the box node just before \@outputpage
-- moves it into the page), the walk prefers the stamped vlists — every one
-- found anywhere in the shipped tree, in order, never descending into one —
-- and falls back to the anatomy only when the page carries none.
local body_route_logged = false
local function body_vlists(head, out)
    for n in node.traverse(head) do
        local t = node.type(n.id)
        if (t == "vlist" or t == "hlist") then
            if t == "vlist" and node.get_attribute(n, BODY_ATTR) == 1 then
                out[#out + 1] = n
            else
                body_vlists(n.head, out)
            end
        end
    end
    return out
end

function Serializer.shipout(boxnum)
    local b = tex.box[boxnum]
    if not b then
        texio.write_nl("serializer: shipout box " .. tostring(boxnum) .. " is nil")
        return
    end
    local pending = { sp = 0, explicit = 0, body_seen = false }
    local bodies = body_vlists(b.head, {})
    if not body_route_logged then
        body_route_logged = true
        texio.write_nl(string.format("serializer: page body located by %s",
            #bodies > 0 and "attribute (laxreflow \\@outputpage stamp)" or "output-routine anatomy"))
    end
    if #bodies > 0 then
        for _, body in ipairs(bodies) do
            walk_page(body.head, pending, body.width)
        end
        return
    end
    local frames = {}
    for n in node.traverse(b.head) do
        if node.type(n.id) == "vlist" then frames[#frames + 1] = n end
    end
    if #frames == 0 then
        walk_page(b.head, pending, b.width)
        return
    end
    local headheight = tex.dimen.headheight or 0
    for _, frame in ipairs(frames) do
        local first = true
        for c in node.traverse(frame.head) do
            if node.type(c.id) == "vlist" then
                if not (first and c.height == headheight) then
                    walk_page(c.head, pending, c.width)
                end
                first = false
            end
        end
    end
end

local function write_output()
    -- The endnotes: TeX's own \footnoterule (see endnote_sep) with
    -- \skip\footins above it, then every footnote in document order.
    if #footnote_content > 0 then
        local first_para
        for _, it in ipairs(footnote_content) do
            if it.kind == "paragraph" then first_para = all_paragraphs[it.para]; break end
        end
        local band_w = first_para and first_para.width or cur_band.width
        local sep = endnote_sep or { width = math.floor(band_w * 0.4), height = 26214, depth = 0 }
        local pending = { sp = footnote_skip or 0, explicit = footnote_skip or 0, body_seen = #content > 0 }
        cur_band.indent = first_para and first_para.indent or 0
        cur_band.width  = band_w
        emit_rule_display({ width = sep.width, height = sep.height, depth = sep.depth }, band_w, pending)
        emit_vspace(endnote_sep and endnote_sep.below or 0)
        for _, it in ipairs(footnote_content) do content[#content + 1] = it end
    end
    local f = assert(io.open("output.json", "w"))
    f:write(json_encode({
        fonts      = used_fonts,
        paragraphs = all_paragraphs,
        content    = content,
    }))
    f:close()
    local n_disp = 0
    for _, it in ipairs(content) do
        if it.kind == "display" then n_disp = n_disp + 1 end
    end
    texio.write_nl(string.format(
        "serializer: wrote output.json (%d paragraph(s), %d item(s) in stream, %d display(s))",
        #all_paragraphs, #content, n_disp))
end

luatexbase.add_to_callback("pre_linebreak_filter",   capture_paragraph, "capture_paragraph")
luatexbase.add_to_callback("buildpage_filter",       note_inserts,      "note_inserts")
luatexbase.add_to_callback("finish_pdffile",         write_output,       "write_output")
