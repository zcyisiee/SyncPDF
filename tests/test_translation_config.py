import re

from babeldoc.translation_config import ConfigModel

# Since it is necessary to test whether the functionality meets the expected requirements,
# private functions and private methods are allowed to be called.
# pyright: reportPrivateUsage=false


class TestConfigArgs:
    def test_page_range_regex(self):
        test_strings = [
            "1,3,5,7,9",
            "1-3,5,7-9",
            "1,2-4,5,6-8,9",
            "10-12,14,16-18",
            "1-,5",
            "-5,10",
            "1-, 5, -3, 10-12",
        ]
        pattern = ConfigModel._page_range_pattern()
        for string in test_strings:
            assert re.match(pattern, string)
