"""Parser class and argument helpers shared by the subcommand parser builders."""

from __future__ import annotations

import argparse
import math
import os
import sys
import weakref
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar, overload

from .cli_defaults import (
    _CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS,
    _WORKBENCH_DEFAULT_HOST,
    _WORKBENCH_DEFAULT_PORT,
)

#: The serve flags, by public ``dest``, with the value each takes when no
#: position passed it. They are accepted twice: on the ``asl workbench`` group
#: (whose bare form runs ``serve``) and on the ``serve`` leaf itself.
_WORKBENCH_SERVE_DEFAULTS: dict[str, object] = {
    "host": _WORKBENCH_DEFAULT_HOST,
    "port": _WORKBENCH_DEFAULT_PORT,
    "state_dir": None,
    "plan_root": None,
    "open": False,
    "json": False,
}
_WORKBENCH_SERVE_FLAGS = {
    "host": "--host",
    "port": "--port",
    "state_dir": "--state-dir",
    "plan_root": "--plan-root",
    "open": "--open",
    "json": "--json",
}
# Each position parses into its own suppressed ``dest`` so neither one's
# default can overwrite a value the other was given; parse_args merges them.
_WORKBENCH_SERVE_CHILD_DESTS = {
    name: f"_workbench_serve_{name}" for name in _WORKBENCH_SERVE_DEFAULTS
}
_WORKBENCH_GROUP_DESTS = {
    name: f"_workbench_group_{name}" for name in _WORKBENCH_SERVE_DEFAULTS
}


#: The namespace type ``parse_args`` fills and returns, matching the overloads
#: ``argparse.ArgumentParser.parse_args`` declares.
_NamespaceT = TypeVar("_NamespaceT")


#: The wording every numeric flag uses to refuse a value, shared with the
#: handler-side checks (``assurance closeout --rework-minutes``) so a refusal
#: reads the same whether argparse or the handler reports it.
NON_NEGATIVE_NUMBER = "must be a finite, non-negative number"
POSITIVE_NUMBER = "must be a finite, positive number"
NON_NEGATIVE_INTEGER = "must be a non-negative integer"
POSITIVE_INTEGER = "must be a positive integer"
PORT_NUMBER = "must be a TCP port number from 0 to 65535"

_MAX_PORT = 65535


