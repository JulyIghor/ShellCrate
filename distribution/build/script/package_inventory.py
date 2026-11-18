#!/usr/bin/env python3
"""Find the commands affected by a locked component or a change between revisions."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import subprocess

from package_metadata import package_owners

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()



def component_locks(index, tool):
    locks = {}
    source = 'sourceBuild' in tool
    if source:
        for name in tool['sourceBuild'].get('staticBottles', []):
            locks['brew:' + name] = index['homebrewBottles'][name]
        records = {d['id']: d for d in index.get('buildDependencies', [])}
        pending = list(tool['sourceBuild'].get('dependencies', []))
        locks['source:' + tool['id']] = {k: tool.get(k) for k in ('version', 'url', 'sha256', 'sourceBuild', 'upstreamPatches')}
    else:
        records = index.get('homebrewBottles', {})
        pending = [tool['homebrew']['formula']] if 'homebrew' in tool else []
        if 'outgoing' in tool.get('networkCapabilities', []) and 'homebrew' in tool:
            pending.append('ca-certificates')
    while pending:
        name = pending.pop()
        key = ('source:' if source else 'brew:') + name
        if key in locks:
            continue
        record = records[name]
        locks[key] = record
        pending.extend(record.get('dependencies', []))
    return locks


def build_components(index, tool):
    # A tap refresh updates formulaRevision for every formula even when the bottle is identical.
    return {name: {key: record[key] for key in ('version', 'sha256', 'dependencies') if key in record}
            for name, record in component_locks(index, tool).items()}


def package_keys(index):
    return {t['id']: fingerprint([t, index.get('homebrewTarget'), build_components(index, t)])
            for t in index['tools']}


def affected(before, after):
    old, new = package_keys(before), package_keys(after)
    changed = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    return sorted({owner for index in (before, after) for tool in index['tools']
                   if tool['id'] in changed for owner in package_owners(tool)})


def command_owners(index):
    owners = collections.defaultdict(set)
    for tool in index['tools']:
        for component in component_locks(index, tool):
            owners[component].update(package_owners(tool))
    return {name: sorted(tools) for name, tools in sorted(owners.items())}


def from_revision(ref):
    if ref.startswith('-'):
        raise ValueError('Invalid base revision')
    return json.loads(subprocess.check_output(['git', 'show', ref + ':Tools/index.json'], cwd=ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--component', help='Component name, optionally prefixed with brew: or source:')
    mode.add_argument('--base', help='Compare pinned package and dependency inputs to this Git revision')
    parser.add_argument('--probe', type=Path, help='Run exercises for affected providers in a signed shipping-tools probe')
    args = parser.parse_args()
    index = json.loads((ROOT / 'Tools/index.json').read_text())
    owners = command_owners(index)
    if args.base:
        names = affected(from_revision(args.base), index)
    elif args.component:
        names = sorted({name for key, values in owners.items()
                        if args.component in (key, key.split(':', 1)[1]) for name in values})
    else:
        if args.probe:
            parser.error('--probe requires --base or --component')
        print(json.dumps(owners, indent=2))
        return
    print(json.dumps(names), flush=True)
    if args.probe:
        from tempfile import TemporaryDirectory
        available = {t['id'] for t in index['tools']}
        selected = sorted(set(names) & available)
        if not selected:
            return
        if args.probe.name != 'SandboxProbeHost.app' or not args.probe.is_dir():
            parser.error('Supply the signed SandboxProbeHost.app built with shipping tools')
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / 'results'
            subprocess.run(['/usr/bin/open', '-n', '-W', '--stdout', str(log), str(args.probe.resolve()), '--args',
                            '--learning-self-test', '--learning-packages', ','.join(selected)], check=True, timeout=3600)
            report = log.read_text()
            print(report, end='')
            observed = {line.split()[2] for line in report.splitlines() if line.startswith('PASS lesson ')}
            if observed != set(selected) or 'FAIL ' in report or 'LEARNING CHECKS COMPLETE' not in report:
                raise SystemExit('The affected package exercises did not all pass')


if __name__ == '__main__':
    main()
