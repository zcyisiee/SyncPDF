import base64
import datetime
import hashlib
import threading
import time
from string import ascii_uppercase
from typing import Union

from babeldoc.format.office.document_il.opc.constants import CONTENT_TYPE as CT
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.types import ILDataPart

PartLike = Union[Part, ILDataPart]


def get_main_part(parts: dict[str, PartLike] | list[PartLike]) -> PartLike:
    inner_parts = []
    if isinstance(parts, dict):
        inner_parts = parts.values()
    else:
        inner_parts = parts

    for part in inner_parts:
        if (
            part.content_type == CT.WML_DOCUMENT_MAIN
            or part.content_type == CT.PML_PRESENTATION_MAIN
            or part.content_type == CT.SML_SHEET_MAIN
        ):
            return part


def _make_hashable(obj):
    """
    递归地将对象转换为可哈希的格式：
    - dict -> frozenset of (key, value)
    - list/tuple -> tuple
    - set -> frozenset
    - bytes -> base64 编码的字符串
    - datetime -> ISO 格式字符串
    - 其他对象 -> 尝试用 str() 处理
    """
    if isinstance(obj, dict):
        return frozenset((k, _make_hashable(v)) for k, v in obj.items())
    elif isinstance(obj, list | tuple):
        return tuple(_make_hashable(v) for v in obj)
    elif isinstance(obj, set):
        return frozenset(_make_hashable(v) for v in obj)
    elif isinstance(obj, bytes):
        return base64.b64encode(obj).decode()  # 转成 base64 方便哈希
    elif isinstance(obj, datetime.datetime):
        return obj.isoformat()  # 统一格式
    elif isinstance(obj, int | float | str | bool | type(None)):
        return obj  # 基本数据类型直接返回
    else:
        return str(obj)  # 兜底处理，防止崩溃


def group_hashes(hashes):
    """
    将哈希值分组，分配简短的字母标签（如 A, B, C...）。

    参数:
        hashes (list[str]): 哈希值列表。

    返回:
        list[str]: 生成的分组标签列表，与输入哈希值一一对应。
    """
    unique_hashes = sorted(set(hashes))  # 获取唯一哈希，并排序保持一致性
    hash_to_label = {
        h: ascii_uppercase[i] for i, h in enumerate(unique_hashes)
    }  # 映射哈希 -> 'A', 'B', 'C'...

    return [hash_to_label[h] for h in hashes]  # 返回原顺序的标签列表


def hash_dict(d: dict) -> str:
    """
    计算字典的哈希值，支持不可 JSON 序列化的对象。

    参数:
        d (dict): 需要计算哈希的字典。

    返回:
        str: SHA-256 哈希值。
    """
    hashable = _make_hashable(d)
    hash_str = repr(hashable)  # 转换成字符串，保证一致性
    return hashlib.sha256(hash_str.encode()).hexdigest()


class QueueProcessor:
    def __init__(self, threshold, process_interval):
        self.queue = []  # 累积数据的队列
        self.lock = threading.Lock()  # 保护队列的锁
        self.threshold = threshold  # 阈值，达到则触发处理
        self.process_interval = process_interval  # 定时器的时间间隔（秒）
        self._stop_event = threading.Event()
        # 启动定时检查线程
        self.timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self.timer_thread.start()

    def add_item(self, item):
        """多线程调用的入口函数A，将item加入队列，并检查是否需要立即处理"""
        process_now = False
        temp_queue = None

        # 加锁保护队列操作
        with self.lock:
            self.queue.append(item)
            # 当达到处理阈值时，交换队列内容，准备处理
            if len(self.queue) >= self.threshold:
                temp_queue = self.queue
                self.queue = []  # 清空原队列
                process_now = True

        # 如果触发了处理，则在锁外进行数据处理
        if process_now and temp_queue:
            self.process_items(temp_queue)

    def process_items(self, items):
        pass

    def _timer_loop(self):
        """
        后台定时器线程：定时检查队列，如果队列内有数据但未达到阈值，则进行处理
        """
        while not self._stop_event.is_set():
            time.sleep(self.process_interval)
            temp_queue = None
            with self.lock:
                if self.queue:
                    temp_queue = self.queue
                    self.queue = []  # 清空队列
            if temp_queue:
                self.process_items(temp_queue)

    def stop(self):
        """停止定时器线程"""
        self._stop_event.set()
        self.timer_thread.join()
