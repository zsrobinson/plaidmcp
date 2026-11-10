"""Offline statement ingestion. Only allowlisted numeric/date/category data reaches SQLite."""
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import stat
import uuid

from store import publish

ALIASES = {
    'date': ('datetime', 'date', 'transactiondate', 'timestamp'),
    'amount': ('amounttotal', 'totalamount', 'amount', 'total'),
    'type': ('type', 'transactiontype', 'paymenttype', 'category'),
    'direction': ('direction', 'flow', 'debitcredit'),
    'status': ('status', 'transactionstatus'),
    'fee': ('amountfee', 'feeamount', 'fee'),
}
TYPES = {
    'payment': 'peer', 'charge': 'peer', 'peerpayment': 'peer', 'peersent': 'peer_sent',
    'peerreceived': 'peer_received', 'paymentsent': 'peer_sent', 'paymentreceived': 'peer_received',
    'purchase': 'purchase', 'merchantpayment': 'purchase', 'merchanttransaction': 'purchase',
    'venmocardtransaction': 'purchase', 'venmodebitcardpurchase': 'purchase',
    'standardtransfer': 'bank_transfer', 'instanttransfer': 'bank_transfer',
    'banktransfer': 'bank_transfer', 'transfertobank': 'bank_out', 'cashout': 'bank_out',
    'transferfrombank': 'bank_in', 'addfunds': 'bank_in', 'addmoney': 'bank_in',
    'fee': 'fee', 'transactionfee': 'fee', 'servicefee': 'fee',
}
IN = {'in', 'incoming', 'credit', 'received', 'receive'}
OUT = {'out', 'outgoing', 'debit', 'sent', 'send'}
SKIP_STATUS = {'pending', 'failed', 'cancelled', 'canceled', 'declined'}
OK_STATUS = {'', 'complete', 'completed', 'settled', 'successful', 'success', 'posted'}
FOOTERS = {'beginningbalance', 'endingbalance', 'accountbalance', 'total', 'totals', 'summary'}

class ImportError(ValueError):
    """Fixed messages only: never include input cells, raw rows or filenames."""


def norm(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())


def column_map(value='{}'):
    try:
        mapping = json.loads(value)
        if not isinstance(mapping, dict) or set(mapping) - set(ALIASES): raise ValueError()
        if any(not isinstance(v, str) for v in mapping.values()): raise ValueError()
        return mapping
    except (ValueError, TypeError):
        raise ImportError('Invalid Venmo column mapping') from None


def header_indices(row, mapping):
    normalized = [norm(cell) for cell in row]
    result = {}
    for field, aliases in ALIASES.items():
        choices = (norm(mapping[field]),) if field in mapping else aliases
        for choice in choices:
            found = [i for i, name in enumerate(normalized) if name == choice]
            if len(found) > 1: raise ImportError('Ambiguous Venmo columns')
            if found:
                result[field] = found[0]
                break
    return result if {'date', 'amount', 'type'} <= result.keys() else None


def parse_date(value):
    try:
        try: return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError: return datetime.strptime(value.strip().split()[0], '%m/%d/%Y').date().isoformat()
    except (ValueError, IndexError):
        raise ImportError('Invalid Venmo date') from None


def parse_amount(value):
    text = re.sub(r'\s+', '', value).replace('$', '').replace(',', '')
    if text.startswith('(') and text.endswith(')'): text = '-' + text[1:-1]
    if len(text) > 32 or not re.fullmatch(r'[+-]?\d+(?:\.\d{1,2})?', text): raise ImportError('Invalid Venmo amount')
    amount = Decimal(text)
    cents = int(abs(amount) * 100)
    if cents > 10**12: raise ImportError('Venmo amount exceeds limit')
    direction = 'out' if text.startswith('-') else 'in' if text.startswith('+') else None
    return cents, direction


