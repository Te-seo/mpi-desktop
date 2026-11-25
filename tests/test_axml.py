"""Unit tests for the binary AndroidManifest / resources.arsc reader.

The parser is validated end-to-end against real APKs on a device during
development; these tests pin the wire-format handling with hand-built fixtures
so a future refactor can't silently regress the label path, and assert the
defensive contract (malformed input yields ``None``, never an exception).
"""

from __future__ import annotations

import struct

from mpi.android import axml

_ANDROID_URI = "http://schemas.android.com/apk/res/android"

# Chunk type codes mirrored from the parser, kept local so a typo in the parser
# can't make a test vacuously agree with it.
_RES_STRING_POOL = 0x0001
_RES_TABLE = 0x0002
_RES_XML = 0x0003
_RES_XML_START_ELEMENT = 0x0102
_RES_TABLE_PACKAGE = 0x0200
_RES_TABLE_TYPE = 0x0201
_TYPE_REFERENCE = 0x01
_TYPE_STRING = 0x03


def _len_utf8(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    return bytes([0x80 | (n >> 8), n & 0xFF])


def _utf8_string_pool(strings: list[str]) -> bytes:
    blob = bytearray()
    offsets = []
    for s in strings:
        offsets.append(len(blob))
        encoded = s.encode("utf-8")
        blob += _len_utf8(len(s))  # char count (UTF-16 units; ASCII == byte len)
        blob += _len_utf8(len(encoded))  # byte count
        blob += encoded
        blob += b"\x00"
    header_size = 28
    strings_start = header_size + len(strings) * 4
    chunk_size = strings_start + len(blob)
    pad = (-chunk_size) % 4
    chunk_size += pad
    out = bytearray()
    out += struct.pack("<HHI", _RES_STRING_POOL, header_size, chunk_size)
    out += struct.pack("<IIIII", len(strings), 0, 1 << 8, strings_start, 0)  # UTF8_FLAG
    for off in offsets:
        out += struct.pack("<I", off)
    out += blob
    out += b"\x00" * pad
    return bytes(out)


def _start_element(name_idx: int, attrs: list[tuple]) -> bytes:
    # attrs: (ns_idx, name_idx, raw_idx, data_type, data)
    header_size = 16  # ResChunk(8) + lineNumber(4) + comment(4)
    body = struct.pack("<II", 1, 0xFFFFFFFF)  # lineNumber, comment(none)
    body += struct.pack(
        "<IIHHHHHH",
        0xFFFFFFFF,  # element namespace (none)
        name_idx,
        20,  # attributeStart
        20,  # attributeSize
        len(attrs),
        0,
        0,
        0,
    )
    for ns_idx, a_name_idx, raw_idx, data_type, data in attrs:
        body += struct.pack("<IIIHBBI", ns_idx, a_name_idx, raw_idx, 8, 0, data_type, data)
    return struct.pack("<HHI", _RES_XML_START_ELEMENT, header_size, 8 + len(body)) + body


def _axml(attr_type: int, attr_data: int, raw_idx: int) -> bytes:
    # pool: 0=android uri, 1="label", 2="application", 3="MyApp"
    pool = _utf8_string_pool([_ANDROID_URI, "label", "application", "MyApp"])
    elem = _start_element(2, [(0, 1, raw_idx, attr_type, attr_data)])
    body = pool + elem
    return struct.pack("<HHI", _RES_XML, 8, 8 + len(body)) + body


def _minimal_arsc(value: str, *, compact: bool = False) -> bytes:
    """A one-string resource table: value at id 0x7f010000.

    With ``compact=True`` the entry uses the packed 8-byte FLAG_COMPACT form
    that modern AAPT2 emits (key in ``size``, data type in ``flags`` high byte,
    value in the trailing uint32) instead of the classic entry + Res_value.
    """
    config = struct.pack("<I", 28) + b"\x00" * 24  # default config (no locale)
    type_body = struct.pack("<BBHII", 1, 0, 0, 1, 52)  # id, flags, reserved, count, entriesStart
    type_body += config
    type_body += struct.pack("<I", 0)  # offset table: entry 0 at offset 0
    if compact:
        # size=key idx(0), flags=(TYPE_STRING<<8)|FLAG_COMPACT, data=value pool idx 0
        type_body += struct.pack("<HHI", 0, (_TYPE_STRING << 8) | 0x0008, 0)
    else:
        type_body += struct.pack("<HHI", 8, 0, 0)  # ResTable_entry: size, flags, key
        type_body += struct.pack("<HBBI", 8, 0, _TYPE_STRING, 0)  # Res_value -> value pool idx 0
    type_chunk = struct.pack("<HHI", _RES_TABLE_TYPE, 48, 8 + len(type_body)) + type_body

    pkg_fields = struct.pack("<I", 0x7F) + b"\x00" * 256 + struct.pack("<IIII", 0, 0, 0, 0)
    pkg_body = pkg_fields + type_chunk
    pkg_chunk = struct.pack("<HHI", _RES_TABLE_PACKAGE, 8 + len(pkg_fields), 8 + len(pkg_body))
    pkg_chunk += pkg_body

    value_pool = _utf8_string_pool([value])
    after_header = value_pool + pkg_chunk
    return struct.pack("<HHII", _RES_TABLE, 12, 12 + len(after_header), 1) + after_header


def test_manifest_inline_string_label():
    axml_bytes = _axml(_TYPE_STRING, 3, 3)  # TYPE_STRING -> pool idx 3 ("MyApp")
    assert axml.parse_application_label(axml_bytes) == "MyApp"


def test_manifest_reference_label_returns_resource_id():
    res_id = 0x7F010000
    axml_bytes = _axml(_TYPE_REFERENCE, res_id, 0xFFFFFFFF)
    assert axml.parse_application_label(axml_bytes) == res_id


def test_arsc_resolves_reference_to_string():
    assert axml.resolve_resource_string(_minimal_arsc("AppName"), 0x7F010000) == "AppName"


def test_arsc_resolves_unicode_value():
    assert axml.resolve_resource_string(_minimal_arsc("네이버"), 0x7F010000) == "네이버"


def test_arsc_resolves_compact_entry():
    arsc = _minimal_arsc("Compact App", compact=True)
    assert axml.resolve_resource_string(arsc, 0x7F010000) == "Compact App"


def _chain_arsc(value: str) -> bytes:
    """Resource table where entry 0 is a REFERENCE to entry 1 (a string), i.e.
    0x7f010000 -> @0x7f010001 -> value. Mirrors the indirection real apps use
    (``application@label`` aliasing another string resource)."""
    config = struct.pack("<I", 28) + b"\x00" * 24
    entries_start = 48 + 2 * 4  # header(48) + 2-entry uint32 offset table
    type_body = struct.pack("<BBHII", 1, 0, 0, 2, entries_start)
    type_body += config
    type_body += struct.pack("<II", 0, 16)  # offset table: entry0@0, entry1@16
    type_body += struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, _TYPE_REFERENCE, 0x7F010001)
    type_body += struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, _TYPE_STRING, 0)
    type_chunk = struct.pack("<HHI", _RES_TABLE_TYPE, 48, 8 + len(type_body)) + type_body

    pkg_fields = struct.pack("<I", 0x7F) + b"\x00" * 256 + struct.pack("<IIII", 0, 0, 0, 0)
    pkg_body = pkg_fields + type_chunk
    pkg_chunk = struct.pack("<HHI", _RES_TABLE_PACKAGE, 8 + len(pkg_fields), 8 + len(pkg_body))
    pkg_chunk += pkg_body

    after_header = _utf8_string_pool([value]) + pkg_chunk
    return struct.pack("<HHII", _RES_TABLE, 12, 12 + len(after_header), 1) + after_header


