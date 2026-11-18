"""Generate catalog records from package configuration and pinned formula metadata."""
import copy
from pathlib import PurePosixPath
import re

POLICIES = {'standard', 'interactive', 'rg', 'fd', 'jq', '7zz', 'curl', 'wget', 'rsync', 'mc', 'bash', 'zsh', 'kubectl'}
FORMULA_POLICIES = {'ripgrep': 'rg', 'fd': 'fd', 'jq': 'jq', 'sevenzip': '7zz',
                    'curl': 'curl', 'wget': 'wget', 'rsync': 'rsync',
                    'kubernetes-cli': 'kubectl', 'midnight-commander': 'mc', 'bash': 'bash', 'zsh': 'zsh'}
DISPLAY_FIELDS = ('name', 'summary', 'upstream', 'restrictions', 'variant', 'category', 'symbol')
PACKAGE_FIELDS = {'id', 'formula', 'binary', 'xpcServices', 'sourceBuild', 'reason',
                  'command', 'launchPolicy', 'manuals', 'manualArchive', 'extraNotices',
                  'noticeSource', 'argumentPrefix', 'transportFor', *DISPLAY_FIELDS}


def relative_path(value):
    path = PurePosixPath(value) if isinstance(value, str) else PurePosixPath('/')
    return bool(value) and not path.is_absolute() and '..' not in path.parts and str(path) == value


def pinned_archive(value):
    return (isinstance(value, dict) and isinstance(value.get('url'), str)
            and value['url'].startswith('https://')
            and isinstance(value.get('sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', value['sha256']))


def policy_for(entry):
    family = entry['formula'].split('@', 1)[0]
    expected = FORMULA_POLICIES.get(family)
    policy = entry.get('launchPolicy', expected or 'standard')
    if policy not in POLICIES or (expected and policy != expected):
        raise ValueError('Invalid launch policy for ' + entry['id'] + '. Existing formula restrictions must be preserved.')
    return policy


def validate_argument_prefix(entry, policy):
    prefix = entry.get('argumentPrefix', [])
    if (not isinstance(prefix, list) or len(prefix) > 16
            or any(not isinstance(value, str) or not value or '\0' in value for value in prefix)
            or sum(len(value.encode('utf-8')) for value in prefix) > 1024
            or (prefix and policy != 'standard')):
        raise ValueError('Invalid fixed argument prefix')


# Private executable slots have fixed consumers and never become terminal commands.
PRIVATE_PACKAGES = {
    'rsync-ssh': ('openssh', 'bin/ssh', ['rsync', 'restic', 'git-client'], ['OutgoingWorker']),
    'git-https': ('git', 'libexec/git-core/git-remote-https', ['gh', 'glab', 'git-client'], ['OutgoingWorker']),
}


def package_owners(tool):
    return tool.get('transportFor', [tool['id']])


def validate_package(entry):
    if 'transportFor' in entry or entry['id'] in PRIVATE_PACKAGES:
        if PRIVATE_PACKAGES.get(entry['id']) != (entry['formula'], entry['binary'], entry.get('transportFor'), entry['xpcServices']):
            raise ValueError('Unsupported private transport package')
    if entry['id'] in ('standard', 'interactive'):
        raise ValueError('Generic launch policy names are reserved package IDs')
    unknown = set(entry) - PACKAGE_FIELDS
    if unknown:
        raise ValueError('Unknown package configuration fields ' + ', '.join(sorted(unknown)))
    command = entry.get('command', PurePosixPath(entry['binary']).name)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}', command)
            or command in ('shellcrate', 'ShellCrateCLI')):
        raise ValueError('Invalid or reserved exported command')
    validate_argument_prefix(entry, policy_for(entry))
    for key in DISPLAY_FIELDS:
        if key in entry and (not isinstance(entry[key], str) or not entry[key].strip()):
            raise ValueError('Invalid package ' + key)
    for key in ('sourceBuild',):
        if key in entry and type(entry[key]) is not bool:
            raise ValueError('Invalid package ' + key)
    paths = entry.get('extraNotices', [])
    if not isinstance(paths, list) or any(not relative_path(path) for path in paths):
        raise ValueError('Notice paths must stay inside the pinned source')
    if 'manuals' in entry:
        manual = entry['manuals']
        if (not isinstance(manual, dict) or set(manual) != {'root', 'paths'}
                or manual['root'] not in ('bottle', 'source', 'build')
                or not isinstance(manual['paths'], list) or not manual['paths']
                or any(not relative_path(path) for path in manual['paths'])):
            raise ValueError('Manuals require a root and safe relative paths')
    for key in ('noticeSource', 'manualArchive'):
        if key in entry and (not pinned_archive(entry[key])
                             or not isinstance(entry[key].get('version'), str)):
            raise ValueError(key + ' requires a version, HTTPS URL and SHA-256 checksum')


