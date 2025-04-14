import os
from datetime import datetime

from babeldoc.format.office.translator.translator import Translator


class DummyTranslator(Translator):
    def translate(self, text: str, style_hint: str = "") -> str:
        if isinstance(text, list):
            return self.translate_batch(text, [style_hint])

        return self.translate_batch([text], [style_hint])[0]

    def translate_batch(
        self, texts: list[str], style_hints: list[str] = []
    ) -> list[str]:
        """
        A dummy translator that returns predefined translations based on the example data.
        This implementation simply returns the next item in the list for each input text.

        Args:
            texts: List of strings to translate
            style_hints: List of style hints (not used in this implementation)

        Returns:
            List of translated strings
        """
        # If the input is empty, return an empty list
        if os.environ.get("USE_DUMMY_SAME", "false") == "true":
            return ["t_" + i for i in texts]

        rtn = []
        for i in texts:
            # Check if i is an empty string or a number
            if (
                i == ""
                or (isinstance(i, str) and i.isdigit())
                or isinstance(i, (int, float))
            ):
                rtn.append(i)
            else:
                rtn.append(str(datetime.now()))
        return rtn
