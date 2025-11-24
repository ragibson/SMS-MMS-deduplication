# /// script
# dependencies = [
#   "lxml",
# ]
# ///

import argparse
import os
from collections import defaultdict
from time import time

from lxml.etree import XMLParser, iterparse, Element, tostring
from lxml.etree import xmlfile

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


def element_to_bytes(elem):
    """Convert an XML element to its byte string representation for logging."""
    return tostring(elem, encoding='UTF-8', xml_declaration=False)


def removal_summary_from_bytes(element_to_remove_bytes, element_to_keep_bytes, args, field_length_limit=1000):
    """
    Returns a string of the removed message details for logging purposes.
    
    Takes byte strings of XML elements instead of Element objects.
    """
    # Parse the byte strings back into elements for property extraction
    from lxml.etree import fromstring
    element_to_remove = fromstring(element_to_remove_bytes)
    element_to_keep = fromstring(element_to_keep_bytes)
    
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


def first_pass_compute_deduplication(filepaths, log_file, args):
    """
    First streaming pass: determine which messages to keep.
    
    Returns:
        1) elements_to_keep: list of (file_index, element_index) tuples for elements to keep
        2) message_count_by_tag: dict of original message counts
        3) unique_message_count_by_tag: dict of deduplicated message counts
        4) root_attribs: dict of root element attributes from first file
    """
    import hashlib
    
    message_count_by_tag = defaultdict(int)
    unique_message_hashes_by_tag = defaultdict(set)  # Store hashes instead of full tuples
    data_stripped_by_tag = defaultdict(set)  # Store hashes
    data_stripped_to_original = {}  # hash -> hash
    deduplication_hash_to_location = {}  # hash -> (file_idx, elem_idx)
    
    # Track which elements to keep: (file_index, element_index) tuples
    elements_to_keep = []
    # Track removals for logging: (removed_location, kept_location)
    elements_to_remove = []
    
    root_attribs = None
    
    def hash_attributes(attrs):
        """Hash attribute tuple to save memory instead of storing full tuple."""
        return hashlib.md5(str(attrs).encode('utf-8')).hexdigest()
    
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
    
    for file_idx, filepath in enumerate(filepaths):
        with open(filepath, 'rb') as file:
            context = iterparse(file, events=('start', 'end'), huge_tree=True)
            elem_idx = 0
            
            for event, elem in context:
                if event == 'start' and elem.tag == 'smses':
                    if root_attribs is None:
                        root_attribs = dict(elem.attrib)
                    if elem.tag != 'smses':
                        raise ValueError(f"Unexpected root tag {repr(elem.tag)} in {filepath} (expected 'smses').")
                    continue
                    
                if event == 'end' and elem.tag in EXPECTED_XML_TAGS:
                    child_tag, child_attributes = retrieve_message_properties_and_tag(elem, args)
                    attr_hash = hash_attributes(child_attributes)
                    location = (file_idx, elem_idx)
                    
                    if attr_hash in unique_message_hashes_by_tag[child_tag]:
                        # This message is a duplicate
                        kept_location = deduplication_hash_to_location[attr_hash]
                        elements_to_remove.append((location, kept_location))
                    else:
                        # This is a new unique message
                        unique_message_hashes_by_tag[child_tag].add(attr_hash)
                        deduplication_hash_to_location[attr_hash] = location
                        elements_to_keep.append(location)
                        
                        if message_has_data(child_attributes):
                            data_stripped_attributes = strip_data_from_message(child_attributes)
                            data_stripped_hash = hash_attributes(data_stripped_attributes)
                            data_stripped_by_tag[child_tag].add(data_stripped_hash)
                            data_stripped_to_original[data_stripped_hash] = attr_hash
                    
                    message_count_by_tag[child_tag] += 1
                    elem_idx += 1
                    
                    # Clear element to free memory
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
    
    # Second deduplication pass: handle messages without data that match messages with data
    elements_to_keep_set = set(elements_to_keep)
    
    for file_idx, filepath in enumerate(filepaths):
        with open(filepath, 'rb') as file:
            context = iterparse(file, events=('end',), huge_tree=True)
            elem_idx = 0
            
            for event, elem in context:
                if elem.tag in EXPECTED_XML_TAGS:
                    location = (file_idx, elem_idx)
                    child_tag, child_attributes = retrieve_message_properties_and_tag(elem, args)
                    attr_hash = hash_attributes(child_attributes)
                    
                    if not message_has_data(child_attributes):
                        data_stripped_hash = attr_hash  # Same as attr_hash if no data
                        if data_stripped_hash in data_stripped_by_tag[child_tag]:
                            # This message matches one with data, so remove it
                            if location in elements_to_keep_set:
                                elements_to_keep_set.remove(location)
                                unique_message_hashes_by_tag[child_tag].remove(attr_hash)
                                
                                original_hash = data_stripped_to_original[data_stripped_hash]
                                kept_location = deduplication_hash_to_location[original_hash]
                                elements_to_remove.append((location, kept_location))
                    
                    elem_idx += 1
                    
                    # Clear element to free memory
                    elem.clear()
                    while elem.getprevious() is not None:
                        del elem.getparent()[0]
    
    # Write log entries by re-reading the elements from file
    write_removal_log(filepaths, elements_to_remove, log_file, args)
    
    # Rebuild final list of elements to keep
    elements_to_keep = sorted(elements_to_keep_set)
    
    return elements_to_keep, message_count_by_tag, {k: len(v) for k, v in unique_message_hashes_by_tag.items()}, root_attribs


