from yadt.il_try_1.document_il import il_try_1


class ILCreater:
    def __init__(self):
        self.current_page = None
        self.docs = il_try_1.Document(page=[])

    def onPageStart(self):
        self.current_page = il_try_1.Page()
        self.docs.page.append(self.current_page)

    def onPageCropBox(
        self,
        x0: float | int,
        y0: float | int,
        x1: float | int,
        y1: float | int,
    ):
        box = il_try_1.Box(x=float(x0), y=float(y0), x2=float(x1), y2=float(y1))
        self.current_page.cropbox = il_try_1.Cropbox(box=box)

    def onPageMediaBox(
        self,
        x0: float | int,
        y0: float | int,
        x1: float | int,
        y1: float | int,
    ):
        box = il_try_1.Box(x=float(x0), y=float(y0), x2=float(x1), y2=float(y1))
        self.current_page.mediabox = il_try_1.Mediabox(box=box)

    def onPageNumber(self, pageNumber: int):
        assert isinstance(pageNumber, int)
        assert pageNumber >= 0
        self.current_page.page_number = pageNumber

    def CreateIL(self):
        return self.docs

    def onTotalPages(self, totalPages: int):
        assert isinstance(totalPages, int)
        assert totalPages > 0
        self.docs.total_pages = totalPages