def _parse_float(text: str, requirement: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{requirement}; got {text!r}") from None
    # ``float`` accepts "nan" and "inf"; neither is a usable duration, count,
    # or age, and NaN would reach JSON output as a bare, invalid ``NaN``.
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{requirement}; got {text!r}")
    return value


def _parse_int(text: str, requirement: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{requirement}; got {text!r}") from None


def flag_refusal(flag: str, requirement: str, value: object) -> str:
    """Word a handler-side refusal of ``flag`` like a ``type=`` refusal.

    Flags a handler validates itself, so the refusal keeps its JSON ``reason``
    under ``--json``, use this to read the same as the argparse-typed ones:
    ``--since-days must be a positive integer; got '-1'``. A whole float is
    echoed without its ``.0`` so ``--stale-days -1`` does not read back as
    ``-1.0``.
    """
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        echoed = str(int(value))
    else:
        echoed = str(value)
    return f"{flag} {requirement}; got {echoed!r}"


def non_negative_float(text: str) -> float:
    """``type=`` for a finite number of seconds, days or minutes, zero allowed."""
    value = _parse_float(text, NON_NEGATIVE_NUMBER)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{NON_NEGATIVE_NUMBER}; got {text!r}")
    return value


def positive_float(text: str) -> float:
    """``type=`` for a finite number that must be strictly above zero."""
    value = _parse_float(text, POSITIVE_NUMBER)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{POSITIVE_NUMBER}; got {text!r}")
    return value


def non_negative_int(text: str) -> int:
    """``type=`` for a count or index where zero is meaningful."""
    value = _parse_int(text, NON_NEGATIVE_INTEGER)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{NON_NEGATIVE_INTEGER}; got {text!r}")
    return value


def positive_int(text: str) -> int:
    """``type=`` for a count, limit or timeout that must be at least one."""
    value = _parse_int(text, POSITIVE_INTEGER)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{POSITIVE_INTEGER}; got {text!r}")
    return value


def port_number(text: str) -> int:
    """``type=`` for a TCP port; 0 asks the kernel for an ephemeral one."""
    value = _parse_int(text, PORT_NUMBER)
    if not 0 <= value <= _MAX_PORT:
        raise argparse.ArgumentTypeError(f"{PORT_NUMBER}; got {text!r}")
    return value


def int_in_range(low: int, high: int) -> Callable[[str], int]:
    """``type=`` for an integer in ``low..high`` inclusive, such as an hour."""
    requirement = f"must be an integer from {low} to {high}"

    def parse(text: str) -> int:
        value = _parse_int(text, requirement)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{requirement}; got {text!r}")
        return value

    return parse


def _workbench_dest(name: str, *, child_parser: bool) -> str:
    if child_parser:
        return _WORKBENCH_SERVE_CHILD_DESTS[name]
    return _WORKBENCH_GROUP_DESTS[name]


_UNSET = object()


def _pop_attr(args: argparse.Namespace, name: str) -> object:
    value = getattr(args, name, _UNSET)
    if value is not _UNSET:
        delattr(args, name)
    return value


def _normalize_workbench_serve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Merge the group- and leaf-position serve flags into public dests.

    Only bare ``asl workbench`` and ``asl workbench serve`` carry serve flags
    this far; the subparsers action already refused them (or forwarded
    ``--json``) for every other leaf.
    """
    if getattr(args, "command", None) != "workbench" or getattr(
        args, "workbench_command", None
    ) not in (None, "serve"):
        return args

    for name, default in _WORKBENCH_SERVE_DEFAULTS.items():
        group_value = _pop_attr(args, _WORKBENCH_GROUP_DESTS[name])
        child_value = _pop_attr(args, _WORKBENCH_SERVE_CHILD_DESTS[name])
        if name == "plan_root":
            roots: list[object] = []
            for value in (group_value, child_value):
                if isinstance(value, list):
                    roots.extend(value)
            setattr(args, name, roots or None)
        elif child_value is not _UNSET:
            setattr(args, name, child_value)
        elif group_value is not _UNSET:
            setattr(args, name, group_value)
        else:
            setattr(args, name, default)
    return args


#: ``asl workbench`` group parsers, whose serve flags only bare ``asl
#: workbench`` and ``serve`` use; the subparsers action checks other leaves.
_WORKBENCH_GROUP_PARSERS: weakref.WeakSet[argparse.ArgumentParser] = weakref.WeakSet()


def _reconcile_workbench_group_flags(
    parser: argparse.ArgumentParser,
    leaf: argparse.ArgumentParser,
    leaf_name: str,
    namespace: argparse.Namespace,
) -> None:
    """Forward a group ``--json`` to a non-serve leaf; refuse the rest.

    ``asl workbench --json registry`` means the same as ``asl workbench
    registry --json``. The bind and browser flags mean nothing to a leaf other
    than ``serve``, so passing them there is an error rather than a silent
    no-op.
    """
    leaf_has_json = any(action.dest == "json" for action in leaf._actions)
    if leaf_has_json and _pop_attr(namespace, _WORKBENCH_GROUP_DESTS["json"]) is True:
        namespace.json = True
    given = [
        flag
        for name, flag in _WORKBENCH_SERVE_FLAGS.items()
        if hasattr(namespace, _WORKBENCH_GROUP_DESTS[name])
    ]
    if given:
        parser.error(
            f"{', '.join(given)} only apply to `asl workbench serve` (or bare "
            f"`asl workbench`); pass `{leaf_name}` flags after `{leaf_name}`"
        )


if TYPE_CHECKING:
    _SubParsersBase = argparse._SubParsersAction["_AgentSessionLoopArgumentParser"]
else:  # the runtime class is not subscriptable
    _SubParsersBase = argparse._SubParsersAction


class _DescribedSubParsersAction(_SubParsersBase):
    """Subparser registry that gives every leaf a ``--help`` summary.

    ``help=`` only labels a subcommand in its parent's listing; argparse leaves
    the subcommand's own ``--help`` without a summary unless ``description=``
    is passed too. Defaulting the description to the help text here means a
    new ``add_parser`` call gets a summary without repeating itself, while an
    explicit, longer ``description=`` still wins.
    """

    def add_parser(self, name: str, **kwargs: Any) -> _AgentSessionLoopArgumentParser:
        help_text = kwargs.get("help")
        if (
            kwargs.get("description") is None
            and isinstance(help_text, str)
            and help_text != argparse.SUPPRESS
        ):
            # Choice help is %-expanded in the parent listing; descriptions
            # are printed verbatim, so undo the escaping.
            kwargs["description"] = help_text.replace("%%", "%")
        return super().add_parser(name, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """Report leftover tokens from the subcommand that was matched.

        argparse hands a subcommand's unrecognized tokens back up to the root,
        which then prints the root usage listing every command. A subcommand
        consumes every remaining token, so nothing above it could have used
        them: erroring here names the leaf's own usage instead. Nested groups
        run their own action first, so the deepest matched parser reports.
        """
        super().__call__(parser, namespace, values, option_string)
        chosen = self._name_parser_map.get(values[0])
        if chosen is None:
            return
        leftovers = getattr(namespace, argparse._UNRECOGNIZED_ARGS_ATTR, None)
        if leftovers:
            delattr(namespace, argparse._UNRECOGNIZED_ARGS_ATTR)
            chosen.error(f"unrecognized arguments: {' '.join(leftovers)}")
        if chosen in _PLAN_DIR_ALIAS_PARSERS:
            _reconcile_plan_dir_alias(chosen, namespace, "plan_dir")
        if parser in _WORKBENCH_GROUP_PARSERS and values[0] != "serve":
            _reconcile_workbench_group_flags(parser, chosen, values[0], namespace)


_PLAN_DIR_POSITIONAL_DEST = "_plan_dir_positional"

#: Leaves built by :func:`_add_plan_dir_with_option_alias`, whose two plan
#: spellings the subparsers action reconciles once the leaf has parsed.
_PLAN_DIR_ALIAS_PARSERS: weakref.WeakSet[argparse.ArgumentParser] = weakref.WeakSet()


def _add_plan_dir_with_option_alias(
    parser: argparse.ArgumentParser, *, help_text: str
) -> None:
    """Accept ``PLAN_DIR`` positionally, keeping ``--plan-dir`` as an alias.

    Exactly one spelling is required, and passing both is refused unless they
    name the same path. The subparsers action reconciles the two after the
    leaf parses, so the error names the leaf's own usage line.
    """
    parser.add_argument(
        _PLAN_DIR_POSITIONAL_DEST,
        metavar="PLAN_DIR",
        nargs="?",
        default=None,
        help=help_text,
    )
    parser.add_argument(
        "--plan-dir",
        dest="plan_dir",
        metavar="PLAN_DIR",
        default=None,
        help="Alias for the PLAN_DIR positional",
    )
    _PLAN_DIR_ALIAS_PARSERS.add(parser)


def _reconcile_plan_dir_alias(
    parser: argparse.ArgumentParser, namespace: argparse.Namespace, dest: str
) -> None:
    positional = getattr(namespace, _PLAN_DIR_POSITIONAL_DEST, None)
    option = getattr(namespace, dest, None)
    if hasattr(namespace, _PLAN_DIR_POSITIONAL_DEST):
        delattr(namespace, _PLAN_DIR_POSITIONAL_DEST)
    if (
        positional is not None
        and option is not None
        and os.path.realpath(os.path.expanduser(positional))
        != os.path.realpath(os.path.expanduser(option))
    ):
        parser.error(
            f"PLAN_DIR {positional!r} and --plan-dir {option!r} disagree; "
            "pass one of them"
        )
    chosen = positional if positional is not None else option
    if chosen is None:
        parser.error("the following arguments are required: PLAN_DIR")
    setattr(namespace, dest, chosen)


class _AgentSessionLoopArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Python 3.15 turns typo suggestions on by default; 3.14 needs asking.
        kwargs.setdefault("suggest_on_error", True)
        # A unique prefix must not stand in for a long option: ``gc --app``
        # would otherwise run as ``--apply``. Subparsers inherit this class.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        # Nested ``add_subparsers`` calls build children of this same class,
        # so every level inherits the described-subparser registry.
        self.register("action", "parsers", _DescribedSubParsersAction)

    def show_as_alternatives(self, *actions: argparse.Action) -> None:
        """Render ``actions`` as ``[A | B]`` in usage without enforcing it.

        For a pair the handler already validates itself -- so a conflict keeps
        its JSON ``reason`` instead of argparse's plain-text exit 2 -- while
        the usage line still tells the reader that the two are alternatives.
        """
        group = argparse._MutuallyExclusiveGroup(self)
        group._group_actions.extend(actions)
        self._display_only_groups.append(group)

    @property
    def _display_only_groups(self) -> list[argparse._MutuallyExclusiveGroup]:
        groups = self.__dict__.setdefault("_display_groups", [])
        return groups

    @contextmanager
    def _with_display_groups(self) -> Iterator[None]:
        added = self._display_only_groups
        self._mutually_exclusive_groups.extend(added)
        try:
            yield
        finally:
            for group in added:
                self._mutually_exclusive_groups.remove(group)

    def format_usage(self) -> str:
        with self._with_display_groups():
            return super().format_usage()

    def format_help(self) -> str:
        with self._with_display_groups():
            return super().format_help()

    def error(self, message: str) -> NoReturn:
        # A mistyped command already gets argparse's "maybe you meant" hint;
        # the forty-name list after it buries that hint, and `--help` has it.
        if "maybe you meant" in message:
            message, _, _ = message.partition(" (choose from ")
            message += f" Run `{self.prog} --help` for the full list."
        super().error(message)

    @overload
    def parse_args(
        self, args: Iterable[str] | None = None, namespace: None = None
    ) -> argparse.Namespace: ...

    @overload
    def parse_args(
        self, args: Iterable[str] | None, namespace: _NamespaceT
    ) -> _NamespaceT: ...

    @overload
    def parse_args(self, *, namespace: _NamespaceT) -> _NamespaceT: ...

    def parse_args(
        self,
        args: Iterable[str] | None = None,
        namespace: Any | None = None,
    ) -> Any:
        # Materialize once: ``args`` is read twice below, and an iterator
        # would reach the provenance record already exhausted.
        argv = None if args is None else list(args)
        parsed = super().parse_args(argv, namespace)
        parsed = _normalize_workbench_serve_args(parsed)
        # Record the exact tokens that produced this namespace so lazy handler
        # dispatch (e.g. `runner.run_from_args`) can detect which flags were
        # passed explicitly without falling back to the process-global
        # `sys.argv`. This keeps `--resume` explicit-flag provenance correct
        # through `cli.main(argv)` and direct resolved-handler dispatch.
        if not hasattr(parsed, "_run_argv"):
            parsed._run_argv = list(sys.argv[1:] if argv is None else argv)
        return parsed


def _add_workbench_serve_args(
    parser: argparse.ArgumentParser, *, child_parser: bool = False
) -> None:
    if not child_parser:
        _WORKBENCH_GROUP_PARSERS.add(parser)
    parser.add_argument(
        "--host",
        dest=_workbench_dest("host", child_parser=child_parser),
        default=argparse.SUPPRESS,
        metavar="HOST",
        help=(
            "Loopback interface to bind; other addresses are refused "
            f"(default: {_WORKBENCH_DEFAULT_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        dest=_workbench_dest("port", child_parser=child_parser),
        type=port_number,
        default=argparse.SUPPRESS,
        metavar="PORT",
        help=(
            f"TCP port to bind (default: {_WORKBENCH_DEFAULT_PORT}; "
            "use 0 for an ephemeral port)"
        ),
    )
    parser.add_argument(
        "--state-dir",
        dest=_workbench_dest("state_dir", child_parser=child_parser),
        default=argparse.SUPPRESS,
        metavar="DIR",
        help="Override operational state directory",
    )
    parser.add_argument(
        "--plan-root",
        dest=_workbench_dest("plan_root", child_parser=child_parser),
        action="append",
        default=argparse.SUPPRESS,
        metavar="DIR",
        help=(
            "Extra plan root directory to scan for discoverable plans (may be repeated)"
        ),
    )
    parser.add_argument(
        "--open",
        dest=_workbench_dest("open", child_parser=child_parser),
        action="store_true",
        default=argparse.SUPPRESS,
        help="Open the Workbench URL in the default browser after binding",
    )
    parser.add_argument(
        "--json",
        dest=_workbench_dest("json", child_parser=child_parser),
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON output for the startup record",
    )


def _add_plan_reference_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR_OR_ID",
        help=(
            "Split plan directory or plan label (the compact plan label "
            "printed after 'plan' by 'asl sessions')"
        ),
    )
    parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )


def _add_control_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the runner's receipt instead of firing and forgetting",
    )
    parser.add_argument(
        "--timeout",
        # Spelled as the mailbox waits spell it too, and bound to the same
        # dest, so the value is never confused with an unrelated `timeout` on
        # a shared namespace and a supervisor need not remember which family
        # a wait belongs to.
        "--wait-timeout",
        dest="wait_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Receipt wait timeout in seconds; requires --wait "
            f"(default: {_CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS:g}; "
            "maximum: 1800)"
        ),
    )


def _add_unsafe_bypass_flags(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(unsafe_bypass=True)
    parser.add_argument(
        "--unsafe-bypass",
        dest="unsafe_bypass",
        action="store_true",
        help="Use harness-specific bypass flags for sandbox and approval prompts "
        "(default)",
    )
    parser.add_argument(
        "--no-unsafe-bypass",
        dest="unsafe_bypass",
        action="store_false",
        help="Use the harness default sandbox and approval behavior",
    )


# Nested groups list their leaves and nothing else, so an agent that reads
# `asl outbox --help` sees no flags and concludes they do not exist. Every group
# says where the flags actually live instead. `parent_form_leaf` names the leaf a
# group's bare form runs when the group installs that leaf's flags on itself, so
# the wording never denies flags the same help output displays.
def _group_epilogue(
    group: str,
    leaf_example: str,
    *,
    guide: bool = False,
    parent_form_leaf: str | None = None,
) -> str:
    if parent_form_leaf is None:
        lines = [
            f"This help lists the `asl {group}` leaf commands only.",
            f"Flags belong to a leaf, so read `{leaf_example}` for its flags.",
        ]
    else:
        lines = [
            f"This help lists the `asl {group}` leaf commands plus the flags "
            f"the bare `asl {group}` form accepts.",
            f"Bare `asl {group}` runs `{parent_form_leaf}` with those flags; "
            f"every other leaf has its own, so read `{leaf_example}` "
            "or that leaf's help.",
        ]
    if guide:
        lines.append("Run `asl supervisor-guide` for the whole supervision workflow.")
    return "\n".join(lines)
