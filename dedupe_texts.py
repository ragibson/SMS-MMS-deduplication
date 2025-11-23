# /// script
# dependencies = [
#   "lxml",
# ]
# ///

import argparse
import os
from collections import defaultdict
from time import time

from lxml.etree import XMLParser, parse, iterparse, tostring, indent

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


def read_input_xml(filepath):
    p = XMLParser(huge_tree=True, encoding='UTF-8')  # huge_tree is required for larger backups
    with open(filepath, 'rb') as file:
        # NOTE: lxml (and other Python XML parsing libraries) have issues with large XML files or element, usually
        # crashing with cryptic or misleading error messages. It's suspected that this is due to poor memory management.
        #
        # For unknown reasons, the issue is worse on Windows systems, which fail on smaller files.
        #
        # Regardless, opening the file here and passing it to lxml relieves memory requirements and helps avoid crashes.
        tree = parse(file, parser=p)
    return tree


def combine_input_xmls(filepaths):
    """
    Read one or more XML backups and combine their messages under a single root tree.

    The first file acts as the base tree; all subsequent files' children (<sms> / <mms>)
    are appended to the base tree root. The base count is not updated here; the final
    count is rewritten after deduplication.
    """
    if not filepaths:
        raise ValueError("No input files provided to combine.")

    base_tree = read_input_xml(filepaths[0])
    base_root = base_tree.getroot()

    if base_root.tag != 'smses':
        raise ValueError(f"Unexpected root tag {repr(base_root.tag)} in {filepaths[0]} (expected 'smses').")

    for fp in filepaths[1:]:
        other_tree = read_input_xml(fp)
        other_root = other_tree.getroot()
        if other_root.tag != 'smses':
            raise ValueError(f"Unexpected root tag {repr(other_root.tag)} in {fp} (expected 'smses').")

        # Append only expected message elements
        for child in other_root.iterchildren():
            if child.tag in EXPECTED_XML_TAGS:
                base_root.append(child)
            else:
                # Ignore any non-message children silently? Prefer strictness consistent with later checks
                raise ValueError(f"Encountered unexpected XML tag {repr(child.tag)} directly under root in {fp}.")

    return base_tree


def stream_input_xmls(filepaths):
    """
    Stream XML elements from one or more input files using iterparse.
    
    Yields (element, filepath, message_index) tuples for each message element.
    Uses huge_tree=True parser for large file support.
    """
    if not filepaths:
        raise ValueError("No input files provided.")

    p = XMLParser(huge_tree=True, encoding='UTF-8')
    message_index = 0
    
    for fp in filepaths:
        with open(fp, 'rb') as file:
            # Note: iterparse doesn't accept parser argument directly in all lxml versions
            # We rely on default parser which should handle huge trees
            context = iterparse(file, events=('start', 'end'), tag=('smses', 'sms', 'mms'), huge_tree=True)
            
            root_seen = False
            for event, elem in context:
                if event == 'start' and elem.tag == 'smses':
                    if root_seen:
                        raise ValueError(f"Multiple root elements in {fp}?")
                    root_seen = True
                elif event == 'end' and elem.tag in EXPECTED_XML_TAGS:
                    yield elem, fp, message_index
                    message_index += 1
                    # Clear element after yielding to free memory
                    elem.clear()
                    # Also clear previous siblings to free more memory
                    while elem.getprevious() is not None:
                        try:
                            del elem.getparent()[0]
                        except:
                            break
            
            if not root_seen:
                raise ValueError(f"No root 'smses' element found in {fp}.")


def get_root_info_streaming(filepath):
    """
    Extract root element info from first input file using streaming.
    Returns the raw bytes of the opening root tag and its attributes dict.
    """
    p = XMLParser(huge_tree=True, encoding='UTF-8')
    
    with open(filepath, 'rb') as file:
        context = iterparse(file, events=('start',), tag='smses', huge_tree=True)
        for event, elem in context:
            root_attribs = dict(elem.attrib)
            elem.clear()
            return root_attribs
    
    raise ValueError(f"No root 'smses' element found in {filepath}.")


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