def write_removal_log(filepaths, elements_to_remove, log_file, args):
    """
    Write removal log by re-reading elements from files in batches.
    
    This avoids storing full element bytes in memory for all removals at once.
    For very large removal sets, writes a summary instead of detailed logs to save memory.
    """
    if not elements_to_remove:
        return
    
    # For very large removal sets (>100k), write a summary to save memory and time
    if len(elements_to_remove) > 100000:
        log_file.write(f"Removed {len(elements_to_remove)} duplicate messages.\n")
        log_file.write("Detailed logging skipped for large removal sets to conserve memory.\n\n")
        return
    
    # Process removals in batches to limit memory usage
    BATCH_SIZE = 5000
    
    for batch_start in range(0, len(elements_to_remove), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(elements_to_remove))
        batch = elements_to_remove[batch_start:batch_end]
        
        # Group batch by file for efficient re-reading
        removal_map = {}  # (file_idx, elem_idx) -> kept_location
        for removed_loc, kept_loc in batch:
            removal_map[removed_loc] = kept_loc
        
        # Read needed elements from files
        element_cache = {}  # location -> elem_bytes
        
        for file_idx, filepath in enumerate(filepaths):
            needed_indices = {elem_idx for (f_idx, elem_idx) in removal_map.keys() if f_idx == file_idx}
            needed_indices |= {elem_idx for (f_idx, elem_idx) in removal_map.values() if f_idx == file_idx}
            
            if not needed_indices:
                continue
            
            with open(filepath, 'rb') as file:
                context = iterparse(file, events=('end',), huge_tree=True)
                elem_idx = 0
                
                for event, elem in context:
                    if elem.tag in EXPECTED_XML_TAGS:
                        if elem_idx in needed_indices:
                            element_cache[(file_idx, elem_idx)] = element_to_bytes(elem)
                        
                        elem_idx += 1
                        
                        # Clear element to free memory
                        elem.clear()
                        while elem.getprevious() is not None:
                            del elem.getparent()[0]
        
        # Write log entries for this batch
        for removed_loc, kept_loc in batch:
            removed_bytes = element_cache[removed_loc]
            kept_bytes = element_cache[kept_loc]
            log_file.write(removal_summary_from_bytes(removed_bytes, kept_bytes, args))
        
        # Clear cache for this batch
        element_cache.clear()


def second_pass_write_output(filepaths, elements_to_keep, output_filepath, root_attribs, final_count):
    """
    Second streaming pass: write only the kept messages to output.
    
    Writes elements directly using lxml's incremental XML writer without building a tree in memory.
    Post-processes to add the standalone attribute to the XML declaration.
    """
    from io import BytesIO
    import tempfile
    
    elements_to_keep_set = set(elements_to_keep)
    running_id = 0
    
    # Write to a temporary file first using xmlfile
    with tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_file:
        temp_filepath = temp_file.name
        
        with xmlfile(temp_file, encoding='UTF-8') as xf:
            root_attribs['count'] = str(final_count)
            
            with xf.element('smses', root_attribs):
                for file_idx, filepath in enumerate(filepaths):
                    with open(filepath, 'rb') as infile:
                        context = iterparse(infile, events=('end',), huge_tree=True)
                        elem_idx = 0
                        
                        for event, elem in context:
                            if elem.tag in EXPECTED_XML_TAGS:
                                location = (file_idx, elem_idx)
                                
                                if location in elements_to_keep_set:
                                    # Update the _id if present
                                    if '_id' in elem.attrib:
                                        elem.attrib['_id'] = str(running_id)
                                        running_id += 1
                                    
                                    # Also update _id for any child elements
                                    for child in elem.iter():
                                        if child != elem and '_id' in child.attrib:
                                            child.attrib['_id'] = str(running_id)
                                            running_id += 1
                                    
                                    # Write element directly without building a tree
                                    xf.write(elem, pretty_print=True)
                                
                                elem_idx += 1
                                
                                # Clear element to free memory
                                elem.clear()
                                while elem.getprevious() is not None:
                                    del elem.getparent()[0]
    
    # Read the temp file and add XML declaration with standalone='yes'
    with open(temp_filepath, 'rb') as temp_file:
        xml_content = temp_file.read()
    
    # Remove temp file
    import os
    os.unlink(temp_filepath)
    
    # Add XML declaration
    with open(output_filepath, 'wb') as f:
        f.write(b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n")
        f.write(xml_content)


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

    # First pass: compute deduplication decisions
    print(f"Reading {', '.join(repr(fp) for fp in input_fps)}... ", end='', flush=True)
    st = time()
    
    with open(log_fp, "w", encoding="utf-8") as log_file:
        print(f"Done in 0.0 s.")
        print(f"Preparing log file {repr(log_fp)}.")
        print("Searching for duplicates... ", end='', flush=True)
        st = time()
        elements_to_keep, input_message_counts, output_message_counts, root_attribs = \
            first_pass_compute_deduplication(input_fps, log_file, args)
    
    print(f"Done in {time() - st:.1f} s.")
    
    # print summary of original and final message counts
    print_summary(input_message_counts, output_message_counts)
    
    # write the trimmed XML to the output file (if any duplicates were removed)
    if input_message_counts == output_message_counts:
        print("No duplicate messages found. Skipping writing of output file.")
    else:
        print(f"Writing {repr(output_fp)}... ", end='', flush=True)
        st = time()
        final_count = sum(count for tag, count in output_message_counts.items())
        second_pass_write_output(input_fps, elements_to_keep, output_fp, root_attribs, final_count)
        print(f"Done in {time() - st:.1f} s")
