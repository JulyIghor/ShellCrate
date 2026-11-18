#!/usr/bin/env python3
"""Pinned standalone tool packages and their permitted worker profiles."""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import hashlib
from package_metadata import POLICIES, locked_configuration, validate_argument_prefix
from precompiled_tools import embedded_signing_identifier, valid_signing_identifier

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / 'Tools/index.json'
BUILD = ROOT / '.build/tools'


def load_index(path=INDEX):
    index = json.loads(path.read_text())
    if index.get('schemaVersion') != 2 or not isinstance(index.get('tools'), list):
        raise ValueError('Unsupported tool index')
    bottles = index.get('homebrewBottles', {})
    for name, bottle in bottles.items():
        if (not re.fullmatch(r'[a-z0-9][a-z0-9@+._-]*', name) or bottle.get('name') != name
                or not re.fullmatch(r'[0-9a-f]{64}', bottle.get('sha256', ''))
                or not re.fullmatch(r'https://ghcr.io/v2/homebrew/core/[a-z0-9@+._/-]+/blobs/sha256:' + bottle['sha256'], bottle.get('url', ''))
                or bottle.get('tag') not in ('all', index.get('homebrewTarget', {}).get('bottleTag'))
                or not isinstance(bottle.get('dependencies'), list)
                or not set(bottle['dependencies']) <= bottles.keys()):
            raise ValueError('Invalid locked Homebrew bottle ' + name)
    ids = set()
    for tool in index['tools']:
        name = tool['id']
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name) or name in ids or name in ('standard', 'interactive'):
            raise ValueError('Invalid or duplicate tool identifier')
        ids.add(name)
        paths = tool['libraries']
        if len(paths) != len(set(paths)) or any(not re.fullmatch(r'lib/lib[A-Za-z0-9_.-]+\.dylib', p) for p in paths):
            raise ValueError('Tool libraries must be unique files in the private lib folder')
        if tool['commandDependencies']:
            raise ValueError('Automatic command dependencies are not enabled in this build')
        command = tool.get('command', name)
        policy = tool.get('launchPolicy', name)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}', command) or command in ('shellcrate', 'ShellCrateCLI'):
            raise ValueError('Invalid or reserved exported command')
        if policy not in POLICIES:
            raise ValueError('A supported launch policy is required for each package')
        validate_argument_prefix(tool, policy)
        if 'system' in tool:
            from system_tools import validate_system
            validate_system(tool)
            capabilities = tool.get('networkCapabilities')
            if not isinstance(capabilities, list) or len(capabilities) != len(set(capabilities)) or not set(capabilities) <= {'outgoing', 'incoming'}:
                raise ValueError('Invalid system tool network capabilities')
            continue
        if 'engine' in tool or tool.get('executable') != 'bin/' + command:
            raise ValueError('Tools must use an indexed standalone bin executable')
        if 'signingIdentifiers' in tool:
            validate_signing_identifiers(tool)
        if sum(key in tool for key in ('precompiled', 'sourceBuild', 'homebrew')) != 1:
            raise ValueError('Choose exactly one pinned tool input type')
        metadata = tool.get('builtArtifact', tool.get('precompiled', {}))
        if 'precompiled' in tool:
            for key in ('sha256', 'executableSHA256', 'programSHA256'):
                if not re.fullmatch(r'[0-9a-f]{64}', metadata.get(key, '')):
                    raise ValueError('Precompiled release checksums are missing')
        elif 'homebrew' in tool:
            homebrew = tool['homebrew']
            bottle = index.get('homebrewBottles', {}).get(homebrew.get('formula'))
            if (not bottle or bottle.get('version') != tool['version']
                    or not re.fullmatch(r'[0-9a-f]{64}', bottle.get('sha256', ''))
                    or not bottle.get('url', '').startswith('https://ghcr.io/v2/homebrew/core/')
                    or bottle.get('tag') not in ('all', index.get('homebrewTarget', {}).get('bottleTag'))
                    or not re.fullmatch(r'(?:bin|libexec/bin|libexec/git-core)/[A-Za-z0-9._+-]+', homebrew.get('binary', ''))):
                raise ValueError('Unpinned or mismatched Homebrew input')
        else:
            from source_tools import RECIPES, source_patches, upstream_patches
            recipe = tool['sourceBuild']
            if recipe.get('recipe') != policy or policy not in RECIPES or paths:
                raise ValueError('Unsupported source build recipe')
            static_bottles = recipe.get('staticBottles', [])
            if static_bottles and (policy != 'mc' or static_bottles != ['libssh2', 'openssl@4'] or not set(static_bottles) <= bottles.keys()):
                raise ValueError('Unsupported static bottle inputs')
            dependencies = {entry['id'] for entry in index.get('buildDependencies', [])}
            if not set(recipe.get('dependencies', [])) <= dependencies:
                raise ValueError('Unknown source build dependency')
            if not re.fullmatch(r'[0-9a-f]{64}', tool.get('sha256', '')) or not tool.get('url', '').startswith('https://'):
                raise ValueError('Source release checksums are missing')
            source_patches(tool)
            upstream_patches(tool)
            if 'builtArtifact' in tool:
                for key in ('executableSHA256', 'programSHA256'):
                    if not re.fullmatch(r'[0-9a-f]{64}', tool['builtArtifact'].get(key, '')):
                        raise ValueError('Invalid source build artifact metadata')
        for library in paths:
            for key in ('librarySHA256', 'libraryProgramSHA256'):
                if not re.fullmatch(r'[0-9a-f]{64}', metadata.get(key, {}).get(library, '')):
                    raise ValueError('Private library provenance is missing')
        validate_resource_metadata(metadata.get('resourceSHA256', {}))
        capabilities = tool.get('networkCapabilities')
        if not isinstance(capabilities, list) or len(set(capabilities)) != len(capabilities) or not set(capabilities) <= {'outgoing', 'incoming'}:
            raise ValueError('Invalid tool network capabilities')
    commands_by_id = {tool['id']: tool.get('command', tool['id']) for tool in index['tools']}
    for command in commands_by_id.values():
        if command in commands_by_id and commands_by_id[command] != command:
            raise ValueError('A command conflicts with another package identity')
    dependencies = index.get('buildDependencies', [])
    if len({entry['id'] for entry in dependencies}) != len(dependencies):
        raise ValueError('Duplicate build dependency')
    from source_tools import DEPENDENCY_RECIPES
    prepared = set()
    for entry in dependencies:
        if entry['id'] not in DEPENDENCY_RECIPES or not entry['url'].startswith('https://') or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError('Unpinned build dependency')
        if not set(entry.get('dependencies', [])) <= prepared:
            raise ValueError('Build dependencies must be listed in dependency order')
        prepared.add(entry['id'])
    if path == INDEX and 'homebrewTarget' in index:
        from homebrew_tools import load_config, PROFILES
        config = load_config()
        if index['homebrewTarget'] != {key: config[key] for key in ('architecture', 'minimumMacOS', 'bottleTag')}:
            raise ValueError('Homebrew target changed. Run homebrew_tools.py --update-lock.')
        packages = {entry['id']: entry for entry in config['packages']}
        if set(packages) != ids:
            raise ValueError('Homebrew package list and catalog differ. Update the lock.')
        for tool in index['tools']:
            entry = packages[tool['id']]
            if (tool.get('homebrewFormula') != entry['formula']
                    or ('homebrew' in tool and tool['homebrew']['binary'] != entry['binary'])
                    or bool(entry.get('sourceBuild')) != ('sourceBuild' in tool)
                    or tool.get('packageConfiguration') != locked_configuration(entry)):
                raise ValueError('Homebrew package settings changed. Run homebrew_tools.py --update-lock.')
            capabilities = set().union(*(set(PROFILES[name]) for name in entry['xpcServices']))
            tool['networkCapabilities'] = [direction for direction in ('outgoing', 'incoming') if direction in capabilities]
    return index


