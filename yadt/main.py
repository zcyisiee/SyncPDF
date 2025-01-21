import os

from yadt.const import get_cache_file_path, CACHE_FOLDER
import yadt.high_level
import logging
import configargparse
import httpx
from yadt.document_il.translator.translator import (
    OpenAITranslator,
    GoogleTranslator,
    BingTranslator,
)
from yadt.document_il.translator.translator import set_translate_rate_limiter
from yadt.translation_config import TranslationConfig  # noqa: E402

logger = logging.getLogger(__name__)


def create_cache_folder():
    try:
        os.makedirs(CACHE_FOLDER, exist_ok=True)
    except OSError:
        logger.critical(
            f"Failed to create cache folder at {CACHE_FOLDER}", exc_info=True
        )
        exit(1)


def create_parser():
    parser = configargparse.ArgParser(
        config_file_parser_class=configargparse.TomlConfigParser(["yadt"]),
    )
    parser.add_argument(
        "-c",
        "--my-config",
        required=False,
        is_config_file=True,
        help="config file path",
    )
    parser.add_argument(
        "--files",
        type=str,
        # nargs="*",
        action="append",
        help="One or more paths to PDF files.",
    )
    parser.add_argument(
        "--debug",
        "-d",
        default=False,
        action="store_true",
        help="Use debug logging level.",
    )
    translation_params = parser.add_argument_group(
        "Translation",
        description="Used during translation",
    )
    translation_params.add_argument(
        "--font",
        type=str,
        default=None,
        help="The font to use for pdf output. " "If not set, use the default font.",
    )
    translation_params.add_argument(
        "--pages",
        "-p",
        type=str,
        help="Pages to translate. "
        "If not set, translate all pages. "
        "like: 1,2,1-,-3,3-5",
    )
    translation_params.add_argument(
        "--lang-in",
        "-li",
        type=str,
        default="en",
        help="The code of source language.",
    )
    translation_params.add_argument(
        "--lang-out",
        "-lo",
        type=str,
        default="zh",
        help="The code of target language.",
    )
    translation_params.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output directory for files. if not set, use same as input.",
    )
    translation_params.add_argument(
        "--qps",
        "-q",
        type=int,
        default=4,
        help="QPS limit of translation service",
    )
    translation_params.add_argument(
        "--ignore-cache",
        "-ic",
        default=False,
        action="store_true",
        help="Ignore translation cache.",
    )
    translation_params.add_argument(
        "--no-dual",
        default=False,
        action="store_true",
        help="Do not output bilingual PDF files",
    )
    translation_params.add_argument(
        "--no-mono",
        default=False,
        action="store_true",
        help="Do not output monolingual PDF files",
    )
    translation_params.add_argument(
        "--formular-font-pattern",
        type=str,
        default=None,
        help="Font pattern to identify formula text",
    )
    translation_params.add_argument(
        "--formular-char-pattern",
        type=str,
        default=None,
        help="Character pattern to identify formula text",
    )
    translation_params.add_argument(
        "--split-short-lines",
        default=False,
        action="store_true",
        help="Force split short lines into different paragraphs (may cause poor typesetting & bugs)",
    )
    translation_params.add_argument(
        "--short-line-split-factor",
        type=float,
        default=0.8,
        help="Split threshold factor. The actual threshold is the median length of all lines on the current page * this factor",
    )
    service_params = translation_params.add_mutually_exclusive_group()
    service_params.add_argument(
        "--openai",
        default=False,
        action="store_true",
        help="Use OpenAI translator.",
    )
    service_params.add_argument(
        "--google",
        default=False,
        action="store_true",
        help="Use Google translator.",
    )
    service_params.add_argument(
        "--bing",
        default=False,
        action="store_true",
        help="Use Bing translator.",
    )
    openai_params = parser.add_argument_group(
        "Translation - OpenAI Options", description="OpenAI specific options"
    )
    openai_params.add_argument(
        "--openai-model",
        "-m",
        type=str,
        default="gpt-4o-mini",
        help="The OpenAI model to use for translation.",
    )
    openai_params.add_argument(
        "--openai-base-url",
        "-b",
        type=str,
        default=None,
        help="The base URL for the OpenAI API.",
    )
    openai_params.add_argument(
        "--openai-api-key",
        "-k",
        type=str,
        default=None,
        help="The API key for the OpenAI API.",
    )

    return parser


