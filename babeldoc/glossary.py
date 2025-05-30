import csv
import re
from pathlib import Path


class GlossaryEntry:
    def __init__(self, source: str, target: str, target_language: str | None = None):
        self.source = source
        self.target = target
        self.target_language = target_language

    def __repr__(self):
        return f"GlossaryEntry(source='{self.source}', target='{self.target}', target_language='{self.target_language}')"


class Glossary:
    def __init__(self, name: str, entries: list[GlossaryEntry]):
        self.name = name
        self.entries = entries
        self.source_terms_regex: re.Pattern | None = None
        self.normalized_lookup: dict[str, tuple[str, str]] = {}
        self._build_regex_and_lookup()

    def _normalize_source(self, source_term: str) -> str:
        """Normalizes a source term by lowercasing and standardizing whitespace."""
        term = source_term.lower()
        term = re.sub(
            r"\s+", " ", term
        )  # Replace multiple whitespace with single space
        return term.strip()

    def _build_regex_and_lookup(self):
        """
        Builds a combined regex for all source terms and a lookup dictionary
        from normalized source terms to (original_source, original_target).
        Regex patterns are sorted by length in descending order to prioritize longer matches.
        """
        self.normalized_lookup = {}
        regex_patterns_for_or: list[str] = []

        if not self.entries:
            self.source_terms_regex = None
            return

        for entry in self.entries:
            normalized_key = self._normalize_source(entry.source)
            self.normalized_lookup[normalized_key] = (entry.source, entry.target)

            # Create a regex pattern for the source term, replacing spaces with \s+
            # and escaping other special characters.
            escaped_parts = [re.escape(part) for part in entry.source.split()]
            if escaped_parts:  # Ensure there are parts to join
                pattern_for_entry = r"\s+".join(escaped_parts)
                regex_patterns_for_or.append(pattern_for_entry)

        if regex_patterns_for_or:
            # Sort patterns by length, longest first, to help with overlapping terms
            regex_patterns_for_or.sort(key=len, reverse=True)
            combined_regex_str = "|".join(
                f"({pattern})" for pattern in regex_patterns_for_or
            )  # Add capturing groups
            self.source_terms_regex = re.compile(
                combined_regex_str, flags=re.IGNORECASE
            )
        else:
            self.source_terms_regex = None

    @classmethod
    def from_csv(cls, file_path: Path, target_lang_out: str) -> "Glossary":
        """
        Loads glossary entries from a CSV file.
        CSV format: source,target,tgt_lng (tgt_lng is optional)
        Filters entries based on tgt_lng matching target_lang_out.
        The glossary name is derived from the CSV filename.
        """
        glossary_name = file_path.stem
        loaded_entries: list[GlossaryEntry] = []

        # Normalize target_lang_out once for comparison
        normalized_target_lang_out = target_lang_out.lower().replace("-", "_")

        try:
            with file_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if not all(col in reader.fieldnames for col in ["source", "target"]):
                    raise ValueError(
                        f"CSV file {file_path} must contain 'source' and 'target' columns."
                    )

                for row in reader:
                    source = row["source"]
                    target = row["target"]
                    tgt_lng = row.get("tgt_lng", None)  # Handle optional tgt_lng

                    if tgt_lng and tgt_lng.strip():
                        normalized_entry_tgt_lng = (
                            tgt_lng.strip().lower().replace("-", "_")
                        )
                        if normalized_entry_tgt_lng != normalized_target_lang_out:
                            continue  # Skip if language doesn't match

                    loaded_entries.append(GlossaryEntry(source, target, tgt_lng))
        except FileNotFoundError:
            # Or handle as per your project's error strategy, e.g., log and return empty Glossary
            raise
        except Exception as e:
            # Or handle as per your project's error strategy
            raise ValueError(
                f"Error reading or parsing CSV file {file_path}: {e}"
            ) from e

        return cls(name=glossary_name, entries=loaded_entries)

    def __repr__(self):
        return f"Glossary(name='{self.name}', num_entries={len(self.entries)})"
