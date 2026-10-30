"""DorkForge — dork library, scope-gated harvesting and lead generation.

Layers
------
library   : the dork recipe catalogue (templates + metadata)
parser    : plain-English prompt  ->  structured Target
forge     : Target + recipes      ->  ready-to-run dork queries
fetch     : polite, scope-gated page retrieval (robots.txt, rate limits, SSRF guard)
harvest   : page HTML            ->  contacts (emails, phones, people, org data)
leads     : dedupe, score, persist, export
pipeline  : orchestration + live event log
"""

__version__ = "2.0.0"
