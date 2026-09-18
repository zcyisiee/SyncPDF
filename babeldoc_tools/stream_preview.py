"""Coalesce completed translation blocks into an isolated, disposable PDF preview.

One compiler runs at a time. Translation never waits for a build; pending blocks
are merged for the next build. Published previews are distinct from final PDFs.
"""
from __future__ import annotations

import json
import pickle
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf


class StreamPreview:
    def __init__(self, workdir, recorder):
        self.workdir = Path(workdir).resolve()
        self.recorder = recorder
        self.run_id = recorder.run_id
        self.isolated = self.workdir / ".bdt-serve" / f"stream-{self.run_id}"
        self.condition = threading.Condition()
        self.blocks = {}
        self.revision = 0
        self.stopped = False
        self.proc = None
        self.thread = None

    def submit(self, pid, body, label):
        with self.condition:
            self.blocks[pid] = (body, label)
            self.revision += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._worker, daemon=True)
                self.thread.start()
            self.condition.notify_all()

    def _prepare(self):
        from babeldoc_tools.serve.compile import _link_cache_dirs
        from babeldoc_tools.serve.compile import _snapshot_ignore

        shutil.copytree(self.workdir, self.isolated,
                        ignore=lambda directory, names: _snapshot_ignore(directory, names) +
                        [name for name in names if name == "preview"])
        _link_cache_dirs(self.workdir, self.isolated)
        state_file = self.isolated / "agent/state.pkl"
        with state_file.open("rb") as handle:
            state = pickle.load(handle)  # noqa: S301 - private workdir artifact
        source = resolve_source_pdf(state, self.workdir)
        if source is None:
            raise FileNotFoundError("Prepared PDF missing for preview")
        # Read-only source is safe to share; all IR and output writes are isolated.
        state["temp_pdf_path"] = str(source)
        with state_file.open("wb") as handle:
            pickle.dump(state, handle)
        (self.isolated / "agent/translated.md").unlink(missing_ok=True)

    def _command(self, stage):
        argv = [sys.executable, "-m", "babeldoc_tools", stage, "--workdir", str(self.isolated)]
        # Temporary files avoid unbounded pipe buffering and stdout deadlocks.
        with (self.isolated / f"{stage}.log").open("w+b") as output:
            with self.condition:
                if self.stopped:
                    return False
                self.proc = subprocess.Popen(argv, stdout=output, stderr=subprocess.DEVNULL)  # noqa: S603
                proc = self.proc
            code = proc.wait()
            with self.condition:
                self.proc = None
            if code:
                return False
            output.seek(0)
            envelope = json.load(output)
            return envelope.get("ok") is True

    def _build(self, blocks):
        from babeldoc_tools.translate import merge_translated_markdown

        merge_translated_markdown(self.isolated, blocks)
        if not self._command("apply") or not self._command("build"):
            raise RuntimeError("Preview apply/build failed")
        report = json.loads((self.isolated / "agent/reconstruct_report.json").read_text())
        pdf = Path(report["mono_pdf"])
        import pymupdf

        with pymupdf.open(pdf) as document:
            if document.page_count == 0:
                raise ValueError("Empty preview PDF")
        return pdf

    def _worker(self):
        try:
            self._prepare()
            built = 0
            while True:
                with self.condition:
                    self.condition.wait_for(lambda built=built: self.stopped or self.revision > built)
                    if self.stopped:
                        return
                    revision = self.revision
                    blocks = dict(self.blocks)
                pdf = self._build(blocks)
                with self.condition:
                    if self.stopped:
                        return
                    folder = self.workdir / "preview"
                    folder.mkdir(exist_ok=True)
                    # Immutable names let concurrent PDF range requests finish safely.
                    target = folder / f"{self.run_id}-{revision}.pdf"
                    pending = target.with_suffix(".tmp")
                    shutil.copyfile(pdf, pending)
                    pending.replace(target)
                    self.recorder.record_event("translate", "preview_ready", {
                        "artifact": f"preview/{target.name}", "revision": revision,
                        "paragraphs": len(blocks), "provisional": True,
                    })
                    built = revision
        except Exception as exc:
            with self.condition:
                if not self.stopped:
                    self.recorder.record_event("translate", "preview_failed", {
                        "error": type(exc).__name__, "message": "增量预览失败，最终编译仍会继续",
                    })
        finally:
            shutil.rmtree(self.isolated, ignore_errors=True)

    def close(self, *, failed=False):
        with self.condition:
            self.stopped = True
            if self.proc is not None:
                from babeldoc_tools.process_stream import kill_process_tree

                kill_process_tree(self.proc)
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        if failed:
            for path in (self.workdir / "preview").glob(f"{self.run_id}-*.pdf"):
                path.unlink(missing_ok=True)
