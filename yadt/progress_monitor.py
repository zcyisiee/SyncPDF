import tqdm
from rich.progress import (
    Progress,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
TimeElapsedColumn,
)


class ProgressMonitor:
    def __init__(self, translation_config, stages: list[str]):
        if translation_config.use_rich_pbar:
            self.rich_pbar = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            )
            self.translate_task_id = self.rich_pbar.add_task("translate", total=100)
        else:
            self.tqdm_pbar = tqdm.tqdm(total=100, desc="translate")
        self.stage = {k: TranslationStage(k, 0, self) for k in stages}
        self.translation_config = translation_config
        self.use_rich_pbar = translation_config.use_rich_pbar

    def stage_start(self, stage_name: str, total: int):
        stage = self.stage[stage_name]
        stage.total = total
        if self.use_rich_pbar:
            stage.task_id = self.rich_pbar.add_task(stage_name, total=total)
        else:
            self.tqdm_pbar.set_description(stage_name)
        return stage

    def __enter__(self):
        if self.use_rich_pbar:
            self.rich_pbar.__enter__()
        else:
            self.tqdm_pbar.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.use_rich_pbar:
            self.rich_pbar.__exit__(exc_type, exc_val, exc_tb)
        else:
            self.tqdm_pbar.__exit__(exc_type, exc_val, exc_tb)

    def stage_done(self, stage):
        if self.use_rich_pbar:
            self.rich_pbar.refresh()
        else:
            self.tqdm_pbar.refresh()

    def stage_update(self, stage, n: int):
        relative_progress = n * 100 / (stage.total * len(self.stage))
        if self.use_rich_pbar:
            self.rich_pbar.update(stage.task_id, advance=n)
            self.rich_pbar.update(self.translate_task_id, advance=relative_progress)
        else:
            self.tqdm_pbar.update(relative_progress)


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
