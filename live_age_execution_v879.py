"""Opt-in automated SOL<->SPL execution, with live-only quote safety for promoted AgeTrack.

The original AgeTrack.observe still decides entry eligibility and exit thresholds.
Live-only executable quotes replace indicative price signals for held positions;
orders and on-chain accounting remain separate.  Without explicit opt-in no
transaction path exists.  Never put private-key material in files or logs.

On ambiguous transactions the worker stops automatic orders until reconciled.
This is a transaction-integrity requirement, not a market loss circuit breaker.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

from shadow_age_tracks_v876 import AgeTrack

LOG = logging.getLogger(__name__)
SOL_MINT = 'So11111111111111111111111111111111111111112'
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
JUPITER = 'https://api.jup.ag/swap/v2'


class LiveError(RuntimeError): pass
class AmbiguousTransaction(LiveError): pass


# LiveError messages originate in this module. Only emit a fixed diagnostic code,
# never exception text (which can include provider details, URLs, or secrets).
def safe_live_error_code(exc: BaseException) -> str:
    if isinstance(exc, AmbiguousTransaction):
        return 'transaction_outcome_ambiguous'
    known = {
        'wallet does not hold recorded token quantity': 'token_balance_less_than_recorded',
        'Jupiter has no executable quote': 'jupiter_quote_missing',
        'Jupiter order unavailable': 'jupiter_order_unavailable',
        'Jupiter cannot build route; skip order (no synthetic fill)': 'jupiter_route_unbuildable',
        'insufficient SOL for swap and network fees': 'insufficient_sol_fees',
        'unreadable live order journal; refusing orders': 'journal_unreadable',
        'unresolved signed/confirmed order; freeze live orders': 'journal_unresolved_order',
        'submission response unknown; manual reconciliation required': 'transaction_outcome_ambiguous',
        'Jupiter execute failed; uncertain': 'jupiter_execute_uncertain',
        'returned signature differs; manual reconciliation required': 'transaction_outcome_ambiguous',
        'confirmed without reliable amounts': 'transaction_outcome_ambiguous',
        'failed to sign Jupiter transaction': 'transaction_signing_failed',
        'SOL price quote invalid': 'sol_quote_invalid',
        'amount_raw must be positive': 'amount_invalid',
        'inconsistent token decimals': 'token_decimals_inconsistent',
    }
    label = known.get(str(exc))
    if label:
        return label
    if isinstance(exc, LiveError):
        if str(exc).startswith('RPC '):
            return 'solana_rpc_failure'
        if str(exc).startswith('unresolved live order '):
            return 'journal_unresolved_order'
    return 'unclassified_live_error'


def durable_write(path: Path, item: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    try:
        with tmp.open('x', encoding='utf8') as out:
            json.dump(item, out, separators=(',', ':'), allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
        fd=os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


class TransactionJournal:
    """Persist intent BEFORE broadcast and confirmation BEFORE portfolio commit."""
    HOLD = {'intent','signed','uncertain','confirmed'}
    def __init__(self, path: Path):
        self.path=Path(path)
        if not self.path.is_absolute(): raise ValueError('journal path must be absolute')
        self.path.mkdir(parents=True, exist_ok=True)

    def records(self):
        rows=[]
        for p in sorted(self.path.glob('*.json')):
            try:
                d=json.loads(p.read_text())
                if d.get('status') not in self.HOLD | {'applied','failed'}:
                    raise ValueError('bad status')
                rows.append(d)
            except (OSError, ValueError, TypeError) as exc:
                raise LiveError('unreadable live order journal; refusing orders') from exc
        return rows

    def pending(self):return [r for r in self.records() if r['status'] in self.HOLD]

    def begin(self, *, action, mint, proposal, price, before_raw):
        if self.pending(): raise LiveError('unresolved signed/confirmed order; freeze live orders')
        d={'id':uuid.uuid4().hex, 'action':action,'mint':mint,'status':'intent',
           'proposal':proposal,'sol_usd':price,'before_token_raw':before_raw,
           'created_at':time.time(),'signature':None}
        self.update(d)
        return d

    def update(self,d): durable_write(self.path/(d['id']+'.json'),d)


@dataclass(frozen=True)
class RealReceipt:
    signature: str
    input_raw: int
    output_raw: int
    sol_usd: float


class JupiterAutoBroker:
    """Jupiter v2 order/execute with real wallet signer, RPC balance checks."""
    def __init__(self, *, key_b58:str, rpc_url:str, jupiter_key:str, journal_dir:Path,
                 session=None):
        if not rpc_url.startswith('https://') or not jupiter_key:
            raise LiveError('HTTPS Solana RPC and Jupiter API key required')
        try:
            from solders.keypair import Keypair
            self.keypair=Keypair.from_base58_string(key_b58)
        except Exception as exc:
            raise LiveError('invalid live wallet secret; never log it') from exc
        self.wallet=str(self.keypair.pubkey())
        claimed=os.getenv('SOLANA_WALLET_ADDRESS','').strip()
        if claimed and claimed!=self.wallet:
            raise LiveError('configured public wallet does not match the signing key')
        self.rpc_url=rpc_url
        self.api_key=jupiter_key
        self.http=session or requests.Session()
        self.journal=TransactionJournal(journal_dir)
        self._sol_price=0.;self._sol_price_at=0.
        # v8.8.6: never admit a new trade with >5% estimated round-trip cost.
        # Even an existing Railway override of 12% cannot disable this ceiling.
        self.max_roundtrip_loss_pct=min(5.,float(os.getenv('CLEAN_LIVE_MAX_ROUNDTRIP_LOSS_PCT','5')))
        self.estimated_exit_fee_sol=float(os.getenv('CLEAN_LIVE_ESTIMATED_EXIT_FEE_SOL','0.00005'))
        if not (0 <= self.max_roundtrip_loss_pct < 50 and 0 <= self.estimated_exit_fee_sol <= .01):
            raise LiveError('invalid live quote risk configuration')

    def _rpc(self,method,params):
        try:
            r=self.http.post(self.rpc_url,json={'jsonrpc':'2.0','id':1,'method':method,
                                                   'params':params},timeout=12)
            r.raise_for_status(); j=r.json()
            if j.get('error') or 'result' not in j:
                raise LiveError('RPC '+method+' failed')
            return j['result']
        except (requests.RequestException,ValueError,KeyError) as exc:
            raise LiveError('RPC '+method+' unavailable') from exc

    def balance_lamports(self):
        return int(self._rpc('getBalance',[self.wallet,{'commitment':'confirmed'}])['value'])

    def token_balance(self,mint):
        data=self._rpc('getTokenAccountsByOwner',[
            self.wallet,{'mint':mint},{'encoding':'jsonParsed','commitment':'confirmed'}])
        amount=0; decimals=None
        for a in data.get('value',[]):
            tok=a['account']['data']['parsed']['info']['tokenAmount']
            amount+=int(tok['amount'])
            if decimals is not None and decimals != int(tok['decimals']):
                raise LiveError('inconsistent token decimals')
            decimals=int(tok['decimals'])
        return amount,decimals

    def sol_usd(self):
        if self._sol_price>0 and time.monotonic()-self._sol_price_at<45:
            return self._sol_price
        # 1 SOL -> USDC no taker: price-only request, not a trade.
        quote=self.order(input_mint=SOL_MINT,output_mint=USDC_MINT,amount=10**9,
                         taker=None)
        v=int(quote['outAmount'])/1_000_000
        if not (1.0<v<100000.): raise LiveError('SOL price quote invalid')
        self._sol_price=v;self._sol_price_at=time.monotonic()
        return v

    def cash_usd(self):return self.balance_lamports()/1e9*self.sol_usd()

    def order(self,*,input_mint,output_mint,amount,taker):
        params={'inputMint':input_mint,'outputMint':output_mint,'amount':str(amount)}
        if taker:params['taker']=taker
        try:
            r=self.http.get(JUPITER+'/order',params=params,
                            headers={'x-api-key':self.api_key},timeout=12)
            r.raise_for_status(); j=r.json()
            if not isinstance(j,dict) or int(j.get('outAmount',0))<=0:
                raise LiveError('Jupiter has no executable quote')
            return j
        except (requests.RequestException,ValueError,TypeError) as exc:
            raise LiveError('Jupiter order unavailable') from exc

    def prebuy_roundtrip(self, mint, buy_lamports, *, buy_order=None):
        """Price a round trip *before* spending SOL; a quote is not a guarantee.

        The reverse price-only quote uses the estimated received token amount,
        not a fictional fixed-size sell, and must be nonzero.  The buy order
        passed here is the exact order later signed, avoiding quote-to-order
        drift introduced by fetching a second buy quote.
        """
        first=buy_order or self.order(input_mint=SOL_MINT,output_mint=mint,
                                      amount=buy_lamports,taker=self.wallet)
        predicted_raw=int(first['outAmount'])
        if predicted_raw<=0:raise LiveError('prebuy returned no token amount')
        reverse=self.order(input_mint=mint,output_mint=SOL_MINT,
                           amount=predicted_raw,taker=None)
        back=int(reverse['outAmount'])
        # Reserve for an exit network fee; Jupiter's outAmount is not net of it.
        usable=max(0,back-round(self.estimated_exit_fee_sol*1e9))
        loss=100*(1-usable/buy_lamports)
        if loss>self.max_roundtrip_loss_pct:
            raise LiveError('prebuy round trip exceeds configured loss ceiling')
        return first,round(loss,5),back

    def executable_exit_net_pct(self, mint, token_raw, entry_sol_lamports,
                                entry_sol_usd, *, entry_wallet_cost_lamports=None,
                                entry_network_fee_lamports=None):
        """USD return of selling recorded token raw amount at *current* quote.

        Quote already contains Jupiter route costs; a separate expected network
        fee is reserved, but a quote can still be stale or become unfillable.
        Does not reuse the paper strategy's flat 6% modeled cost deduction.
        """
        if min(token_raw,entry_sol_lamports)<=0 or entry_sol_usd<=0:
            raise LiveError('invalid recorded live position cost')
        reverse=self.order(input_mint=mint,output_mint=SOL_MINT,
                           amount=token_raw,taker=None)
        possible_sol=int(reverse['outAmount'])
        # An ATA creation may temporarily debit SOL rent, but rent is
        # refundable when the token account is closed. Do NOT treat that rent
        # as market loss: it previously triggered stops immediately on $2 buys.
        # Count only the actual SOL swapped + verified NON-REFUNDABLE network
        # fees. Keep total wallet debit/rent in the cash-flow audit separately.
        # `entry_wallet_cost_lamports` remains accepted for older call sites,
        # but must never be used to compute this strategy stop-loss.
        nonrefundable_fee=max(0,int(entry_network_fee_lamports or 0))
        paid=int(entry_sol_lamports)+nonrefundable_fee
        exit_lamports=max(0,possible_sol-round(self.estimated_exit_fee_sol*1e9))
        current_sol_usd=self.sol_usd()
        net=100*((exit_lamports*current_sol_usd)/(paid*entry_sol_usd)-1)
        if not math.isfinite(net):raise LiveError('invalid executable exit net return')
        return net

    def transaction_costs(self, signature):
        """Return on-chain cash movement / charged fee or explicitly unknown.

        Solana RPC can lag execution; missing metadata is NOT estimated as zero.
        A wallet lamport delta includes paid fees and ATA rent, which should be
        reported as cash-flow until rent is recovered, not confused with P&L.
        """
        try:
            tx=self._rpc('getTransaction',[signature,{'encoding':'jsonParsed',
                         'commitment':'confirmed','maxSupportedTransactionVersion':0}])
            if not tx or not isinstance(tx,dict):return None
            meta=tx.get('meta') or {}
            keys=tx['transaction']['message']['accountKeys']
            names=[str(k.get('pubkey')) if isinstance(k,dict) else str(k) for k in keys]
            if self.wallet not in names or meta.get('err') is not None:return None
            i=names.index(self.wallet)
            before=int(meta['preBalances'][i]);after=int(meta['postBalances'][i])
            fee=int(meta['fee'])
            if fee<0:return None
            return {'wallet_sol_change_lamports':after-before,
                    'network_fee_lamports':fee,
                    'fee_payer_is_wallet':bool(names and names[0]==self.wallet),
                    'solana_rpc_slot':tx.get('slot')}
        except (LiveError,TypeError,ValueError,KeyError,IndexError):
            return None

    def _sign(self, unsigned_b64):
        try:
            from solders.transaction import VersionedTransaction
            from solders.message import to_bytes_versioned
            raw=base64.b64decode(unsigned_b64,validate=True)
            tx=VersionedTransaction.from_bytes(raw)
            keys=list(tx.message.account_keys)
            required=tx.message.header.num_required_signatures
            signer_index=keys[:required].index(self.keypair.pubkey())
            sigs=list(tx.signatures)
            sig=self.keypair.sign_message(to_bytes_versioned(tx.message))
            sigs[signer_index]=sig
            signed=VersionedTransaction.populate(tx.message,sigs)
            # Chain transaction ID is ALWAYS the first required signature.
            # It may differ from the wallet signer on sponsored/RFQ routes.
            return base64.b64encode(bytes(signed)).decode('ascii'),str(signed.signatures[0])
        except Exception as exc:
            raise LiveError('failed to sign Jupiter transaction') from exc

    def signature_status(self,sig):
        return self._rpc('getSignatureStatuses',[[sig],{'searchTransactionHistory':True}])['value'][0]

    def execute(self,*,action,mint,amount_raw,proposal,price):
        """Return (receipt, journal record). Caller MUST record ledger before mark applied."""
        if amount_raw<=0: raise LiveError('amount_raw must be positive')
        before_token, _ = self.token_balance(mint)
        before_sol=self.balance_lamports()
        if action=='buy':
            # Reserve network fees and one ATA creation, not an age/momentum restriction.
            if before_sol <= amount_raw + 10_000_000:
                raise LiveError('insufficient SOL for swap and network fees')
            in_mint,out_mint=SOL_MINT,mint
        elif action=='sell':
            if before_token<amount_raw:
                raise LiveError('wallet does not hold recorded token quantity')
            in_mint,out_mint=mint,SOL_MINT
        else: raise LiveError('unsupported order side')
        order=self.order(input_mint=in_mint,output_mint=out_mint,
                         amount=amount_raw,taker=self.wallet)
        if action=='buy':
            order,loss,back=self.prebuy_roundtrip(mint,amount_raw,buy_order=order)
            LOG.info('LIVE 0_60 PREBUY checked mint=%s modeled_roundtrip_loss_pct=%.3f',mint,loss)
        if not order.get('transaction') or not order.get('requestId'):
            raise LiveError('Jupiter cannot build route; skip order (no synthetic fill)')
        quoted_out_raw=int(order['outAmount'])
        signed,signature=self._sign(order['transaction'])
        d=self.journal.begin(action=action,mint=mint,proposal=proposal,
                             price=price,before_raw=before_token)
        d.update(status='signed',signature=signature,request_id=order['requestId'],
                 amount_raw=amount_raw,before_sol_lamports=before_sol,
                 order_quoted_out_raw=quoted_out_raw)
        self.journal.update(d)  # durable BEFORE broadcasting
        try:
            r=self.http.post(JUPITER+'/execute',json={'signedTransaction':signed,
                                     'requestId':order['requestId']},
                    headers={'x-api-key':self.api_key,'Content-Type':'application/json'},
                    timeout=35)
            r.raise_for_status(); outcome=r.json()
        except (requests.RequestException,ValueError) as exc:
            d['status']='uncertain';self.journal.update(d)
            raise AmbiguousTransaction('submission response unknown; manual reconciliation required') from exc
        if outcome.get('status')!='Success' or int(outcome.get('code',-1))!=0:
            # A non-success carrying a signature may still need on-chain review.
            d['status']='uncertain' # submission happened: never assume a failed response means no landing
            d['execute_code']=outcome.get('code')
            self.journal.update(d)
            raise LiveError('Jupiter execute failed; '+d['status'])
        if outcome.get('signature')!=signature:
            d['status']='uncertain';self.journal.update(d)
            raise AmbiguousTransaction('returned signature differs; manual reconciliation required')
        try:
            spent=int(outcome['totalInputAmount'])
            received=int(outcome['totalOutputAmount'])
            if spent<=0 or received<=0:raise ValueError('empty execution amount')
        except (KeyError,ValueError,TypeError) as exc:
            d['status']='uncertain';self.journal.update(d)
            raise AmbiguousTransaction('confirmed without reliable amounts') from exc
        cost_details=self.transaction_costs(signature)
        d.update(status='confirmed',filled_input_raw=spent,filled_output_raw=received,
                 execution_status='Success',chain_costs=cost_details,
                 realized_vs_order_quote_pct=round(100*(received/quoted_out_raw-1),4),
                 chain_costs_status='verified' if cost_details else 'unavailable')
        if received<quoted_out_raw*.8:
            LOG.warning('LIVE 0_60 SWAP EXECUTION MUCH WORSE THAN ORDER QUOTE mint=%s action=%s quoted_out=%d actual_out=%d',
                        mint,action,quoted_out_raw,received)
        self.journal.update(d) # persist receipt before ledger update
        return RealReceipt(signature,spent,received,price),d

    def finalized_failed_buy_receipt(self, entry):
        """Read-only chain proof. None means remain frozen; NEVER treat missing as failed.

        Require signature-status and full transaction receipt to agree on a
        finalized failure. Verify token holdings did not change from the
        recorded pre-broadcast balance. Do not infer outcome from HTTP/Jupiter.
        """
        if entry.get('action') != 'buy' or entry.get('status') not in ('signed','uncertain'):
            return None
        signature=entry.get('signature')
        if not isinstance(signature,str) or not (80 <= len(signature) <= 90):
            return None
        try:
            expected_before=int(entry['before_token_raw'])
            if expected_before < 0: return None
            status=self.signature_status(signature)
            if not isinstance(status,dict) or status.get('confirmationStatus')!='finalized' or status.get('err') is None:
                return None
            transaction=self._rpc('getTransaction',[signature,{
                'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':0}])
            if not isinstance(transaction,dict) or not isinstance(transaction.get('meta'),dict):
                return None
            meta=transaction['meta']
            if meta.get('err') is None or meta['err']!=status['err']:
                return None
            if int(transaction.get('slot',-1)) != int(status.get('slot',-2)):
                return None
            chain_sigs=((transaction.get('transaction') or {}).get('signatures') or [])
            if not chain_sigs or chain_sigs[0]!=signature:
                return None
            fee=int(meta['fee'])
            if fee < 0: return None
            observed_balance,_=self.token_balance(entry['mint'])
            if observed_balance != expected_before:
                return None
            keys=((transaction.get('transaction') or {}).get('message') or {}).get('accountKeys') or []
            payer=(keys[0] if keys and isinstance(keys[0],str) else None)
            return {'slot':int(status['slot']), 'fee_lamports':fee,
                    'fee_payer_is_wallet':payer==self.wallet if payer else None,
                    'verified_token_balance_raw':observed_balance,
                    'confirmation_status':'finalized'}
        except (LiveError, KeyError, ValueError, TypeError, IndexError):
            # RPC unavailable / incomplete history / inconsistent receipt must
            # never clear the journal quarantine.
            return None

    def recover_pending(self):
        """Expose unresolved states; confirmed-but-unapplied can be replayed."""
        return self.journal.pending()


class LiveAgeTrack(AgeTrack):
    """Retain AgeTrack thresholds; use live sell quotes and confirmed fills."""
    def __init__(self,*,broker:JupiterAutoBroker,allow_new_buys:bool=False,
                 acknowledge_zero_balance_mints=(), recover_failed_buy_order_id='',**kwargs):
        kwargs.setdefault('trailing_activation_net_pct',10.)
        kwargs.setdefault('trailing_drawdown_pct_points',5.)
        kwargs.setdefault('take_profit_net_pct',40.)
        kwargs.setdefault('take_profit_enabled',True)
        super().__init__(**kwargs)
        self.broker=broker
        self.allow_new_buys=bool(allow_new_buys)
        self.acknowledge_zero_balance_mints=tuple(acknowledge_zero_balance_mints)
        self.recover_failed_buy_order_id=str(recover_failed_buy_order_id).strip()
        self._recovering=False
        self._journal_safety_freeze=False
        self._journal_pending_count=0
        self._last_live_quote_at={}
        self.inventory_exceptions=[]
        self._last_inventory_probe={}

    def load(self):
        if self.loaded:return
        super().load()
        if self.read_only:return
        try:
            existing=json.loads(self.path.read_text(encoding='utf8'))
            exceptions=existing.get('inventory_exceptions',[])
            if not isinstance(exceptions,list) or not all(isinstance(x,dict) for x in exceptions):
                raise ValueError('invalid inventory exceptions')
            self.inventory_exceptions=exceptions[-1000:]
        except FileNotFoundError:
            pass
        except (ValueError,TypeError,OSError):
            self.read_only=True;self.error='invalid_inventory_exceptions';return
        # Never crash-loop a deployed scanner because an order is unresolved.
        # An intent/signed/uncertain entry may have reached the chain even if
        # the local ledger has no receipt. Report and freeze *all* live orders.
        # Do not replay confirmed entries out of order across an unresolved one.
        journal_pending=self.broker.recover_pending()
        # Explicit single-order opt-in: a finalized on-chain *failed buy* can
        # be marked terminal without a fake position or fabricated proceeds.
        # Never auto-clear other statuses, sells, missing signatures, or RPC
        # errors. Additional pending orders prevent reconciliation.
        wanted=self.recover_failed_buy_order_id
        if wanted and len(journal_pending)==1:
            entry=journal_pending[0]
            if (entry.get('id')==wanted and entry.get('action')=='buy' and
                    entry.get('status') in ('signed','uncertain') and
                    entry.get('mint') not in self.positions):
                proof=self.broker.finalized_failed_buy_receipt(entry)
                if proof is not None:
                    terminal=copy.deepcopy(entry)
                    terminal.update(status='failed',
                                    resolution='verified_finalized_onchain_failed_buy',
                                    resolution_at=time.time(),
                                    resolution_rpc_slot=proof['slot'],
                                    failed_chain_fee_lamports=proof['fee_lamports'],
                                    failed_chain_fee_payer_is_wallet=proof['fee_payer_is_wallet'],
                                    resolution_wallet_token_balance_raw=proof['verified_token_balance_raw'],
                                    failed_buy_no_token_fill=True)
                    self.broker.journal.update(terminal)
                    # Even when Railway still has new buys enabled, require a
                    # second reviewed deployment before the first new trade.
                    self.allow_new_buys=False
                    LOG.warning('LIVE 0_60 FAILED BUY VERIFIED order_id=%s '
                                'mint=%s status=failed finality=finalized '
                                'token_holdings_match=True fee_lamports=%s '
                                'no_fill_recorded=True no_rebroadcast=True',
                                wanted,entry['mint'],proof['fee_lamports'])
                    LOG.warning('LIVE 0_60 FAILED BUY RECOVERY BUY PAUSE '
                                'new_buys_enabled=False for_this_process=True '
                                'review_before_rearming=True')
                    journal_pending=self.broker.recover_pending()
                else:
                    LOG.warning('LIVE 0_60 FAILED BUY RECOVERY deferred order_id=%s '
                                'reason=chain_failure_not_fully_verified_or_balance_mismatch '
                                'journal_unchanged=True',wanted)
            else:
                LOG.warning('LIVE 0_60 FAILED BUY RECOVERY deferred order_id=%s '
                            'reason=nonmatching_order_or_open_position journal_unchanged=True',wanted)
        elif wanted and journal_pending:
            LOG.warning('LIVE 0_60 FAILED BUY RECOVERY deferred order_id=%s '
                        'reason=multiple_pending_or_mismatched_journal journal_unchanged=True',wanted)
        ambiguous=[x for x in journal_pending if x['status']!='confirmed']
        if ambiguous:
            self._journal_safety_freeze=True
            self._journal_pending_count=len(journal_pending)
            for entry in ambiguous[:20]:
                LOG.critical('LIVE 0_60 JOURNAL QUARANTINE order_id=%s status=%s '
                             'action=%s mint=%s signature=%s '
                             'new_buys_blocked=True all_live_orders_frozen=True '
                             'manual_chain_reconciliation_required=True no_rebroadcast=True',
                             entry.get('id'),entry.get('status'),entry.get('action'),
                             entry.get('mint'),entry.get('signature') or 'none')
            LOG.critical('LIVE 0_60 JOURNAL SAFETY FREEZE pending=%d ambiguous=%d '
                         'scanner_continues=True live_swaps_disabled=True '
                         'journal_unchanged=True',len(journal_pending),len(ambiguous))
            # No writes to ledger or journal, no guessed sales or costs.
            return
        # Confirmed entries are safe to replay using their durable receipts.
        for entry in journal_pending:
            self._apply_confirmed(entry)
        self.cash=self.broker.cash_usd()
        self.changed=True
        self.save()

    def save(self):
        """Atomic persistence of ledger and inventory exceptions; never fabricate fills."""
        self.load()
        if self.read_only or not self.changed:return False
        payload={'version':self.VERSION,'name':self.name,'cash':self.cash,
                 'positions':self.positions,'closed':self.closed,
                 'inventory_exceptions':self.inventory_exceptions[-1000:],
                 'seen':sorted(self.seen)[-4000:],'healthy_samples':self.healthy_samples,
                 'open_count':self.open_count,'missed_quotes':self.missed_quotes,
                 'halted':self.halted,'halt_reason':self.halt_reason,
                 'historical_halt_reason':self.historical_halt_reason}
        durable_write(self.path,payload)
        self.changed=False
        return True

    def _archive_unsellable(self,mint,*,kind,balance_raw,now):
        """Remove only from active execution queue, not accounting history.

        Zero wallet balance with missing journal provenance is an inventory
        EXCEPTION, not realized sale P&L; no receipt or proceeds are invented.
        """
        rec=self.positions[mint]
        item={'mint':mint,'symbol':rec.get('symbol',''),
              'kind':kind,'status':'unreconciled' if kind=='zero_balance_unexplained' else 'dust_writeoff',
              'verified_wallet_balance_raw':int(balance_raw),
              'recorded_token_raw':int(rec.get('real_token_raw',0)),
              'residual_cost_basis_lamports':int(rec.get('real_spent_lamports',0)),
              'entry_sol_usd':float(rec.get('real_entry_sol_usd',0)),
              'prior_confirmed_sells':copy.deepcopy(rec.get('real_partial_sells',[])),
              'pending_exit':rec.get('pending_real_exit'),
              'recorded_at':float(now),'realized_pnl_status':'unknown_no_sale_receipt',
              'auto_buy_blocking':kind=='zero_balance_unexplained'}
        # An earlier confirmed partial sell stays in the original position history.
        self.inventory_exceptions.append(item)
        self.positions.pop(mint)
        self.changed=True
        self.save()
        LOG.warning('LIVE 0_60 INVENTORY archived mint=%s reason=%s wallet_balance_raw=%d realized_sale_recorded=False new_buys_blocked=%s',
                    mint,kind,balance_raw,item['auto_buy_blocking'])
        return 'live_inventory_'+kind

    def acknowledge_unexplained_zero_inventory(self,*,now=None):
        """Explicitly clear only the BUY BLOCK for verified, already-archived zero inventory.

        This is NOT financial reconciliation: no sell, writeoff P&L, or new
        closed trade is created. Transaction provenance remains unknown in the
        durable exception record. Must be independently opted into by exact mint.
        """
        if not self.acknowledge_zero_balance_mints or self.read_only:
            return 0
        stamp=time.time() if now is None else float(now)
        try:
            if self.broker.recover_pending():
                LOG.warning('LIVE 0_60 INVENTORY ACK deferred reason=pending_order')
                return 0
        except (LiveError,ValueError,OSError):
            LOG.warning('LIVE 0_60 INVENTORY ACK deferred reason=journal_unavailable')
            return 0
        count=0
        for mint in self.acknowledge_zero_balance_mints:
            # A mistyped/unknown mint must not silently exempt another record.
            candidates=[x for x in self.inventory_exceptions if x.get('mint')==mint
                        and x.get('kind')=='zero_balance_unexplained']
            if len(candidates)!=1 or mint in self.positions:
                LOG.warning('LIVE 0_60 INVENTORY ACK skipped mint=%s reason=unknown_or_active_position',mint)
                continue
            item=candidates[0]
            if item.get('status')=='operator_acknowledged_zero_balance' and not item.get('auto_buy_blocking'):
                continue
            if (item.get('status')!='unreconciled' or not item.get('auto_buy_blocking') or
                item.get('verified_wallet_balance_raw')!=0):
                LOG.warning('LIVE 0_60 INVENTORY ACK skipped mint=%s reason=ineligible_exception',mint)
                continue
            try:
                # Two fresh RPC reads; zero holdings must be verified at the
                # actual configured signer wallet, not by UI portfolio history.
                first,decimals=self.broker.token_balance(mint)
                second,second_decimals=self.broker.token_balance(mint)
                if int(first)!=0 or int(second)!=0 or decimals!=second_decimals:
                    LOG.warning('LIVE 0_60 INVENTORY ACK skipped mint=%s reason=wallet_nonzero_or_inconsistent',mint)
                    continue
                if self.broker.recover_pending():
                    LOG.warning('LIVE 0_60 INVENTORY ACK deferred reason=pending_order')
                    break
            except (LiveError,ValueError,TypeError,OSError):
                LOG.warning('LIVE 0_60 INVENTORY ACK deferred mint=%s reason=rpc_unavailable',mint)
                continue
            item['status']='operator_acknowledged_zero_balance'
            item['auto_buy_blocking']=False
            item['operator_acknowledged_at']=stamp
            item['acknowledgement_basis']='two_fresh_zero_wallet_rpc_reads_and_no_pending_orders'
            item['realized_pnl_status']='unknown_no_sale_receipt'
            # Save each successfully acknowledged record atomically; never
            # delete history or change any live traded quantities/closed P&L.
            self.changed=True
            self.save()
            count+=1
            LOG.warning('LIVE 0_60 INVENTORY ACKNOWLEDGED mint=%s wallet_balance_raw=0 new_buys_blocked=False '
                        'sale_proceeds_unknown=True no_realized_sale_recorded=True',mint)
        return count

    def reconcile_position(self,mint,*,now=None):
        """Two confirmed RPC reads before zero archiving; no guessed receipts.

        Invalid/lagging RPC or unknown signed transactions fail closed. Never
        liquidate the entire wallet's tokens to compensate for ledger drift.
        """
        if mint not in self.positions:return 'not_held'
        position=self.positions[mint]
        if any(x.get('status') in TransactionJournal.HOLD for x in self.broker.recover_pending()):
            return 'unresolved_journal'
        # Fake brokers used by historical offline tests can omit inventory RPC.
        balance_fn=getattr(self.broker,'token_balance',None)
        if not callable(balance_fn):return 'legacy_broker_no_rpc'
        stamp=time.time() if now is None else now
        if stamp-self._last_inventory_probe.get(mint,float('-inf'))<10.:
            if position.get('wallet_zero_first_seen_at') is not None:
                return 'wallet_zero_pending_confirmation'
            return ('inventory_mismatch' if position.get('inventory_reconciliation_required')
                    else 'verified_recently')
        try:
            balance,decimals=balance_fn(mint)
            balance=int(balance)
            if balance<0:raise LiveError('invalid wallet token balance')
            if balance==0:
                # Two polls separated by >=15 seconds prevent falsely
                # archiving a newly bought token during confirmed-RPC lag.
                first=float(position.get('wallet_zero_first_seen_at',stamp))
                position.setdefault('wallet_zero_first_seen_at',stamp)
                self.changed=True;self.save()
                if stamp-first<15. or stamp-float(position.get('opened_at',stamp))<45.:
                    self._last_inventory_probe[mint]=stamp
                    return 'wallet_zero_pending_confirmation'
                again,_=balance_fn(mint)
                if int(again)!=0:
                    position.pop('wallet_zero_first_seen_at',None)
                    self.changed=True;self.save()
                    return 'wallet_balance_inconsistent'
                return self._archive_unsellable(mint,kind='zero_balance_unexplained',balance_raw=0,now=stamp)
            if position.pop('wallet_zero_first_seen_at',None) is not None:
                self.changed=True;self.save()
            recorded=int(position.get('real_token_raw',0))
            if recorded<=0:
                return 'invalid_recorded_position'
            if balance<recorded:
                position['inventory_reconciliation_required']=True
                position['last_verified_wallet_balance_raw']=balance
                self.changed=True;self.save()
                LOG.warning('LIVE 0_60 INVENTORY mismatch mint=%s recorded_raw=%d wallet_raw=%d no_auto_sale=True',mint,recorded,balance)
                self._last_inventory_probe[mint]=stamp
                return 'inventory_mismatch'
            if balance==recorded and position.pop('inventory_reconciliation_required',None):
                position.pop('last_verified_wallet_balance_raw',None)
                self.changed=True;self.save()
            if (position.get('pending_real_exit') and
                    position.get('real_partial_sells') and
                    int(position.get('real_spent_lamports',0))<=1000 and
                    recorded==balance):
                # Very small residual after confirmed sale. Require unavailable
                # Jupiter route or a sell quote smaller than estimated exit fee.
                try:
                    quote=self.broker.order(input_mint=mint,output_mint=SOL_MINT,
                                            amount=recorded,taker=None)
                    quoted=int(quote.get('outAmount',0))
                    network_fee=round(float(getattr(self.broker,'estimated_exit_fee_sol',.00005))*1e9)
                    if quoted>network_fee:return 'dust_still_economic'
                except LiveError:
                    pass
                return self._archive_unsellable(mint,kind='unsellable_dust',balance_raw=balance,now=stamp)
        except (LiveError,ValueError,TypeError,KeyError) as exc:
            LOG.warning('LIVE 0_60 INVENTORY probe_unavailable mint=%s error_code=%s auto_archive=False',
                        mint,safe_live_error_code(exc))
            return 'wallet_balance_unavailable'
        self._last_inventory_probe[mint]=stamp
        return 'wallet_balance_verified'

    def reconcile_all_positions(self,*,now=None):
        """Read-only wallet inventory sweep; safe without a Dex price quote.

        This is also called when evaluating NEW buys, preventing stale held
        tokens from bypassing reconciliation solely because their pool vanished.
        """
        if self.read_only:return {'read_only':1}
        results={}
        for mint in list(self.positions):
            status=self.reconcile_position(mint,now=now)
            results[status]=results.get(status,0)+1
        return results

    def stats(self):
        # Periodic primary audits also reconcile positions with missing pools.
        self.load()
        if not self.read_only and not self.broker.recover_pending():
            self.reconcile_all_positions()
        self.acknowledge_unexplained_zero_inventory()
        report=super().stats()
        # Current journal is the authority, not a previously cached startup flag.
        pending=self.broker.recover_pending()
        report['live_journal_safety_freeze']=bool(pending)
        report['live_journal_pending_count']=len(pending)
        report['inventory_exceptions']=len(self.inventory_exceptions)
        report['unreconciled_inventory_exceptions']=sum(1 for x in self.inventory_exceptions
                                                       if x.get('auto_buy_blocking'))
        report['operator_acknowledged_missing_inventory']=sum(1 for x in self.inventory_exceptions
                                                      if x.get('status')=='operator_acknowledged_zero_balance')
        return report

    def _apply_confirmed(self,d):
        mint=d['mint']
        proposal=d['proposal']
        if d['action']=='buy':
            if mint not in self.positions:
                rec=copy.deepcopy(proposal['position'])
                rec.update(real_token_raw=int(d['filled_output_raw']),
                           real_spent_lamports=int(d['filled_input_raw']),
                           real_entry_sol_usd=float(d['sol_usd']),
                           real_buy_signature=d['signature'],
                           real_buy_chain_costs=d.get('chain_costs'),
                           live_quote_peak_initialized=False)
                self.positions[mint]=rec
                self.seen.add(mint);self.open_count+=1
        else:
            # idempotent on journal replay even after a crash between ledger save
            # and writing status='applied'. Partial fills remain open to retry
            # only the leftover wallet tokens, never the original full amount.
            if not any(d['id'] in x.get('live_order_ids',[x.get('live_order_id')]) for x in self.closed):
                rec=self.positions.get(mint)
                if rec is not None and d['id'] not in rec.get('applied_sell_order_ids',[]):
                    sold=int(d['filled_input_raw'])
                    previously=int(rec['real_token_raw'])
                    if sold<=0 or sold>previously:
                        raise LiveError('confirmed sell quantity exceeds recorded position; manual reconciliation')
                    sold_fraction=sold/previously
                    allocated_sol=round(int(rec['real_spent_lamports'])*sold_fraction)
                    partial_profit=(int(d['filled_output_raw'])/1e9*float(d['sol_usd'])-
                                    allocated_sol/1e9*float(rec['real_entry_sol_usd']))
                    parts=list(rec.get('real_partial_sells',[]))
                    parts.append({'live_order_id':d['id'],'signature':d['signature'],
                                  'token_raw_sold':sold,'received_sol_lamports':int(d['filled_output_raw']),
                                  'cost_basis_sol_lamports':allocated_sol,
                                  'pnl_usd_before_network_fees':round(partial_profit,6),
                                  'chain_costs':d.get('chain_costs'),
                                  'sale_sol_usd':float(d['sol_usd'])})
                    left=previously-sold
                    if left:
                        rec['real_token_raw']=left
                        rec['real_spent_lamports']=int(rec['real_spent_lamports'])-allocated_sol
                        rec['real_partial_sells']=parts
                        rec.setdefault('pending_real_exit',proposal['closed']['reason'])
                        rec.setdefault('applied_sell_order_ids',[]).append(d['id'])
                    else:
                        self.positions.pop(mint,None)
                        trade=copy.deepcopy(proposal['closed'])
                        all_pnl=sum(x['pnl_usd_before_network_fees'] for x in parts)
                        all_sol=sum(x['received_sol_lamports'] for x in parts)
                        all_cost=sum(x['cost_basis_sol_lamports'] for x in parts)
                        trade.update(execution='jupiter_swap_v2_confirmed',
                                     real_sell_signature=d['signature'],
                                     real_sol_received_lamports=all_sol,
                                     real_pnl_usd_before_network_fees=round(all_pnl,6),
                                     real_entry_sol_lamports=all_cost,
                                     modeled_exit_net_pct=trade.pop('net_pct',None),
                                     live_order_id=d['id'],
                                     live_order_ids=[x['live_order_id'] for x in parts],
                                     real_sell_chain_costs=d.get('chain_costs'),
                                     real_sell_vs_order_quote_pct=d.get('realized_vs_order_quote_pct'),
                                     real_partial_sells=parts)
                        trade['pnl_usd']=round(all_pnl,6)
                        # This is the USD conversion at each actual fill time.
                        trade['proceeds_usd']=round(sum(x['pnl_usd_before_network_fees']
                             +x['cost_basis_sol_lamports']/1e9*float(rec['real_entry_sol_usd'])
                             for x in parts),6)
                        total_wallet_change=0
                        wallet_complete=True
                        for part in parts:
                            fees=part.get('chain_costs')
                            if not fees:wallet_complete=False;break
                            total_wallet_change+=int(fees['wallet_sol_change_lamports'])
                        buy_costs=rec.get('real_buy_chain_costs')
                        if wallet_complete and buy_costs:
                            debit=-int(buy_costs['wallet_sol_change_lamports'])
                            credited_usd=sum(int(part['chain_costs']['wallet_sol_change_lamports'])
                                             /1e9*part['sale_sol_usd'] for part in parts)
                            trade['wallet_cash_flow_pnl_usd']=round(
                                credited_usd-debit/1e9*float(rec['real_entry_sol_usd']),6)
                            trade['wallet_cash_flow_includes_rent']=True
                        trade['network_fee_status']='verified' if wallet_complete and buy_costs else 'unavailable'
                        self.closed.append(trade)
                        self.closed=self.closed[-1000:]
            elif mint in self.positions:
                # A replay of a completed sale can never liquidate a new position.
                raise LiveError('already applied sell conflicts with open position')
        # A live ledger's cash is an on-chain SOL estimate, never a paper fill.
        # If the RPC is temporarily down, keep prior cash; next observation
        # refreshes it, and a new buy still requires an actual wallet balance.
        try:self.cash=self.broker.cash_usd()
        except LiveError:pass
        self.changed=True
        self.save()
        d['status']='applied';self.broker.journal.update(d)

    def observe(self,pair,*,sol_mint,dexes,now=None,surge_change=13.5,surge_volume=2500.):
        self.load()
        if self.read_only:return 'read_only'
        mint=(pair.get('baseToken') or {}).get('address') if isinstance(pair,dict) else None
        if self.broker.recover_pending():
            # Any unknown broadcast blocks further orders; never double-spend.
            return 'live_order_unresolved'
        held=mint in self.positions
        if held:
            inventory=self.reconcile_position(mint,now=now)
            if inventory.startswith('live_inventory_'):
                return inventory
            if inventory in ('inventory_mismatch','unresolved_journal','wallet_balance_inconsistent',
                             'invalid_recorded_position','wallet_balance_unavailable',
                             'wallet_zero_pending_confirmation'):
                return 'live_inventory_review_required'
        if held and self.positions[mint].get('pending_real_exit'):
            return self._sell(mint,self.positions[mint]['pending_real_exit'],None)
        if held:
            pos=self.positions[mint]
            # Verify the source pool identity; a different pool must not adjust
            # the held position's liquidity or override sell decisions.
            if pair.get('pairAddress') != pos['pool']:
                return 'different_pool'
            stamp=time.time() if now is None else now
            liq=(pair.get('liquidity') or {}).get('usd')
            try:liq=float(liq)
            except (TypeError,ValueError):liq=float('nan')
            # Emergency liquidity and time-based exits still apply even when
            # the live sell-price lookup fails (e.g. route vanished in a rug).
            emergency=(math.isfinite(liq) and liq>=0 and
                       liq <= float(pos['entry_liquidity'])*.60)
            expired=stamp-float(pos['opened_at'])>=2700
            try:
                net=self.broker.executable_exit_net_pct(
                    mint,int(pos['real_token_raw']),int(pos['real_spent_lamports']),
                    float(pos['real_entry_sol_usd']),
                    entry_network_fee_lamports=(
                        int(pos['real_buy_chain_costs'].get('network_fee_lamports',0))
                        if pos.get('real_buy_chain_costs') and
                           pos['real_buy_chain_costs'].get('fee_payer_is_wallet') else 0))
            except LiveError:
                self.note_missing_quote(mint,now=stamp)
                # No synthetic -6% cost deduction when Jupiter is unavailable.
                # Emergency liquidity collapse and the maximum hold duration
                # remain independently enforceable; stop-loss requires an
                # actual executable quote. Missing quote is not a real fill.
                if emergency or expired:
                    reason=('liquidity_drop_40pct' if emergency else
                            'expiry_45m')
                    pos['pending_real_exit']=reason
                    self.changed=True;self.save()
                    return self._sell(mint,reason,None)
                return 'live_exit_quote_unavailable'
            # Preserve AgeTrack's original thresholds/reversal logic. Its paper
            # net formula deducts 6% from gross USD change; compensate only
            # within a COPY so the final trigger sees actual quoted net return.
            # The original market discovery/price values remain untouched.
            from early_watch_shadow_v80 import SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT
            pair=copy.deepcopy(pair)
            pair['priceUsd']=float(pos['entry_price'])*max(1e-9,(1+(net+
                           SHADOW_ESTIMATED_ROUND_TRIP_COST_PCT)/100))
            if not pos.get('live_quote_peak_initialized'):
                pos['peak_net_pct']=-100.
                pos['live_quote_peak_initialized']=True
            pos['last_executable_quote_net_pct']=round(net,5)
            self.changed=True
        if not held:
            if not self.allow_new_buys:return 'live_new_buys_paused'
            # Apply only the explicitly selected historical exceptions; no
            # conditional clearance of other missing or partially held tokens.
            self.acknowledge_unexplained_zero_inventory(now=now)
            inventory_statuses=self.reconcile_all_positions(now=now)
            if any(label not in ('wallet_balance_verified','verified_recently',
                                 'legacy_broker_no_rpc','dust_still_economic')
                   for label in inventory_statuses):
                return 'live_new_buys_paused_inventory_verification'
            # Do not increase wallet exposure while a recorded exit has failed.
            # Already-held positions remain free to retry and be sold normally.
            if any(v.get('pending_real_exit') or v.get('inventory_reconciliation_required')
                   for v in self.positions.values()):
                return 'live_new_buys_paused_pending_exits'
            if any(x.get('auto_buy_blocking') for x in self.inventory_exceptions):
                return 'live_new_buys_paused_unreconciled_inventory'
            try:self.cash=self.broker.cash_usd()
            except LiveError:return 'live_cash_quote_unavailable'
            self.changed=True
        before={'positions':copy.deepcopy(self.positions),'closed':copy.deepcopy(self.closed),
                'cash':self.cash,'seen':set(self.seen),'open_count':self.open_count,
                'changed':self.changed,'halted':self.halted,'halt_reason':self.halt_reason}
        event=super().observe(pair,sol_mint=sol_mint,dexes=dexes,now=now,
                              surge_change=surge_change,surge_volume=surge_volume)
        if event=='opened':
            position=copy.deepcopy(self.positions[mint])
            self._restore(before)
            return self._buy(mint,position)
        if event.startswith('closed:'):
            closed=copy.deepcopy(self.closed[-1])
            self._restore(before)
            # Save an exit obligation even if the subsequent API quote fails.
            self.positions[mint]['pending_real_exit']=event.partition(':')[2]
            self.positions[mint]['last_exit_observation']=closed.get('closed_at')
            self.changed=True;self.save()
            return self._sell(mint,event.partition(':')[2],closed)
        return event

    def observe_live_wakeup(self, mint: str, pinned_pool: str, *, now=None):
        """Fast *quote-backed* live exit observation on an authenticated pool WS wake.

        The WS message is only a wakeup, NOT a price or a sell quote. Avoid the
        DexScreener HTTP round trip and immediately request a fresh executable
        Jupiter sell quote through the existing held-position observe path.
        Entry price and entry liquidity are used solely as a valid synthetic
        carrier for AgeTrack's existing held-position validation: the current
        sell price and profit come ONLY from broker.executable_exit_net_pct.
        A WS wake can never indicate a 40% liquidity drain; scheduled real-pool
        observations retain sole authority for liquidity-based exits.
        """
        self.load()
        if self.read_only: return 'read_only'
        pos=self.positions.get(mint)
        if not pos or pos.get('pool')!=pinned_pool: return 'not_held_pinned_pool'
        price=float(pos.get('entry_price') or 0.)
        liq=float(pos.get('entry_liquidity') or 0.)
        if not (math.isfinite(price) and price>0 and math.isfinite(liq) and liq>0):
            return 'invalid_held_quote_carrier'
        from shadow_age_tracks_v876 import AgeTrack
        # Synthetic data is NEVER a market update or independent price signal.
        # This label is restricted to the already-held path; no new mint entry.
        carrier={'chainId':'solana','dexId':'internal_live_quote_wakeup',
                 'pairAddress':pinned_pool,'baseToken':{'address':mint},
                 'quoteToken':{'address':SOL_MINT},'priceUsd':price,
                 'liquidity':{'usd':liq}}
        return self.observe(carrier,sol_mint=SOL_MINT,
                            dexes={'internal_live_quote_wakeup'},now=now)

    def _restore(self,before):
        for key,val in before.items():setattr(self,key,val)

    def _buy(self,mint,position):
        try:
            sol_price=self.broker.sol_usd()
            sol_lamports=round(self.size/sol_price*1e9)
            receipt,d=self.broker.execute(action='buy',mint=mint,amount_raw=sol_lamports,
                                          proposal={'position':position},price=sol_price)
            self._apply_confirmed(d)
            LOG.info('LIVE 0_60 BUY confirmed mint=%s sig=%s token_raw=%d',
                     mint,receipt.signature,receipt.output_raw)
            return 'opened'
        except LiveError as exc:
            LOG.warning('LIVE 0_60 BUY skipped mint=%s type=%s',mint,type(exc).__name__)
            return 'live_buy_not_filled'

    def _sell(self,mint,reason,closed):
        pos=self.positions[mint]
        if closed is None:
            closed={'mint':mint,'symbol':pos.get('symbol',''),'pool':pos['pool'],
                    'opened_at':pos['opened_at'],'closed_at':time.time(),
                    'entry_age_minutes':pos['entry_age_minutes'],
                    'entry_market_cap_usd':pos.get('entry_market_cap_usd'),
                    'entry_volume5_usd':pos.get('entry_volume5_usd'),
                    'entry_buys5':pos.get('entry_buys5'),
                    'entry_sells5':pos.get('entry_sells5'),
                    'entry_change5_pct':pos.get('entry_change5_pct'),
                    'entry_5m_partial':pos['entry_5m_partial'],
                    'minutes_held':round((time.time()-pos['opened_at'])/60,3),
                    'reason':reason,'invested_usd':self.size,'net_pct':pos.get('last_net_pct'),
                    'observations':pos.get('observations',0)}
        try:
            # Sell exactly the bot's recorded quantity, not unrelated wallet tokens.
            price=self.broker.sol_usd()
            receipt,d=self.broker.execute(action='sell',mint=mint,
                         amount_raw=int(pos['real_token_raw']),
                         proposal={'position':copy.deepcopy(pos),'closed':closed},price=price)
            self._apply_confirmed(d)
            LOG.info('LIVE 0_60 SELL confirmed mint=%s sig=%s reason=%s real_sol=%0.9f',
                     mint,receipt.signature,reason,receipt.output_raw/1e9)
            return 'closed:'+reason
        except LiveError as exc:
            LOG.warning('LIVE 0_60 SELL pending mint=%s reason=%s type=%s error_code=%s position_remains_open=True',
                        mint,reason,type(exc).__name__,safe_live_error_code(exc))
            return 'live_sell_pending'
