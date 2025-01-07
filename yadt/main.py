import os

import yadt.high_level
import logging
import argparse

logger = logging.getLogger(__name__)

def create_cache_folder():
    try:
        cache_folder = os.path.join(os.path.expanduser("~"), ".cache", "yadt")
        os.makedirs(cache_folder, exist_ok=True)
    except Exception as e:
        logger.critical(f'Failed to create cache folder at "~/.cache/yadt"', exc_info=True)
        exit(1)

def create_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "files",
        type=str,
        nargs="+",
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
        '--font',
        type=str,
        default=None,
        help='The font to use for pdf output. If not set, use the default font.'
    )
    translation_params.add_argument(
        "--pages",
        "-p",
        type=str,
        help="The list of page numbers to parse.",
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
    service_params = translation_params.add_mutually_exclusive_group()
    service_params.add_argument(
        '--openai', '-oai', default=False, action='store_true', help='Use OpenAI translator.'
    )
    service_params.add_argument(
        '--google', '-g', default=False, action='store_true', help='Use Google translator.'
    )
    openai_params = parser.add_argument_group('Translation - OpenAI Options', description='OpenAI specific options')
    openai_params.add_argument('--model', '-m', type=str, default='gpt-4o-mini', help='The OpenAI model to use for translation.')
    openai_params.add_argument('--base-url', '-b', type=str, default=None, help='The base URL for the OpenAI API.')
    openai_params.add_argument('--api-key', '-k', type=str, default=None, help='The API key for the OpenAI API.')

    return parser

def main():
    logging.basicConfig(level=logging.INFO)

    parser = create_parser()
    parsed_args = parser.parse_args()

    create_cache_folder()
    if parsed_args.debug:
        logger.setLevel(logging.DEBUG)
    yadt.high_level.translate()


if __name__ == "__main__":
    main()