def package_path(tool):
    """Stable slot paths deliberately exclude release versions and build numbers."""
    return Path('Contents/Helpers/Packages') / tool['id'] / tool['executable']


def validate_signing_identifiers(tool):
    identifiers = tool.get('signingIdentifiers')
    if (not isinstance(identifiers, dict)
            or set(identifiers) != {tool['executable'], *tool['libraries']}
            or not all(valid_signing_identifier(value) for value in identifiers.values())):
        raise ValueError('Missing or invalid sealed package signing identifiers')
    return identifiers


def package_signing_identifiers(tool, directory):
    """Derive identities from pinned program sections before sealing the catalog."""
    prefix = 'app.ShellCrate.mac.Precompiled.' + tool['id']
    defaults = {tool['executable']: prefix}
    defaults.update({library: prefix + '.Library.' + Path(library).stem for library in tool['libraries']})
    return {name: embedded_signing_identifier(directory / name) or fallback
            for name, fallback in defaults.items()}


def artifact_metadata(tool):
    if 'precompiled' in tool:
        return tool['precompiled']
    if 'builtArtifact' in tool:
        return tool['builtArtifact']
    if 'homebrew' in tool:
        return json.loads((BUILD / 'homebrew-artifacts.json').read_text())[tool['id']]
    return json.loads((BUILD / 'source-artifacts.json').read_text())[tool['id']]


