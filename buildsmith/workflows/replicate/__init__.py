"""W1 — replicate: a faithful, complete copy of an existing site into Builder.

A productized service, not a redesign tool. The success floor is *all original
content present, site navigable, routes preserved* — not "looks nicer".
"""

from buildsmith.workflows.replicate.build import ReplicateResult, emit, replicate
from buildsmith.workflows.replicate.crawl import (
    CrawlResult,
    crawl_local,
    crawl_site,
    fetch_assets,
    save_crawl,
)
from buildsmith.workflows.replicate.features import Inventory, extract_site
from buildsmith.workflows.replicate.htmlblocks import (
    ConversionError,
    ConversionResult,
    html_to_blocks,
)

__all__ = [
    "ConversionError",
    "ConversionResult",
    "CrawlResult",
    "Inventory",
    "extract_site",
    "ReplicateResult",
    "crawl_local",
    "crawl_site",
    "fetch_assets",
    "save_crawl",
    "emit",
    "html_to_blocks",
    "replicate",
]
