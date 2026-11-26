# /// script
# dependencies = [
#   "lxml",
# ]
# ///

import argparse
from collections import defaultdict
from contextlib import closing, contextmanager
from hashlib import sha256
from itertools import groupby
import os
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory, TemporaryFile
from time import time
from typing import NamedTuple

from lxml.etree import XMLSyntaxError, iterparse, xmlfile

from message_properties import AGGRESSIVE_FIELDS, RELEVANT_FIELDS, SortedValues, field_values
from streaming_xml import BLOCK_SIZE, InvalidXML, LargeAttribute, XMLCursor, XMLProjection, copy_message

EXPECTED_XML_TAGS = {'sms', 'mms'}
PROPERTY_NAMES = frozenset(field.encode() for field in RELEVANT_FIELDS)
SURROGATE_PAIR = re.compile(rb'&#(\d+);&#(\d+);')


def parse_arguments():
    parser = argparse.ArgumentParser(description='Deduplicate text messages from XML backup.')
    parser.add_argument('-i', '--input', dest='input_file', action='append', required=True,
                        help='The input XML to deduplicate. May be provided multiple times.')
    parser.add_argument('-o', '--output', dest='output_file',
                        help='The output file to save deduplicated entries. '
                             'Defaults to the input filepath with "_deduplicated" appended to the filename.')
    parser.add_argument('-l', '--log', dest='log_file',
                        help='The log file to record details of each removed message. '
                             'Defaults to the input filepath with "_deduplication.log" appended to the filename.')
    parser.add_argument('--default-country-code', nargs='?', default='+1',
                        help='Default country code to assume if a phone number has no country code. '
                             'Treat phone numbers as identical if they include this country code or none at all. '
                             'Defaults to +1 (United States / Canada).')
    parser.add_argument('--ignore-date-milliseconds', action='store_true',
                        help='Ignore millisecond precision in dates if timestamps are slightly inconsistent. '
                             'Treat identical messages as duplicates if received in the same second.')
    parser.add_argument('--ignore-whitespace-differences', action='store_true',
                        help='Ignore whitespace differences in text messages. Treat identical messages as duplicates '
                             'if they differ only in the type of whitespace or leading/trailing spaces.')
    parser.add_argument('--aggressive', action='store_true',
                        help='Only consider timestamp and body/text/data in identifying duplicates. Treat any matching '
                             'messages as duplicates, regardless of address, messaging protocol (SMS, MMS, RCS, etc.), '
                             'or other fields.')
    parser.add_argument('--fix-utf16', action='store_true',
                        help='Preprocess input files to fix invalid UTF-16 surrogate pairs.')
    parser.add_argument('--verify-duplicates', action='store_true',
                        help='Compare full message properties instead of hashes. Uses more memory for large messages.')
    args = parser.parse_args()

    for path in args.input_file:
        if not os.path.exists(path):
            raise ValueError(f"Input file '{path}' does not exist!")
    first_input = args.input_file[0]
    args.output_file = args.output_file or '_deduplicated'.join(os.path.splitext(first_input))
    args.log_file = args.log_file or f'{os.path.splitext(first_input)[0]}_deduplication.log'
    return args


def repair_utf16(source, target):
    def replace(match):
        high, low = int(match[1]), int(match[2])
        if 0xD800 <= high <= 0xDBFF and 0xDC00 <= low <= 0xDFFF:
            return f'&#{0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)};'.encode()
        return match[0]

    pending = b''
    while chunk := source.read(BLOCK_SIZE):
        pending += chunk
        # Keep enough lookahead for numeric references that cross a read boundary.
        end = max(0, len(pending) - 64)
        written = 0
        for match in SURROGATE_PAIR.finditer(pending):
            if match.start() >= end:
                break
            target.write(pending[written:match.start()])
            target.write(replace(match))
            written = match.end()
        end = max(end, written)
        target.write(pending[written:end])
        pending = pending[end:]
    target.write(SURROGATE_PAIR.sub(replace, pending))