def parse_statement(stream, mapping=None, empty_month=None, fee_mode='reject'):
    """Preamble/BOM tolerant; never retain or map any name, username, note or raw ID."""
    if fee_mode not in ('reject','separate','included'): raise ImportError('Invalid Venmo fee mode')
    reader = csv.reader(stream)
    indices = None
    rows, months = [], set()
    for number, row in enumerate(reader):
        if number > 100000: raise ImportError('Venmo statement exceeds row limit')
        if not any(cell.strip() for cell in row): continue
        if indices is None:
            indices = header_indices(row, mapping or {})
            if indices is None and number >= 100: raise ImportError('Venmo header not found')
            continue
        first = next((norm(cell) for cell in row if cell.strip()), '')
        if first in FOOTERS: continue
        def cell(field):
            index = indices.get(field)
            return row[index].strip() if index is not None and index < len(row) else ''
        when = parse_date(cell('date'))
        months.add(when[:7])
        status = norm(cell('status'))
        if status in SKIP_STATUS: continue
        if status not in OK_STATUS: raise ImportError('Unknown Venmo status')
        kind = TYPES.get(norm(cell('type')))
        if kind is None: raise ImportError('Unknown Venmo transaction type')
        cents, signed_direction = parse_amount(cell('amount'))
        explicit = norm(cell('direction'))
        direction = 'in' if explicit in IN else 'out' if explicit in OUT else None
        if explicit and direction is None: raise ImportError('Unknown Venmo direction')
        if direction and signed_direction and direction != signed_direction:
            raise ImportError('Conflicting Venmo direction')
        direction = direction or signed_direction
        expected = {'peer_sent': 'out', 'peer_received': 'in', 'bank_in': 'in', 'bank_out': 'out'}.get(kind)
        if expected and direction and expected != direction: raise ImportError('Conflicting Venmo type')
        direction = direction or expected
        if direction is None and kind in ('purchase', 'fee'): direction = 'out'
        if direction is None: raise ImportError('Ambiguous Venmo direction; use signed amounts or map direction')
        if kind == 'peer': kind = 'peer_sent' if direction == 'out' else 'peer_received'
        if kind in ('bank_in', 'bank_out'): kind = 'bank_transfer'
        fee = parse_amount(cell('fee'))[0] if cell('fee') else 0
        if fee and kind != 'fee':
            if fee_mode == 'reject': raise ImportError('Nonzero fee column needs explicit PLAIDMCP_VENMO_FEE_MODE')
            if fee_mode == 'included': cents += fee if direction == 'in' else -fee
            if cents < 0: raise ImportError('Fee exceeds Venmo total')
            rows.append((when, fee, 'out', 'fee'))
        rows.append((when, cents, direction, kind))
    if indices is None: raise ImportError('Venmo header not found')
    if not months and empty_month:
        try: months.add(date.fromisoformat(empty_month+'-01').strftime('%Y-%m'))
        except ValueError: raise ImportError('Invalid empty statement month') from None
    if len(months) != 1: raise ImportError('Expected exactly one Venmo statement month')
    return next(iter(months)), rows


def file_identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def import_folder(db, public, folder, action='delete', archive=None, mapping=None, fee_mode='reject'):
    """Called under the sync user's private_store lock; publish before raw cleanup."""
    if action not in ('delete', 'move'): raise ImportError('PLAIDMCP_VENMO_PROCESSED must be delete or move')
    folder = Path(folder)
    if not folder.exists(): return 0
    if folder.is_symlink() or not folder.is_dir(): raise ImportError('Invalid Venmo import folder')
    if action == 'move':
        if not archive: raise ImportError('Set PLAIDMCP_VENMO_ARCHIVE for move mode')
        archive = Path(archive)
        if archive.resolve() == folder.resolve() or archive.is_symlink(): raise ImportError('Invalid Venmo archive folder')
        archive.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(archive, 0o700)
    paths = sorted(p for p in folder.iterdir() if p.suffix.casefold() == '.csv')
    if not paths: return 0
    # CSV is a fallback, not a second feed. Refuse overlap before importing any files.
    if db.execute("SELECT 1 FROM items WHERE id!='venmo-csv' AND lower(institution) LIKE '%venmo%'").fetchone():
        raise ImportError('Venmo Plaid source already exists; CSV fallback would double count')
    count = 0
    for path in paths:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8-sig', newline='') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 25*1024*1024:
                raise ImportError('Invalid or oversized Venmo CSV')
            match = re.search(r'(?<!\d)(\d{4}-\d{2})(?!\d)', path.stem)
            try:
                month, rows = parse_statement(source, mapping, match[1] if match else None, fee_mode)
            except (csv.Error, UnicodeError): raise ImportError('Invalid Venmo CSV encoding or structure') from None
            if file_identity(before) != file_identity(os.fstat(source.fileno())):
                raise ImportError('Venmo CSV changed during import; retry after upload completes')
        start = month+'-01'
        year, number = map(int, month.split('-'))
        end = date(year+(number==12), number % 12+1, 1).isoformat()
        with db:
            db.execute('DELETE FROM venmo_rows WHERE date>=? AND date<?', (start, end))
            db.executemany('INSERT INTO venmo_rows VALUES(?,?,?,?)', rows)
            db.execute("INSERT OR REPLACE INTO items VALUES('venmo-csv','Venmo CSV',?,NULL)",
                       (datetime.now(timezone.utc).isoformat(),))
        publish(db, public)
        db.commit()  # persist source/account identities before raw cleanup
        # If publication/cleanup fails, raw remains and a retry replaces the same month.
        if file_identity(before) != file_identity(path.lstat()):
            raise ImportError('Venmo CSV changed before cleanup; raw file retained')
        if action == 'delete': path.unlink()
        else: os.replace(path, archive / (month+'-'+uuid.uuid4().hex+'.csv'))
        count += 1
    return count


def import_configured(db, public):
    root = Path(os.environ.get('PLAIDMCP_PRIVATE', 'private'))
    return import_folder(db, public, os.environ.get('PLAIDMCP_VENMO_IMPORT_DIR', str(root/'import'/'venmo')),
        os.environ.get('PLAIDMCP_VENMO_PROCESSED', 'delete'), os.environ.get('PLAIDMCP_VENMO_ARCHIVE'),
        column_map(os.environ.get('PLAIDMCP_VENMO_COLUMN_MAP', '{}')), os.environ.get('PLAIDMCP_VENMO_FEE_MODE','reject'))


def main():
    # This standalone path works with no Plaid keys and never constructs a Plaid Client.
    from sync import private_store
    with private_store() as db:
        import_configured(db, os.environ.get('PLAIDMCP_PUBLIC', 'aggregate.sqlite'))

if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('Venmo import failed; raw CSV retained. Check local format/configuration; no row details logged.') from None
