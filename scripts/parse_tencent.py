"""Decode observed Tencent sheet preload protobuf packages, without networking.

Preserve coordinates, value types, attributes and link evidence. No business-row
merging, cost allocation or external writes are performed here.
"""
import base64
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib


def varint(data, pos):
    value, shift = 0, 0
    while pos < len(data) and shift <= 63:
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, pos
        shift += 7
    raise ValueError('Invalid protobuf varint')


def message(data):
    result, pos = [], 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = data[pos:pos + size]
            pos += size
            if len(value) != size:
                raise ValueError('Truncated fixed-width field')
        elif wire == 2:
            size, pos = varint(data, pos)
            value = data[pos:pos + size]
            pos += size
            if len(value) != size:
                raise ValueError('Truncated length-delimited field')
        else:
            raise ValueError('Unsupported protobuf wire type')
        result.append((field, wire, value))
    return result


def get(fields, number, default=None):
    return next((value for field, wire, value in fields if field == number), default)


def values(fields, number):
    return [value for field, wire, value in fields if field == number]


def nested_value(raw, number=1, default=None):
    return get(message(raw or b''), number, default)


def envelope(text):
    start = text.find('{')
    if start < 0:
        raise ValueError('No JSON envelope in public preload response')
    result, _ = json.JSONDecoder().raw_decode(text[start:])
    if not result.get('clientVars', {}).get('collab_client_vars', {}).get('initialAttributedText'):
        raise ValueError('Response does not contain sheet data')
    return result


def operations(raw):
    return [message(value) for value in values(message(get(message(raw), 1)), 5)]


def decompress(text):
    return zlib.decompress(base64.b64decode(text, validate=True))


def sheets_from_workbook(raw):
    sheets = []
    for op in operations(raw):
        if get(op, 1) != 1:
            continue
        payload = message(get(op, 2))
        sheets.append({'sheet_id': nested_value(get(payload, 3)).decode('utf-8'),
                       'name': nested_value(get(payload, 5)).decode('utf-8'),
                       'rows': nested_value(get(payload, 7), default=0),
                       'columns': nested_value(get(payload, 8), default=0)})
    return sheets


def leaves(raw, path=(), depth=0):
    if not raw or depth > 20:
        return []
    try:
        fields = message(raw)
    except ValueError:
        try:
            text = raw.decode('utf-8')
            return [(path, text)] if text else []
        except UnicodeDecodeError:
            return []
    result = []
    for field, wire, value in fields:
        if wire == 2:
            result.extend(leaves(value, path + (field,), depth + 1))
    return result


URL = re.compile(r'https?://[^\s<>"\x00-\x1f]+')


def link_leaves(raw, origin):
    result = []
    for path, text in leaves(raw):
        for match in URL.findall(text):
            result.append({'url': match, 'origin': origin, 'wire_path': list(path)})
    return result


