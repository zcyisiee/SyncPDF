from abc import ABC
from abc import abstractmethod

from babeldoc.format.office.document_il.types import ILData


class TranslatablePartsProcessor(ABC):
    # handlers is a dictionary of handlers for each element type
    handlers: dict

    @staticmethod
    @abstractmethod
    def read(il_data: ILData):
        pass

    @staticmethod
    @abstractmethod
    def write(il_data: ILData):
        pass
