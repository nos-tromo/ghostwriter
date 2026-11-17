"""Ghostwriter: the federation's chat assistant.

Talk to it, or attach a document and download the result in the chat.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("ghostwriter")
except PackageNotFoundError:  # running from source without an installed dist
    __version__ = "0+unknown"
