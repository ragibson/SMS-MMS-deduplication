# /// script
# dependencies = [
#   "lxml",
# ]
# ///

import argparse
import hashlib
import os
import struct
import tempfile
from collections import defaultdict
from time import time

from lxml.etree import XMLParser, iterparse, xmlfile, fromstring, tostring

EXPECTED_XML_TAGS = {'sms', 'mms'}  # treat any direct child tags other than this as a fatal error
RELEVANT_FIELDS = ['date', 'address', 'body', 'text', 'subject', 'm_type', 'type', 'data']
AGGRESSIVE_RELEVANT_FIELDS = ['date', 'body', 'text', 'data']

_huge_parser = XMLParser(huge_tree=True, encoding='UTF-8')


def parse_arguments(argv=None):
    """
    Reads CLI arguments and return set of arguments.

    By default,
      * the output filepath is the input filepath with "_deduplicated" appended to the filename
      * the log filepath is the input filepath with "_deduplication.log" appended to the filename
    """
    parser = argparse.ArgumentParser(description='Deduplicate text messages from XML backup.')
    parser.add_argument('-i', '--input', dest='input_file', action='append', required=True,
                        help='The input XML to deduplicate. May be provided multiple times.')
    parser.add_argument('-o', '--output', dest='output_file',
                        help='The output file to save deduplicated entries. '
                             'Defaults to the input filepath with "_deduplicated" appended to the filename.')
    parser.add_argument('-l', '--log', dest='log_file',
                        help='The log file to record details of each removed message. '
                             'Defaults to the input filepath with "_deduplication.log" appended to the filename.')
    parser.add_argument('--default-country-code', nargs='?', default="+1",
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
    parser.add_argument('--verify-duplicates', action='store_true',
                        help='Verify duplicates by comparing full message properties rather than hash-based summaries. '
                             'This handles potential hash collisions at the expense of higher memory usage.')

    args = parser.parse_args(argv)

    # make sure the user's input file(s) actually exists
    for fp in args.input_file:
        if not os.path.exists(fp):
            raise ValueError(f"Input file '{fp}' does not exist!")

    first_input_file = args.input_file[0]
    if not args.output_file:
        args.output_file = "_deduplicated".join(os.path.splitext(first_input_file))

    if not args.log_file:
        args.log_file = f"{os.path.splitext(first_input_file)[0]}_deduplication.log"

    return args


def retrieve_message_properties(child, args, disable_ignores=False):
    """
    Returns message properties to use for uniqueness check.

    Note that this cannot be a shallow analysis, especially for MMS.
    """

    def contains_smil(s):
        """Strip out Synchronized Multimedia Integration Language data due to apparent differences in backup agents."""
        s = s.strip()
        if "<smil" in s and "</smil>" in s:
            # we've checked for the (probable) existence of the <smil...>...</smil> tag, so we try:
            if s.startswith("<?xml") and "?>" in s:
                s = s[s.index("?>") + len("?>"):].strip()  # strip leading XML declaration (and newline) if it exists
            if s.startswith("<!DOCTYPE") and ">" in s:
                s = s[s.index(">") + len(">"):].strip()  # strip leading DOCTYPE (and newline) if it exists
            for smil_element in ("par", "seq", "excl"):
                # strip out rare parallel timing elements as in https://www.w3.org/TR/SMIL3/smil-timing.html
                # these could probably come in any order, but we'll see if anyone reports issues
                if s.startswith(f"<{smil_element}") and f"/{smil_element}>" in s:
                    s = s[s.index(f"/{smil_element}>") + len(f"/{smil_element}>"):].strip()

        contains_smil = s.startswith("<smil") and s.endswith("</smil>")
        if "<" in s and ">" in s and "smil" in s and "/smil" in s:
            if not contains_smil:
                raise RuntimeError(f"This SMIL format is unique / previously unknown and "
                                   f"not captured by the existing check? Please report: {repr(s)}")
        return contains_smil

    def standardize_address(field_name, field_data):
        """Standardize the ordering of the address field."""
        if field_name == 'address':
            # some backup agents conflict on whether they assume a default country code or explicitly include it
            field_data = '~'.join(f'{args.default_country_code}{address}' if not address.startswith('+') else address
                                  for address in field_data.split('~'))
            # for some reason, this field has each number/email/etc. delimited
            # by '~', but the ordering differs by backup agent
            field_data = '~'.join(sorted(field_data.split('~')))
        return field_data

    def truncate_timestamp_precision(field_name, field_data):
        """
        If enabled, truncate timestamp precision to seconds.

        This is only for internal duplicate checking and does not affect the XML export.
        """
        if field_name == 'date' and args.ignore_date_milliseconds:
            # for some reason, some backup agents drop the millisecond precision here
            field_data = field_data[:-3] + "000"
        return field_data

    def normalize_whitespace(field_name, field_data):
        """
        If enabled, replace all whitespace in a text with a single space.

        This is only for internal duplicate checking and does not affect the XML export.
        """
        if field_name in ('text', 'body', 'subject') and args.ignore_whitespace_differences:
            # in rare cases, backup agents may tweak whitespace within text messages
            field_data = " ".join(field_data.strip().split())
        return field_data

    def normalize_field(field_name, field_data):
        """Perform our internal normalizations in sequence and return (field_name, normalized field_data)."""
        field_data = standardize_address(field_name, field_data)
        if not disable_ignores:
            field_data = truncate_timestamp_precision(field_name, field_data)
            field_data = normalize_whitespace(field_name, field_data)
        return field_name, field_data

    def compile_relevant_fields(element):
        relevant_deduplication_fields = RELEVANT_FIELDS
        if args.aggressive and not disable_ignores:
            relevant_deduplication_fields = AGGRESSIVE_RELEVANT_FIELDS

        return tuple(
            normalize_field(field, element.attrib[field])
            for field in relevant_deduplication_fields
            # for some reason, backup agents may either omit fields or fill with null
            if field in element.attrib and element.attrib[field] != 'null'
            and not contains_smil(element.attrib[field])
        )

    result = tuple(item for element in [child] + list(child.iter()) for item in compile_relevant_fields(element))
    if not result:
        raise RuntimeError(f"Encountered completely empty message? {result}")
    return result


def message_has_data(message_attributes):
    return any(field_name == 'data' for field_name, field_value in message_attributes)


def strip_data_from_message(message_attributes):
    return tuple(filter(lambda x: x[0] != 'data', message_attributes))


def removal_summary(element_to_remove, element_to_keep, args, field_length_limit=1000):
    """
    Returns a string of the removed message details for logging purposes.

    Alongside the duplicate (removed) message, it logs the message that was kept in its place.
    """
    tag_remove, tag_keep = element_to_remove.tag, element_to_keep.tag
    element_to_remove = retrieve_message_properties(element_to_remove, args, disable_ignores=True)
    element_to_keep = retrieve_message_properties(element_to_keep, args, disable_ignores=True)

    def collect_unique_field_data(element_attributes, field):
        return " | ".join(sorted({field_data if len(field_data) < field_length_limit
                                  else f"<LENGTH {len(field_data)} OMISSION>"
                                  for field_name, field_data in element_attributes if field == field_name}))

    removal_log = []
    for intro_str, element in [(f"Removing {tag_remove}:", element_to_remove),
                               (f"\nIn favor of keeping {tag_keep}:", element_to_keep)]:
        removal_log.append(intro_str)
        for field in RELEVANT_FIELDS:
            combined_field_data = collect_unique_field_data(element, field)
            if combined_field_data:
                removal_log.append(f"{field:>8}: {combined_field_data}")

    return "\n".join(removal_log) + "\n\n"


def print_summary(input_message_counts, output_message_counts):
    """Prints summary of deduplicated message counts to stdout."""
    if input_message_counts.keys() != output_message_counts.keys():
        raise RuntimeError("Message type (MMS/SMS) was completely lost in deduplication? This should never occur!")

    print("Deduplication Summary:")
    print("|".join(f"{x:^20}" for x in ["Message Type", "Original Count", "Removed", "Deduplicated Count"]))
    for message_tag in input_message_counts.keys() | output_message_counts:
        original_count, final_count = input_message_counts[message_tag], output_message_counts[message_tag]
        print("|".join(f"{x:^20}" for x in [message_tag, original_count, original_count - final_count, final_count]))


def _get_key(child_attributes, verify):
    """Return a hash string or the raw properties tuple depending on verification mode."""
    if verify:
        return child_attributes
    return hashlib.sha256(str(child_attributes).encode('utf-8')).hexdigest()


def _write_temp_record(temp_file, xml_bytes):
    """Write a length-prefixed record to a temp file and return its offset."""
    offset = temp_file.tell()
    temp_file.write(struct.pack('>Q', len(xml_bytes)))
    temp_file.write(xml_bytes)
    temp_file.flush()
    return offset


def _read_temp_record(temp_file, offset):
    """Read a length-prefixed record from a temp file."""
    temp_file.seek(offset)
    length = struct.unpack('>Q', temp_file.read(8))[0]
    return temp_file.read(length)


def _get_root_attributes(filepath):
    """Read the root <smses> attributes from the first input file."""
    for _event, elem in iterparse(filepath, events=('start',), tag='smses', huge_tree=True):
        attrs = dict(elem.attrib)
        elem.clear()
        return attrs
    raise ValueError(f"Unexpected root tag or missing <smses> root in {filepath}")


def _retrieve_message_properties_and_tag(child, args):
    """Reproduce the original duplicate-detection key and tag logic."""
    child_tag, child_attributes = child.tag, retrieve_message_properties(child, args)

    if child_tag not in EXPECTED_XML_TAGS:
        raise ValueError(f"Encountered unexpected XML tag {repr(child_tag)} directly under root. "
                         f"Is the input file malformed?")

    if args.aggressive:
        assert len(AGGRESSIVE_RELEVANT_FIELDS) == 4  # sanity check this is date plus text/body/data
        child_tag = ' or '.join(EXPECTED_XML_TAGS)  # ignore message type

        # treat text/body identically
        child_attributes = tuple({('text' if x[0] in ('text', 'body') else x[0],) + x[1:]
                                  for x in child_attributes})

    return child_tag, child_attributes


def rewrite_element_ids(element, start_id):
    """Rewrite _id attributes on an element and its descendants, returning the next available id."""
    running_id = start_id
    for it in element.iter():
        if "_id" in it.attrib:
            it.attrib["_id"] = str(running_id)
            running_id += 1
    return running_id


def deduplicate_streaming(input_filepaths, log_filepath, args):
    """
    Two-pass streaming deduplication.

    Returns (input_message_counts, output_message_counts, output_body_temp_path).
    The caller is responsible for deleting output_body_temp_path.
    """
    # ------------------------------------------------------------------
    # Pass 1: identify unique keys and build on-disk keeper index
    # ------------------------------------------------------------------
    unique_keys = set()
    data_stripped_keys = set()
    data_stripped_to_keeper_key = {}
    keeper_map = {}  # key -> offset in keeper_temp
    message_count_by_tag = defaultdict(int)

    keeper_temp = tempfile.NamedTemporaryFile(delete=False)
    data_bearer_temp = tempfile.NamedTemporaryFile(delete=False)
    try:
        for fp in input_filepaths:
            for _event, elem in iterparse(fp, huge_tree=True, tag=EXPECTED_XML_TAGS):
                child_tag, child_attributes = _retrieve_message_properties_and_tag(elem, args)
                key = _get_key(child_attributes, args.verify_duplicates)
                message_count_by_tag[child_tag] += 1

                if key not in unique_keys:
                    unique_keys.add(key)
                    xml_bytes = tostring(elem, encoding='UTF-8')
                    keeper_map[key] = _write_temp_record(keeper_temp, xml_bytes)

                    if message_has_data(child_attributes):
                        ds_attrs = strip_data_from_message(child_attributes)
                        ds_key = _get_key(ds_attrs, args.verify_duplicates)
                        data_stripped_keys.add(ds_key)
                        data_stripped_to_keeper_key[ds_key] = key
                        _write_temp_record(data_bearer_temp, xml_bytes)

                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
    finally:
        keeper_temp.close()
        data_bearer_temp.close()

    # ------------------------------------------------------------------
    # Pass 2: write log, collect output body, produce counts
    # ------------------------------------------------------------------
    output_body_temp = tempfile.NamedTemporaryFile(delete=False)
    try:
        already_output_keys = set()
        final_count_by_tag = defaultdict(int)
        running_id = 0

        with open(log_filepath, 'w', encoding='utf-8') as log_file, \
             open(keeper_temp.name, 'rb') as keeper_f, \
             open(data_bearer_temp.name, 'rb'):

            for fp in input_filepaths:
                for _event, elem in iterparse(fp, huge_tree=True, tag=EXPECTED_XML_TAGS):
                    child_tag, child_attributes = _retrieve_message_properties_and_tag(elem, args)
                    key = _get_key(child_attributes, args.verify_duplicates)

                    is_duplicate = False
                    keeper_ref = None

                    if key in already_output_keys:
                        is_duplicate = True
                        keeper_ref = keeper_map[key]
                    elif key in unique_keys:
                        if not message_has_data(child_attributes):
                            ds_attrs = strip_data_from_message(child_attributes)
                            ds_key = _get_key(ds_attrs, args.verify_duplicates)
                            if ds_key in data_stripped_keys:
                                is_duplicate = True
                                keeper_key = data_stripped_to_keeper_key[ds_key]
                                keeper_ref = keeper_map[keeper_key]
                    else:
                        # key not in unique_keys -> perfect duplicate seen in pass 1
                        is_duplicate = True
                        keeper_ref = keeper_map[key]

                    if is_duplicate:
                        keeper_bytes = _read_temp_record(keeper_f, keeper_ref)
                        keeper_elem = fromstring(keeper_bytes, parser=_huge_parser)
                        log_file.write(removal_summary(elem, keeper_elem, args))
                    else:
                        running_id = rewrite_element_ids(elem, running_id)
                        xml_bytes = tostring(elem, encoding='UTF-8')
                        _write_temp_record(output_body_temp, xml_bytes)
                        final_count_by_tag[child_tag] += 1
                        already_output_keys.add(key)

                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]

        output_body_temp.close()
        return message_count_by_tag, final_count_by_tag, output_body_temp.name
    except Exception:
        output_body_temp.close()
        os.unlink(output_body_temp.name)
        raise
    finally:
        os.unlink(keeper_temp.name)
        os.unlink(data_bearer_temp.name)