def test_arsc_follows_reference_chain():
    # A real-world shape: the label resource aliases another string resource.
    assert axml.resolve_resource_string(_chain_arsc("KakaoNavi"), 0x7F010000) == "KakaoNavi"


def test_end_to_end_reference_then_resolve():
    axml_bytes = _axml(_TYPE_REFERENCE, 0x7F010000, 0xFFFFFFFF)
    label = axml.parse_application_label(axml_bytes)
    assert isinstance(label, int)
    assert axml.resolve_resource_string(_minimal_arsc("Good Lock"), label) == "Good Lock"


def test_arsc_missing_id_returns_none():
    assert axml.resolve_resource_string(_minimal_arsc("AppName"), 0x7F010005) is None
    # wrong package / type ids must also miss rather than mis-resolve.
    assert axml.resolve_resource_string(_minimal_arsc("AppName"), 0x7E010000) is None
    assert axml.resolve_resource_string(_minimal_arsc("AppName"), 0x7F020000) is None


def test_graceful_on_garbage():
    for blob in (b"", b"\x00\x00", b"not a real chunk", b"\x03\x00\x08\x00\xff\xff\xff\xff"):
        assert axml.parse_application_label(blob) is None
        assert axml.resolve_resource_string(blob, 0x7F010000) is None
