# SMS-MMS-deduplication

This is a simple tool to remove duplicate text messages from XML backups of
the "SMS Backup & Restore" format.

It also supports removal of more complicated duplicates than other tools.
 * Those created due to conflicts arising from multiple backup agents being
   used simultaneously (e.g., accidentally recovering data from Google's
   backups ***and*** Samsung's backups)
 * Duplicates where one contains a data attachment, but another does not

## Usage

The usage of this tool is extremely simple and can handle files of several
gigabytes in a few seconds.

```bash
Usage: python3 dedupe_texts.py input_file [output_file]
```

This will produce output of the following form.

```
Reading 'example-input.xml'... Done in 8.1 s.
Searching for duplicates... Done in 5.9 s.
Deduplication Summary:
    Message Type    |   Original Count   |      Removed       | Deduplicated Count 
        mms         |       24893        |       10325        |       14568        
        sms         |       19828        |         0          |       19828        
Writing 'example-output.xml'... Done in 4.2 s
```

or

```
Reading 'example-input.xml'... Done in 8.1 s.
Searching for duplicates... Done in 5.8 s.
Deduplication Summary:
    Message Type    |   Original Count   |      Removed       | Deduplicated Count 
        mms         |       14341        |         0          |       14341        
        sms         |       19676        |         0          |       19676        
No duplicate messages found. Skipping writing of output file.
```

## Related Work

This tool is somewhat inspired by
[legacy work by radj](https://github.com/radj/AndroidSMSBackupRestoreCleaner),
but is significantly simpler, much more up to date, requires far fewer
dependencies/setup, and supports MMS messages.