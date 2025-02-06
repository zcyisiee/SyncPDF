import asyncio
import threading
import time
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class ProgressMonitor:
    def __init__(
        self,
        translation_config,
        stages: list[str],
        progress_change_callback: callable = None,
        finish_callback: callable = None,
        report_interval: float = 0.1,
        finish_event: asyncio.Event = None,
        cancel_event: threading.Event = None,
            loop: Optional[asyncio.AbstractEventLoop] = None
    ):
        self.stage = {k: TranslationStage(k, 0, self) for k in stages}
        self.translation_config = translation_config
        self.progress_change_callback = progress_change_callback
        self.finish_callback = finish_callback
        self.report_interval = report_interval
        self.last_report_time = 0
        self.finish_stage_count = 0
        self.finish_event = finish_event
        self.cancel_event = cancel_event
        self.loop = loop
        if finish_event and not loop:
            raise ValueError("finish_event requires a loop")

    def stage_start(self, stage_name: str, total: int):
        stage = self.stage[stage_name]
        stage.total = total
        if self.progress_change_callback:
            self.progress_change_callback(
                type="progress_start",
                stage=stage_name,
                stage_progress=0.0,
                stage_current=0,
                stage_total=total,
            )
        return stage

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logger.debug("ProgressMonitor __exit__")
        if self.finish_event and self.loop:
            self.loop.call_soon_threadsafe(self.finish_event.set)

    def stage_done(self, stage):
        self.last_report_time = 0
        self.finish_stage_count += 1
        assert stage.current == stage.total
        if self.progress_change_callback:
            self.progress_change_callback(
                type="progress_end",
                stage=stage.name,
                stage_progress=100.0,
                stage_current=stage.total,
                stage_total=stage.total,
                overall_progress=self.calculate_current_progress(),
            )

    def calculate_current_progress(self, stage=None):
        progress = self.finish_stage_count * 100 / len(self.stage)
        if stage is not None:
            progress += stage.current * 100 / stage.total / len(self.stage)
        return progress

    def stage_update(self, stage, n: int):
        if (
            self.progress_change_callback
            and time.time() - self.last_report_time > self.report_interval
        ):
            self.progress_change_callback(
                type="progress_update",
                stage=stage.name,
                stage_progress=stage.current * 100 / stage.total,
                stage_current=stage.current,
                stage_total=stage.total,
                overall_progress=self.calculate_current_progress(stage),
            )
            self.last_report_time = time.time()

    def translate_done(self, translate_result):
        if self.finish_callback:
            self.finish_callback(type="finish", translate_result=translate_result)

    def translate_error(self, error):
        if self.finish_callback:
            self.finish_callback(type="error", error=str(error))

    def raise_if_cancelled(self):
        if self.cancel_event and self.cancel_event.is_set():
            logger.info("Translation canceled")
            raise asyncio.CancelledError

class TranslationStage:
    def __init__(self, name: str, total: int, pm: ProgressMonitor):
        self.name = name
        self.current = 0
        self.total = total
        self.pm = pm

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pm.stage_done(self)

    def advance(self, n: int = 1):
        self.current += n
        self.pm.stage_update(self, n)
