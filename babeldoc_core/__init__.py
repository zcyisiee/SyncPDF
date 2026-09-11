"""Public, resumable core API for BabelDOC translation jobs.

The package deliberately keeps orchestration independent from a particular
model gateway.  Applications inject a provider implementing one of the small
interfaces in :mod:`babeldoc_core.providers`.
"""

from .config import JobConfig
from .job import DocumentJob
from .models import JobError, JobState
from .providers import LayoutProvider, ReviewerProvider, TranslatorProvider
from .protocol import (
    DocumentBlock,
    DocumentIR,
    FormulaAtom,
    ProtocolViolation,
    SkipPolicy,
    StyleAtom,
    TranslationProtocol,
    extract_formula_atoms,
    extract_style_atoms,
    atom_multiset,
    parse_markdown,
    serialize_markdown,
    validate_translation,
)

__all__ = [
    "DocumentJob",
    "DocumentBlock",
    "DocumentIR",
    "FormulaAtom",
    "JobConfig",
    "JobError",
    "JobState",
    "LayoutProvider",
    "ProtocolViolation",
    "SkipPolicy",
    "StyleAtom",
    "TranslationProtocol",
    "ReviewerProvider",
    "TranslatorProvider",
    "extract_formula_atoms",
    "extract_style_atoms",
    "atom_multiset",
    "parse_markdown",
    "serialize_markdown",
    "validate_translation",
]

__version__ = "0.1.0"