def main(argv=None):
    # read in I/O filepaths from command line arguments
    args = parse_arguments(argv)
    input_fps, output_fp, log_fp = args.input_file, args.output_file, args.log_file

    # read and optionally combine input XML file(s)
    print(f"Reading {', '.join(repr(fp) for fp in input_fps)}... ", end='', flush=True)
    st = time()
    print(f"Done in {time() - st:.1f} s.")

    # search for duplicate messages and remove them from the XML tree
    print(f"Preparing log file {repr(log_fp)}.")
    with open(log_fp, "w", encoding="utf-8") as _log_placeholder:
        pass  # ensure log file exists even if deduplication raises early

    print("Searching for duplicates... ", end='', flush=True)
    st = time()
    input_message_counts, output_message_counts, output_body_temp = deduplicate_streaming(
        input_fps, log_fp, args
    )
    print(f"Done in {time() - st:.1f} s.")

    # print summary of original and final message counts
    print_summary(input_message_counts, output_message_counts)

    # write the trimmed XML tree to the output file (if any duplicates were removed)
    if input_message_counts == output_message_counts:
        print("No duplicate messages found. Skipping writing of output file.")
        os.unlink(output_body_temp)
    else:
        print(f"Writing {repr(output_fp)}... ", end='', flush=True)
        st = time()

        total_messages = sum(output_message_counts.values())
        root_attrs = _get_root_attributes(input_fps[0])
        root_attrs['count'] = str(total_messages)

        with xmlfile(output_fp, encoding='UTF-8', close=True) as xf:
            xf.write_declaration(standalone=True)
            with xf.element('smses', **root_attrs):
                with open(output_body_temp, 'rb') as f:
                    while True:
                        size_bytes = f.read(8)
                        if len(size_bytes) < 8:
                            break
                        length = struct.unpack('>Q', size_bytes)[0]
                        xml_bytes = f.read(length)
                        elem = fromstring(xml_bytes, parser=_huge_parser)
                        xf.write('\n    ')
                        xf.write(elem)
                xf.write('\n')

        os.unlink(output_body_temp)
        print(f"Done in {time() - st:.1f} s")


if __name__ == "__main__":
    main()