def download_font_assets():
    assets = [
        (
            "noto.ttf",
            "https://github.com/satbyy/"
            "go-noto-universal/releases/download/v7.0/"
            "GoNotoKurrent-Regular.ttf",
        ),
        (
            "source-han-serif-cn.ttf",
            "https://github.com/junmer/source-han-serif-ttf"
            "/raw/refs/heads/master/SubsetTTF/CN/SourceHanSerifCN-Regular.ttf",
        ),
        (
            "source-han-serif-cn-bold.ttf",
            "https://github.com/junmer/source-han-serif-ttf"
            "/raw/refs/heads/master/SubsetTTF/CN/SourceHanSerifCN-Bold.ttf",
        ),
        (
            "SourceHanSansSC-Regular.ttf",
            "https://github.com/iizyd/SourceHanSansCN-TTF-Min"
            "/raw/refs/heads/main/source-file/ttf/SourceHanSansSC-Regular.ttf",
        ),
        (
            "SourceHanSansSC-Bold.ttf",
            "https://github.com/iizyd/SourceHanSansCN-TTF-Min"
            "/raw/refs/heads/main/source-file/ttf/SourceHanSansSC-Bold.ttf",
        ),
        (
            "LXGWWenKai-Regular.ttf",
            "https://github.com/lxgw/LxgwWenKai"
            "/raw/refs/heads/main/fonts/TTF/LXGWWenKai-Regular.ttf",
        ),
    ]
    for name, url in assets:
        save_path = get_cache_file_path(name)
        if os.path.exists(save_path):
            continue
        r = httpx.get(url, follow_redirects=True)
        if not r.is_success:
            logger.critical("cannot download noto font", exc_info=True)
            exit(1)
        with open(save_path, "wb") as f:
            f.write(r.content)


def main():
    from rich.logging import RichHandler
    logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])

    logging.getLogger("httpx").setLevel("CRITICAL")
    logging.getLogger("httpx").propagate = False
    logging.getLogger("openai").setLevel("CRITICAL")
    logging.getLogger("openai").propagate = False
    logging.getLogger("httpcore").setLevel("CRITICAL")
    logging.getLogger("httpcore").propagate = False
    logging.getLogger("http11").setLevel("CRITICAL")
    logging.getLogger("http11").propagate = False
    for v in logging.Logger.manager.loggerDict.values():
        if getattr(v, "name", None) is None:
            continue
        if (
            v.name.startswith("pdfminer")
            or v.name.startswith("peewee")
            or v.name.startswith("httpx")
            or "http11" in v.name
            or "openai" in v.name
        ):
            v.disabled = True
            v.propagate = False

    parser = create_parser()
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    create_cache_folder()

    # 验证翻译服务选择
    if not (args.openai or args.google or args.bing):
        parser.error("必须选择一个翻译服务：--openai、--google 或 --bing")

    # 验证 OpenAI 参数
    if args.openai and not args.openai_api_key:
        parser.error("使用 OpenAI 服务时必须提供 API key")

    # 实例化翻译器
    if args.openai:
        translator = OpenAITranslator(
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            model=args.openai_model,
            base_url=args.openai_base_url,
            api_key=args.openai_api_key,
            ignore_cache=args.ignore_cache,
        )
    elif args.bing:
        translator = BingTranslator(
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            ignore_cache=args.ignore_cache,
        )
    else:
        translator = GoogleTranslator(
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            ignore_cache=args.ignore_cache,
        )

    # 设置翻译速率限制
    set_translate_rate_limiter(args.qps)

    pending_files = []
    for file in args.files:
        # 清理文件路径，去除两端的引号
        if file.startswith("--files="):
            file = file.lstrip("--files=")
        file = file.lstrip("-").strip("\"'")
        if not os.path.exists(file):
            logger.error(f"文件不存在：{file}")
            exit(1)
        if not file.endswith(".pdf"):
            logger.error(f"文件不是 PDF 文件：{file}")
            exit(1)
        pending_files.append(file)

    font_path = args.font
    if not font_path:
        font_path = get_cache_file_path("source-han-serif-cn.ttf")
        download_font_assets()

    # 验证字体
    if font_path:
        if not os.path.exists(font_path):
            logger.error(f"字体文件不存在：{font_path}")
            exit(1)
        if not font_path.endswith(".ttf"):
            logger.error(f"字体文件不是 TTF 文件：{font_path}")
            exit(1)

    if args.output:
        if not os.path.exists(args.output):
            logger.info(f"输出目录不存在，创建：{args.output}")
            try:
                os.makedirs(args.output, exist_ok=True)
            except OSError:
                logger.critical(
                    f"Failed to create output folder at {args.output}", exc_info=True
                )
                exit(1)
    else:
        args.output = None

    for file in pending_files:
        # 清理文件路径，去除两端的引号
        file = file.strip("\"'")
        # 创建配置对象
        config = TranslationConfig(
            input_file=file,
            font=font_path,
            pages=args.pages,
            output_dir=args.output,
            translator=translator,
            debug=args.debug,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            no_dual=args.no_dual,
            no_mono=args.no_mono,
            qps=args.qps,
            formular_font_pattern=args.formular_font_pattern,
            formular_char_pattern=args.formular_char_pattern,
            split_short_lines=args.split_short_lines,
            short_line_split_factor=args.short_line_split_factor,
        )

        # 开始翻译
        yadt.high_level.translate(config)


if __name__ == "__main__":
    main()