def validate_resource_metadata(resources):
    if not isinstance(resources, dict) or len(resources) > 10000:
        raise ValueError('Invalid package resource inventory')
    for name, checksum in resources.items():
        parts = name.split('/')
        if (len(parts) < 3 or parts[0] != 'share' or any(part in ('', '.', '..') for part in parts)
                or not re.fullmatch(r'[A-Za-z0-9_+./-]+', name)
                or not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)):
            raise ValueError('Invalid package resource path or checksum')


def resource_metadata(tool):
    resources = artifact_metadata(tool).get('resourceSHA256', {})
    validate_resource_metadata(resources)
    return resources


def check_resources(tool, directory):
    for name, checksum in resource_metadata(tool).items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o111:
            raise ValueError('Package resources must be regular nonexecutable files')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('Package resource differs from its prepared upstream input')


def prepared_index(*, include_signing_identifiers=True):
    index = load_index()
    for tool in index['tools']:
        if 'sourceBuild' in tool or 'homebrew' in tool:
            tool['builtArtifact'] = artifact_metadata(tool)
        if 'homebrew' in tool:
            tool['libraries'] = sorted(tool['builtArtifact']['librarySHA256'])
        date = artifact_metadata(tool).get('builtAt')
        if date:
            tool['builtAt'] = date
        else:
            tool.pop('builtAt', None)
        if include_signing_identifiers:
            tool['signingIdentifiers'] = package_signing_identifiers(tool, BUILD / tool['id'])
            validate_signing_identifiers(tool)
        else:
            tool.pop('signingIdentifiers', None)
    return index


def allowed_profiles(tool):
    if tool.get('transportFor'):
        from package_metadata import PRIVATE_PACKAGES
        return [name.removesuffix('Worker') for name in PRIVATE_PACKAGES[tool['id']][3]]
    capabilities = set(tool['networkCapabilities'])
    return [profile for profile, required in (
        ('Offline', set()), ('Outgoing', {'outgoing'}),
        ('Incoming', {'incoming'}), ('Bidirectional', {'outgoing', 'incoming'})
    ) if required <= capabilities]


def output(*arguments):
    return subprocess.check_output(arguments, text=True).strip()


def check_binary(binary, install_name=None, private_libraries=(), private_rpath=None):
    """Reject developer-machine paths, missing private dependencies and escaping rpaths."""
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('Missing or symlinked code file ' + str(binary))
    if output('/usr/bin/lipo', '-archs', str(binary)) != 'arm64':
        raise ValueError('Unexpected architecture in ' + str(binary))
    commands = output('/usr/bin/otool', '-l', str(binary))
    # Check actual Mach-O deployment targets, never infer them from the host OS
    # or bottle filename. This applies to every transitive private library.
    deployment = re.search(r'^MACOSX_DEPLOYMENT_TARGET = (\d+\.\d+(?:\.\d+)?)$', (ROOT / 'Config/Base.xcconfig').read_text(), re.M)[1]
    normalize = lambda value: tuple(map(int, value.split('.'))) + (0,) * (3 - len(value.split('.')))
    os_versions = re.findall(r'cmd LC_BUILD_VERSION\n.*?minos (\d+\.\d+(?:\.\d+)?)|cmd LC_VERSION_MIN_MACOSX\n.*?version (\d+\.\d+(?:\.\d+)?)', commands, re.S)
    if any(normalize(first or second) > normalize(deployment) for first, second in os_versions):
        raise ValueError('Binary minimum macOS exceeds the app deployment target in ' + str(binary))
    identifiers = re.findall(r'cmd LC_ID_DYLIB\n\s+cmdsize \d+\n\s+name (.+?) \(offset', commands)
    if identifiers != ([install_name] if install_name else []):
        raise ValueError('Unexpected library install name in ' + str(binary))
    rpaths = re.findall(r'cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (.+?) \(offset', commands)
    safe_rpaths = set(private_rpath) if isinstance(private_rpath, (tuple, list)) else {private_rpath}
    if any(path not in safe_rpaths for path in rpaths):
        raise ValueError('Unsafe library search path in ' + str(binary))
    dependencies = output('/usr/bin/otool', '-L', str(binary)).splitlines()[1:]
    if install_name:
        if not dependencies or dependencies.pop(0).strip().split(' (')[0] != install_name:
            raise ValueError('Library identity is missing in ' + str(binary))
    for line in dependencies:
        dependency = line.strip().split(' (')[0]
        if dependency.startswith(('/usr/lib/', '/System/Library/')) and '..' not in Path(dependency).parts:
            continue
        if rpaths and dependency in {'@rpath/' + name for name in private_libraries}:
            continue
        if dependency in {path + '/' + name for path in safe_rpaths if path for name in private_libraries}:
            continue
        raise ValueError('Unbundled or unsafe library dependency in ' + str(binary) + ' - ' + dependency)


