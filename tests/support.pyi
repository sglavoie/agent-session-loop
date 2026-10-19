"""Static typing surface for the runtime compatibility facade."""

from typing import Any

def __getattr__(name: str) -> Any: ...
