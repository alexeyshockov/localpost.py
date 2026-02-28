from dataclasses import dataclass
from typing import Self, final


@final
@dataclass(frozen=True, slots=True)
class URITemplate:
    """Basic URI Template (RFC 6570) implementation, just the first level from the spec."""

    template: str
    variable_names: tuple[str, ...]

    @classmethod
    def parse(cls, template: str) -> Self:
        # TODO Parse the URI template and extract variable names.
        raise NotImplementedError

    def match(self, uri: str) -> dict[str, str] | None:
        # TODO Implement URI template matching and variable extraction.
        raise NotImplementedError
