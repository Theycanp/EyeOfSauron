from __future__ import annotations

import re
from re import _constants, _parser


class UnsafeRegexError(ValueError):
    pass


_REPEAT_OPS = {
    _constants.MAX_REPEAT,
    _constants.MIN_REPEAT,
    _constants.POSSESSIVE_REPEAT,
}


def _walk(subpattern: _parser.SubPattern, *, inside_unbounded_repeat: bool = False) -> None:
    for operation, argument in subpattern.data:
        if operation in {_constants.GROUPREF, _constants.GROUPREF_EXISTS}:
            raise UnsafeRegexError("backreferences are not allowed")
        if operation in _REPEAT_OPS:
            _, maximum, child = argument
            unbounded = maximum == _constants.MAXREPEAT
            if inside_unbounded_repeat:
                raise UnsafeRegexError(
                    "nested repetition inside an unbounded repetition is not allowed"
                )
            if maximum != _constants.MAXREPEAT and maximum > 1000:
                raise UnsafeRegexError("bounded repetition cannot exceed 1000")
            _walk(child, inside_unbounded_repeat=unbounded)
            continue
        if operation is _constants.SUBPATTERN:
            _walk(argument[-1], inside_unbounded_repeat=inside_unbounded_repeat)
            continue
        if operation is _constants.BRANCH:
            if inside_unbounded_repeat:
                raise UnsafeRegexError(
                    "alternation inside an unbounded repetition is not allowed"
                )
            for branch in argument[1]:
                _walk(branch)
            continue
        if operation in {_constants.ASSERT, _constants.ASSERT_NOT, _constants.ATOMIC_GROUP}:
            child = argument[-1] if isinstance(argument, tuple) else argument
            _walk(child, inside_unbounded_repeat=inside_unbounded_repeat)


def compile_safe_regex(expression: str) -> re.Pattern[str]:
    """Compile the bounded-backtracking subset accepted by Argus rules."""

    parsed = _parser.parse(expression, 0)
    _walk(parsed)
    return re.compile(expression)
