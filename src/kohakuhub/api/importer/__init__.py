"""Importing a repository from a fallback source into this hub.

``repo.import`` (``task.py``) does the work; the endpoints in ``router.py``
accept, follow and cancel it; ``source.py`` reads the source. Importing is how
a repository that only exists on HuggingFace (or another KohakuHub) becomes a
local one, instead of downloading it elsewhere and uploading it by hand.
"""

from kohakuhub.api.importer import task  # noqa: F401 - registers the task kinds
from kohakuhub.api.importer.router import router

__all__ = ["router", "task"]
