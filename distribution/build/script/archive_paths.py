"""Resolve archive links using archive metadata only, never the host filesystem."""
from pathlib import PurePosixPath
import posixpath


def resolve_member(members, name, prefix, visited=()):
    name = posixpath.normpath(name)
    if not name.startswith(prefix) or name in visited or len(visited) > 64:
        raise ValueError('Escaping or cyclic archive link ' + name)
    parts = PurePosixPath(name).parts
    for count in range(1, len(parts) + 1):
        parent = '/'.join(parts[:count])
        item = members.get(parent)
        if item is not None and (item.issym() or item.islnk()):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(parent), item.linkname)
                                       if item.issym() else item.linkname)
            target = posixpath.join(target, *parts[count:]) if count < len(parts) else target
            return resolve_member(members, target, prefix, (*visited, name))
    if name not in members:
        raise ValueError('Missing archive link target ' + name)
    return members[name]
