"""Durable, fail-closed intent ledger. Does NOT submit or sign transactions.

An intent is intentionally non-retryable until reconciled on-chain by a future
reviewed executor. Never mark an ambiguous broadcast as failed automatically.
"""
from __future__ import annotations
import json, os, uuid
from pathlib import Path
from datetime import datetime, timezone

class JournalError(RuntimeError):
    pass

ALLOWED = {'intent', 'submitted', 'confirmed', 'failed', 'uncertain', 'reconciled'}
BLOCKING = {'intent', 'submitted', 'uncertain', 'confirmed'}

def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)

class Journal:
    def __init__(self, directory='/data/live_orders'):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise JournalError('journal directory must be absolute')

    def entries(self):
        if not self.directory.exists(): return []
        out = []
        for p in sorted(self.directory.glob('*.json')):
            try:
                obj = json.loads(p.read_text())
                if obj.get('status') not in ALLOWED: raise ValueError('invalid status')
            except (ValueError, OSError) as exc:
                raise JournalError(f'unreadable journal entry: {p.name}') from exc
            out.append(obj)
        return out

    def assert_no_unresolved(self):
        if any(e['status'] in BLOCKING for e in self.entries()):
            raise JournalError('unresolved order exists; block new orders')

    def create_intent(self, *, action, mint, usd):
        self.assert_no_unresolved()
        if action != 'buy' or not isinstance(mint, str) or not mint or not (0 < usd <= 10):
            raise JournalError('invalid intent or purchase exceeds $10')
        order_id = uuid.uuid4().hex
        record = {'id': order_id, 'status': 'intent', 'action': action, 'mint': mint,
                  'usd': usd, 'created_at': datetime.now(timezone.utc).isoformat(), 'signature': None}
        _atomic_write(self.directory / (order_id + '.json'), record)
        return record

    def status(self):
        items = self.entries()
        return {'orders': len(items), 'unresolved': sum(e['status'] in BLOCKING for e in items),
                'statuses': [e['status'] for e in items]}
