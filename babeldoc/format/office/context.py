from babeldoc.format.office.translator import Translator


class Context:
    max_workers: int
    translator: Translator
    parts_processor: any

    def __init__(self):
        self.translator = None
        self.parts_processor = None
