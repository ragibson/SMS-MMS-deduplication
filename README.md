# SMS-MMS-deduplication

This is a simple tool to remove duplicate text messages from XML backups of
the "SMS Backup & Restore" format.

## Usage

The usage of this tool is extremely simple and can handle files of several
gigabytes in a few seconds.

```bash
Usage: python3 dedupe_texts.py input_file [output_file]
```

This will produce output of the following form.

```
Reading 'example-input.xml'... Done in 3.4 s.
Searching for duplicates... Done in 1.0 s.
Deduplication Summary:
    Message Type    |   Original Count   |      Removed       | Deduplicated Count 
        mms         |       24893        |        7611        |       17282        
        sms         |       19676        |         0          |       19676        
Writing 'example-output.xml'... Done in 1.7 s
```

or

```
Reading 'example-input.xml'... Done in 2.1 s.
Searching for duplicates... Done in 0.7 s.
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