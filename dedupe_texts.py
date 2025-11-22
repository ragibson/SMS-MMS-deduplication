# /// script
# dependencies = [
#   "lxml",
# ]
# ///

import argparse
import os
from collections import defaultdict
from time import time

from lxml.etree import XMLParser, parse, iterparse, Element, tostring, fromstring, indent

EXPECTED_XML_TAGS = {'sms', 'mms'}  # treat any direct child tags other than this as a fatal error
RELEVANT_FIELDS = ['date', 'address', 'body', 'text', 'subject', 'm_type', 'type', 'data']
AGGRESSIVE_RELEVANT_FIELDS = ['date', 'body', 'text', 'data']


def parse_arguments():
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

    args = parser.parse_args()

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


def stream_input_xmls(filepaths):
    """
    Stream XML elements from one or more input files using iterparse.
    
    Yields (element, filepath) tuples for each message element.
    This allows processing all input files without loading them entirely into memory.
    """
    if not filepaths:
        raise ValueError("No input files provided.")

    for fp in filepaths:
        with open(fp, 'rb') as file:
            context = iterparse(file, events=('start', 'end'), tag=('smses', 'sms', 'mms'))
            
            root_seen = False
            for event, elem in context:
                if event == 'start' and elem.tag == 'smses':
                    if root_seen:
                        raise ValueError(f"Multiple root elements in {fp}?")
                    root_seen = True
                elif event == 'end' and elem.tag in EXPECTED_XML_TAGS:
                    yield elem, fp
                    # Clear the element to free memory after processing
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
            
            if not root_seen:
                raise ValueError(f"No root 'smses' element found in {fp}.")


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


def removal_summary(element_xml, element_to_keep_xml, args, field_length_limit=1000):
    """
    Returns a string of the removed message details for logging purposes.

    Alongside the duplicate (removed) message, it logs the message that was kept in its place.
    
    element_xml and element_to_keep_xml are XML strings representing the elements.
    """
    # Parse the XML strings back to elements for processing
    element_to_remove = parse_xml_string(element_xml)
    element_to_keep = parse_xml_string(element_to_keep_xml)
    
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


def parse_xml_string(xml_string):
    """Parse an XML string back to an element."""
    return fromstring(xml_string)


