#!/usr/bin/env python3
"""Discover and render documentation from the locked bottle or source archives."""
import argparse
import bz2
import gzip
import hashlib
from html.parser import HTMLParser
import json
import lzma
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap

from tool_packages import load_index
from precompiled_tools import fetch, unpack
from homebrew_tools import unpack as unpack_bottle

ROOT = Path(__file__).resolve().parent.parent
GENERATED = ROOT / '.build/manuals-generated'


class ManualText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('head', 'script', 'style'):
            self.hidden += 1
        if not self.hidden and tag in ('p', 'div', 'pre', 'h1', 'h2', 'h3', 'li', 'tr', 'br'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('head', 'script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ('p', 'div', 'pre', 'h1', 'h2', 'h3', 'li', 'tr'):
            self.parts.append('\n')
        if not self.hidden and tag in ('td', 'th'):
            self.parts.append('  ')

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text.replace('\xa0', ' '))


def html_text(text):
    parser = ManualText()
    parser.feed(text)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(line.rstrip() for line in ''.join(parser.parts).splitlines())).strip()


def page_text(path):
    opener = {'.gz': gzip.open, '.bz2': bz2.open, '.xz': lzma.open}.get(path.suffix, open)
    with opener(path, 'rb') as stream:
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('Manual page exceeds the document size limit')
    return data.decode('utf-8')


def expand_includes(page, directory, chain=()):
    page = page.resolve()
    if not page.is_relative_to(directory.resolve()) or page in chain or len(chain) > 16:
        raise ValueError('Escaping or cyclic manual include')
    lines = []
    for line in page_text(page).splitlines(keepends=True):
        match = re.match(r"^[.']\s*(so|mso|pso|sy)\s+(.+?)\s*$", line)
        if not match:
            lines.append(line)
            continue
        # Asciidoctor emits this optional groff web-formatting macro. mandoc
        # renders the document without it. Never load host macro files.
        if match[1] == 'mso' and match[2] == 'www.tmac':
            continue
        if match[1] != 'so':
            raise ValueError('Unsupported external manual request')
        name = match[2]
        if not name or name.startswith('/') or '\\' in name or any(c.isspace() for c in name):
            raise ValueError('Invalid manual include')
        candidates = [page.parent / name, directory / name, directory / 'share/man' / name]
        candidates += [Path(str(path) + suffix) for path in candidates for suffix in ('.gz', '.bz2', '.xz')]
        target = next((path for path in candidates if path.is_file() and path.resolve().is_relative_to(directory.resolve())), None)
        if target is None:
            raise ValueError('Missing or escaping manual include ' + name)
        lines.append(expand_includes(target, directory, (*chain, page)))
    result = ''.join(lines)
    if len(result) > 16 * 1024 * 1024:
        raise ValueError('Expanded manual exceeds the document size limit')
    return result


def render_page(page, directory):
    if page.suffix.lower() in ('.htm', '.html'):
        return [dict(title=page.stem, body=html_text(page_text(page)))]
    if page.suffix.lower() in ('.txt', '.md', '.mkd', '.markdown', '.rst', '.adoc') or page.name.lower() == 'readme':
        return [dict(title=page.stem, body=page_text(page).strip())]
    # Feed expanded text on stdin so roff cannot fetch files outside its pinned
    # documentation root. No shell, upstream binary or build script is run.
    with tempfile.TemporaryDirectory() as working_directory:
        rendered = subprocess.check_output(['/usr/bin/mandoc', '-Tascii', '-Owidth=100'],
            input=expand_includes(page, directory).encode(), cwd=working_directory,
            stderr=subprocess.DEVNULL, timeout=30).decode()
    while '\b' in rendered:
        rendered = re.sub(r'.\x08', '', rendered)
    sections, title, lines = [], None, []
    for line in rendered.splitlines():
        if re.fullmatch(r'[A-Z][A-Z0-9 /()._-]+', line) and '  ' not in line:
            if title:
                sections.append(dict(title=title.title(), body=textwrap.dedent('\n'.join(lines)).strip()))
            title, lines = line, []
        elif title:
            lines.append(line)
    if title:
        sections.append(dict(title=title.title(), body=textwrap.dedent('\n'.join(lines)).strip()))
    return sections or [dict(title='Manual', body=rendered.strip())]


