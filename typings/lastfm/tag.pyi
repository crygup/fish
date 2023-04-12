"""
This type stub file was generated by pyright.
"""

from typing import Any, Dict, Optional

from .http import HTTPClient
from .wiki import Wiki

__all__ = ("Tag",)

class Tag:
    __slots__ = ...
    def __init__(self, data: Dict[str, Any], http: HTTPClient) -> None: ...
    def __repr__(self) -> str: ...
    @property
    def wiki(self) -> Optional[Wiki]: ...