def deduplicate_messages_streaming(input_fps, output_fp, log_file, args):
    """
    Stream messages from input files, deduplicate them, and write unique ones to output.
    
    Uses a two-pass approach:
    1. First pass: Identify all duplicates (exact matches and data-stripped duplicates)
    2. Second pass: Write unique messages to output file
    
    This keeps only deduplication dicts in memory, not full XML elements.
    
    Returns:
        1) total_message_count_by_tag dict
        2) unique_message_count_by_tag dict
    """
    message_count_by_tag = defaultdict(int)
    unique_messages_by_tag = defaultdict(set)
    data_stripped_by_tag = defaultdict(set)
    data_stripped_to_original = {}
    deduplication_fields_to_xml = {}  # Store minimal XML string instead of element
    messages_to_skip = set()  # Track message indices to skip (0-indexed)
    
    def retrieve_message_properties_and_tag(child, args):
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
    
    # First pass: Identify all duplicates
    message_index = 0
    for elem, fp in stream_input_xmls(input_fps):
        child_tag, child_attributes = retrieve_message_properties_and_tag(elem, args)
        
        # Serialize element to string for later logging if needed
        elem_xml = tostring(elem, encoding='unicode')
        
        is_duplicate = False
        
        # Check for exact duplicate
        if child_attributes in unique_messages_by_tag[child_tag]:
            messages_to_skip.add(message_index)
            log_file.write(removal_summary(elem_xml, deduplication_fields_to_xml[child_attributes], args))
            is_duplicate = True
        # Check for data-stripped duplicate (message without data matching one with data)
        elif not message_has_data(child_attributes) and child_attributes in data_stripped_by_tag[child_tag]:
            messages_to_skip.add(message_index)
            original_attrs = data_stripped_to_original[child_attributes]
            log_file.write(removal_summary(elem_xml, deduplication_fields_to_xml[original_attrs], args))
            is_duplicate = True
        
        # If not a duplicate, track it
        if not is_duplicate:
            unique_messages_by_tag[child_tag].add(child_attributes)
            deduplication_fields_to_xml[child_attributes] = elem_xml
            
            # Track data-stripped version for future comparison
            if message_has_data(child_attributes):
                data_stripped_attributes = strip_data_from_message(child_attributes)
                data_stripped_by_tag[child_tag].add(data_stripped_attributes)
                data_stripped_to_original[data_stripped_attributes] = child_attributes

        message_count_by_tag[child_tag] += 1
        message_index += 1
    
    # Second pass: Write unique messages to output
    message_index = 0
    running_id = 0
    
    with open(output_fp, 'wb') as out_file:
        # Write XML declaration and opening root tag (with placeholder count)
        out_file.write(b'<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\' ?>\n')
        out_file.write(b'<smses count="PLACEHOLDER_COUNT" type="full">\n')
        
        for elem, fp in stream_input_xmls(input_fps):
            if message_index not in messages_to_skip:
                # Update _id if present
                if "_id" in elem.attrib:
                    elem.attrib["_id"] = str(running_id)
                
                # Update _id in nested elements (for MMS)
                for nested_elem in elem.iter():
                    if "_id" in nested_elem.attrib and nested_elem.tag != elem.tag:
                        nested_elem.attrib["_id"] = str(running_id)
                        running_id += 1
                
                # Write element to output
                # Use indent to set proper indentation, then serialize without pretty_print to preserve it
                indent(elem, space='    ')
                elem_str = tostring(elem, encoding='UTF-8', pretty_print=False)
                # Add 4-space indent to all lines
                lines = elem_str.split(b'\n')
                for line in lines:
                    if line.strip():
                        out_file.write(b'    ' + line + b'\n')
                
                running_id += 1
            
            message_index += 1
        
        out_file.write(b'</smses>\n')
    
    # Update count in output file
    unique_count = sum(len(v) for v in unique_messages_by_tag.values())
    with open(output_fp, 'r+b') as out_file:
        content = out_file.read()
        content = content.replace(b'count="PLACEHOLDER_COUNT"', 
                                  f'count="{unique_count}"'.encode('UTF-8'))
        out_file.seek(0)
        out_file.write(content)
        out_file.truncate()
    
    return message_count_by_tag, {k: len(v) for k, v in unique_messages_by_tag.items()}


def print_summary(input_message_counts, output_message_counts):
    """Prints summary of deduplicated message counts to stdout."""
    if input_message_counts.keys() != output_message_counts.keys():
        raise RuntimeError("Message type (MMS/SMS) was completely lost in deduplication? This should never occur!")

    print("Deduplication Summary:")
    print("|".join(f"{x:^20}" for x in ["Message Type", "Original Count", "Removed", "Deduplicated Count"]))
    for message_tag in input_message_counts.keys() | output_message_counts:
        original_count, final_count = input_message_counts[message_tag], output_message_counts[message_tag]
        print("|".join(f"{x:^20}" for x in [message_tag, original_count, original_count - final_count, final_count]))



if __name__ == "__main__":
    # read in I/O filepaths from command line arguments
    args = parse_arguments()
    input_fps, output_fp, log_fp = args.input_file, args.output_file, args.log_file

    # search for duplicate messages and write unique ones to output file
    print(f"Reading {', '.join(repr(fp) for fp in input_fps)}...")
    print(f"Preparing log file {repr(log_fp)}.")
    with open(log_fp, "w", encoding="utf-8") as log_file:
        print("Searching for duplicates and writing output... ", end='', flush=True)
        st = time()
        input_message_counts, output_message_counts = deduplicate_messages_streaming(
            input_fps, output_fp, log_file, args
        )
    print(f"Done in {time() - st:.1f} s.")

    # print summary of original and final message counts
    print_summary(input_message_counts, output_message_counts)

    # clean up output file if no duplicates were found
    if input_message_counts == output_message_counts:
        print("No duplicate messages found. Removing output file.")
        if os.path.exists(output_fp):
            os.unlink(output_fp)
