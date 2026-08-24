import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

xml = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<foo>&xxe;</foo>"""

try:
    ET.fromstring(xml)
except ET.ParseError as e:
    print("ParseError:", e)
except DefusedXmlException as e:
    print("DefusedXmlException:", type(e), e)
