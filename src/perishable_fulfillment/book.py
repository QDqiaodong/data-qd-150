"""CLI for the v2 perishable commitment ledger.

Subcommands:
  book-init   --db DB --source JSON
  book-apply  --db DB --source JSON --output JSON
  book-report --db DB --output JSON

Business computation stays fully offline and uses only the standard library.
The legacy v1 single-shot CLI (perishable_fulfillment.cli with --source/
--output) is unchanged.
"""

import argparse
import json
import sys
from pathlib import Path

from .common import read_bytes, save
from . import ledger

# A commit has already landed in SQLite when this is raised; the picked facts
# must not be rolled back just because receipt publication failed.
class OutputAfterCommitError(Exception):
    def __init__(self, report):
        super().__init__('report publication failed after commit')
        self.report = report


def _emit(report, output):
    """Atomically publish the receipt, then echo it on stdout."""
    save(output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))


def book_init(db, source):
    raw = read_bytes(source)
    from .common import parse_json
    import hashlib
    data = parse_json(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return ledger.initialize(db, data, digest)


def book_apply(db, source, output):
    raw = read_bytes(source)
    report, replayed = ledger.apply_batch(db, raw)
    if output is not None:
        try:
            _emit(report, output)
        except OSError as exc:
            print('commit succeeded but report write failed: %s' % exc,
                  file=sys.stderr)
            raise OutputAfterCommitError(report)
    else:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
    return report, replayed


def book_report(db, output):
    report = ledger.current_report(db)
    if output is not None:
        _emit(report, output)
    else:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                         allow_nan=False))
    return report


def build_parser():
    parser = argparse.ArgumentParser(
        prog='perishable-book',
        description='Perishable commitment ledger (book-init/apply/report)')
    sub = parser.add_subparsers(dest='command', required=True)

    init_p = sub.add_parser('book-init', help='create a new ledger')
    init_p.add_argument('--db', type=Path, required=True)
    init_p.add_argument('--source', type=Path, required=True)

    apply_p = sub.add_parser('book-apply', help='commit one event batch')
    apply_p.add_argument('--db', type=Path, required=True)
    apply_p.add_argument('--source', type=Path, required=True)
    apply_p.add_argument('--output', type=Path, required=True)

    report_p = sub.add_parser('book-report', help='print the current receipt')
    report_p.add_argument('--db', type=Path, required=True)
    report_p.add_argument('--output', type=Path, required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'book-init':
            report = book_init(args.db, args.source)
            print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                             allow_nan=False))
        elif args.command == 'book-apply':
            book_apply(args.db, args.source, args.output)
        else:
            book_report(args.db, args.output)
    except OutputAfterCommitError:
        # Facts are durably committed; the caller can retry the same bytes.
        return 3
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
