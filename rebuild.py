#!/usr/bin/env python3
"""Build the packaged MC or zsh from an extracted source edition on macOS."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys

from verify_sources import ROOT, hash_file, safe_path


def rebuild(args, work):
    dist = ROOT / 'distribution'
    manifest = json.loads((dist / 'source-index.json').read_text())
    lock = json.loads((dist / 'build/Tools/index.json').read_text())
    rows = {r['sha256']: r for r in manifest['sources']}
    def input_file(sha):
        file = args.sources.resolve() / safe_path(rows[sha]['path'])
        if hash_file(file) != sha:
            raise ValueError('Changed source input ' + str(file))
        return file

    for name, sha in manifest['files'].items():
        if name.startswith('build/'):
            path = dist / safe_path(name)
            if hash_file(path) != sha:
                raise ValueError('Changed published recipe ' + name)
            target = work / safe_path(name[len('build/'):])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    sys.path.insert(0, str(work / 'script'))
    import source_tools
    tool = next(t for t in lock['tools'] if t['id'] == args.command)
    deps = lock['buildDependencies'] if args.command == 'mc' else []
    entries = {t['id']: t for t in [*deps, tool]}
    replacements = {}
    for assignment in args.replace_source:
        name, sep, path = assignment.partition('=')
        replacement = Path(path).resolve()
        if not sep or name not in entries or not replacement.is_dir() or name in replacements:
            raise ValueError('Expected an applicable component=source-directory pair')
        if work == replacement or work in replacement.parents or replacement in work.parents:
            raise ValueError('Choose replacement sources outside the build directory')
        replacements[name] = replacement
    sources = work / '.build/sources'
    sources.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        if name in replacements:
            target = sources / entry['sourceDirectory']
            if target.exists():
                source_tools.remove_generated_directory(target)
            shutil.copytree(replacements[name], target, symlinks=True)
        else:
            source_tools.unpack_source(input_file(entry['sha256']), sources, entry['sourceDirectory'])
    receipts = {}
    for entry in deps:
        _, receipts[entry['id']] = source_tools.build(entry, [receipts[n] for n in entry.get('dependencies', [])])
    dependencies = [receipts[n] for n in tool['sourceBuild']['dependencies']]
    if args.command == 'mc':
        # Use only the headers and archives selected by the shipping recipe.
        # The public kit also accepts independently rebuilt replacements.
        link = work / '.build/relink'
        hashes = {}
        for formula, files in manifest['linkInputs'].items():
            custom = args.ssh_prefix if formula == 'libssh2' else args.crypto_prefix
            for name, sha in files.items():
                src = custom / safe_path(name) if custom else input_file(sha)
                dest = link / safe_path(name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                hashes[name] = hash_file(dest)
        os.environ['LIBSSH_CFLAGS'] = '-I' + str(link / 'include')
        os.environ['LIBSSH_LIBS'] = (f'-L{link / "lib"} -lssh2 -lcrypto -lz '
                                     '-framework Security -framework CoreFoundation')
        dependencies.append(hashes)
        tool = copy.deepcopy(tool)
        # Replacing bottle extraction with the verified link kit preserves the
        # original link flags and permits rebuilding without network access.
        tool['sourceBuild']['staticBottles'] = []
    directory, receipt = source_tools.build(tool, dependencies)
    print(directory / receipt['executable'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('mc', 'zsh'))
    parser.add_argument('--sources', type=Path, required=True, help='Directory created by verify_sources.py --extract')
    parser.add_argument('--work', type=Path, required=True, help='Dedicated build directory without spaces')
    parser.add_argument('--replace-source', action='append', default=[], metavar='NAME=DIR')
    parser.add_argument('--ssh-prefix', type=Path, help='Use locally built libssh2 headers and lib/libssh2.a')
    parser.add_argument('--crypto-prefix', type=Path, help='Use a locally built OpenSSL lib/libcrypto.a')
    args = parser.parse_args()
    work = args.work.resolve()
    if any(c.isspace() for c in str(work)):
        parser.error('Use a build directory without spaces for these upstream makefiles')
    if args.command != 'mc' and (args.ssh_prefix or args.crypto_prefix):
        parser.error('Only MC links libssh2 and libcrypto')
    work.mkdir(parents=True, exist_ok=True)
    marker = work / '.shellcrate-source-work'
    if any(work.iterdir()) and not marker.is_file():
        parser.error('Choose an empty working directory or an existing ShellCrate source build')
    with marker.open('a+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        rebuild(args, work)


if __name__ == '__main__':
    main()
