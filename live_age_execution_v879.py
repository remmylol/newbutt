"""Opt-in automated SOL<->SPL execution for the UNCHANGED promoted AgeTrack.

The original AgeTrack.observe is used to decide both entries and exits.  The
transport and real-money accounting are separate.  Without explicit opt-in no
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
        if not order.get('transaction') or not order.get('requestId'):
            raise LiveError('Jupiter cannot build route; skip order (no synthetic fill)')
        signed,signature=self._sign(order['transaction'])
        d=self.journal.begin(action=action,mint=mint,proposal=proposal,
                             price=price,before_raw=before_token)
        d.update(status='signed',signature=signature,request_id=order['requestId'],
                 amount_raw=amount_raw,before_sol_lamports=before_sol)
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
            d['status']='uncertain' if outcome.get('signature') else 'failed'
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
        d.update(status='confirmed',filled_input_raw=spent,filled_output_raw=received,
                 execution_status='Success')
        self.journal.update(d) # persist receipt before ledger update
        return RealReceipt(signature,spent,received,price),d

    def recover_pending(self):
        """Expose unresolved states; confirmed-but-unapplied can be replayed."""
        return self.journal.pending()


class LiveAgeTrack(AgeTrack):
    """Run exactly AgeTrack.observe for signals; commit only REAL confirmed fills."""
    def __init__(self,*,broker:JupiterAutoBroker,**kwargs):
        super().__init__(**kwargs)
        self.broker=broker
        self._recovering=False

    def load(self):
        if self.loaded:return
        super().load()
        if self.read_only:return
        # We can deterministically finish a confirmed swap that was journaled
        # before a crash, without rebroadcasting the transaction.
        for entry in self.broker.recover_pending():
            if entry['status']=='confirmed':
                self._apply_confirmed(entry)
            else:
                raise LiveError('unresolved live order '+entry['id']+'; no automatic re-broadcast')
        self.cash=self.broker.cash_usd()
        self.changed=True
        self.save()

    def _apply_confirmed(self,d):
        mint=d['mint']
        proposal=d['proposal']
        if d['action']=='buy':
            if mint not in self.positions:
                rec=copy.deepcopy(proposal['position'])
                rec.update(real_token_raw=int(d['filled_output_raw']),
                           real_spent_lamports=int(d['filled_input_raw']),
                           real_entry_sol_usd=float(d['sol_usd']),
                           real_buy_signature=d['signature'])
                self.positions[mint]=rec
                self.seen.add(mint);self.open_count+=1
        else:
            if mint in self.positions or not any(x.get('live_order_id')==d['id'] for x in self.closed):
                rec=self.positions.pop(mint,None)
                trade=copy.deepcopy(proposal['closed'])
                actual_sol_received=int(d['filled_output_raw'])
                token_entry=proposal['position']
                cost_sol=int(token_entry['real_spent_lamports'])
                pnl_usd=(actual_sol_received/1e9*float(d['sol_usd'])-
                         cost_sol/1e9*float(token_entry['real_entry_sol_usd']))
                trade.update(execution='jupiter_swap_v2_confirmed',
                             real_sell_signature=d['signature'],
                             real_sol_received_lamports=actual_sol_received,
                             real_pnl_usd_before_network_fees=round(pnl_usd,6),
                             real_entry_sol_lamports=cost_sol,
                             modeled_exit_net_pct=trade.pop('net_pct'),
                             live_order_id=d['id'])
                trade['pnl_usd']=round(pnl_usd,6)
                trade['proceeds_usd']=round(actual_sol_received/1e9*float(d['sol_usd']),6)
                self.closed.append(trade)
                self.closed=self.closed[-1000:]
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
        if held and self.positions[mint].get('pending_real_exit'):
            return self._sell(mint,self.positions[mint]['pending_real_exit'],None)
        if not held:
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
            LOG.warning('LIVE 0_60 SELL pending mint=%s reason=%s type=%s',
                        mint,reason,type(exc).__name__)
            return 'live_sell_pending'
