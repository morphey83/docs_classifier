"""Domain roles and the capabilities they grant.

Six fixed roles (locked in docs/architecture.md §0). Capabilities are the
atomic permission units checked by the API.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    owner = "owner"
    admin = "admin"
    editor = "editor"
    tagger = "tagger"
    viewer = "viewer"
    scanner = "scanner"


class Cap(StrEnum):
    view = "view"  # see documents & metadata, search, preview
    upload = "upload"  # add new documents / archives
    write = "write"  # edit tags & metadata, process the inbox
    download = "download"  # download originals, run exports
    process = "process"  # request OCR / indexing
    manage = "manage"  # members, invites, tag vocabulary, domain settings
    delete = "delete"  # soft-delete / restore / purge
    own = "own"  # delete the domain, transfer ownership


_ALL: frozenset[Cap] = frozenset(Cap)

ROLE_CAPS: dict[Role, frozenset[Cap]] = {
    Role.owner: _ALL,
    Role.admin: _ALL - {Cap.own},
    Role.editor: frozenset({Cap.view, Cap.upload, Cap.write, Cap.download, Cap.process}),
    Role.tagger: frozenset({Cap.view, Cap.write, Cap.download, Cap.process}),
    Role.viewer: frozenset({Cap.view, Cap.download}),
    Role.scanner: frozenset({Cap.view, Cap.process}),
}


def role_has(role: Role, cap: Cap) -> bool:
    return cap in ROLE_CAPS[role]


# Roles that can be assigned to a member (owner is set only via domain creation
# or an ownership transfer, never a plain "add member").
ASSIGNABLE_ROLES: tuple[Role, ...] = (
    Role.admin,
    Role.editor,
    Role.tagger,
    Role.viewer,
    Role.scanner,
)
