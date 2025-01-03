Yet Another Document Translator
===

## Background
There a lot projects and teams working on to make document editing and tranlslating easier like:
- [mathpix](https://mathpix.com/)
- [Doc2X](https://doc2x.noedgeai.com/)
- [minerU](https://github.com/opendatalab/MinerU)
- [PDFMathTranslate](https://github.com/Byaidu/PDFMathTranslate)

There are also some solutions to solve specific parts of the problem like:
- [layoutreader](https://github.com/microsoft/unilm/tree/master/layoutreader): the read order of the text block in a pdf
- [Surya](https://github.com/surya-is/surya): the structure of the pdf

This project hope to promote a standard pipeline and interface to solve the problem. 

In fact, there are two mainy stage of a PDF parser or translator:
- **Parsing**: A stage of parsing means to get the structure of the pdf such as text blocks, images, tables, etc.
- **Rendering**: A stage of rendering means to render the structure into a new pdf or other format.

For a service like mathpix, it will parse the pdf into a structure may be in a XML format, and then render them using a single column reader order as [layoutreader](https://github.com/microsoft/unilm/tree/master/layoutreader) does. The bad news is that the orignal structure lost.

Some people will use Adobe PDF Parser because it will generate a Word document and it keep the original structure. But it is some while expensive.
And you know, a pdf or word document is not a good for reading in mobile devices.

We offer a intermediate representation of the results from parser and can be rendered into a new pdf or other format. The pipeline is also a plugin-based system which everybody can add their new model, ocr, renderer, etc.

## Roadmap

Our fisrt 1.0 version goal is to finish a translation from [PDF Reference, Version 1.7](https://opensource.adobe.com/dc-acrobat-sdk-docs/pdfstandards/pdfreference1.7old.pdf) to the following language version:

- Simplified Chinese
- Traditional Chinese
- Japanese
- Spanish

And meet the following requirements:
- layout error less than 1%
- content loss less than 1%