def deduplicate_messages_in_tree(tree, log_file, args):
    """
    Removes duplicate messages from XML tree and additionally returns original/final message counts.

    :returns:
        1) the deduplicated XML tree
        2) a total_message_count_by_tag dict
        3) a unique_message_count_by_tag dict
    """
    message_count_by_tag, unique_messages_by_tag = defaultdict(int), defaultdict(set)
    data_stripped_by_tag = defaultdict(set)  # tag -> message attributes without data fields
    data_stripped_to_original = {}  # message attributes without data fields -> original attributes
    deduplication_fields_to_element = {}
    removal_count = 0

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

    def remove_element(element_to_remove, element_to_keep):
        nonlocal removal_count, tree
        log_file.write(removal_summary(element_to_remove, element_to_keep, args))
        tree.getroot().remove(element_to_remove)
        removal_count += 1

    for child in tree.getroot().iterchildren():
        child_tag, child_attributes = retrieve_message_properties_and_tag(child, args)

        if child_attributes in unique_messages_by_tag[child_tag]:
            # this message has a perfect match, so we drop it
            remove_element(child, deduplication_fields_to_element[child_attributes])
        else:
            unique_messages_by_tag[child_tag].add(child_attributes)
            deduplication_fields_to_element[child_attributes] = child
            if message_has_data(child_attributes):  # only fill in the data stripping info for messages with data
                data_stripped_attributes = strip_data_from_message(child_attributes)
                data_stripped_by_tag[child_tag].add(data_stripped_attributes)
                data_stripped_to_original[data_stripped_attributes] = child_attributes

        message_count_by_tag[child_tag] += 1

    # for some reason, some backup agents create duplicates without MMS
    # attachments, so we have to check for that failure mode as well
    for child in tree.getroot().iterchildren():
        child_tag, child_attributes = retrieve_message_properties_and_tag(child, args)
        if not message_has_data(child_attributes) and child_attributes in data_stripped_by_tag[child_tag]:
            # this message has a perfect match that also includes data, so we drop it
            remove_element(child, deduplication_fields_to_element[data_stripped_to_original[child_attributes]])
            unique_messages_by_tag[child_tag].remove(child_attributes)

    # sanity check that the bookkeeping is correctly keeping track of removed messages
    original_total_count = sum(v for v in message_count_by_tag.values())
    final_total_count = sum(len(v) for v in unique_messages_by_tag.values())
    if original_total_count - removal_count != final_total_count:
        raise RuntimeError(f"Removed {removal_count} messages from set of {original_total_count}, but ended up with "
                           f"inconsistent number of messages {final_total_count}?")

    return tree, message_count_by_tag, {k: len(v) for k, v in unique_messages_by_tag.items()}


