#!/usr/bin/env python3
"""Prepare pinned upstream executables during development, never at app runtime."""
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import struct
import tarfile
import urllib.parse
from xml.parsers.expat import ExpatError

from pinned_downloads import download

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.build'


def fetch(url, checksum):
    name = Path(urllib.parse.urlsplit(url).path).name
    if not name or name in ('.', '..'):
        raise ValueError('Pinned download must name an archive')
    # Keep the named path used by source-patch receipts.
    return download(url, checksum, BUILD / 'downloads' / name)


def unpack(archive, destination):
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe entry in precompiled release archive')
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        source.extractall(destination)


def input_directory(tool):
    return BUILD / 'precompiled' / tool['id'] / tool['version']


def prepare(tool):
    metadata = tool['precompiled']
    directory = input_directory(tool)
    archive = fetch(metadata['url'], metadata['sha256'])
    if metadata.get('format', 'tar') == 'binary':
        if directory.exists(): shutil.rmtree(directory)
        directory.mkdir(parents=True)
        shutil.copy2(archive, directory / Path(tool['executable']).name)
    else:
        unpack(archive, directory)
    executable = directory / (metadata.get('binaryPath') or Path(tool['executable']).name)
    if hashlib.sha256(executable.read_bytes()).hexdigest() != metadata['executableSHA256']:
        raise ValueError('Precompiled executable checksum mismatch')
    if program_digest(executable) != metadata['programSHA256']:
        raise ValueError('Precompiled program sections differ from the pinned release')


def executable_input(tool):
    return input_directory(tool) / (tool['precompiled'].get('binaryPath') or Path(tool['executable']).name)


def macho_sections(path):
    """Read bounded file-backed sections without executing an upstream binary."""
    data = path.read_bytes()
    if len(data) < 32 or struct.unpack_from('<I', data)[0] != 0xFEEDFACF:
        raise ValueError('Expected a thin 64-bit Mach-O executable')
    count = struct.unpack_from('<I', data, 16)[0]
    commands_end = 32 + struct.unpack_from('<I', data, 20)[0]
    if commands_end > len(data):
        raise ValueError('Invalid Mach-O load command bounds')
    cursor = 32
    sections = {}
    for _ in range(count):
        if cursor + 8 > commands_end:
            raise ValueError('Truncated Mach-O load command')
        command, length = struct.unpack_from('<II', data, cursor)
        if length < 8 or cursor + length > commands_end:
            raise ValueError('Invalid Mach-O load command')
        if command == 0x19:
            if length < 72:
                raise ValueError('Truncated Mach-O segment')
            section_count = struct.unpack_from('<I', data, cursor + 64)[0]
            if 72 + section_count * 80 > length:
                raise ValueError('Invalid Mach-O section table')
            for index in range(section_count):
                values = struct.unpack_from('<16s16sQQ8I', data, cursor + 72 + index * 80)
                name, segment, _, size, offset, _, _, _, flags, _, _, _ = values
                if flags & 0xFF in (1, 0xC, 0x12):
                    continue
                if offset + size > len(data):
                    raise ValueError('Invalid Mach-O section bounds')
                key = (segment.rstrip(b'\0').decode(), name.rstrip(b'\0').decode())
                if key in sections:
                    raise ValueError('Duplicate Mach-O section')
                sections[key] = memoryview(data)[offset:offset + size]
        cursor += length
    if cursor != commands_end:
        raise ValueError('Invalid Mach-O command count')
    if ('__TEXT', '__text') not in sections:
        raise ValueError('Executable machine code is missing')
    return sections


def section_hashes(path):
    """Compare program sections across re-signing without hashing the new signature."""
    return {key: hashlib.sha256(data).hexdigest() for key, data in macho_sections(path).items()}


def valid_signing_identifier(identifier):
    return (isinstance(identifier, str) and len(identifier) <= 255
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', identifier) is not None)


def embedded_signing_identifier(path):
    """App Store signatures must match an upstream embedded CFBundleIdentifier."""
    section = macho_sections(path).get(('__TEXT', '__info_plist'))
    if section is None:
        return None
    raw = bytes(section)
    # Some linkers retain a C string terminator in XML sections. Binary plists
    # have a binary trailer, so their zero bytes must not be stripped.
    if not raw.startswith(b'bplist'):
        raw = raw.rstrip(b'\0')
    try:
        info = plistlib.loads(raw)
    except (ValueError, TypeError, ExpatError, plistlib.InvalidFileException) as error:
        raise ValueError('Invalid embedded Info.plist in ' + str(path)) from error
    if not isinstance(info, dict):
        raise ValueError('Embedded Info.plist must be a dictionary in ' + str(path))
    if 'CFBundleIdentifier' not in info:
        return None
    identifier = info['CFBundleIdentifier']
    if not valid_signing_identifier(identifier):
        raise ValueError('Invalid embedded CFBundleIdentifier in ' + str(path))
    return identifier


def program_digest(path):
    sections = sorted((segment, name, digest) for (segment, name), digest in section_hashes(path).items())
    return hashlib.sha256(json.dumps(sections, separators=(',', ':')).encode()).hexdigest()
