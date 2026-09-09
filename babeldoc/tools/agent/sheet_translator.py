"""占位符协议提供者：为 extract/apply 提供与旧 LLM 路径一致的占位符格式。

ILTranslator 的 pre/post 处理依赖 translate_engine 的三个占位符方法来生成
``{vN}``（公式）与 ``<style id='N'>``/``</style>``（富文本）标记。本类只提供
这套协议，不做任何翻译调用——翻译本身由外部 agent subagent 完成。
"""

from babeldoc.translator.translator import BaseTranslator


class SheetProtocolTranslator(BaseTranslator):
    name = "sheet_protocol"

    def do_translate(self, text, rate_limit_params: dict = None):
        raise NotImplementedError(
            "SheetProtocolTranslator 不执行翻译；翻译由 agent subagent 完成，"
            "经 extract/apply 工具写回。"
        )

    def do_llm_translate(self, text, rate_limit_params: dict = None):
        if text is None:
            # ILTranslator 以 do_llm_translate(None) 探测 LLM 批量协议支持：
            # 返回 None 表示支持（从而启用富文本占位符路径），与旧 OpenAI 实现一致。
            return None
        raise NotImplementedError(
            "SheetProtocolTranslator 不执行翻译；翻译由 agent subagent 完成，"
            "经 extract/apply 工具写回。"
        )

    def get_formular_placeholder(self, placeholder_id: int | str):
        return "{v" + str(placeholder_id) + "}", f"{{\\s*v\\s*{placeholder_id}\\s*}}"

    def get_rich_text_left_placeholder(self, placeholder_id: int | str):
        return (
            f"<style id='{placeholder_id}'>",
            f"<\\s*style\\s*id\\s*=\\s*'\\s*{placeholder_id}\\s*'\\s*>",
        )

    def get_rich_text_right_placeholder(self, placeholder_id: int | str):
        return "</style>", r"<\s*\/\s*style\s*>"
