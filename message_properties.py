"""Normalize and summarize the fields used to identify duplicate messages."""

from hashlib import sha256
import sqlite3

from streaming_xml import LargeAttribute


RELEVANT_FIELDS = ['date', 'address', 'body', 'text', 'subject', 'm_type', 'type', 'data']
AGGRESSIVE_FIELDS = ['date', 'body', 'text', 'data']
LOG_LIMIT = 1000


class SortedValues:
    """Sort small collections in memory and spill unusually wide messages to disk."""

    def __init__(self, distinct=True):
        self.distinct = distinct
        self.values = []
        self.db = None

    def add(self, field, value):
        if self.db is None:
            self.values.append((field, value))
            if len(self.values) < 128:
                return
            self.db = sqlite3.connect('')
            self.db.execute('PRAGMA temp_store = FILE')
            self.db.execute('PRAGMA cache_size = -256')
            self.db.execute('CREATE TABLE fields (field INTEGER, value)')
            unique = 'UNIQUE' if self.distinct else ''
            self.db.execute(f'CREATE {unique} INDEX sorted_fields ON fields (field, value)')
            self.db.executemany('INSERT OR IGNORE INTO fields VALUES (?, ?)', self.values)
            self.values.clear()
        else:
            self.db.execute('INSERT OR IGNORE INTO fields VALUES (?, ?)', (field, value))

    def __iter__(self):
        if self.db is None:
            return iter(sorted(set(self.values) if self.distinct else self.values))
        return iter(self.db.execute('SELECT field, value FROM fields ORDER BY field, value'))

    def close(self):
        if self.db is not None:
            self.db.close()


class TextSpan:
    """String operations needed by the SMIL check, over a re-readable attribute."""

    def __init__(self, value, start=0, stop=None):
        self.value, self.start, self.stop = value, start, stop

    def __iter__(self):
        start, stop = self.start, self.stop
        for chunk in self.value.chunks():
            if stop is not None and stop <= 0:
                break
            if start < len(chunk):
                yield chunk[max(0, start):stop]
            start -= len(chunk)
            if stop is not None:
                stop -= len(chunk)

    def strip(self):
        first, last, position = None, 0, self.start
        for chunk in self:
            if chunk.rstrip():
                if first is None:
                    first = position + len(chunk) - len(chunk.lstrip())
                last = position + len(chunk.rstrip())
            position += len(chunk)
        return TextSpan(self.value, first or 0, last)

    def find(self, token):
        tail, position = '', 0
        for chunk in self:
            text = tail + chunk
            index = text.find(token)
            if index >= 0:
                return position - len(tail) + index
            tail = text[-len(token) + 1:] if len(token) > 1 else ''
            position += len(chunk)
        return -1

    def __contains__(self, token):
        return self.find(token) >= 0

    def startswith(self, token):
        head = ''
        for chunk in self:
            head += chunk[:len(token) - len(head)]
            if len(head) >= len(token):
                return head == token
        return False

    def endswith(self, token):
        tail = ''
        for chunk in self:
            tail = (tail + chunk)[-len(token):]
        return tail == token

    def after(self, token):
        return TextSpan(self.value, self.start + self.find(token) + len(token), self.stop)

    def __repr__(self):
        return repr(next(iter(self), '')[:LOG_LIMIT]) + ' (oversized attribute)'


def contains_smil(value):
    """Ignore SMIL presentation data, which can differ between backup agents."""
    value = value.strip()
    if '<smil' in value and '</smil>' in value:
        for opening, closing in (('<?xml', '?>'), ('<!DOCTYPE', '>'),
                                 ('<par', '/par>'), ('<seq', '/seq>'), ('<excl', '/excl>')):
            if value.startswith(opening) and closing in value:
                value = (value[value.index(closing) + len(closing):] if isinstance(value, str)
                         else value.after(closing)).strip()

    is_smil = value.startswith('<smil') and value.endswith('</smil>')
    if not is_smil and all(token in value for token in ('<', '>', 'smil', '/smil')):
        raise RuntimeError(f'This SMIL format is unique / previously unknown and '
                           f'not captured by the existing check? Please report: {value!r}')
    return is_smil


def standardize_address(value, country_code):
    return '~'.join(sorted(address if address.startswith('+') else f'{country_code}{address}'
                           for address in value.split('~')))


def normalize(field, value, args):
    if field == 'date' and args.ignore_date_milliseconds:
        return value[:-3] + '000'
    if field in ('text', 'body', 'subject') and args.ignore_whitespace_differences:
        return ' '.join(value.split())
    return value


def shortened(value):
    return value if len(value) < LOG_LIMIT else f'<LENGTH {len(value)} OMISSION>'


def address_digest(chunks, country_code):
    """Canonically hash the sorted group of country-code-normalized addresses."""
    addresses = SortedValues(distinct=False)
    digest, at_start, length = sha256(), True, 0
    prefix = str(country_code)
    try:
        for chunk in chunks:
            for index, part in enumerate(chunk.split('~')):
                if index:
                    if at_start:
                        digest.update(prefix.encode())
                        length += len(prefix)
                    addresses.add(0, digest.digest())
                    digest, at_start = sha256(), True
                    length += 1
                if part:
                    if at_start and not part.startswith('+'):
                        digest.update(prefix.encode())
                        length += len(prefix)
                    digest.update(part.encode())
                    length += len(part)
                    at_start = False
        if at_start:
            digest.update(prefix.encode())
            length += len(prefix)
        addresses.add(0, digest.digest())
        return digest_parts(value for _, value in addresses), length
    finally:
        addresses.close()


def digest_parts(parts):
    digest = sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def hash_large(field, value, args):
    """Return a normalized hash and original log value without materializing the attribute."""
    if field == 'address':
        digest, length = address_digest(value.chunks(), args.default_country_code)
        if '<' in TextSpan(value) and contains_smil(TextSpan(value)):
            return None, None
        return digest, f'<LENGTH {length} OMISSION>'

    collapse = field in ('text', 'body', 'subject') and args.ignore_whitespace_differences
    truncate = field == 'date' and args.ignore_date_milliseconds
    digest, length, tail, head = sha256(), 0, '', ''
    has_markup, needs_space, started = False, False, False
    for chunk in value.chunks():
        has_markup |= '<' in chunk
        length += len(chunk)
        head += chunk[:max(0, LOG_LIMIT - len(head))]
        if collapse:
            content = ' '.join(chunk.split())
            if content:
                if started and (needs_space or chunk[0].isspace()):
                    digest.update(b' ')
                started = True
                needs_space = chunk[-1].isspace()
            elif chunk:
                needs_space = True
            chunk = content
        elif truncate:
            chunk, tail = (tail + chunk)[:-3], (tail + chunk)[-3:]
        digest.update(chunk.encode())
    if has_markup and contains_smil(TextSpan(value)):
        return None, None
    if truncate:
        digest.update(b'000')
    return digest.digest(), head if length < LOG_LIMIT else f'<LENGTH {length} OMISSION>'


def field_values(field, value, args):
    """Return the duplicate-key and log representations of one XML attribute."""
    if isinstance(value, LargeAttribute):
        return hash_large(field, value, args)
    if value == 'null' or contains_smil(value):
        return None, None
    log_value = standardize_address(value, args.default_country_code) if field == 'address' else value
    normalized = normalize(field, log_value, args)
    if args.verify_duplicates:
        return normalized, shortened(log_value)
    if field == 'address':
        digest, _ = address_digest((value,), args.default_country_code)
    else:
        digest = sha256(normalized.encode()).digest()
    return digest, shortened(log_value)
