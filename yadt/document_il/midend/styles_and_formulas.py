from yadt.document_il.il_version_1 import Page, Document

class StylesAndFormulas:
    def process(self, document:Document):
        for page in document.page:
            self.process_page(page)

    def process_page(self, page: Page):
        pass