def check_tool(tool, build=BUILD):
    directory = build / tool['id']
    expected = {tool['executable'], *tool['libraries'], *resource_metadata(tool)}
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Missing private tool build folder ' + str(directory))
    actual = {str(p.relative_to(directory)) for p in directory.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != expected:
        raise ValueError('Unindexed or missing tool files in ' + str(directory))
    check_resources(tool, directory)
    names = [Path(p).name for p in tool['libraries']]
    check_binary(directory / tool['executable'], private_libraries=names,
                 private_rpath=('@executable_path/../lib', '@loader_path/../lib'))
    for library in tool['libraries']:
        check_binary(directory / library, '@rpath/' + Path(library).name, names, '@loader_path')
        if hashlib.sha256((directory / library).read_bytes()).hexdigest() != artifact_metadata(tool)['librarySHA256'][library]:
            raise ValueError('Private library differs from its pinned upstream input')
    executable = directory / tool['executable']
    if hashlib.sha256(executable.read_bytes()).hexdigest() != artifact_metadata(tool)['executableSHA256']:
        raise ValueError('Executable differs from its prepared upstream input')


def sign(path, identifier, identity):
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', identity, '--options', 'runtime',
                    '--identifier', identifier, str(path)], check=True)


def embed_tools():
    identity = os.environ.get('EXPANDED_CODE_SIGN_IDENTITY')
    if os.environ.get('CODE_SIGNING_ALLOWED', 'YES') != 'YES' or not os.environ.get('DEVELOPMENT_TEAM') or not identity:
        raise ValueError('A real signing configuration is required to embed tools')
    contents = Path(os.environ['TARGET_BUILD_DIR']) / os.environ['CONTENTS_FOLDER_PATH']
    if contents.name != 'Contents' or contents.parent.name != 'ShellCrate.app':
        raise ValueError('Unexpected app build destination')
    from system_tools import add_system_records
    index = add_system_records(prepared_index())
    destination = contents / 'Resources/Tools'
    destination.mkdir(parents=True, exist_ok=True)
    active = {tool['id'] for tool in index['tools']}
    for path in destination.iterdir():
        if path.is_dir() and path.name not in active:
            shutil.rmtree(path)
    for tool in index['tools']:
        if 'system' in tool:
            continue
        check_tool(tool)
        resources = destination / tool['id']
        if resources.exists(): shutil.rmtree(resources)
        shutil.copytree(ROOT / '.build/manuals-generated' / tool['id'], resources)
        legacy = contents / 'Frameworks' / (tool['id'] + '.framework')
        if legacy.exists(): shutil.rmtree(legacy)
        (contents / 'Frameworks' / ('libShellCrate_' + tool['id'] + '.dylib')).unlink(missing_ok=True)
        (contents / 'Resources' / (tool['id'] + '-manual.json')).unlink(missing_ok=True)
    (contents / 'Resources/Catalog.json').unlink(missing_ok=True)
    (destination / 'index.json').write_text(json.dumps(index, indent=2) + '\n')


if __name__ == '__main__':
    embed_tools()
