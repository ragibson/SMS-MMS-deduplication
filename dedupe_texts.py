from lxml.etree import XMLParser, parse
import os
import re
import sys
from time import time
from collections import defaultdict

EXPECTED_XML_TAGS = {'sms', 'mms'}  # treat any child tags other than this as a fatal error


def simple_read_argv():
    """Reads sys.argv naively and returns input_filepath, output_filepath."""
    if len(sys.argv) < 2:
        print(f"Usage: python3 dedupe_texts.py input_file [output_file]")
        exit()
    return sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "_deduplicated".join(os.path.splitext(sys.argv[1]))


def read_input_xml(filepath):
    p = XMLParser(huge_tree=True, encoding='UTF-8')  # huge_tree is required for larger backups
    tree = parse(filepath, parser=p)
    return tree


def retrieve_message_properties(child):
    """Returns message properties to use for uniqueness check.

    Note that this cannot be a shallow analysis, especially for MMS."""

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
            # for some reason, this field has each number/email/etc. delimited
            # by '~', but the ordering differs by backup agent
            field_data = '~'.join(sorted(field_data.split('~')))
        return field_name, field_data

    def compile_relevant_fields(element):
        return tuple(
            standardize_address(field, element.attrib[field])
            for field in ['date', 'address', 'body', 'text', 'data']
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


def parse_message_tree(tree):
    """
    Removes duplicate messages from XML tree and additionally returns original/final message counts.

    :returns:
        1) the deduplicated XML tree
        2) a total_message_count_by_tag dict
        3) a unique_message_count_by_tag dict
    """
    message_count_by_tag, unique_messages_by_tag = defaultdict(int), defaultdict(set)
    unique_data_messages_by_tag = defaultdict(set)
    removal_count = 0

    for child in tree.getroot().iterchildren():
        child_tag, child_attributes = child.tag, retrieve_message_properties(child)

        if child_tag not in EXPECTED_XML_TAGS:
            raise ValueError(f"Encountered unexpected XML tag {repr(child_tag)} directly under root. "
                             f"Is the input file malformed?")

        if child_attributes in unique_messages_by_tag[child_tag]:
            tree.getroot().remove(child)
            removal_count += 1
        else:
            unique_messages_by_tag[child_tag].add(child_attributes)
            if message_has_data(child_attributes):
                unique_data_messages_by_tag[child_tag].add(strip_data_from_message(child_attributes))

        message_count_by_tag[child_tag] += 1

    # for some reason, some backup agents create duplicates without MMS
    # attachments, so we have to check for that failure mode as well
    for child in input_tree.getroot().iterchildren():
        child_tag, child_attributes = child.tag, retrieve_message_properties(child)
        if not message_has_data(child_attributes) and child_attributes in unique_data_messages_by_tag[child_tag]:
            # this message has a perfect match that also includes data, so we drop it
            tree.getroot().remove(child)
            removal_count += 1
            unique_messages_by_tag[child_tag].remove(child_attributes)

    # sanity check that the bookkeeping is correctly keeping track of removed messages
    original_total_count = sum(v for v in message_count_by_tag.values())
    final_total_count = sum(len(v) for v in unique_messages_by_tag.values())
    if original_total_count - removal_count != final_total_count:
        raise RuntimeError(f"Removed {removal_count} messages from set of {original_total_count}, but ended up with "
                           f"inconsistent number of messages {final_total_count}?")

    return tree, message_count_by_tag, {k: len(v) for k, v in unique_messages_by_tag.items()}


def print_summary(input_message_counts, output_message_counts):
    if input_message_counts.keys() != output_message_counts.keys():
        raise RuntimeError(f"Message type (MMS/SMS) was completely lost in deduplication? This should never occur!")

    print("Deduplication Summary:")
    print("|".join(f"{x:^20}" for x in ["Message Type", "Original Count", "Removed", "Deduplicated Count"]))
    for message_tag in input_message_counts.keys() | output_message_counts:
        original_count, final_count = input_message_counts[message_tag], output_message_counts[message_tag]
        print("|".join(f"{x:^20}" for x in [message_tag, original_count, original_count - final_count, final_count]))


def write_output_xml(tree, filepath):
    # note that the encoding, xml_declaration, and standalone tags are required to match the SMS B&R format
    tree.write(filepath, encoding='UTF-8', xml_declaration=True, pretty_print=True, standalone=True)


if __name__ == "__main__":
    # read in I/O filepaths from command line arguments
    input_fp, output_fp = simple_read_argv()

    # read entire input XML file
    print(f"Reading {repr(input_fp)}... ", end='', flush=True)
    st = time()
    input_tree = read_input_xml(input_fp)
    print(f"Done in {time() - st:.1f} s.")

    # search for duplicate messages and remove them from the XML tree
    print(f"Searching for duplicates... ", end='', flush=True)
    st = time()
    output_tree, input_message_counts, output_message_counts = parse_message_tree(input_tree)
    print(f"Done in {time() - st:.1f} s.")

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
