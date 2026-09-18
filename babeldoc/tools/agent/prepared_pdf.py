"""Locate the prepared PDF when an agent workdir has been moved or copied."""
from pathlib import Path


def resolve_source_pdf(state: dict, workdir: Path | str, *, cwd: Path | str | None = None) -> Path | None:
    """Prefer this workdir's normalized input; never substitute the original PDF.

    The prepared PDF has normalized page boxes/annotations used by the saved IR.
    A raw source.pdf is not interchangeable with it.
    """
    workdir = Path(workdir)
    candidates = []
    pdf_path = state.get("pdf_path")
    if isinstance(pdf_path, str) and pdf_path:
        candidates.append(workdir / Path(pdf_path).stem / "input.pdf")
    candidates.append(workdir / "input.pdf")
    recorded = state.get("temp_pdf_path")
    if isinstance(recorded, str) and recorded:
        path = Path(recorded)
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend((workdir / path, (Path(cwd) if cwd else Path.cwd()) / path))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None
