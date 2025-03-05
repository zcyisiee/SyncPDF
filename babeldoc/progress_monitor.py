import asyncio
import logging
import threading
import time
from asyncio import CancelledError
from collections.abc import Callable

logger = logging.getLogger(__name__)


class ProgressMonitor:
    def __init__(
        self,
        stages: list[tuple[str, float]],
        progress_change_callback: Callable | None = None,
        finish_callback: Callable | None = None,
        report_interval: float = 0.1,
        finish_event: asyncio.Event | None = None,
        cancel_event: threading.Event | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        # Convert stages list to dict with name and weight
        self.stage = {}
        total_weight = sum(weight for _, weight in stages)
        for name, weight in stages:
            normalized_weight = weight / total_weight
            self.stage[name] = TranslationStage(name, 0, self, normalized_weight)

        self.progress_change_callback = progress_change_callback
        self.finish_callback = finish_callback
        self.report_interval = report_interval
        logger.debug(f"report_interval: {self.report_interval}")
        self.last_report_time = 0
        self.finish_stage_count = 0
        self.finish_event = finish_event
        self.cancel_event = cancel_event
        self.loop = loop
        self.disable = False
        if finish_event and not loop:
            raise ValueError("finish_event requires a loop")
        if self.progress_change_callback:
            self.progress_change_callback(
                type="stage_summary",
                stages=[
                    {
                        "name": name,
                        "percent": self.stage[name].weight,
                    }
                    for name, _ in stages
                ],
            )
        self.lock = threading.Lock()

    def stage_start(self, stage_name: str, total: int):
        if self.disable:
            return DummyTranslationStage(stage_name, total, self, 0)
        stage = self.stage[stage_name]
        stage.run_time += 1
        stage.name = stage_name
        stage.display_name = (
            f"{stage_name} ({stage.run_time})" if stage.run_time > 1 else stage_name
        )
        stage.current = 0
        stage.total = total
        if self.progress_change_callback:
            self.progress_change_callback(
                type="progress_start",
                stage=stage.display_name,
                stage_progress=0.0,
                stage_current=0,
                stage_total=total,
                overall_progress=self.calculate_current_progress(),
            )
        self.last_report_time = 0.0
        return stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.debug("ProgressMonitor __exit__")

    def on_finish(self):
        if self.disable:
            return
        if self.cancel_event:
            self.cancel_event.set()
        if self.finish_event and self.loop:
            self.loop.call_soon_threadsafe(self.finish_event.set)
        if self.cancel_event and self.cancel_event.is_set():
            self.finish_callback(type="error", error=CancelledError)

    def stage_done(self, stage):
        if self.disable:
            return
        self.last_report_time = 0.0
        self.finish_stage_count += 1
        if (
            stage.current != stage.total
            and self.cancel_event is not None
            and not self.cancel_event.is_set()
        ):
            logger.warning(
                f"Stage {stage.name} completed with {stage.current}/{stage.total} items",
            )
            return
        if self.progress_change_callback:
            self.progress_change_callback(
                type="progress_end",
                stage=stage.display_name,
                stage_progress=100.0,
                stage_current=stage.total,
                stage_total=stage.total,
                overall_progress=self.calculate_current_progress(),
            )

    def calculate_current_progress(self, stage=None):
        # Count completed stages
        completed_stages = sum(
            1 for s in self.stage.values() if s.run_time > 0 and s.current == s.total
        )

        # If all stages are complete, return exactly 100
        if completed_stages == len(self.stage):
            return 100

        # Calculate progress based on weights
        progress = sum(
            s.weight * 100
            for s in self.stage.values()
            if s.run_time > 0 and s.current == s.total
        )
        if stage is not None and stage.total > 0:
            progress += stage.weight * stage.current * 100 / stage.total
        return progress

    def stage_update(self, stage, n: int):
        if self.disable:
            return
        with self.lock:
            report_time_delta = time.time() - self.last_report_time
            if report_time_delta < self.report_interval and stage.total > 3:
                return
            if self.progress_change_callback:
                self.progress_change_callback(
                    type="progress_update",
                    stage=stage.display_name,
                    stage_progress=stage.current * 100 / stage.total,
                    stage_current=stage.current,
                    stage_total=stage.total,
                    overall_progress=self.calculate_current_progress(stage),
                )
                self.last_report_time = time.time()

    def translate_done(self, translate_result):
        if self.disable:
            return
        if self.finish_callback:
            self.finish_callback(type="finish", translate_result=translate_result)

    def translate_error(self, error):
        if self.disable:
            return
        if self.finish_callback:
            self.finish_callback(type="error", error=error)

    def raise_if_cancelled(self):
        if self.cancel_event and self.cancel_event.is_set():
            raise asyncio.CancelledError

    def cancel(self):
        if self.disable:
            return
        if self.cancel_event:
            logger.info("Translation canceled")
            self.cancel_event.set()


class TranslationStage:
    def __init__(self, name: str, total: int, pm: ProgressMonitor, weight: float):
        self.name = name
        self.display_name = name
        self.current = 0
        self.total = total
        self.pm = pm
        self.run_time = 0
        self.weight = weight

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pm.stage_done(self)

    def advance(self, n: int = 1):
        self.current += n
        self.pm.stage_update(self, n)


class DummyTranslationStage:
    def __init__(self, name: str, total: int, pm: ProgressMonitor, weight: float):
        self.name = name
        self.display_name = name
        self.current = 0
        self.total = total
        self.pm = pm

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def advance(self, n: int = 1):
        pass
