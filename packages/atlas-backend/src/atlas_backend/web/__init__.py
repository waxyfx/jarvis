"""Looking things up on the internet, so answers can be current.

The reason this exists: a model answers "what is the latest X" from training
data without ever saying that is where the answer came from. Old facts stated
confidently are worse than no answer, because nothing about them looks wrong.

Everything here runs on the backend, never on the laptop — see
``ToolManifest.runs_on``. The laptop may be switched off, and more to the point
fetching arbitrary addresses is not something to do from inside the owner's own
network.
"""

from atlas_backend.web.reader import Page, PageReader, WebFetchError
from atlas_backend.web.search import SearchError, SearchResult, WebSearch
from atlas_backend.web.tools import WEB_TOOLS, WebToolError, WebTools, run_web_tool

__all__ = [
    "WEB_TOOLS",
    "Page",
    "PageReader",
    "SearchError",
    "SearchResult",
    "WebFetchError",
    "WebSearch",
    "WebToolError",
    "WebTools",
    "run_web_tool",
]
