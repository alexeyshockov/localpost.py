import re
from dataclasses import dataclass
from typing import Self, final

_VAR_PATTERN = re.compile(r"\{([^}]+)\}")


@final
@dataclass(frozen=True, slots=True)
class URITemplate:
    """Basic URI Template (RFC 6570) implementation, just the first level from the spec."""

    template: str
    variable_names: tuple[str, ...]
    _regex: re.Pattern[str]

    @classmethod
    def parse(cls, template: str) -> Self:
        variable_names: list[str] = []
        regex_parts: list[str] = []
        last_end = 0
        for m in _VAR_PATTERN.finditer(template):
            # Escape literal text between variables
            regex_parts.append(re.escape(template[last_end : m.start()]))
            var_name = m.group(1)
            variable_names.append(var_name)
            regex_parts.append(f"(?P<{var_name}>[^/]+)")
            last_end = m.end()
        regex_parts.append(re.escape(template[last_end:]))
        pattern = re.compile("^" + "".join(regex_parts) + "$")
        return cls(
            template=template,
            variable_names=tuple(variable_names),
            _regex=pattern,
        )

    def match(self, uri: str) -> dict[str, str] | None:
        m = self._regex.match(uri)
        if m is None:
            return None
        return m.groupdict()
