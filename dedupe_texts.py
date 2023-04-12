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

    def omit_smil(s):
        """Strip out Synchronized Multimedia Integration Language data due to apparent differences in backup agents."""
        if s.strip().startswith("<smil") and s.strip().endswith("</smil>"):
            return "<smil>OMIT\0SMIL\0CONTENTS</smil>"
        if "<" in s and ">" in s and "smil" in s and "/smil" in s:
            raise RuntimeError(f"Encountered SMIL data not captured by existing check? {repr(s)}")
        return s

    def search_for_relevant_fields(element):
        return tuple((field, omit_smil(element.attrib[field])) for field in ['date', 'address', 'text', 'data']
                     if field in element.attrib)

    return tuple(item for element in [child] + list(child.iter()) for item in search_for_relevant_fields(element))


def parse_message_tree(tree):
    """
    Removes duplicate messages from XML tree and additionally returns original/final message counts.

    :returns:
        1) the deduplicated XML tree
        2) a total_message_count_by_tag dict
        3) a unique_message_count_by_tag dict
    """
    message_count_by_tag, unique_messages_by_tag = defaultdict(int), defaultdict(set)
    for child in input_tree.getroot().iterchildren():
        child_tag, child_attributes = child.tag, retrieve_message_properties(child)

        if child_tag not in EXPECTED_XML_TAGS:
            raise ValueError(f"Encountered unexpected XML tag {repr(child_tag)} directly under root. "
                             f"Is the input file malformed?")

        if child_attributes in unique_messages_by_tag[child_tag]:
            tree.getroot().remove(child)
        else:
            unique_messages_by_tag[child_tag].add(child_attributes)
        message_count_by_tag[child_tag] += 1

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
