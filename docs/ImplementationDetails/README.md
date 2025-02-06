# Implementation Details

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Core Processing Flow

Main processing stages in order of actual execution and corresponding documentation:

1. [PDFParser.md](PDFParsing/PDFParsing.md): **PDF Parsing and Intermediate Layer Creation**

2. [LayoutParser](https://github.com/funstory-ai/yadt/blob/main/yadt/document_il/midend/layout_parser.py): **Layout OCR**

3. [ParagraphFinding.md](ParagraphFinding/ParagraphFinding.md): **Paragraph Recognition**

4. [StylesAndFormulas.md](StylesAndFormulas/StylesAndFormulas.md): **Style and Formula Processing**

5. [ILTranslator.md](ILTranslator/ILTranslator.md): **Intermediate Layer Translation**

6. [Typesetting.md](Typesetting/Typesetting.md): **Typesetting Processing**

7. [FontMapper](https://github.com/funstory-ai/yadt/blob/main/yadt/document_il/utils/fontmap.py): **Font Mapping**

8. [PDFCreation.md](PDFCreation/PDFCreation.md): **PDF Generation**

## API

1. [Async Translation API](AsyncTranslate/AsyncTranslate.md): **Async Translation API**

> [!TIP]
>
> Click on document links to view detailed implementation principles and configuration options
