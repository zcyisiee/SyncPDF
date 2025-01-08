

class StylesAndFormulas:
    def process(self, document):
        for page in document.page:
            self.process_page(page)