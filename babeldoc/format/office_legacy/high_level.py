import concurrent.futures
import threading
import time

from loguru import logger
from lxml import etree

from babeldoc.format.office.context import Context
from babeldoc.format.office.document_il.backend.document_builder import OfficeBuilder
from babeldoc.format.office.document_il.frontend.il_builder import OfficeILCreator
from babeldoc.format.office.document_il.types import ILData
from babeldoc.format.office.progress_monitor import ProgressMonitor
from babeldoc.format.office.translation_config import TranslateResult
from babeldoc.format.office.translator import Translator

IL_PATH = "output.il.json"


class OfficeTranslator:
    def __init__(self, translator: Translator, max_workers: int):
        self.global_context = Context()
        self.global_context.translator = translator
        self.global_context.max_workers = max_workers

        self.handlers = {}  # init in translate method

    def _recursive_process(
        self, element: etree.Element, il_data: ILData, context: Context
    ):
        local_name = etree.QName(element).localname
        try:
            if local_name in self.handlers:
                self.handlers[local_name](element, il_data, context)
            else:
                for child in list(element):
                    local_name = etree.QName(child).localname
                    if local_name in self.handlers:
                        self.handlers[local_name](child, il_data, context)
                    self._recursive_process(child, il_data, context)
        except Exception as e:
            logger.exception(e)

        return element

    def translate(self, docx_path: str, output_path: str):
        try:
            # Initialize progress monitor with 3 main stages
            stages = [
                ("document_processing", 20),  # 20% weight
                ("translation", 60),  # 60% weight
                ("document_rebuild", 20),  # 20% weight
            ]
            pm = ProgressMonitor(
                stages=stages,
                progress_change_callback=self._handle_progress_update,
                finish_callback=self._handle_completion,
            )

            start_time = time.time()

            # Document processing stage
            with pm.stage_start("document_processing", total=1) as stage:
                il_creator = OfficeILCreator(docx_path)
                self.handlers = il_creator.translatable_parts_processor.handlers
                il_data = il_creator.process_document()
                stage.advance()

            thread_local = threading.local()

            def get_thread_context():
                if not hasattr(thread_local, "context"):
                    thread_local.context = Context()
                    thread_local.context.translator = self.global_context.translator
                    if hasattr(self.global_context, "max_workers"):
                        thread_local.context.max_workers = (
                            self.global_context.max_workers
                        )
                return thread_local.context

            # 修改处理函数以使用线程本地context
            def process_single_element(element):
                context = get_thread_context()  # 获取当前线程的context
                etree_element = etree.fromstring(element.original_xml)
                processed_element = self._recursive_process(
                    etree_element, il_data, context
                )
                return etree.tostring(
                    processed_element, encoding="unicode", method="xml"
                )

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.global_context.max_workers
            ) as executor:
                futures = {}

                for element in il_data.document.elements:
                    future = executor.submit(process_single_element, element)
                    futures[future] = element

                time.sleep(0.35)  # for empty queue

                # async handler may already updated handled_xml
                # so we need to check if it is existed
                # Translation stage with progress tracking
                with pm.stage_start(
                    "translation", total=len(il_data.document.elements)
                ) as stage:
                    for future in concurrent.futures.as_completed(futures):
                        element = futures[future]
                        if not element.handled_xml:
                            element.handled_xml = future.result()
                        stage.advance()  # Update progress after each element

            # Document rebuild stage
            with pm.stage_start("document_rebuild", total=3) as stage:
                il_creator.save_il(IL_PATH)
                stage.advance()

                il_builder = OfficeBuilder(IL_PATH)
                il_builder.build_document()
                stage.advance()

                il_builder.save(output_path)
                stage.advance()

            # 初始化 TranslateResult 为 result
            finish_time = time.time()
            result = TranslateResult(
                original_pdf_path=docx_path,
                total_seconds=finish_time - start_time,
                mono_pdf_path=output_path,
                dual_pdf_path=None,
                no_watermark_mono_pdf_path=None,
                no_watermark_dual_pdf_path=None,
            )
            pm.translate_done(result)
        except Exception as e:
            logger.exception(f"translate error: {e}")
            pm.translate_error(e)
            raise
        finally:
            logger.debug("do_translate finally")
            pm.on_finish()
            # clean up output_path
            # os.remove(output_path)
            # os.remove(IL_PATH)
            # os.remove(docx_path)

    def _handle_progress_update(self, **kwargs):
        """Handle progress updates"""
        # Example implementation - could log to console or update UI
        logger.info(f"Progress: {kwargs.get('overall_progress', 0)}%")

    def _handle_completion(self, **kwargs):
        """Handle completion"""
        if kwargs.get("type") == "error":
            logger.error(f"Error: {kwargs.get('error')}")
        else:
            logger.info("Processing completed successfully")
