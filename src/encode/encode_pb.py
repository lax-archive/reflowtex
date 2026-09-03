#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encode a node-list output.json into a protobuf Document (latex.proto).

Replaces encode-standalone.mjs (kiwi). Descriptor-driven: fields are set by
walking the message descriptor, so the .proto is the single source of truth for
which fields exist and their types — the encoder can't drift from it. proto2
presence is preserved by only setting keys that exist in the source dict.

Usage: python3 encode_pb.py <output.json> <nodelist.pb>
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
# lax: latex_pb2.py is regenerated from the (patched) schema into the
# checkout's build/ area at fetch time — never at run time, and never into
# this tree (see pipeline._ensure_pb2). Prefer that copy over any stale
# committed one, or the marker fields would silently be missing.
_BUILD_DIR = Path(__file__).resolve().parent.parent.parent / 'build'
if (_BUILD_DIR / 'latex_pb2.py').exists():
    sys.path.insert(0, str(_BUILD_DIR))
import latex_pb2 as L
from google.protobuf.descriptor import FieldDescriptor as FD

# Match encode-standalone.mjs: these are omitted when 0 (the renderer treats
# absent and 0 identically for them), keeping the wire form small and the decoded
# document byte-identical in shape to the kiwi one.
OMIT_IF_ZERO = {'stretch_order', 'shrink_order', 'subtype', 'shift', 'glue_sign', 'glue_order'}
# Fields the pipeline carries but the wire format does not (dropped, as stripNode did).
NOT_ENCODED = {'file', 'gindex'}
# lax: proto2 scopes enum value names to the package, so NodeType cannot also
# name a value 'marker' (ItemKind claims it). The serializer's JSON keeps
# type:"marker" for nodes; on the wire that is NodeType.mark.
ENUM_ALIASES = {('latex.NodeType', 'marker'): 'mark'}


def intern_glyph_metrics(doc: dict) -> None:
    """Replace each glyph's inline width/height/depth with a 1-based `metrics`
    index into doc['glyph_metrics'] (deduped). Mirrors internGlyphMetrics()."""
    table, seen = [], {}

    def intern(w, h, d):
        key = (w, h, d)
        i = seen.get(key)
        if i is None:
            table.append({'width': w, 'height': h, 'depth': d})
            i = len(table)
            seen[key] = i
        return i

    def walk(nodes):
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            if n.get('type') == 'glyph':
                n['metrics'] = intern(n.get('width', 0) or 0,
                                      n.get('height', 0) or 0,
                                      n.get('depth', 0) or 0)
                n.pop('width', None); n.pop('height', None); n.pop('depth', None)
            walk(n.get('children')); walk(n.get('pre')); walk(n.get('post')); walk(n.get('replace'))
            if n.get('leader'):
                walk([n['leader']])

    for p in doc.get('paragraphs', []):
        walk(p.get('nodes'))
    for it in doc.get('content', []):
        if it.get('box'):
            walk([it['box']])
    doc['glyph_metrics'] = table


def fill(msg, d: dict) -> None:
    """Set message fields from a dict, driven by the message descriptor."""
    fields = msg.DESCRIPTOR.fields_by_name
    for k, v in d.items():
        if v is None or k in NOT_ENCODED:
            continue
        f = fields.get(k)
        if f is None:                       # not in the schema -> drop (like stripNode)
            continue
        if k in OMIT_IF_ZERO and v == 0:
            continue
        if f.type == FD.TYPE_ENUM:
            # lax: name the schema when an enum value is unknown — a stale
            # latex_pb2.py meeting a marker item otherwise dies as a bare
            # KeyError: 'marker' three frames deep.
            wire_name = ENUM_ALIASES.get((f.enum_type.full_name, v), v)
            value = f.enum_type.values_by_name.get(wire_name)
            if value is None:
                raise ValueError(f'{f.enum_type.full_name} has no value {v!r} — '
                                 f'latex_pb2.py is out of step with latex.proto '
                                 f'(regenerate it at fetch time)')
            setattr(msg, k, value.number)
        elif f.type == FD.TYPE_MESSAGE:
            if f.is_repeated:
                for item in v:
                    fill(getattr(msg, k).add(), item)
            else:
                fill(getattr(msg, k), v)
        else:
            if f.is_repeated:
                getattr(msg, k).extend(v)
            else:
                setattr(msg, k, v)


def build_document(data: dict) -> L.Document:
    doc = {
        # fonts is a map keyed by id in output.json; the wire form is a list.
        'fonts': [{'id': int(k), 'name': f['name'], 'size_sp': f['size_sp'], 'filename': f['filename']}
                  for k, f in data['fonts'].items()],
        'paragraphs': data.get('paragraphs', []),
        'content': data.get('content', []),
        'pictures': data.get('pictures', []),
    }
    intern_glyph_metrics(doc)
    out = L.Document()
    fill(out, doc)
    return out


def serialize_document(data: dict) -> bytes:
    """The one serialization site (lax): deterministic=True makes protobuf's
    ordering guarantees explicit — the bundle digest must not depend on which
    runtime serialized it — rather than measured-stable."""
    return build_document(data).SerializeToString(deterministic=True)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit('Usage: python3 encode_pb.py <output.json> <nodelist.pb>')
    data = json.loads(Path(sys.argv[1]).read_text())
    blob = serialize_document(data)
    Path(sys.argv[2]).write_bytes(blob)
    print(f'{sys.argv[2]}  {len(blob)} bytes')


if __name__ == '__main__':
    main()
