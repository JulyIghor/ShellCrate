#!/usr/bin/env python3
"""Verify a ShellCrate source edition and optionally extract its original inputs."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent


def safe_path(name):
    path = PurePosixPath(name)
    if not path.parts or '\x00' in name or path.is_absolute() or '..' in path.parts or path.as_posix() != name or '\\' in name:
        raise ValueError('Unexpected path in source index ' + repr(name))
    return path


def hash_file(path):
    with path.open('rb') as stream:
        return hash_stream(stream)


def hash_stream(stream, output=None):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
        if output is not None:
            output.write(block)
    return h.hexdigest()


def build_hashes(dist, index, command):
    lock = json.loads((dist / 'build/Tools/index.json').read_text())
    entry = next(t for t in lock['tools'] if t['id'] == command and t.get('sourceBuild'))
    hashes = {entry['sha256']}
    if command == 'mc':
        hashes.update(t['sha256'] for t in lock['buildDependencies'])
        hashes.update(sha for files in index['linkInputs'].values() for sha in files.values())
    return hashes


def verify_references(dist, index, digests):
    formulae = index['formulae']
    packages = json.loads((dist / 'packages.json').read_text())
    lock = json.loads((dist / 'build/Tools/index.json').read_text())
    if {p['id']: p['version'] for p in packages} != {p['id']: p['version'] for p in lock['tools']}:
        raise ValueError('Package list differs from the build lock')
    needed = {p['source'] for p in packages} | {p['sha256'] for p in lock['buildDependencies']}
    for package in packages:
        if package.get('formula') and (package['source'] != formulae[package['formula']]['source']
                                      or not set(package['components']) <= formulae.keys()):
            raise ValueError('Incomplete source record for ' + package['id'])
        for patch in package.get('build', {}).get('patches', []):
            if index['files'].get('build/' + patch['path']) != patch['sha256']:
                raise ValueError('Build patch differs from its lock')
    for name, formula in formulae.items():
        if formula['recipe'] not in index['files'] or formula['bottleSHA256'] != lock['homebrewBottles'][name]['sha256']:
            raise ValueError('Incomplete build recipe for ' + name)
        needed.add(formula['source'])
        needed.update(formula['resources'].values())
        for patch in formula['patches']:
            actual = index['files'][patch['file']]
            if not patch.get('inline'):
                if actual != patch['sha256']:
                    raise ValueError('Patch differs from its source archive')
                needed.add(actual)
    for files in index['linkInputs'].values():
        needed.update(files.values())
    haskell = json.loads((dist / 'build/Tools/haskell-sources.json').read_text())
    needed.update(p['sha256'] for p in haskell['packages'].values())
    needed.update(r['sha256'] for p in haskell['packages'].values() for r in p.get('cabalRevisions', []))
    if not needed <= digests:
        raise ValueError('A package or build input is missing its corresponding source')
    notice_index = json.loads((dist / 'licenses/ToolLicenses.json').read_text())
    if notice_index['noticeSHA256'] != index['files']['licenses/ThirdPartyNotices.txt']:
        raise ValueError('Notices and license index differ')


def verify(dist, assets=None, extract=None, command=None):
    index = json.loads((dist / 'source-index.json').read_text())
    if index['schemaVersion'] != 1 or not re.fullmatch(r'sources-[0-9a-f]{20}', index['edition']):
        raise ValueError('Unsupported source edition')
    assets = assets or ROOT / 'release-assets' / index['edition']
    for name, sha in index['files'].items():
        path = dist / safe_path(name)
        if path.resolve() != path.absolute() or hash_file(path) != sha:
            raise ValueError('Source documentation or recipe differs at ' + name)
    by_pack = {}
    entries = {}
    for row in index['sources']:
        name = row['path']
        safe_path(name)
        if name in entries or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('Invalid or duplicate source member ' + name)
        entries[name] = row
        by_pack.setdefault(row['archive'], set()).add(name)
    archives = {r['name'] for r in index['archives']}
    if len(archives) != len(index['archives']) or archives != set(by_pack):
        raise ValueError('Source archives and member index disagree')
    verify_references(dist, index, {r['sha256'] for r in entries.values()})
    chosen = build_hashes(dist, index, command) if command else None
    if chosen is not None and not chosen <= {r['sha256'] for r in entries.values()}:
        raise ValueError('Build inputs missing from index')
    if extract and (extract.exists() or extract.is_symlink()):
        raise ValueError('Choose a new directory for extracted sources')
    target_parent = extract.parent if extract else None
    with tempfile.TemporaryDirectory(dir=target_parent) as temp:
        staging = Path(temp) / 'materials'
        if extract:
            staging.mkdir()
        for archive in index['archives']:
            name = archive['name']
            if not re.fullmatch(r'[a-z-]+-[0-9]+\.tar', name):
                raise ValueError('Unexpected archive name ' + name)
            expected = by_pack[name]
            if chosen is not None and not any(entries[m]['sha256'] in chosen for m in expected):
                continue
            path = assets / name
            if path.stat().st_size != archive['bytes'] or hash_file(path) != archive['sha256']:
                raise ValueError('Archive checksum mismatch for ' + name)
            seen = set()
            with tarfile.open(path, mode='r|') as tar:
                for member in tar:
                    if member.name not in expected or member.name in seen or not member.isfile():
                        raise ValueError('Unexpected archive member in ' + name)
                    seen.add(member.name)
                    row = entries[member.name]
                    if member.size != row['bytes']:
                        raise ValueError('Source size mismatch for ' + member.name)
                    with tar.extractfile(member) as stream:
                        if extract and (chosen is None or row['sha256'] in chosen):
                            dest = staging / safe_path(member.name)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with dest.open('xb') as output:
                                sha = hash_stream(stream, output)
                        else:
                            sha = hash_stream(stream)
                    if sha != row['sha256']:
                        raise ValueError('Source checksum mismatch for ' + member.name)
                if seen != expected:
                    raise ValueError('Missing sources from ' + name)
        if extract:
            staging.rename(extract)
    print('Verified ' + index['edition'] + (' for ' + command if command else
          ' with ' + str(len(entries)) + ' inputs in ' + str(len(archives)) + ' archives'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distribution', type=Path, default=ROOT / 'distribution')
    parser.add_argument('--assets', type=Path, help='Directory containing the downloaded .tar files')
    parser.add_argument('--extract', type=Path, help='Write original archives and link inputs to a new directory')
    parser.add_argument('--for-build', choices=('mc', 'zsh'), help='Verify only the archives required for this build')
    args = parser.parse_args()
    try:
        verify(args.distribution.resolve(), args.assets, args.extract, args.for_build)
    except (OSError, ValueError, KeyError, tarfile.TarError) as e:
        parser.exit(1, str(e) + '\n')