def locked_configuration(entry):
    # Placement remains editable without refreshing immutable download pins.
    return {key: value for key, value in entry.items() if key not in ('xpcServices', 'reason')}


def license_expression(value):
    """Homebrew uses SPDX strings and nested any_of/all_of/with expressions."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict) and len(value) == 1:
        key, parts = next(iter(value.items()))
        if key in ('any_of', 'all_of') and isinstance(parts, list) and parts:
            return '(' + (' OR ' if key == 'any_of' else ' AND ').join(map(license_expression, parts)) + ')'
        if isinstance(parts, dict) and isinstance(parts.get('with'), str):
            return key + ' WITH ' + parts['with']
    raise ValueError('Missing or unsupported Homebrew license expression')


def catalog_record(entry, formula, previous=None):
    version = formula['versions']['stable']
    if not isinstance(version, str) or not version:
        raise ValueError('Homebrew package has no stable version')
    command = entry.get('command', PurePosixPath(entry['binary']).name)
    policy = policy_for(entry)
    if previous and (previous.get('homebrewFormula') != entry['formula']
                     or previous.get('command', previous['id']) != command
                     or (previous.get('launchPolicy', previous['id']) != policy
                         and (entry['id'], previous.get('launchPolicy'), policy) not in {
                             ('kubectl', 'standard', 'kubectl'), ('git-client', 'standard', 'interactive')})):
        raise ValueError('Keep package identity, formula, command and policy stable for ' + entry['id'] + '. Use a new ID for a different package.')
    same_release = previous and previous['version'] == version
    tool = copy.deepcopy(previous) if same_release else {}
    if entry.get('sourceBuild'):
        if not same_release or 'sourceBuild' not in tool:
            raise ValueError('Update the reviewed source recipe for ' + entry['id'] + ' before changing its release')
        source = formula['urls']['stable']
        if source.get('checksum') != tool['sha256']:
            raise ValueError('Source exception differs from the Homebrew release ' + entry['id'])
        tool['homebrewRelease'] = {'version': version, 'sourceSHA256': source['checksum'],
                                  'formulaRevision': formula['tap_git_head']}
    else:
        for key in ('precompiled', 'sourceBuild', 'builtArtifact'):
            tool.pop(key, None)
        tool['homebrew'] = {'formula': entry['formula'], 'binary': entry['binary']}
        if not same_release or tool.get('sourceLayout') == 'auto' or 'noticeSource' in entry:
            source = entry.get('noticeSource') or formula['urls']['stable']
            pin = {'url': source.get('url'), 'sha256': source.get('sha256', source.get('checksum'))}
            if not pinned_archive(pin):
                raise ValueError('No checksum-pinned source archive for ' + entry['id'] + '. Set noticeSource in homebrew-packages.json.')
            tool.update(pin)
            # Commands from one formula share the same verified source tree.
            # Executable slots remain independent and stable across releases.
            tool['sourceDirectory'] = 'package-' + entry['formula'] + '-' + pin['sha256'][:16]
            tool['sourceLayout'] = 'auto'
            tool.pop('revision', None)
    for key in ('noticeSource', 'manualArchive'):
        if key in entry and entry[key]['version'] != version:
            raise ValueError('Refresh ' + key + ' for ' + entry['id'] + ' ' + version)
    tool.update(id=entry['id'], version=version, command=command, launchPolicy=policy,
                executable='bin/' + command, libraries=[], commandDependencies=[],
                homebrewFormula=entry['formula'], packageConfiguration=locked_configuration(entry),
                name=entry.get('name', formula['name']),
                summary=entry.get('summary', formula.get('desc') or formula['name']),
                upstream=entry.get('upstream', formula['homepage']),
                license=license_expression(formula.get('license')),
                restrictions=entry.get('restrictions', 'Folder access and networking follow this package\'s Permissions. Child processes inherit its sandbox.'))
    for key in ('variant', 'category', 'symbol', 'manuals', 'manualArchive', 'extraNotices', 'argumentPrefix', 'transportFor'):
        if key in entry:
            tool[key] = copy.deepcopy(entry[key])
        else:
            tool.pop(key, None)
    return tool
