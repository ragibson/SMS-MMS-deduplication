"""Bounded XML projection for SMS Backup & Restore's attribute-based format."""

from codecs import utf_8_decode
from collections import deque
from itertools import chain
import re
from uuid import uuid4

from lxml.etree import XMLParser, fromstring


BLOCK_SIZE = 64 * 1024
SPACE = re.compile(rb'[^ \t\r\n]')
NAME_END = re.compile(rb'[ \t\r\n=/>]')
DECLARATION_DELIMITER = re.compile(rb'''[\[\]>"']''')


class InvalidXML(ValueError):
    pass


class XMLCursor:
    """Scan XML delimiters without buffering the strings between them."""

    def __init__(self, source):
        self.source = source
        self.offset = source.tell()
        self.buffer = b''
        self.index = 0

    def seek(self, offset):
        index = self.index + offset - self.offset
        if 0 <= index <= len(self.buffer):
            self.index = index
        else:
            self.source.seek(offset)
            self.buffer, self.index = b'', 0
        self.offset = offset

    def peek(self, size=1):
        if len(self.buffer) - self.index < size:
            self.buffer = self.buffer[self.index:] + self.source.read(BLOCK_SIZE)
            self.index = 0
        return self.buffer[self.index:self.index + size]

    def take(self, size):
        chunk = self.buffer[self.index:self.index + size]
        self.index += len(chunk)
        self.offset += len(chunk)
        return chunk

    def until(self, delimiter):
        overlap = len(delimiter) - 1
        while self.peek(len(delimiter)):
            end = self.buffer.find(delimiter, self.index)
            if end >= 0:
                if end > self.index:
                    yield self.take(end - self.index)
                return
            available = len(self.buffer) - self.index
            yield self.take(max(1, available - overlap))

    def match_until(self, pattern):
        match = pattern.search(self.buffer, self.index) if self.peek() else None
        return self.take((match.start() if match else len(self.buffer)) - self.index)

    def markup(self, delimiter):
        yield from self.until(delimiter)
        yield self.take(len(delimiter))

    def tokens(self):
        """Yield markup and offsets, and iterators over attribute value chunks."""
        while self.peek():
            yield 'text', self.until(b'<')
            start = self.offset
            prefix = self.peek(9)
            if not prefix:
                return
            delimiters = ((b'<!--', b'-->'), (b'<![CDATA[', b']]>'), (b'<?', b'?>'), (b'</', b'>'))
            for opening, closing in delimiters:
                if prefix.startswith(opening):
                    self.take(len(opening))
                    yield 'section', (opening, closing, self.until(closing))
                    yield 'markup', self.take(len(closing))
                    if opening == b'</':
                        yield 'end', self.offset
                    break
            else:
                if prefix.startswith(b'<!'):
                    # The internal DTD subset can contain quoted strings and declarations of its own.
                    yield 'markup', self.take(2)
                    yield from self.declaration()
                else:
                    yield 'start', start
                    yield 'markup', self.take(1)
                    yield from self.start_tag()

    def declaration(self):
        brackets = 0
        while self.peek():
            match = DECLARATION_DELIMITER.search(self.buffer, self.index)
            if not match:
                yield 'markup', self.take(len(self.buffer))
                continue
            yield 'markup', self.take(match.end() - self.index)
            token = match.group()
            if token in (b'"', b"'"):
                for chunk in self.markup(token):
                    yield 'markup', chunk
            elif token == b'[':
                brackets += 1
            elif token == b']':
                brackets -= 1
            elif not brackets:
                return

    def start_tag(self):
        # Names and whitespace can straddle read boundaries.
        def matching(pattern):
            while chunk := self.match_until(pattern):
                yield chunk

        for chunk in matching(NAME_END):
            yield 'markup', chunk
        while self.peek():
            for chunk in matching(SPACE):
                yield 'markup', chunk
            if self.peek(2).startswith(b'/>'):
                yield 'markup', self.take(2)
                yield 'empty', self.offset
                return
            if self.peek() == b'>':
                yield 'markup', self.take(1)
                return
            name = b''.join(matching(NAME_END))
            yield 'markup', name
            for chunk in matching(SPACE):
                yield 'markup', chunk
            if not name or self.peek() != b'=':
                raise InvalidXML('Invalid XML start tag.')
            yield 'markup', self.take(1)
            for chunk in matching(SPACE):
                yield 'markup', chunk
            quote = self.take(1)
            if quote not in (b'"', b"'"):
                raise InvalidXML('Invalid XML attribute quoting.')
            yield 'markup', quote
            yield 'attribute', (name, quote, self.offset, self.until(quote))
            yield 'markup', self.take(1)


