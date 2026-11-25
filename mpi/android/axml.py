"""Minimal binary AndroidManifest (AXML) + ``resources.arsc`` (ARSC) reader.

Just enough to resolve an installed app's user-visible label
(``<application android:label=...>``) without aapt, on-device tooling, or any
third-party dependency. Android ships no shell command that returns the label
*string* — ``dumpsys package`` only exposes the ``labelRes`` resource id — so we
parse the two binary resources straight out of the APK (streamed over adb, see
:meth:`AndroidBackend.fetch_app_label`).

Resolution is two-step, mirroring how the platform itself resolves a label:

1. Parse the binary ``AndroidManifest.xml`` to read ``application@android:label``.
   It is usually a *reference* (a ``@string/...`` resource id), occasionally an
   inline string.
2. When it is a reference, look the id up in ``resources.arsc`` and return the
   string value, preferring the default (no-locale) configuration.

Both formats are Google's chunked ``ResChunk`` layout documented in AOSP's
``ResourceTypes.h``; only the chunks needed for the label path are implemented.
Everything is defensive: malformed input yields ``None`` rather than raising, so
a single weird APK degrades to showing the bundle id, never a crash.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple, Union

# --- ResChunk type codes (subset) ----------------------------------------
_RES_STRING_POOL_TYPE = 0x0001
_RES_TABLE_TYPE = 0x0002
_RES_XML_TYPE = 0x0003
_RES_XML_RESOURCE_MAP_TYPE = 0x0180
_RES_XML_START_ELEMENT_TYPE = 0x0102
_RES_TABLE_PACKAGE_TYPE = 0x0200
_RES_TABLE_TYPE_TYPE = 0x0201

# String-pool header flags.
_UTF8_FLAG = 1 << 8

# Res_value data types we care about.
_TYPE_REFERENCE = 0x01
_TYPE_STRING = 0x03

# ResTable_type entry-offset encodings (the byte at type-chunk offset +9).
_TYPE_FLAG_SPARSE = 0x01
_TYPE_FLAG_OFFSET16 = 0x02

# ResTable_entry flags.
_ENTRY_FLAG_COMPLEX = 0x0001
_ENTRY_FLAG_COMPACT = 0x0008

_NO_ENTRY32 = 0xFFFFFFFF
_NO_ENTRY16 = 0xFFFF
_NO_REF = 0xFFFFFFFF

# Resource id of the framework ``android:label`` attribute. Matching by id (via
# the XML resource map) is more robust than matching the attribute name string,
# which some obfuscated manifests omit from the pool.
_ATTR_LABEL_RES_ID = 0x01010001
_ANDROID_NS = "http://schemas.android.com/apk/res/android"


# --- String pools ---------------------------------------------------------


class _StringPool:
    __slots__ = ("strings",)

    def __init__(self, strings: List[str]):
        self.strings = strings

    def get(self, index: int) -> Optional[str]:
        if index is None or index < 0 or index >= len(self.strings):
            return None
        return self.strings[index]


def _read_len_utf8(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode a UTF-8 pool length prefix (1 or 2 bytes). Returns (length, pos)."""
    first = data[pos]
    pos += 1
    if first & 0x80:
        length = ((first & 0x7F) << 8) | data[pos]
        pos += 1
    else:
        length = first
    return length, pos