class Message:
    def __init__(self, tag, args):
        self.tag, self.args = tag, args
        self.has_data = False
        self.has_properties = False
        self.exact = [] if args.verify_duplicates else None
        self.aggressive = SortedValues() if args.aggressive and self.exact is None else None
        self.full, self.stripped = sha256(), sha256()
        self.log_fields = SortedValues()
        self.log_error = None

    def add_element(self, element, projection, repeat=1):
        fields = []
        for index, field in enumerate(RELEVANT_FIELDS):
            raw_value = element.get(field)
            if raw_value is None:
                continue
            relevant = not self.args.aggressive or field in AGGRESSIVE_FIELDS
            try:
                value, log_value = field_values(field, projection.value(raw_value), self.args)
            except RuntimeError as error:
                if relevant:
                    raise
                self.log_error = str(error)
                continue
            if log_value is None:
                continue
            self.log_fields.add(index, log_value)
            if relevant:
                self.has_data |= field == 'data'
                self.has_properties = True
                key_field = 'text' if self.args.aggressive and field == 'body' else field
                fields.append((key_field, value))
        # The original key included the top-level element twice.
        if self.exact is not None:
            self.exact.extend(fields * repeat)
        else:
            for field, value in (fields if self.aggressive is not None else fields * repeat):
                index = RELEVANT_FIELDS.index(field)
                if self.aggressive is not None:
                    self.aggressive.add(index, value)
                else:
                    self.hash_property(index, value)

    def hash_property(self, index, value):
        entry = bytes([index]) + value
        self.full.update(entry)
        if RELEVANT_FIELDS[index] != 'data':
            self.stripped.update(entry)

    def keys(self):
        if not self.has_properties:
            raise RuntimeError('Encountered completely empty message? ()')
        tag = 'mms or sms' if self.args.aggressive else self.tag
        if self.exact is not None:
            full = tuple(set(self.exact)) if self.args.aggressive else tuple(self.exact)
            stripped = tuple(item for item in full if item[0] != 'data') if self.has_data else full
        else:
            if self.aggressive is not None:
                for index, value in self.aggressive:
                    self.hash_property(index, value)
            full = self.full.digest()
            stripped = self.stripped.digest() if self.has_data else full
        return (tag, full), (tag, stripped)

    def write_details(self, file):
        start = file.tell()
        file.write(f'{self.tag}:\n'.encode())
        for index, entries in groupby(self.log_fields, key=lambda item: item[0]):
            _, first = next(entries)
            second = next(entries, None)
            if first or second is not None:
                file.write(f'{RELEVANT_FIELDS[index]:>8}: {first}'.encode())
                if second is not None:
                    file.write(f' | {second[1]}'.encode())
                for _, value in entries:
                    file.write(f' | {value}'.encode())
                file.write(b'\n')
        return start, file.tell(), self.log_error

    def close(self):
        self.log_fields.close()
        if self.aggressive is not None:
            self.aggressive.close()


def read_messages(source, values, path, args):
    projection = XMLProjection(source, values, PROPERTY_NAMES, project_attributes=not args.verify_duplicates)
    parser = iterparse(projection, events=('start', 'end', 'comment', 'pi'), huge_tree=True,
                       encoding='UTF-8', resolve_entities='internal')
    root = message = None
    try:
        for event, element in parser:
            if event in ('comment', 'pi'):
                parent = element.getparent()
                if root is not None and parent is root and element.text != projection.marker:
                    raise ValueError(f'Encountered unexpected XML tag {element.tag!r} directly under root in {path}.')
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]
                continue
            parent = element.getparent()
            if event == 'start':
                if root is None:
                    root = element
                    if root.tag != 'smses':
                        raise ValueError(f"Unexpected root tag {root.tag!r} in {path} (expected 'smses').")
                    attributes = {}
                    for key, raw in root.attrib.items():
                        value = projection.value(raw)
                        attributes[key] = ''.join(value.chunks()) if isinstance(value, LargeAttribute) else value
                    yield (attributes, root.nsmap, projection.root_offset)
                elif parent is root:
                    if element.tag not in EXPECTED_XML_TAGS:
                        raise ValueError(f'Encountered unexpected XML tag {element.tag!r} directly under root '
                                         f'in {path}.')
                    message = Message(element.tag, args)
                    message.add_element(element, projection, repeat=2)
                else:
                    message.add_element(element, projection)
            elif parent is not None:
                if parent is root:
                    yield message, projection.spans.popleft()
                    message.close()
                    message = None
                element.clear()
                while element.getprevious() is not None:
                    del parent[0]
    except (XMLSyntaxError, InvalidXML, UnicodeDecodeError) as error:
        raise ValueError(f"Input file '{path}' is an illegal XML file! "
                         'You may want to try rerunning with the --fix-utf16 flag.') from error
    finally:
        if message is not None:
            message.close()


class Record(NamedTuple):
    file_number: int
    start: int
    end: int
    details: tuple
    has_data: bool


def copy_range(source, target, start, end):
    source.seek(start)
    for offset in range(start, end, BLOCK_SIZE):
        target.write(source.read(min(BLOCK_SIZE, end - offset)))


def log_removal(log, removed, kept, archive, removed_archive):
    start, end, error = removed
    keep_start, keep_end, keep_error = kept
    if error or keep_error:
        raise RuntimeError(error or keep_error)
    log.write(b'Removing ')
    copy_range(removed_archive, log, start, end)
    log.write(b'\nIn favor of keeping ')
    copy_range(archive, log, keep_start, keep_end)
    log.write(b'\n')


