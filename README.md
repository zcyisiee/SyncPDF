# Yet Another Document Translator

## Getting Started

### Install from PyPI

We recommend using the Tool feature of [uv](https://github.com/astral-sh/uv) to install yadt.

1. First, you need to refer to [uv installation](https://github.com/astral-sh/uv#installation) to install uv and set up the `PATH` environment variable as prompted.

2. Use the following command to install yadt:
```bash
uv tool install --python 3.12 yadt

yadt --help
```

3. Use the `yadt` command. For example:
```bash
yadt --bing  --files example.pdf

# multiple files
yadt --bing  --files example1.pdf --files example2.pdf
```

### Install from Source

We still recommend using [uv](https://github.com/astral-sh/uv) to manage virtual environments.

1. First, you need to refer to [uv installation](https://github.com/astral-sh/uv#installation) to install uv and set up the `PATH` environment variable as prompted.

2. Use the following command to install yadt:
```bash
# clone the project
git clone https://github.com/funstory-ai/yadt

# enter the project directory
cd yadt

# install dependencies and run yadt
uv run yadt --help
```

3. Use the `uv run yadt` command. For example:
```bash
uv run yadt --bing --files examples/pdf/il_try_1/这是一个测试文件.pdf

# multiple files
uv run yadt --bing --files examples/pdf/il_try_1/这是一个测试文件.pdf --files example2.pdf
```
## Tips

1. It is recommended to use the absolute path.

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


## Known Issues

1. Parsing errors in the author and reference sections; they get merged into one paragraph after translation.
2. Lines are not supported.
3. Poor support for capitalizing initial letters.
4. Multi-letter corner mark

## How to Contribute

This project is not yet ready to accept community contributions. Please be patient. Thank you for your support! Community contributions will be open in the future.

However, currently, the following two types of issue reports are especially accepted:

1. Compatibility issues with pdf2zh: [#20](https://github.com/funstory-ai/yadt/issues/20)
2. Bad cases of this project found downstream: [#23](https://github.com/funstory-ai/yadt/issues/23)
