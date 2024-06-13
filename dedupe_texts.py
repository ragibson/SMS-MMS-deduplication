import argparse
import os
import sys
from collections import defaultdict
from lxml.etree import XMLParser, parse
from time import time

EXPECTED_XML_TAGS = {'sms', 'mms'}  # treat any direct child tags other than this as a fatal error
RELEVANT_FIELDS = ['date', 'address', 'body', 'text', 'subject', 'm_type', 'type', 'data']


def parse_arguments():
    """
    Reads CLI arguments and return set of arguments.

    By default,
      * the output filepath is the input filepath with "_deduplicated" appended to the filename
      * the log filepath is the input filepath with "_deduplication.log" appended to the filename
    """
    parser = argparse.ArgumentParser(description='Deduplicate text messages from XML backup.')
    parser.add_argument('input_file', type=str, help='The input XML to deduplicate.')
    parser.add_argument('output_file', type=str, nargs='?',
                        help='The output file to save deduplicated entries. '
                             'Defaults to the input filepath with "_deduplicated" appended to the filename.')
    parser.add_argument('log_file', type=str, nargs='?',
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

    args = parser.parse_args()

    if not args.output_file:
        args.output_file = "_deduplicated".join(os.path.splitext(sys.argv[1]))

    if not args.log_file:
        args.log_file = f"{os.path.splitext(sys.argv[1])[0]}_deduplication.log"

    return args


def read_input_xml(filepath):
    p = XMLParser(huge_tree=True, encoding='UTF-8')  # huge_tree is required for larger backups
    tree = parse(filepath, parser=p)
    return tree


def retrieve_message_properties(child, args):
    """
    Returns message properties to use for uniqueness check.

    Note that this cannot be a shallow analysis, especially for MMS.
    """

    def contains_smil(s):
        """Strip out Synchronized Multimedia Integration Language data due to apparent differences in backup agents."""
        contains_smil = s.strip().startswith("<smil") and s.strip().endswith("</smil>")
        if "<" in s and ">" in s and "smil" in s and "/smil" in s:
            if not contains_smil:
                raise RuntimeError(f"Encountered SMIL data not captured by existing check? {repr(s)}")
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
        field_data = truncate_timestamp_precision(field_name, field_data)
        field_data = standardize_address(field_name, field_data)
        field_data = normalize_whitespace(field_name, field_data)
        return field_name, field_data

    def compile_relevant_fields(element):
        return tuple(
            normalize_field(field, element.attrib[field])
            for field in RELEVANT_FIELDS
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


def removal_summary(element_tag, element_to_remove, element_to_keep, field_length_limit=1000):
    """
    Returns a string of the removed message details for logging purposes.

    Alongside the duplicate (removed) message, it logs the message that was kept in its place.
    """

    def collect_unique_field_data(element_attributes, field):
        return " | ".join(sorted({field_data if len(field_data) < field_length_limit
                                  else f"<LENGTH {len(field_data)} OMISSION>"
                                  for field_name, field_data in element_attributes if field == field_name}))

    removal_log = []
    for intro_str, element in [(f"Removing {element_tag}:", element_to_remove),
                               (f"\nIn favor of keeping {element_tag}:", element_to_keep)]:
        removal_log.append(intro_str)
        for field in RELEVANT_FIELDS:
            combined_field_data = collect_unique_field_data(element, field)
            if combined_field_data:
                removal_log.append(f"{field:>8}: {combined_field_data}")

    return "\n".join(removal_log) + "\n\n"


def deduplicate_messages_in_tree(tree, log_file):
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
    removal_count = 0

    def retrieve_message_properties_and_tag(child, args):
        child_tag, child_attributes = child.tag, retrieve_message_properties(child, args)

        if child_tag not in EXPECTED_XML_TAGS:
            raise ValueError(f"Encountered unexpected XML tag {repr(child_tag)} directly under root. "
                             f"Is the input file malformed?")

        return child_tag, child_attributes

    def remove_element(element, element_tag, element_attributes, attribute_match):
        nonlocal removal_count, tree
        tree.getroot().remove(element)
        log_file.write(removal_summary(element_tag, element_attributes, attribute_match))
        removal_count += 1

    for child in tree.getroot().iterchildren():
        child_tag, child_attributes = retrieve_message_properties_and_tag(child, args)

        if child_attributes in unique_messages_by_tag[child_tag]:
            # this message has a perfect match, so we drop it
            remove_element(child, child_tag, child_attributes, child_attributes)
        else:
            unique_messages_by_tag[child_tag].add(child_attributes)
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
            remove_element(child, child_tag, child_attributes, data_stripped_to_original[child_attributes])
            unique_messages_by_tag[child_tag].remove(child_attributes)

    # sanity check that the bookkeeping is correctly keeping track of removed messages
    original_total_count = sum(v for v in message_count_by_tag.values())
    final_total_count = sum(len(v) for v in unique_messages_by_tag.values())
    if original_total_count - removal_count != final_total_count:
        raise RuntimeError(f"Removed {removal_count} messages from set of {original_total_count}, but ended up with "
                           f"inconsistent number of messages {final_total_count}?")

    return tree, message_count_by_tag, {k: len(v) for k, v in unique_messages_by_tag.items()}


def print_summary(input_message_counts, output_message_counts):
    """Prints summary of deduplicated message counts to stdout."""
    if input_message_counts.keys() != output_message_counts.keys():
        raise RuntimeError(f"Message type (MMS/SMS) was completely lost in deduplication? This should never occur!")

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
    # note that the encoding, xml_declaration, and standalone tags are required to match the SMS B&R format
    tree.write(filepath, encoding='UTF-8', xml_declaration=True, pretty_print=True, standalone=True)


if __name__ == "__main__":
    # read in I/O filepaths from command line arguments
    args = parse_arguments()
    input_fp, output_fp, log_fp = args.input_file, args.output_file, args.log_file

    # read entire input XML file
    print(f"Reading {repr(input_fp)}... ", end='', flush=True)
    st = time()
    input_tree = read_input_xml(input_fp)
    print(f"Done in {time() - st:.1f} s.")

    # search for duplicate messages and remove them from the XML tree
    print(f"Preparing log file {repr(log_fp)}.")
    with open(log_fp, "w", encoding="utf-8") as log_file:
        print(f"Searching for duplicates... ", end='', flush=True)
        st = time()
        output_tree, input_message_counts, output_message_counts = deduplicate_messages_in_tree(input_tree, log_file)
    print(f"Done in {time() - st:.1f} s.")

    # rewrite message count and ID numbers in XML tree
    rewrite_tree_ids_and_count(output_tree, sum(count for tag, count in output_message_counts.items()))

    # print summary of original and final message counts
    print_summary(input_message_counts, output_message_counts)

    # write the trimmed XML tree to the output file (if any duplicates were removed)
    if input_message_counts == output_message_counts:
        print(f"No duplicate messages found. Skipping writing of output file.")
    else:
        print(f"Writing {repr(output_fp)}... ", end='', flush=True)
        st = time()
        write_output_xml(output_tree, output_fp)
        print(f"Done in {time() - st:.1f} s")
