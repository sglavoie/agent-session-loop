"""Http constants."""

from __future__ import annotations

#: The ``command`` field every Workbench envelope carries, in both modes and
#: on the CLI. Kept here because the envelope is now built here.
COMMAND_NAME = "workbench"


#: Which provider is carrying out the work. Named rather than inferred: a
#: capability report that could not say which mode produced it would be a
#: report a frontend could not act on.
MODE_LOCAL = "local"


MODE_REMOTE = "remote"


MODES: tuple[str, ...] = (MODE_LOCAL, MODE_REMOTE)


#: Methods that may not change anything. Everything else is a mutation and is
#: authorised as one.
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


#: 1 MiB, the historic local cap. The largest legitimate body is a redirect
#: correction or a reply, both of which are prose.
MAX_REQUEST_BODY = 1024 * 1024


#: The one path a connector posts to, and the only route whose body is a
#: transport frame rather than a form. Named here because the route table is
#: what a deployment reads it from; the Mac connector holds its own copy of the
#: string, because this package is not importable from there.
UPLINK_PATH = "/v1/ingest"


COMMAND_REDEEM_PATH = "/v1/commands/redeem"