def _read_len_utf16(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode a UTF-16 pool length prefix (1 or 2 words). Returns (length, pos)."""
    first = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    if first & 0x8000:
        second = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        length = ((first & 0x7FFF) << 16) | second
    else:
        length = first
    return length, pos


def _read_string_pool(data: bytes, offset: int) -> Optional[_StringPool]:
    """Parse a ResStringPool chunk starting at ``offset``."""
    try:
        _type, _header_size, _size = struct.unpack_from("<HHI", data, offset)
        string_count, _style_count, flags, strings_start, _styles_start = (
            struct.unpack_from("<IIIII", data, offset + 8)
        )
    except struct.error:
        return None

    is_utf8 = bool(flags & _UTF8_FLAG)
    offsets_base = offset + 28  # 8-byte ResChunk header + 5 × uint32 fields
    data_base = offset + strings_start
    strings: List[str] = []
    for i in range(string_count):
        try:
            rel = struct.unpack_from("<I", data, offsets_base + i * 4)[0]
            pos = data_base + rel
            if is_utf8:
                _char_len, pos = _read_len_utf8(data, pos)
                byte_len, pos = _read_len_utf8(data, pos)
                value = data[pos : pos + byte_len].decode("utf-8", "replace")
            else:
                unit_len, pos = _read_len_utf16(data, pos)
                value = data[pos : pos + unit_len * 2].decode("utf-16-le", "replace")
        except (struct.error, IndexError):
            value = ""
        strings.append(value)
    return _StringPool(strings)


# --- AndroidManifest.xml (binary XML) ------------------------------------

_NOT_APPLICATION = object()


def parse_application_label(axml: bytes) -> Optional[Union[str, int]]:
    """Return ``application@android:label``: a ``str`` (inline value) or an
    ``int`` resource id to resolve via :func:`resolve_resource_string`. Returns
    ``None`` when the manifest is unparseable or carries no label."""
    if len(axml) < 8:
        return None
    try:
        file_type, header_size, _file_size = struct.unpack_from("<HHI", axml, 0)
    except struct.error:
        return None
    if file_type != _RES_XML_TYPE:
        return None

    pool: Optional[_StringPool] = None
    resource_map: List[int] = []
    pos = header_size or 8
    n = len(axml)
    while pos + 8 <= n:
        try:
            c_type, c_header, c_size = struct.unpack_from("<HHI", axml, pos)
        except struct.error:
            break
        if c_size < 8:
            break
        if c_type == _RES_STRING_POOL_TYPE and pool is None:
            pool = _read_string_pool(axml, pos)
        elif c_type == _RES_XML_RESOURCE_MAP_TYPE:
            count = (c_size - c_header) // 4
            try:
                resource_map = list(
                    struct.unpack_from("<%dI" % count, axml, pos + c_header)
                )
            except struct.error:
                resource_map = []
        elif c_type == _RES_XML_START_ELEMENT_TYPE:
            label = _start_element_label(axml, pos, c_header, pool, resource_map)
            if label is not _NOT_APPLICATION:
                return label  # type: ignore[return-value]  # str | int | None
        pos += c_size
    return None


def _start_element_label(
    data: bytes,
    pos: int,
    header_size: int,
    pool: Optional[_StringPool],
    resource_map: List[int],
) -> object:
    """Inspect one START_ELEMENT chunk. Returns the label value when the element
    is ``<application>`` (``str`` / ``int`` / ``None``), else ``_NOT_APPLICATION``."""
    if pool is None:
        return _NOT_APPLICATION
    base = pos + header_size  # ResXMLTree_attrExt
    try:
        _ns, name_ref, attr_start, attr_size, attr_count = struct.unpack_from(
            "<IIHHH", data, base
        )
    except struct.error:
        return _NOT_APPLICATION
    if pool.get(name_ref) != "application":
        return _NOT_APPLICATION

    attrs_base = base + attr_start
    for i in range(attr_count):
        try:
            a_ns, a_name, a_raw, _v_size, _v_res0, v_type, v_data = struct.unpack_from(
                "<IIIHBBI", data, attrs_base + i * attr_size
            )
        except struct.error:
            break
        if not _attr_is_label(a_ns, a_name, pool, resource_map):
            continue
        if v_type == _TYPE_REFERENCE:
            return int(v_data)
        if v_type == _TYPE_STRING:
            return pool.get(v_data)
        if a_raw != _NO_REF:
            return pool.get(a_raw)
        return None
    return None  # <application> present but unlabeled


def _attr_is_label(
    a_ns: int, a_name: int, pool: _StringPool, resource_map: List[int]
) -> bool:
    if a_name < len(resource_map) and resource_map[a_name] == _ATTR_LABEL_RES_ID:
        return True
    if pool.get(a_name) == "label":
        ns = pool.get(a_ns)
        return ns in (_ANDROID_NS, None)
    return False


# --- resources.arsc (resource table) -------------------------------------


def resolve_resource_string(arsc: bytes, res_id: int) -> Optional[str]:
    """Resolve a resource id to its string value from ``resources.arsc``,
    preferring the default (no-locale) configuration. Follows reference chains
    (a label often points at ``@string/a`` which aliases ``@string/b``) up to a
    small depth. Returns ``None`` when the id is absent, resolves to a
    non-string, or the table is unparseable."""
    if not res_id or len(arsc) < 12:
        return None
    try:
        table_type, table_header, _table_size = struct.unpack_from("<HHI", arsc, 0)
    except struct.error:
        return None
    if table_type != _RES_TABLE_TYPE:
        return None

    value_pool: Optional[_StringPool] = None
    package_offsets: List[int] = []
    pos = table_header or 12
    n = len(arsc)
    while pos + 8 <= n:
        try:
            c_type, _c_header, c_size = struct.unpack_from("<HHI", arsc, pos)
        except struct.error:
            break
        if c_size < 8:
            break
        if c_type == _RES_STRING_POOL_TYPE and value_pool is None:
            value_pool = _read_string_pool(arsc, pos)
        elif c_type == _RES_TABLE_PACKAGE_TYPE:
            package_offsets.append(pos)
        pos += c_size
    if value_pool is None:
        return None

    seen: set[int] = set()
    current = res_id
    for _ in range(8):  # bounded so a self-referential alias can't loop forever
        if not current or current in seen:
            return None
        seen.add(current)
        resolved = _resolve_id(arsc, package_offsets, current, value_pool)
        if resolved is None:
            return None
        kind, value = resolved
        if kind == "string":
            return value
        current = value  # kind == "ref": follow the alias
    return None


def _resolve_id(
    data: bytes,
    package_offsets: List[int],
    res_id: int,
    value_pool: _StringPool,
) -> Optional[Tuple[str, object]]:
    target_pkg = (res_id >> 24) & 0xFF
    type_id = (res_id >> 16) & 0xFF
    entry_index = res_id & 0xFFFF
    for pkg_pos in package_offsets:
        found = _resolve_in_package(
            data, pkg_pos, target_pkg, type_id, entry_index, value_pool
        )
        if found is not None:
            return found
    return None


def _resolve_in_package(
    data: bytes,
    pkg_pos: int,
    target_pkg: int,
    type_id: int,
    entry_index: int,
    value_pool: _StringPool,
) -> Optional[Tuple[str, object]]:
    try:
        _c_type, c_header, c_size = struct.unpack_from("<HHI", data, pkg_pos)
        pkg_id = struct.unpack_from("<I", data, pkg_pos + 8)[0]
    except struct.error:
        return None
    if (pkg_id & 0xFF) != target_pkg:
        return None

    pkg_end = min(pkg_pos + c_size, len(data))
    best_score = -1
    best_value: Optional[Tuple[str, object]] = None
    pos = pkg_pos + c_header
    while pos + 8 <= pkg_end:
        try:
            c_type, _ch, cs = struct.unpack_from("<HHI", data, pos)
        except struct.error:
            break
        if cs < 8:
            break
        if c_type == _RES_TABLE_TYPE_TYPE:
            candidate = _resolve_in_type_chunk(
                data, pos, type_id, entry_index, value_pool
            )
            if candidate is not None:
                score, value = candidate
                if score > best_score:
                    best_score, best_value = score, value
        pos += cs
    return best_value


def _resolve_in_type_chunk(
    data: bytes,
    pos: int,
    type_id: int,
    entry_index: int,
    value_pool: _StringPool,
) -> Optional[Tuple[int, Tuple[str, object]]]:
    try:
        _c_type, c_header, _c_size = struct.unpack_from("<HHI", data, pos)
        chunk_type_id = data[pos + 8]
        flags = data[pos + 9]
        entry_count = struct.unpack_from("<I", data, pos + 12)[0]
        entries_start = struct.unpack_from("<I", data, pos + 16)[0]
        config_size = struct.unpack_from("<I", data, pos + 20)[0]
    except (struct.error, IndexError):
        return None
    if chunk_type_id != type_id:
        return None

    entry_off = _entry_offset(
        data, pos + c_header, entry_count, entry_index, flags
    )
    if entry_off is None:
        return None
    value = _read_entry_value(data, pos + entries_start + entry_off, value_pool)
    if value is None:
        return None
    return _config_score(data, pos + 20, config_size), value


def _entry_offset(
    data: bytes, table_off: int, entry_count: int, entry_index: int, flags: int
) -> Optional[int]:
    """Byte offset (from a type chunk's ``entriesStart``) of ``entry_index``'s
    entry, honouring the sparse / 16-bit / 32-bit offset encodings. ``None`` when
    the type has no entry at that index."""
    try:
        if flags & _TYPE_FLAG_SPARSE:
            for i in range(entry_count):
                idx, off = struct.unpack_from("<HH", data, table_off + i * 4)
                if idx == entry_index:
                    return off * 4
            return None
        if entry_index >= entry_count:
            return None
        if flags & _TYPE_FLAG_OFFSET16:
            off16 = struct.unpack_from("<H", data, table_off + entry_index * 2)[0]
            return None if off16 == _NO_ENTRY16 else off16 * 4
        off32 = struct.unpack_from("<I", data, table_off + entry_index * 4)[0]
        return None if off32 == _NO_ENTRY32 else off32
    except struct.error:
        return None


def _read_entry_value(
    data: bytes, pos: int, value_pool: _StringPool
) -> Optional[Tuple[str, object]]:
    """Read a resource entry's value as ``("string", text)`` or
    ``("ref", res_id)``; ``None`` for bag entries or any other value type."""
    try:
        entry_size, entry_flags = struct.unpack_from("<HH", data, pos)
    except struct.error:
        return None

    if entry_flags & _ENTRY_FLAG_COMPACT:
        # Compact entry packs everything into 8 bytes: the key index lives in the
        # `size` field, the Res_value data type in the high byte of `flags`, and
        # the value data in the trailing uint32.
        data_type = (entry_flags >> 8) & 0xFF
        try:
            value_data = struct.unpack_from("<I", data, pos + 4)[0]
        except struct.error:
            return None
        return _value_of(data_type, value_data, value_pool)

    if entry_flags & _ENTRY_FLAG_COMPLEX:
        return None  # bag/map entry, not a simple value

    try:
        _v_size, _v_res0, v_type, v_data = struct.unpack_from(
            "<HBBI", data, pos + entry_size
        )
    except struct.error:
        return None
    return _value_of(v_type, v_data, value_pool)


def _value_of(
    data_type: int, data: int, value_pool: _StringPool
) -> Optional[Tuple[str, object]]:
    if data_type == _TYPE_STRING:
        text = value_pool.get(data)
        return ("string", text) if text is not None else None
    if data_type == _TYPE_REFERENCE and data:
        return ("ref", data)
    return None


def _config_score(data: bytes, config_off: int, config_size: int) -> int:
    """Rank a ResTable_config: default (no locale) > English > anything else, so
    the displayed label prefers the developer's base name over a translation."""
    if config_size < 12:
        return 100  # default / empty config
    try:
        language = data[config_off + 8 : config_off + 10]
    except IndexError:
        return 0
    if language == b"\x00\x00":
        return 100
    if language == b"en":
        return 50
    return 10