def discover_pages(entry, roots):
    override = entry.get('manuals')
    if override:
        directory = roots.get(override['root'])
        if directory is None:
            raise ValueError('Unavailable manual root for ' + entry['id'])
        pages = [directory / path for path in override['paths']]
        if any(not path.is_file() or not path.resolve().is_relative_to(directory.resolve()) for path in pages):
            raise ValueError('Missing or escaping configured manual for ' + entry['id'])
        return directory, pages
    command = entry.get('command', entry['id'])
    names = [command]
    binary = entry.get('homebrew', {}).get('binary')
    if binary and Path(binary).name not in names:
        names.append(Path(binary).name)
    patterns = [re.compile(re.escape(name) + r'\.[1-9](?:\.gz|\.bz2|\.xz)?$') for name in names]
    for origin in ('bottle', 'build', 'source'):
        directory = roots.get(origin)
        if directory is None or not directory.is_dir():
            continue
        search = directory / 'prefix/share/man' if origin == 'build' else directory
        pages = sorted((path for path in search.rglob('*') if path.is_file() and any(p.fullmatch(path.name) for p in patterns)),
                       key=lambda path: (next(i for i, p in enumerate(patterns) if p.fullmatch(path.name)),
                                         path.name, len(path.relative_to(search).parts), str(path)))
        if pages:
            primary = pages[0]
            if entry.get('launchPolicy', entry['id']) == 'zsh':
                pages = [primary] + [path for path in sorted(primary.parent.glob('zsh*.1'))
                                     if path.name not in (primary.name, 'zshall.1')]
            else:
                pages = [primary]
            return directory, pages
    # Several upstream projects publish a usage guide instead of a man page.
    # Keep that exact pinned document, without executing an untrusted --help
    # command during preparation or inventing a replacement manual.
    for origin in ('bottle', 'source'):
        directory = roots.get(origin)
        if directory is None:
            continue
        for name in ('README.md', 'README.mkd', 'README.markdown', 'README.rst', 'README.adoc', 'README.txt', 'README', 'readme.md'):
            page = directory / name
            if page.is_file() and page.resolve().is_relative_to(directory.resolve()) and page.stat().st_size:
                return directory, [page]
    raise ValueError('No manual found for ' + entry['id'] + '. Set manuals.root and manuals.paths in Tools/homebrew-packages.json.')


def archive_manual(entry):
    archive = entry['manualArchive']
    directory = ROOT / '.build/manuals' / entry['id']
    unpack(fetch(archive['url'], archive['sha256']), directory)
    sections = []
    if entry.get('launchPolicy', entry['id']) == '7zz':
        sections.append(dict(title='Synopsis', body='7zz <command> [switches] <archive> [files...]\n\nUse 7zz wherever the upstream manual shows 7z.\n\n' + (directory / 'readme.txt').read_text()))
    for path in sorted(directory.rglob('*')):
        if path.is_file() and path.suffix.lower() in ('.htm', '.html'):
            sections.append(dict(title=' / '.join(path.relative_to(directory).with_suffix('').parts),
                                 body=html_text(path.read_text())))
    return sections, [dict(archive=archive['url'], sha256=archive['sha256'])]


def prepare_manual(entry, index, verified_bottles=None):
    if entry.get('manualArchive'):
        sections, sources = archive_manual(entry)
    else:
        roots = {'source': ROOT / '.build/sources' / entry['sourceDirectory'],
                 'build': ROOT / '.build/source-build' / entry['id']}
        if 'homebrew' in entry:
            # A multi-command formula shares one immutable documentation tree.
            # Verify it once in this preparation pass, rather than rehashing
            # every binary for each command's manual.
            formula = entry['homebrew']['formula']
            directories = verified_bottles if verified_bottles is not None else {}
            if formula not in directories:
                directories[formula] = unpack_bottle(index['homebrewBottles'][formula])
            roots['bottle'] = directories[formula]
        directory, pages = discover_pages(entry, roots)
        sections, sources = [], []
        for number, page in enumerate(pages):
            rendered = render_page(page, directory)
            for section in rendered:
                if number:
                    section['title'] = page.stem + ' / ' + section['title']
            sections.extend(rendered)
            sources.append(dict(path=str(page.relative_to(directory)), sha256=hashlib.sha256(page.read_bytes()).hexdigest()))
    sections = [section for section in sections if section['body'].strip()]
    if not sections:
        raise ValueError('Empty manual for ' + entry['id'])
    seen = set()
    for section in sections:
        title, suffix = section['title'], 1
        while section['title'] in seen:
            suffix += 1
            section['title'] = title + ' (' + str(suffix) + ')'
        seen.add(section['title'])
    document = dict(tool=entry['id'], version=entry['version'], sections=sections, sources=sources)
    destination = GENERATED / entry['id'] / 'manual.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    print(entry['id'], len(sections), 'manual sections', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path)
    args = parser.parse_args()
    index = load_index(args.index) if args.index else load_index()
    verified_bottles = {}
    for entry in index['tools']:
        prepare_manual(entry, index, verified_bottles)


if __name__ == '__main__':
    main()
