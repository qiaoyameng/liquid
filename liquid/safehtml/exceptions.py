"""Exceptions raised by the opt-in contextual HTML escaping mode."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Optional
from typing import Union

if TYPE_CHECKING:
    from pathlib import Path

    from liquid.token import Token


class ContextualEscapeError(Exception):
    """Raised when dynamic data reaches an unsupported or unsafe HTML position.

    Unlike most Liquid errors, contextual escaping errors are never downgraded
    by the environment's error tolerance mode. Rendering an unsafe template is
    refused rather than guessed at.

    Args:
        message: A description of the unsafe position.
        token: Optional token used to add line information to the message.
        template_name: Optional template name used in the message.
    """

    def __init__(
        self,
        message: str,
        *,
        token: Optional["Token"] = None,
        template_name: Union[str, "Path", None] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.token = token
        self.template_name = template_name

    def __str__(self) -> str:
        token = self.token
        if token is None or token.start_index < 0:
            return self.message

        try:
            lines = token.source.splitlines()
            cumulative = 0
            for line_number, line in enumerate(lines, start=1):
                cumulative += len(line) + 1
                if token.start_index < cumulative:
                    column = token.start_index - (
                        cumulative - len(line) - 1
                    )
                    location = (
                        f"{self.template_name}:{line_number}:{column}"
                        if self.template_name
                        else f"line {line_number}, column {column + 1}"
                    )
                    return f"{self.message} ({location})"
        except (AttributeError, TypeError):
            pass
        return self.message
