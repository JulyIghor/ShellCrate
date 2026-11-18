#!/usr/bin/env python3
"""Acquire locked Homebrew bottles at build time, independent of the host prefix.

Only --update-lock consults current formula metadata. Ordinary builds download
content-addressed bottles, never run brew install or compile a missing bottle.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse

from precompiled_tools import program_digest, section_hashes
from pinned_downloads import digest, download as download_pinned, request
from package_metadata import validate_package, catalog_record
from archive_paths import resolve_member

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'Tools/homebrew-packages.json'
INDEX = ROOT / 'Tools/index.json'
WORK = ROOT / '.build/homebrew'
PROFILES = {'OfflineWorker': (), 'OutgoingWorker': ('outgoing',),
            'IncomingWorker': ('incoming',), 'BidirectionalWorker': ('outgoing', 'incoming')}


def load_config(path=CONFIG):
    config = json.loads(path.read_text())
    if (config.get('schemaVersion') != 1 or config.get('architecture') != 'arm64'
            or not re.fullmatch(r'arm64_[a-z_]+', config.get('bottleTag', ''))
            or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', config.get('minimumMacOS', ''))):
        raise ValueError('Invalid Homebrew target configuration')
    if not isinstance(config.get('packages'), list) or not config['packages']:
        raise ValueError('The package list must not be empty')
    seen = set()
    for entry in config['packages']:
        if (entry['id'] in seen or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', entry['id'])
                or not re.fullmatch(r'[a-z0-9][a-z0-9@+._-]*', entry['formula'])
                or not re.fullmatch(r'(?:bin|libexec/bin|libexec/git-core)/[A-Za-z0-9._+-]+', entry['binary'])):
            raise ValueError('Invalid or duplicate Homebrew package')
        validate_package(entry)
        seen.add(entry['id'])
        services = entry['xpcServices']
        if (not isinstance(services, list) or len(set(services)) != len(services)
                or not set(services) <= PROFILES.keys()):
            raise ValueError('Invalid XPC service list for ' + entry['id'])
        capabilities = set().union(*(set(PROFILES[name]) for name in services))
        expected = {name for name, directions in PROFILES.items() if set(directions) <= capabilities}
        if not entry.get('transportFor') and set(services) != expected:
            raise ValueError('XPC services must include every narrower network profile for ' + entry['id'])
        if entry.get('sourceBuild') and entry['id'] not in ('mc', 'zsh'):
            raise ValueError('Only the reviewed MC and zsh source exceptions are supported')
    return config


def bottle_build_date(manifest, bottle):
    """Read the bottle's date, never the source archive's modified timestamp."""
    for descriptor in manifest.get('manifests', []):
        annotations = descriptor.get('annotations', {})
        if annotations.get('sh.brew.bottle.digest') != bottle['sha256']:
            continue
        supplement = json.loads(annotations.get('sh.brew.sbom.supplement', '{}'))
        for package in supplement.get('packages', []):
            if (package.get('SPDXID') != 'SPDXRef-Bottle-' + bottle['name']
                    or package.get('downloadLocation') != bottle['url']
                    or not any(c.get('algorithm') == 'SHA256' and c.get('checksumValue') == bottle['sha256']
                               for c in package.get('checksums', []))):
                continue
            value = package.get('builtDate')
            if not isinstance(value, str):
                return None
            try:
                date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return None
            if date.tzinfo is None:
                return None
            return date.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return None


def fetch_build_metadata(bottle, rebuild):
    """Resolve optional display metadata only while refreshing the lock."""
    base = bottle['url'].split('/blobs/', 1)[0]
    version = bottle['version'] + (('_' + str(bottle['revision'])) if bottle['revision'] else '')
    tag = version + (('-' + str(rebuild)) if rebuild else '')
    repository = base.split('/v2/', 1)[1]
    query = urllib.parse.urlencode({'service': 'ghcr.io', 'scope': 'repository:' + repository + ':pull'})
    with request('https://ghcr.io/token?' + query) as response:
        token = json.load(response)['token']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.oci.image.index.v1+json'}
    with request(base + '/manifests/' + urllib.parse.quote(tag, safe=''), headers) as response:
        data = response.read()
    date = bottle_build_date(json.loads(data), bottle)
    if date is None:
        return {}
    return {'builtAt': date, 'buildDateManifest': base + '/manifests/sha256:' + hashlib.sha256(data).hexdigest()}


def download(url, checksum):
    # Source exceptions and document extraction share verified archives.
    source_cache = ROOT / '.build/downloads' / Path(urllib.parse.urlsplit(url).path).name
    return download_pinned(url, checksum, WORK / 'downloads' / checksum, alternatives=(source_cache,))


def unpack(bottle):
    """Materialize safe internal links as files, never follow archive paths."""
    archive = download(bottle['url'], bottle['sha256'])
    directory = WORK / 'bottles' / bottle['sha256']
    receipt = directory / '.shellcrate-receipt.json'
    if receipt.is_file():
        outputs = json.loads(receipt.read_text())
        if all((directory / p).is_file() and not (directory / p).is_symlink()
               and digest(directory / p) == checksum for p, checksum in outputs.items()):
            return directory
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Hard-linked manual pages can seek backwards thousands of times. Inflate
    # once so archive link resolution stays linear instead of re-reading gzip.
    with tempfile.TemporaryDirectory(dir=directory.parent) as staging:
        staged = Path(staging) / 'bottle'
        staged.mkdir()
        with tempfile.TemporaryFile() as plain:
            with gzip.open(archive, 'rb') as compressed:
                shutil.copyfileobj(compressed, plain)
            plain.seek(0)
            with tarfile.open(fileobj=plain, mode='r:') as source:
                unpack_members(source, staged, staged / receipt.name, bottle)
        if directory.exists():
            shutil.rmtree(directory)
        staged.replace(directory)
    return directory


def unpack_members(source, directory, receipt, bottle):
    members = {m.name.rstrip('/'): m for m in source.getmembers()}
    roots = set()
    for member in members.values():
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                or not (member.isfile() or member.isdir() or member.issym() or member.islnk())):
            raise ValueError('Unsafe Homebrew archive entry')
        if len(path.parts) >= 2:
            roots.add('/'.join(path.parts[:2]))
    if len(roots) != 1 or next(iter(roots)).split('/')[0] != bottle['name']:
        raise ValueError('Unexpected Homebrew bottle root')
    prefix = next(iter(roots)) + '/'

    def contents(member):
        target = resolve_member(members, member.name, prefix)
        if not target.isfile():
            raise ValueError('Expected a regular Homebrew archive file')
        return source.extractfile(target).read()

    outputs = {}
    for member in members.values():
        if member.isdir() or not member.name.startswith(prefix):
            continue
        relative = member.name[len(prefix):]
        # Directory symlinks are not copied. Resolve individual Mach-O file
        # aliases below, and keep runtime resources as plain sealed files.
        if member.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
            if target in members and members[target].isdir():
                continue
        data = contents(member)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if data[:4] == b'\xcf\xfa\xed\xfe' else 0o644)
        outputs[relative] = hashlib.sha256(data).hexdigest()
    receipt.write_text(json.dumps(outputs, sort_keys=True))
    return directory


def inspect(binary):
    data = binary.read_bytes()
    if data[:4] != b'\xcf\xfa\xed\xfe' or struct.unpack_from('<I', data, 4)[0] != 0x100000c:
        raise ValueError('Expected an arm64 Mach-O file at ' + str(binary))
    cursor = 32
    record = {'dependencies': [], 'rpaths': [], 'id': None, 'minimumOS': None}
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        command, size = struct.unpack_from('<II', data, cursor)
        if size < 8 or cursor + size > len(data):
            raise ValueError('Invalid Mach-O command')
        if command in (0xc, 0xd, 0x80000018, 0x8000001f, 0x80000023, 0x8000001c):
            offset = struct.unpack_from('<I', data, cursor + 8)[0]
            value = data[cursor + offset:cursor + size].split(b'\0')[0].decode()
            if command == 0xd:
                record['id'] = value
            elif command == 0x8000001c:
                record['rpaths'].append(value)
            else:
                record['dependencies'].append(value)
        if command in (0x32, 0x24):
            if command == 0x32 and struct.unpack_from('<I', data, cursor + 8)[0] != 1:
                raise ValueError('Bottle contains a non-macOS binary')
            value = struct.unpack_from('<I', data, cursor + (12 if command == 0x32 else 8))[0]
            record['minimumOS'] = (value >> 16, (value >> 8) & 255, value & 255)
        cursor += size
    if record['minimumOS'] is None:
        raise ValueError('Mach-O minimum OS is missing')
    return record


def dependency_location(value, owner, directories):
    if value.startswith(('/usr/lib/', '/System/Library/')) and '..' not in PurePosixPath(value).parts:
        return None
    match = re.fullmatch(r'(?:@@HOMEBREW_PREFIX@@|/opt/homebrew|/usr/local)/opt/([^/]+)/(.+)', value)
    if not match:
        match = re.fullmatch(r'(?:@@HOMEBREW_CELLAR@@|/opt/homebrew/Cellar|/usr/local/Cellar)/([^/]+)/[^/]+/(.+)', value)
    if match:
        formula, relative = match.groups()
    elif re.fullmatch(r'@(loader_path|rpath)/lib[A-Za-z0-9_.-]+\.dylib', value):
        formula, relative = owner, 'lib/' + value.rsplit('/', 1)[1]
    else:
        raise ValueError('Unsupported bottle dependency ' + value)
    if formula not in directories or '..' in PurePosixPath(relative).parts:
        raise ValueError('Unpinned bottle dependency ' + value)
    path = directories[formula] / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError('Missing bottle dependency ' + value)
    return formula, path


def relocate(source, destination, owner, directories, names, minimum):
    """Only change Mach-O loader metadata. Never patch executable sections."""
    metadata = inspect(source)
    for rpath in metadata['rpaths']:
        prefix_path = re.fullmatch(r'(?:@@HOMEBREW_PREFIX@@|/opt/homebrew|/usr/local|@loader_path/(?:\.\./){3}\.\.)/opt/[A-Za-z0-9@+._-]+/lib', rpath)
        cellar_path = re.fullmatch(r'(?:@@HOMEBREW_CELLAR@@|/opt/homebrew/Cellar|/usr/local/Cellar)/[A-Za-z0-9@+._-]+/[A-Za-z0-9._+-]+/lib', rpath)
        if rpath not in ('@loader_path', '@loader_path/../lib') and not prefix_path and not cellar_path:
            raise ValueError('Unsupported bottle library search path ' + rpath)
    if metadata['minimumOS'] > minimum:
        raise ValueError(f'{owner}/{source.name} needs macOS {metadata["minimumOS"]}, above {minimum}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    arguments = ['/usr/bin/install_name_tool']
    if metadata['id']:
        arguments += ['-id', '@rpath/' + destination.name]
    for value in metadata['dependencies']:
        target = dependency_location(value, owner, directories)
        if target:
            name = names[(target[0], str(target[1]))]
            relative = '@loader_path/' if metadata['id'] else '@executable_path/../lib/'
            arguments += ['-change', value, relative + name]
    for value in metadata['rpaths']:
        arguments += ['-delete_rpath', value]
    if len(arguments) > 1:
        subprocess.run([*arguments, str(destination)], check=True, capture_output=True)
    if section_hashes(source) != section_hashes(destination):
        raise ValueError('Relocation changed executable program sections in ' + str(source))
    destination.chmod(0o755)


def prepare_tool(tool, lock, directories, config=None):
    config = config or load_config()
    entry = next(p for p in config['packages'] if p['id'] == tool['id'])
    formula = entry['formula']
    source = directories[formula] / entry['binary']
    pending = [(formula, source)]
    visited, names, libraries = set(), {}, {}
    while pending:
        owner, binary = pending.pop()
        key = (owner, str(binary))
        if key in visited:
            continue
        visited.add(key)
        for value in inspect(binary)['dependencies']:
            target = dependency_location(value, owner, directories)
            if target:
                target_owner, path = target
                name = path.name
                if name in libraries and digest(libraries[name][1]) != digest(path):
                    raise ValueError('Private library name collision ' + name)
                libraries[name] = target
                names[(target_owner, str(path))] = name
                pending.append(target)
    directory = WORK / 'prepared' / tool['id']
    if directory.exists():
        shutil.rmtree(directory)
    parts = config['minimumMacOS'].split('.')
    minimum = tuple(map(int, parts)) + (0,) * (3 - len(parts))
    relocate(source, directory / tool['executable'], formula, directories, names, minimum)
    for name, (owner, path) in sorted(libraries.items()):
        relocate(path, directory / 'lib' / name, owner, directories, names, minimum)
    resources = {}
    components = {formula, *[owner for owner, _ in libraries.values()]}
    if formula == 'file-formula':
        # libmagic's default data path points at Homebrew. Supply its matching
        # pinned database through MAGIC in the trusted native supervisor.
        relative = 'share/misc/magic.mgc'
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directories['libmagic'] / relative, target)
        target.chmod(0o644)
        resources[relative] = digest(target)
        components.add('libmagic')
    if 'outgoing' in tool.get('networkCapabilities', []):
        certificates = directories['ca-certificates'] / 'share/ca-certificates/cacert.pem'
        target = directory / 'share/certificates/cacert.pem'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(certificates, target)
        target.chmod(0o644)
        resources['share/certificates/cacert.pem'] = digest(target)
        components.add('ca-certificates')
    # The trusted supervisor supplies this path to ncurses. No host Homebrew
    # terminfo or certificate installation is required by the shipped app.
    if 'ncurses' in {owner for owner, _ in libraries.values()}:
        for path in sorted((directories['ncurses'] / 'share/terminfo').rglob('*')):
            if path.is_file():
                relative = 'share/terminfo/' + str(path.relative_to(directories['ncurses'] / 'share/terminfo'))
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                target.chmod(0o644)
                resources[relative] = digest(target)
    binary = directory / tool['executable']
    metadata = {'executableSHA256': digest(binary), 'programSHA256': program_digest(binary),
                'librarySHA256': {}, 'libraryProgramSHA256': {}, 'resourceSHA256': resources,
                'bottleSHA256': lock[formula]['sha256'],
                'components': sorted(components)}
    if lock[formula].get('builtAt'):
        metadata['builtAt'] = lock[formula]['builtAt']
    for name in libraries:
        relative = 'lib/' + name
        metadata['librarySHA256'][relative] = digest(directory / relative)
        metadata['libraryProgramSHA256'][relative] = program_digest(directory / relative)
    return directory, metadata


def update_lock(cached_metadata=False, config_path=CONFIG, index_path=INDEX):
    config = load_config(config_path)
    index = json.loads(index_path.read_text())
    previous = {t['id']: t for t in index['tools']}
    static_bottles = {name for p in config['packages'] if p.get('sourceBuild')
                      for name in previous.get(p['id'], {}).get('sourceBuild', {}).get('staticBottles', [])}
    pending = [p['formula'] for p in config['packages']] + sorted(static_bottles)
    source_exceptions = {p['formula'] for p in config['packages'] if p.get('sourceBuild')}
    saved = WORK / 'resolved-formulas.json'
    formulas = json.loads(saved.read_text()) if cached_metadata and saved.exists() else {}
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if name not in formulas:
            with request('https://formulae.brew.sh/api/formula/' + urllib.parse.quote(name, safe='') + '.json') as response:
                formulas[name] = json.load(response)
        formula = formulas[name]
        if name not in source_exceptions:
            pending.extend(formula['dependencies'])
        WORK.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps(formulas, indent=2) + '\n')
    required = static_bottles | {p['formula'] for p in config['packages'] if not p.get('sourceBuild')}
    while True:
        expanded = required | {name for formula in required for name in formulas[formula]['dependencies']}
        if expanded == required:
            break
        required = expanded
    lock = {}
    for name in sorted(required):
        formula = formulas[name]
        files = formula.get('bottle', {}).get('stable', {}).get('files', {})
        tag = config['bottleTag'] if config['bottleTag'] in files else 'all'
        if tag not in files:
            # Source exceptions need release metadata but never compile an
            # arbitrary dependency merely because its bottle is missing.
            if any(p['formula'] == name and p.get('sourceBuild') for p in config['packages']):
                continue
            raise ValueError(f'{name} has no {config["bottleTag"]} bottle. Available tags {list(files)}')
        bottle = files[tag]
        lock[name] = dict(name=name, version=formula['versions']['stable'], revision=formula['revision'],
                          tag=tag, **bottle, dependencies=formula['dependencies'],
                          license=formula['license'], source=formula['urls']['stable'],
                          formulaRevision=formula['tap_git_head'], formulaPath=formula['ruby_source_path'],
                          formulaSHA256=formula['ruby_source_checksum']['sha256'])
    tools = []
    dated_formulas = set()
    for entry in config['packages']:
        tool = catalog_record(entry, formulas[entry['formula']], previous.get(entry['id']))
        tools.append(tool)
        capabilities = set().union(*(set(PROFILES[name]) for name in entry['xpcServices']))
        tool['networkCapabilities'] = [d for d in ('outgoing', 'incoming') if d in capabilities]
        if not entry.get('sourceBuild') and entry['formula'] not in dated_formulas:
            dated_formulas.add(entry['formula'])
            bottle = lock[entry['formula']]
            old_bottle = index.get('homebrewBottles', {}).get(entry['formula'], {})
            if old_bottle.get('sha256') == bottle['sha256'] and old_bottle.get('builtAt'):
                bottle.update({key: old_bottle[key] for key in ('builtAt', 'buildDateManifest') if key in old_bottle})
            else:
                try:
                    bottle.update(fetch_build_metadata(bottle, formulas[entry['formula']]['bottle']['stable']['rebuild']))
                except (OSError, ValueError, KeyError) as error:
                    # Display metadata is optional. A missing date must not prevent
                    # a checksum-pinned package from being prepared.
                    print('Build date unavailable for ' + entry['id'] + ' (' + str(error) + ')', file=sys.stderr)
    index['tools'] = tools
    index['homebrewBottles'] = lock
    index['homebrewTarget'] = {k: config[k] for k in ('architecture', 'minimumMacOS', 'bottleTag')}
    # The remaining source exceptions retain only their actual build inputs.
    dependencies = {d['id']: d for d in index.get('buildDependencies', [])}
    required = {name for tool in index['tools'] for name in tool.get('sourceBuild', {}).get('dependencies', [])}
    while True:
        expanded = required | {name for dependency in required for name in dependencies[dependency].get('dependencies', [])}
        if expanded == required:
            break
        required = expanded
    index['buildDependencies'] = [d for d in index.get('buildDependencies', []) if d['id'] in required]
    # Validate before replacing the last usable lock. A failed refresh must not
    # leave a partial catalog behind for Xcode or Cloud builds.
    from tool_packages import load_index
    temporary = index_path.with_suffix('.json.pending')
    try:
        temporary.write_text(json.dumps(index, indent=2) + '\n')
        load_index(temporary)
        temporary.replace(index_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Locked {len(lock)} Homebrew bottles for {config["bottleTag"]}')


def cached_tool(tool, key, lock, directories, config):
    name = tool['id']
    directory = WORK / 'prepared' / name
    receipt = WORK / 'receipts' / (name + '.json')
    if receipt.is_file():
        try:
            saved = json.loads(receipt.read_text())
            if saved['inputs'] == key:
                metadata = saved['artifact']
                expected = {tool['executable']: metadata['executableSHA256'],
                            **metadata['librarySHA256'], **metadata['resourceSHA256']}
                actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() or p.is_symlink()}
                if (expected.keys() == actual and not any(p.is_symlink() for p in directory.rglob('*'))
                        and all(digest(directory / path) == checksum for path, checksum in expected.items())):
                    return metadata
        except (OSError, TypeError, ValueError, KeyError):
            pass
    _, metadata = prepare_tool(tool, lock, directories, config)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    pending = receipt.with_suffix('.pending')
    pending.write_text(json.dumps({'inputs': key, 'artifact': metadata}, sort_keys=True))
    pending.replace(receipt)
    print('Prepared Homebrew ' + name + ' ' + tool['version'], flush=True)
    return metadata


class BottleDirectories:
    """Verify primary bottles and the libraries/resources actually read from them."""
    def __init__(self, lock):
        self.lock = lock
        self.verified = {}

    def __contains__(self, name):
        return name in self.lock

    def __getitem__(self, name):
        if name not in self.verified:
            bottle = self.lock[name]
            print('Verifying bottle ' + name + ' ' + bottle['version'], flush=True)
            self.verified[name] = unpack(bottle)
        return self.verified[name]


def prepare(index, config=None):
    from package_inventory import build_components, fingerprint
    config = config or load_config()
    recipes = {name: digest(ROOT / 'script' / name) for name in
               ('homebrew_tools.py', 'precompiled_tools.py', 'package_inventory.py', 'archive_paths.py')}
    packages = {p['id']: p for p in config['packages']}
    lock = index['homebrewBottles']
    # Formula dependencies also include executables used by other programs
    # from the same bottle, such as the Python tools distributed with ldns.
    directories = BottleDirectories(lock)
    artifacts = {}
    for tool in index['tools']:
        if 'homebrew' not in tool:
            continue
        entry = packages[tool['id']]
        key = fingerprint([recipes, index['homebrewTarget'], entry['formula'], entry['binary'], tool['executable'],
                           tool.get('networkCapabilities'), build_components(index, tool)])
        metadata = cached_tool(tool, key, lock, directories, config)
        metadata.pop('builtAt', None)
        if lock[entry['formula']].get('builtAt'):
            metadata['builtAt'] = lock[entry['formula']]['builtAt']
        artifacts[tool['id']] = metadata
    path = ROOT / '.build/tools/homebrew-artifacts.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifacts, indent=2) + '\n')
    return artifacts, directories


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update-lock', action='store_true')
    parser.add_argument('--cached-metadata', action='store_true', help='Resume a lock refresh using already fetched formula metadata')
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--index', type=Path, default=INDEX)
    args = parser.parse_args()
    if args.update_lock:
        update_lock(args.cached_metadata, args.config, args.index)
    else:
        prepare(json.loads(args.index.read_text()), load_config(args.config))
