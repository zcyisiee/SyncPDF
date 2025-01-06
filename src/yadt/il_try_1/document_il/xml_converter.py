from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.serializers import XmlSerializer
from xsdata.formats.dataclass.serializers.config import SerializerConfig

from yadt.il_try_1.document_il import il_try_1


class XMLConverter:
    def __init__(self):
        self.parser = XmlParser()
        config = SerializerConfig(indent="  ")
        context = XmlContext()
        self.serializer = XmlSerializer(context=context, config=config)

    def to_xml(self, document: il_try_1.Document) -> str:
        return self.serializer.render(document)

    def from_xml(self, xml: str) -> il_try_1.Document:
        return self.parser.from_string(
            xml,
            il_try_1.Document,
        )

    def deepcopy(self, document: il_try_1.Document) -> il_try_1.Document:
        return self.from_xml(self.to_xml(document))