def parse_cells(raw):
    all_cells, ranges, unknowns, merges = [], [], [], []
    for op_index, op in enumerate(operations(raw)):
        if get(op, 1) == 5:
            merge = message(get(message(get(op, 6)), 3))
            merges.append({'sheet_id': get(merge, 1, b'').decode('utf-8'),
                           'start_row': get(merge, 2, 0), 'start_col': get(merge, 3, 0),
                           'end_row': get(merge, 4, 0), 'end_col': get(merge, 5, 0),
                           'schema_evidence': 'Official initial_property MutationType.MergeCells=5; gridRangeData fields.'})
            continue
        if get(op, 1) != 18:
            continue
        payload = message(get(op, 19))
        box = message(get(payload, 3, b''))
        sid = get(box, 1, b'').decode('utf-8')
        if sid:
            ranges.append({'sheet_id': sid, 'start_row': get(box, 2, 0), 'start_col': get(box, 3, 0),
                           'end_row': get(box, 4, 0), 'end_col': get(box, 5, 0), 'operation': op_index})
        pool = message(get(payload, 5, b''))
        strings = [nested_value(value, default=b'').decode('utf-8') for value in values(pool, 1)]
        numbers = [struct.unpack('<d', nested_value(value))[0] for value in values(pool, 3)]
        formula_values = values(pool, 4)
        formula_definitions = values(pool, 5)
        rich = []
        for item in values(pool, 2):
            fragments, links = [], []
            for part in values(message(item), 3):
                part_fields = message(part)
                text = nested_value(get(part_fields, 3, b''), default=b'').decode('utf-8')
                fragments.append(text)
                links.extend(link_leaves(get(part_fields, 1, b''), 'rich_text_attribute'))
                # Preserve text URLs separately; they do not prove a hyperlink.
                links.extend({'url': url, 'origin': 'displayed_url', 'wire_path': [3, 1]} for url in URL.findall(text))
            rich.append({'text': ''.join(fragments), 'links': links})
        for cell_raw in values(payload, 6):
            cell = message(cell_raw)
            row, col = get(cell, 1, 0), get(cell, 2, 0)
            value_raw = get(cell, 3, b'')
            value_fields = message(value_raw)
            kind = get(value_fields, 1, 0)
            index = nested_value(get(value_fields, 2, b''), default=0)
            decoded, links, formula = None, [], None
            if kind == 4:
                decoded = strings[index]
                links = [{'url': url, 'origin': 'displayed_url', 'wire_path': []} for url in URL.findall(decoded)]
            elif kind == 2:
                decoded = float(index) if index < 129 else numbers[index - 129]
            elif kind == 6:
                decoded, links = rich[index]['text'], list(rich[index]['links'])
            elif kind == 5:
                formula_entry = message(formula_values[index])
                cached = message(get(formula_entry, 1, b''))
                cached_kind = get(cached, 1, 0)
                cached_index = nested_value(get(cached, 3, b''), default=0)
                if cached_kind == 2:
                    decoded = float(cached_index) if cached_index < 129 else numbers[cached_index - 129]
                elif cached_kind == 4:
                    decoded = strings[cached_index]
                definition_index = nested_value(get(formula_entry, 3, b''), default=0)
                definition = formula_definitions[definition_index]
                formula = {'cached_value': decoded, 'cached_kind': cached_kind,
                           'raw_formula_base64': base64.b64encode(definition).decode('ascii'),
                           'formula_text': None, 'note': 'Original formula token stream preserved; value is the source cached result.'}
            elif kind != 0:
                unknowns.append({'sheet_id': sid, 'row': row, 'col': col, 'kind': kind})
            # Raw cell attributes may contain actual hyperlinks or formulas.
            for field, wire, blob in value_fields:
                if wire == 2 and field not in (2, 4):
                    links.extend(link_leaves(blob, 'attachment_url' if field == 17 else 'cell_attribute:' + str(field)))
            all_cells.append({'sheet_id': sid, 'row': row, 'col': col, 'a1': column_name(col) + str(row + 1),
                              'value_kind': kind, 'value': decoded, 'links': links,
                              'formula': formula,
                              'wire_sha256': hashlib.sha256(cell_raw).hexdigest(),
                              'raw_cell_base64': base64.b64encode(cell_raw).decode('ascii'),
                              'operation': op_index})
    return {'cells': all_cells, 'ranges': ranges, 'merge_ranges': merges, 'unknown_value_kinds': unknowns}


def column_name(index):
    text = ''
    index += 1
    while index:
        index, part = divmod(index - 1, 26)
        text = chr(65 + part) + text
    return text


def parse_package(text):
    document = envelope(text)
    collab = document['clientVars']['collab_client_vars']
    chunks, sheets = [], []
    for item in collab['initialAttributedText']['text']:
        if item.get('workbook'):
            sheets.extend(sheets_from_workbook(decompress(item['workbook'])))
        for block in item.get('block_datas', []):
            raw = decompress(block['related_sheet'])
            chunks.append({'metadata': {key: value for key, value in block.items() if key != 'related_sheet'},
                           'decoded_sha256': hashlib.sha256(raw).hexdigest(), 'decoded_bytes': len(raw),
                           **parse_cells(raw)})
    return {'sheets': sheets, 'chunks': chunks,
            'declared_max_row': collab.get('maxRow'), 'declared_max_col': collab.get('maxCol')}