def find_duplicates(paths, log, args):
    unique, with_data = {}, {}
    original, final = defaultdict(int), defaultdict(int)
    with TemporaryFile() as archive, TemporaryFile() as duplicate_details:
        for number, path in enumerate(paths):
            with open(path, 'rb') as source, open(path, 'rb') as values:
                with closing(read_messages(source, values, args.input_file[number], args)) as messages:
                    root = next(messages)
                    if number == 0:
                        first_root = root
                    for message, (start, end) in messages:
                        key, stripped = message.keys()
                        tag = key[0]
                        original[tag] += 1
                        if key in unique:
                            duplicate_details.seek(0)
                            details = message.write_details(duplicate_details)
                            log_removal(log, details, unique[key].details, archive, duplicate_details)
                        else:
                            archive.seek(0, os.SEEK_END)
                            record = Record(number, start, end, message.write_details(archive), message.has_data)
                            unique[key] = record
                            if message.has_data:
                                with_data[stripped] = record

        # Keep the first full match, then prefer the last distinct attachment-bearing match.
        # Keep these removals in a separate phase to preserve the log ordering.
        survivors = []
        for key, record in unique.items():
            tag = key[0]
            final[tag] += 0
            if not record.has_data and key in with_data:
                log_removal(log, record.details, with_data[key].details, archive, archive)
            else:
                survivors.append(record)
                final[tag] += 1
    return first_root, survivors, original, final


def print_summary(original, final):
    print('Deduplication Summary:')
    print('|'.join(f'{x:^20}' for x in ['Message Type', 'Original Count', 'Removed', 'Deduplicated Count']))
    for tag in original.keys() | final.keys():
        print('|'.join(f'{x:^20}' for x in [tag, original[tag], original[tag] - final[tag], final[tag]]))


def write_output(output, paths, root, survivors):
    attributes, namespaces, root_offset = root
    attributes['count'] = str(len(survivors))
    running_id = 0
    if '_id' in attributes:
        attributes['_id'] = '0'
        running_id = 1
    # Write the XML envelope with xmlfile. Copy validated message spans into it in
    # bounded chunks, since xmlfile would otherwise serialize each giant attribute at once.
    with xmlfile(output, encoding='UTF-8', buffered=False) as writer:
        writer.write_declaration(standalone=True)
        with open(paths[0], 'rb') as source:
            bom_size = 3 if source.read(3) == b'\xef\xbb\xbf' else 0
            source.seek(bom_size)
            prefix = source.read(min(BLOCK_SIZE, root_offset - bom_size))
            start = bom_size
            if prefix.startswith(b'<?xml') and prefix[5:6] in (b' ', b'\t', b'\r', b'\n'):
                start += prefix.index(b'?>') + 2
            copy_range(source, output, start, root_offset)
        with writer.element('smses', attributes, nsmap=namespaces):
            for number, records in groupby(survivors, key=lambda record: record.file_number):
                with open(paths[number], 'rb') as source:
                    cursor = XMLCursor(source)
                    for record in records:
                        output.write(b'\n  ')
                        running_id = copy_message(cursor, output, record.start, record.end, running_id)
            output.write(b'\n')
    output.write(b'\n')


@contextmanager
def staged_file(path):
    with NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(path)), delete=False) as file:
        try:
            yield file
        finally:
            file.close()
            if os.path.exists(file.name):
                os.unlink(file.name)


def install_file(file, path):
    file.close()
    os.replace(file.name, path)


def run(args, paths):
    print(f'Preparing log file {args.log_file!r}.')
    with staged_file(args.log_file) as log:
        print('Searching for duplicates... ', end='', flush=True)
        start = time()
        root, survivors, original, final = find_duplicates(paths, log, args)
        print(f'Done in {time() - start:.1f} s.')
        print_summary(original, final)
        if original == final:
            install_file(log, args.log_file)
            print('No duplicate messages found. Skipping writing of output file.')
        else:
            print(f'Writing {args.output_file!r}... ', end='', flush=True)
            start = time()
            with staged_file(args.output_file) as output:
                write_output(output, paths, root, survivors)
                install_file(log, args.log_file)
                install_file(output, args.output_file)
            print(f'Done in {time() - start:.1f} s')


def main():
    args = parse_arguments()
    with TemporaryDirectory() as scratch:
        paths = args.input_file
        if args.fix_utf16:
            paths = []
            for index, path in enumerate(args.input_file):
                repaired = os.path.join(scratch, str(index))
                with open(path, 'rb') as source, open(repaired, 'wb') as target:
                    repair_utf16(source, target)
                paths.append(repaired)
        run(args, paths)


if __name__ == '__main__':
    main()