class XMLProjection:
    """Give iterparse the XML structure and references to oversized attributes."""

    def __init__(self, source, values, property_names, project_attributes=True):
        self.values = values
        self.property_names = property_names
        self.project_attributes = project_attributes
        self.marker = uuid4().hex + ':'
        self.separator = b'<!--' + self.marker.encode() + b'-->'
        self.spans = deque()
        self.root_offset = None
        self.chunks = self.project(XMLCursor(source).tokens())
        self.pending = b''

    def project(self, tokens):
        depth = 0
        for kind, token in tokens:
            if kind == 'start':
                if depth == 0 and self.root_offset is None:
                    self.root_offset = token
                if depth == 1:
                    start = token
                depth += 1
            elif kind in ('end', 'empty'):
                depth -= 1
                if depth == 1:
                    self.spans.append((start, token))
            elif kind == 'attribute':
                name, quote, offset, chunks = token
                if not self.project_attributes:
                    yield from chunks
                    continue
                first = next(chunks, b'')
                second = next(chunks, b'')
                # Small attributes that straddle a block boundary still go straight to lxml.
                if len(first) + len(second) < BLOCK_SIZE:
                    yield first + second
                else:
                    length = sum(map(len, chain((first, second), chunks)))
                    if depth > 1 and name not in self.property_names:
                        # Properties are validated when read. Validate other large values here.
                        for _ in LargeAttribute(self.values, offset, length, quote).chunks():
                            pass
                    yield f'{self.marker}{offset}:{length}:{quote.hex()}'.encode()
            elif kind == 'text':
                if depth and self.project_attributes:
                    yield from separated(safe_chunks(token, text=True), self.separator)
                else:
                    yield from token
            elif kind == 'section':
                opening, closing, chunks = token
                yield opening
                if depth and self.project_attributes and opening != b'</':
                    separator = closing + opening
                    if opening == b'<![CDATA[':
                        separator = closing + self.separator + opening
                    elif opening == b'<?':
                        separator = b'?><?continued '
                    yield from separated(safe_chunks(chunks, comment=opening == b'<!--'), separator)
                else:
                    yield from chunks
            else:
                yield token

    def read(self, size):
        pieces = [self.pending]
        length = len(self.pending)
        # Read beyond the last byte so its span is available even at a read boundary.
        while length <= size:
            piece = next(self.chunks, None)
            if piece is None:
                break
            pieces.append(piece)
            length += len(piece)
        result = b''.join(pieces)
        result, self.pending = result[:size], result[size:]
        return result

    def value(self, text):
        if text.startswith(self.marker):
            offset, size, quote = text.split(':')[1:]
            return LargeAttribute(self.values, int(offset), int(size), bytes.fromhex(quote))
        return text


class LargeAttribute:
    """Re-read an attribute in bounded, independently XML-decodable pieces."""

    def __init__(self, source, offset, size, quote):
        self.source, self.offset, self.size, self.quote = source, offset, size, quote

    def raw_chunks(self):
        for position in range(self.offset, self.offset + self.size, BLOCK_SIZE):
            self.source.seek(position)
            yield self.source.read(min(BLOCK_SIZE, self.offset + self.size - position))

    def chunks(self):
        parser = XMLParser(encoding='UTF-8')
        for block in safe_chunks(self.raw_chunks(), attribute=True):
            yield fromstring(b'<v a=' + self.quote + block + self.quote + b'/>', parser).get('a')


def separated(chunks, separator):
    """Break long character data into separate nodes so iterparse can release it."""
    for index, chunk in enumerate(chunks):
        if index:
            yield separator
        yield chunk


def safe_chunks(chunks, *, attribute=False, text=False, comment=False):
    """Keep character references, UTF-8 characters, and XML delimiters intact at seams."""
    chunks = iter(chunks)
    block = next(chunks, b'')
    for following in chunks:
        end = utf_8_decode(block, 'strict', False)[1]
        if block[end - 1:end] == b'\r':
            end -= 1
        if attribute or text:
            amp = block.rfind(b'&', 0, end)
            if amp > block.rfind(b';', 0, end):
                end = amp
        for token in ((b']', b']') if text else (b'-',) if comment else ()):
            if block[end - 1:end] == token:
                end -= 1
        if end:
            yield block[:end]
        elif len(block) >= BLOCK_SIZE:
            raise InvalidXML('Oversized or unterminated XML character reference.')
        block = block[end:] + following
    if block:
        yield block


def copy_message(cursor, output, start, end, running_id):
    """Copy a message verbatim except for the sequential IDs required on restore."""
    cursor.seek(start)
    for kind, token in cursor.tokens():
        if kind == 'attribute':
            name, _, _, chunks = token
            if name == b'_id':
                for _ in chunks:
                    pass
                output.write(str(running_id).encode())
                running_id += 1
            else:
                for chunk in chunks:
                    output.write(chunk)
        elif kind == 'text':
            for chunk in token:
                output.write(chunk)
        elif kind == 'section':
            opening, _, chunks = token
            output.write(opening)
            for chunk in chunks:
                output.write(chunk)
        elif kind == 'markup':
            output.write(token)
        elif kind in ('end', 'empty') and token == end:
            return running_id
    raise InvalidXML('Input changed before the deduplicated XML could be written.')
