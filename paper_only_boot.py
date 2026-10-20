"""Fail-closed PAPER-ONLY Railway entrypoint for v8.9.2.

Use `python paper_only_boot.py` rather than `python clean_boot.py` when
strictly no live orders may be submitted. This file must be installed beside
clean_boot.py and bot.py. It overrides stale Railway live variables and removes
live signing credentials from the Python process before importing the bot.
"""
import os
import sys


def enforce_paper_only(environment):
    # These values take precedence over Railway and any .env defaults.
    for key in (
        'CLEAN_LIVE_0_60_ENABLED',
        'CLEAN_LIVE_NEW_BUYS_ENABLED',
        'LIVE_TRADING_ENABLED',
        'LIVE_0_60_ENABLED',
        'LIVE_0_60_NEW_BUYS_ENABLED',
    ):
        environment[key] = 'false'
    # Prior one-shot recovery and acknowledgment variables are invalid when
    # live execution is disabled; discard them without modifying any journal.
    for key in (
        'CLEAN_LIVE_FAILED_BUY_RECOVER_ORDER_ID',
        'CLEAN_LIVE_ACK_ZERO_BALANCE_MINTS',
        'LIVE_0_60_FAILED_BUY_RECOVER_ORDER_ID',
        'LIVE_0_60_ACK_ZERO_BALANCE_MINTS',
        'CLEAN_LIVE_0_60_ARM',
        'LIVE_0_60_ARM',
        'CLEAN_LIVE_SECRET_KEY_B58',
        'LIVE_0_60_SECRET_KEY_B58',
    ):
        environment.pop(key, None)
    environment['PAPER_MODE'] = 'true'


def main(argv=None):
    enforce_paper_only(os.environ)
    print('PAPER ONLY LOCK ACTIVE: live_orders_disabled=True signer_credentials_unset=True'
          ' historical_live_journal_untouched=True', flush=True)
    from clean_boot import main as clean_main
    return clean_main(argv)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
