#!/usr/bin/env python3
"""Build pinned upstream sources with private static dependencies and reviewed patches."""
import hashlib
import errno
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import tarfile
import time

from precompiled_tools import fetch, program_digest

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.build'
RECIPES = {'curl', 'wget', 'rsync', 'mc', 'bash', 'zsh'}
DEPENDENCY_RECIPES = {'openssl', 'pcre2', 'meson', 'glib', 'gvdb', 'proxy-libintl', 'slang'}


def compilation_input(entry):
    # Catalog descriptions, command selection and display labels do not affect code.
    return {key: entry[key] for key in ('id', 'version', 'url', 'sha256', 'sourceDirectory', 'sourceBuild') if key in entry}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_patches(entry):
    patches = entry.get('sourceBuild', {}).get('patches', [])
    approved = {'Tools/mc/patches/0001-relocatable-data.patch', 'Tools/mc/patches/0002-sftp-configured-home.patch'}
    if not isinstance(patches, list) or len(patches) > len(approved):
        raise ValueError('Invalid source patch inventory')
    result = []
    for record in patches:
        if (entry.get('sourceBuild', {}).get('recipe') != 'mc' or not isinstance(record, dict)
                or record.get('path') not in approved):
            raise ValueError('Unreviewed Midnight Commander source patch')
        approved.remove(record['path'])
        path = ROOT / record['path']
        if not path.is_file() or path.is_symlink() or digest(path) != record.get('sha256'):
            raise ValueError('Source patch checksum mismatch')
        result.append(path)
    return result


def upstream_patches(entry, download=False):
    """GNU's release maintenance patches, separate from our MC modification."""
    result = []
    records = entry.get('sourceBuild', {}).get('upstreamPatches', [])
    if not isinstance(records, list) or len(records) > 99:
        raise ValueError('Invalid upstream patch inventory')
    for number, record in enumerate(records, 1):
        expected = 'https://ftpmirror.gnu.org/bash/bash-5.3-patches/bash53-' + str(number).zfill(3)
        if (entry.get('sourceBuild', {}).get('recipe') != 'bash' or record.get('url') != expected
                or not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))):
            raise ValueError('Unpinned upstream maintenance patch')
        path = BUILD / 'downloads' / expected.rsplit('/', 1)[1]
        if download:
            path = fetch(expected, record['sha256'])
        elif path.exists() and digest(path) != record['sha256']:
            raise ValueError('Upstream maintenance patch checksum mismatch')
        result.append(path)
    return result


def remove_generated_directory(path):
    # Finder can create .DS_Store between a directory scan and rmdir.
    # Retry only that race, and keep permission and other failures visible.
    for attempt in range(4):
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or attempt == 3:
                raise
            time.sleep(0.1)


def unpack_source(archive, destination, directory):
    """Allow only internal file links, never paths beneath a link."""
    with tarfile.open(archive) as source:
        members = {item.name.rstrip('/'): item for item in source.getmembers()}
        links = {item.name for item in members.values() if item.issym() or item.islnk()}
        for item in members.values():
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != directory:
                raise ValueError('Unsafe source archive path')
            if not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise ValueError('Unexpected source archive entry')
            if any(str(parent) in links for parent in path.parents):
                raise ValueError('Source entry beneath a symbolic link')
            if item.issym() or item.islnk():
                target = posixpath.normpath(posixpath.join(str(path.parent), item.linkname) if item.issym() else item.linkname)
                if target not in members or not members[target].isfile():
                    raise ValueError('Source link must name a regular file in this archive')
        target = destination / directory
        if target.exists():
            remove_generated_directory(target)
        destination.mkdir(parents=True, exist_ok=True)
        source.extractall(destination)


def prepare_source(entry):
    unpack_source(fetch(entry['url'], entry['sha256']), BUILD / 'sources', entry['sourceDirectory'])
    upstream_patches(entry, download=True)


def source_digest(entry):
    source = BUILD / 'sources' / entry['sourceDirectory']
    files = [(str(path.relative_to(source)), os.readlink(path) if path.is_symlink() else digest(path))
             for path in sorted(source.rglob('*'))
             if path.name != '.DS_Store' and (path.is_file() or path.is_symlink())]
    if not files:
        raise ValueError('Missing prepared source tree')
    return hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest()


