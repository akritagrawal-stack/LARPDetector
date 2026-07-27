"""detective: a LARP detector that turns a LinkedIn URL into an evidence Dossier.

Original build (not a fork). See README.md for architecture. The reasoning
"brain" is pluggable: ManualProvider ($0, human/Codex-in-the-loop) is the
default; ApiProvider is the open-source, fully-automated path.

No em dashes anywhere in this package (house rule).
"""

from __future__ import annotations

from .models import Buildability, Claim, Dossier, EvidenceTier

__all__ = ["Buildability", "Claim", "Dossier", "EvidenceTier"]
__version__ = "0.1.0"