def deduplicate_messages_streaming(input_fps, output_fp, log_file, args):
    """
    Stream-based deduplication that minimizes memory usage.
    
    Pass 1: Stream all inputs, identify duplicates (store only minimal metadata)
    Pass 2: Stream all inputs again, write unique messages directly to output
    
    This uses O(n) memory for metadata only, not full XML elements.
    """
    message_count_by_tag = defaultdict(int)
    unique_messages_by_tag = defaultdict(set)
    data_stripped_by_tag = defaultdict(set)
    data_stripped_to_original = {}
    # Store minimal info for logging: message index -> (tag, attributes for logging)
    message_info_for_logging = {}
    # Track which message indices to skip
    messages_to_skip = set()
    
    def retrieve_message_properties_and_tag(child, args):
        child_tag, child_attributes = child.tag, retrieve_message_properties(child, args)

        if child_tag not in EXPECTED_XML_TAGS:
            raise ValueError(f"Encountered unexpected XML tag {repr(child_tag)} directly under root. "
                             f"Is the input file malformed?")

        if args.aggressive:
            assert len(AGGRESSIVE_RELEVANT_FIELDS) == 4
            child_tag = ' or '.join(EXPECTED_XML_TAGS)
            child_attributes = tuple({('text' if x[0] in ('text', 'body') else x[0],) + x[1:]
                                      for x in child_attributes})

        return child_tag, child_attributes
    
    # PASS 1: Identify duplicates
    for elem, fp, msg_idx in stream_input_xmls(input_fps):
        child_tag, child_attributes = retrieve_message_properties_and_tag(elem, args)
        
        # Store logging info for this message
        attrs_for_logging = retrieve_message_properties(elem, args, disable_ignores=True)
        message_info_for_logging[msg_idx] = (child_tag, attrs_for_logging)
        
        is_duplicate = False
        kept_msg_idx = None
        
        # Check for exact duplicate
        if child_attributes in unique_messages_by_tag[child_tag]:
            is_duplicate = True
            # Find the kept message index
            for idx, (tag, attrs) in message_info_for_logging.items():
                if idx not in messages_to_skip:
                    test_tag, test_attrs = retrieve_message_properties_and_tag_from_stored(tag, attrs, args)
                    if test_tag == child_tag and test_attrs == child_attributes:
                        kept_msg_idx = idx
                        break
        # Check for data-stripped duplicate
        elif not message_has_data(child_attributes) and child_attributes in data_stripped_by_tag[child_tag]:
            is_duplicate = True
            original_attrs = data_stripped_to_original[child_attributes]
            # Find the kept message index
            for idx, (tag, attrs) in message_info_for_logging.items():
                if idx not in messages_to_skip:
                    test_tag, test_attrs = retrieve_message_properties_and_tag_from_stored(tag, attrs, args)
                    if test_tag == child_tag and test_attrs == original_attrs:
                        kept_msg_idx = idx
                        break
        
        if is_duplicate:
            messages_to_skip.add(msg_idx)
            if kept_msg_idx is not None:
                kept_tag, kept_attrs = message_info_for_logging[kept_msg_idx]
                # Write log entry
                log_file.write(removal_summary_from_attrs(attrs_for_logging, kept_attrs, child_tag, kept_tag, args))
        else:
            unique_messages_by_tag[child_tag].add(child_attributes)
            if message_has_data(child_attributes):
                data_stripped_attributes = strip_data_from_message(child_attributes)
                data_stripped_by_tag[child_tag].add(data_stripped_attributes)
                data_stripped_to_original[data_stripped_attributes] = child_attributes
        
        message_count_by_tag[child_tag] += 1
    
    # Calculate final count
    unique_count = sum(len(v) for v in unique_messages_by_tag.values())
    
    # Get root attributes from first file
    root_attribs = get_root_info_streaming(input_fps[0])
    root_attribs['count'] = str(unique_count)
    
    # PASS 2: Write output
    running_id = 0
    
    with open(output_fp, 'wb') as out_file:
        # Write XML declaration (matching lxml's tree.write format)
        out_file.write(b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n")
        
        # Write root opening tag with attributes
        root_attrs_str = ' '.join(f'{k}="{v}"' for k, v in root_attribs.items())
        out_file.write(f'<smses {root_attrs_str}>\n'.encode('UTF-8'))
        
        # Stream and write unique messages
        for elem, fp, msg_idx in stream_input_xmls(input_fps):
            if msg_idx not in messages_to_skip:
                # Update _id attributes
                for it in elem.iter():
                    if "_id" in it.attrib:
                        it.attrib["_id"] = str(running_id)
                        running_id += 1
                
                # Apply proper indentation (4 spaces)
                indent(elem, space='    ')
                # Serialize element
                elem_bytes = tostring(elem, encoding='UTF-8')
                # Write with base 4-space indentation
                for line in elem_bytes.split(b'\n'):
                    if line.strip():
                        out_file.write(b'    ' + line + b'\n')
        
        # Write closing tag with indentation matching original
        out_file.write(b'    </smses>\n')
    
    return message_count_by_tag, {k: len(v) for k, v in unique_messages_by_tag.items()}


def retrieve_message_properties_and_tag_from_stored(tag, attrs, args):
    """Helper to reconstruct tag/attributes from stored logging info."""
    # attrs are already the full attributes, just need to apply aggressive logic if needed
    if args.aggressive:
        tag = ' or '.join(EXPECTED_XML_TAGS)
        attrs = tuple({('text' if x[0] in ('text', 'body') else x[0],) + x[1:] for x in attrs})
    return tag, attrs


def removal_summary_from_attrs(element_to_remove_attrs, element_to_keep_attrs, tag_remove, tag_keep, args, field_length_limit=1000):
    """
    Returns a string of the removed message details for logging purposes.
    Works with attribute tuples instead of XML elements.
    """
    def collect_unique_field_data(element_attributes, field):
        return " | ".join(sorted({field_data if len(field_data) < field_length_limit
                                  else f"<LENGTH {len(field_data)} OMISSION>"
                                  for field_name, field_data in element_attributes if field == field_name}))

    removal_log = []
    for intro_str, element in [(f"Removing {tag_remove}:", element_to_remove_attrs),
                               (f"\nIn favor of keeping {tag_keep}:", element_to_keep_attrs)]:
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


def rewrite_tree_ids_and_count(tree, new_total):
    """
    Rewrites (MMS) message IDs in the XML tree and total message count in the backup.

    Without these, backup utilities may fail to restore the file (falsely believing
    that they have somehow skipped over messages or that the file itself is corrupt).
    """
    running_id = 0
    for it in tree.iter():
        if it.tag == 'smses':
            it.attrib["count"] = str(new_total)

        if "_id" in it.attrib:
            it.attrib["_id"] = str(running_id)
            running_id += 1


def write_output_xml(tree, filepath):
    # open the file here rather than passing a filepath to help avoid crashes, see read_input_xml() for more details
    with open(filepath, 'wb') as file:
        # note that the encoding, xml_declaration, and standalone tags are required to match the SMS B&R format
        tree.write(file, encoding='UTF-8', xml_declaration=True, pretty_print=True, standalone=True)


if __name__ == "__main__":
    # read in I/O filepaths from command line arguments
    args = parse_arguments()
    input_fps, output_fp, log_fp = args.input_file, args.output_file, args.log_file

    # Use streaming deduplication
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

    # Remove output file if no duplicates were found
    if input_message_counts == output_message_counts:
        print("No duplicate messages found. Removing output file.")
        if os.path.exists(output_fp):
            os.unlink(output_fp)