def environment():
    # Do not discover or link developer-installed Homebrew libraries.
    sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
    compiler = subprocess.check_output(['xcrun', '--find', 'clang'], text=True).strip()
    deployment = re.search(r'^MACOSX_DEPLOYMENT_TARGET = (\d+\.\d+(?:\.\d+)?)$',
                           (ROOT / 'Config/Base.xcconfig').read_text(), re.M)[1]
    return dict(os.environ, CC=compiler, CXX=compiler + '++', SDKROOT=sdk,
                MACOSX_DEPLOYMENT_TARGET=deployment, CFLAGS='-Os -arch arm64 -mmacosx-version-min=' + deployment,
                CXXFLAGS='-Os -arch arm64 -mmacosx-version-min=' + deployment,
                CPPFLAGS='', LDFLAGS='-arch arm64 -mmacosx-version-min=' + deployment,
                PKG_CONFIG_PATH='', PKG_CONFIG_LIBDIR=str(BUILD / 'empty-pkgconfig'),
                CONFIG_SITE='/dev/null')


def run(arguments, directory, env, log):
    log.write(('$ ' + ' '.join(map(str, arguments)) + '\n').encode())
    log.flush()
    subprocess.run(list(map(str, arguments)), cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)


def build(entry, dependencies=(), bottles=None):
    name = entry['id']
    recipe = entry.get('sourceBuild', {}).get('recipe', name)
    if recipe not in RECIPES | DEPENDENCY_RECIPES:
        raise ValueError('Unsupported source recipe')
    env = environment()
    compiler = subprocess.check_output([env['CC'], '--version'], text=True)
    patches = source_patches(entry)
    recipe_files = [digest(Path(__file__))]
    if recipe in {'mc', 'glib', 'pcre2', 'meson', 'gvdb', 'proxy-libintl', 'slang'}:
        recipe_files.append(digest(Path(__file__).with_name('terminal_tool_sources.py')))
    if recipe in {'bash', 'zsh'}:
        recipe_files.append(digest(Path(__file__).with_name('shell_tool_sources.py')))
    static_bottles = entry.get('sourceBuild', {}).get('staticBottles', [])
    pins = [(name, bottles[name]['sha256']) for name in static_bottles]
    fingerprint = hashlib.sha256(json.dumps([compilation_input(entry), dependencies, pins, source_digest(entry), recipe_files,
                                            compiler, env['SDKROOT'], env['MACOSX_DEPLOYMENT_TARGET']], sort_keys=True).encode()).hexdigest()
    directory = BUILD / 'source-build' / name
    receipt_path = directory / 'receipt.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('fingerprint') == fingerprint and all((directory / path).is_file() and digest(directory / path) == value
                                                          for path, value in receipt['outputs'].items()):
            return directory, receipt
    if directory.exists():
        remove_generated_directory(directory)
    source = directory / 'source'
    shutil.copytree(BUILD / 'sources' / entry['sourceDirectory'], source, symlinks=True,
                    ignore=shutil.ignore_patterns('.DS_Store'))
    prefix = directory / 'prefix'
    jobs = str(min(os.cpu_count() or 2, 10))
    logs = BUILD / 'logs'
    logs.mkdir(exist_ok=True)
    print('Building ' + name + ' ' + entry['version'] + (' with reviewed source patches' if patches else ' from unmodified source'), flush=True)
    with (logs / ('source-' + name + '.log')).open('wb') as log:
        for patch in upstream_patches(entry, download=True):
            run(['/usr/bin/patch', '-p0', '-F0', '--batch', '--forward', '-i', patch], source, env, log)
        for patch in patches:
            run(['/usr/bin/patch', '-p1', '-F0', '--batch', '--forward', '-i', patch], source, env, log)
        if recipe == 'openssl':
            run(['./Configure', 'darwin64-arm64-cc', 'no-shared', 'no-module', 'no-engine', 'no-tests',
                 '--prefix=' + str(prefix), '--openssldir=/etc/ssl', '-mmacosx-version-min=14.0'], source, env, log)
            run(['/usr/bin/make', '-j' + jobs, 'build_libs'], source, env, log)
            run(['/usr/bin/make', 'install_dev'], source, env, log)
            outputs = ['prefix/lib/libssl.a', 'prefix/lib/libcrypto.a']
        elif recipe == 'curl':
            tls = BUILD / 'source-build/openssl/prefix'
            build_directory = directory / 'cmake'
            run(['cmake', '-S', source, '-B', build_directory, '-DCMAKE_BUILD_TYPE=MinSizeRel',
                 '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0',
                 '-DCMAKE_OSX_SYSROOT=' + env['SDKROOT'], '-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local',
                 '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF', '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF',
                 '-DBUILD_SHARED_LIBS=OFF', '-DBUILD_STATIC_LIBS=ON', '-DBUILD_STATIC_CURL=ON', '-DBUILD_TESTING=OFF',
                 '-DCURL_USE_OPENSSL=ON', '-DOPENSSL_USE_STATIC_LIBS=ON', '-DOPENSSL_ROOT_DIR=' + str(tls),
                 '-DUSE_APPLE_SECTRUST=ON', '-DCURL_CA_NATIVE=ON', '-DCURL_CA_BUNDLE=/etc/ssl/cert.pem',
                 '-DCURL_CA_PATH=none', '-DCURL_USE_LIBPSL=OFF', '-DUSE_LIBIDN2=OFF', '-DUSE_NGHTTP2=OFF',
                 '-DCURL_USE_LIBSSH2=OFF', '-DCURL_USE_LIBSSH=OFF', '-DCURL_BROTLI=OFF', '-DCURL_ZSTD=OFF',
                 '-DCURL_DISABLE_LDAP=ON', '-DCURL_DISABLE_LDAPS=ON', '-DCURL_USE_GSSAPI=OFF',
                 '-DCURL_ZLIB=ON', '-DZLIB_INCLUDE_DIR=' + env['SDKROOT'] + '/usr/include',
                 '-DZLIB_LIBRARY=' + env['SDKROOT'] + '/usr/lib/libz.tbd'], source, env, log)
            run(['cmake', '--build', build_directory, '--target', 'curl', '--parallel', jobs], source, env, log)
            outputs = ['cmake/src/curl']
        elif recipe == 'wget':
            tls = BUILD / 'source-build/openssl/prefix'
            env['CPPFLAGS'] = '-I' + str(tls / 'include')
            env['LDFLAGS'] += ' -L' + str(tls / 'lib')
            run(['./configure', '--prefix=' + str(prefix), '--with-ssl=openssl', '--with-libssl-prefix=' + str(tls),
                 '--with-openssl=' + str(tls), '--disable-nls', '--disable-iri', '--disable-pcre', '--disable-pcre2',
                 '--disable-rpath', '--without-libpsl', '--without-cares', '--without-metalink',
                 '--without-libuuid', '--without-libiconv-prefix', '--without-libintl-prefix'], source, env, log)
            run(['/usr/bin/make', '-j' + jobs], source, env, log)
            outputs = ['source/src/wget']
        elif recipe == 'rsync':
            run(['./configure.sh', '--prefix=' + str(prefix), '--with-included-popt', '--with-included-zlib',
                 '--disable-openssl', '--disable-xxhash', '--disable-zstd', '--disable-lz4', '--disable-idn',
                 '--disable-md2man', '--disable-debug', '--with-rsh=/usr/bin/false'], source, env, log)
            run(['/usr/bin/make', '-j' + jobs, 'rsync'], source, env, log)
            outputs = ['source/rsync']
        elif recipe in {'bash', 'zsh'}:
            from shell_tool_sources import build_shell_source
            outputs = build_shell_source(recipe, source, prefix, env, jobs, log, run)
        else:
            from terminal_tool_sources import build_terminal_source
            if static_bottles:
                from homebrew_tools import unpack
                ssh, crypto = [unpack(bottles[name]) for name in static_bottles]
                env['LIBSSH_CFLAGS'] = '-I' + str(ssh / 'include')
                # Keep libtool from folding these archives into libmc before the
                # SFTP objects in libinternal are seen by the final executable.
                static = directory / 'ssh-libs'
                static.mkdir()
                (static / 'libssh2.a').symlink_to(ssh / 'lib/libssh2.a')
                (static / 'libcrypto.a').symlink_to(crypto / 'lib/libcrypto.a')
                env['LIBSSH_LIBS'] = f'-L{static} -lssh2 -lcrypto -lz -framework Security -framework CoreFoundation'
            outputs = build_terminal_source(recipe, directory, source, prefix, env, jobs, log, run)
    receipt = {'fingerprint': fingerprint, 'outputs': {path: digest(directory / path) for path in outputs}}
    if recipe in RECIPES:
        binary = directory / outputs[0]
        receipt['artifact'] = {'executableSHA256': digest(binary), 'programSHA256': program_digest(binary),
                               'builtAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        if recipe in {'mc', 'zsh'}:
            receipt['artifact']['resourceSHA256'] = {
                str(path.relative_to(prefix)): digest(path)
                for path in sorted((prefix / 'share' / recipe).rglob('*')) if path.is_file()}
        receipt['executable'] = outputs[0]
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    return directory, receipt


def build_sources(index):
    dependencies = {entry['id']: entry for entry in index.get('buildDependencies', [])}
    receipts = {}
    for entry in dependencies.values():
        _, receipts[entry['id']] = build(entry, [receipts[name] for name in entry.get('dependencies', [])])
    for tool in index['tools']:
        if 'sourceBuild' in tool:
            directory, receipt = build(tool, [receipts[name] for name in tool['sourceBuild']['dependencies']], index.get('homebrewBottles', {}))
            yield tool, directory / receipt['executable'], receipt['artifact']
