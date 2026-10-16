"""Indicative Pump.fun bonding-curve constant-product quotes (not executable guarantees)."""

def buy_quote(curve: dict, sol_in: float, fee_fraction: float = .01) -> dict | None:
    vt = curve.get('virtual_token_raw', 0)
    vs = curve.get('virtual_sol_lamports', 0)
    rt = curve.get('real_token_raw', 0)
    if min(vt, vs, rt) <= 0 or sol_in <= 0 or not 0 <= fee_fraction < 1 or curve.get('complete'):
        return None
    in_raw = sol_in * 1e9 * (1 - fee_fraction)
    tokens = vt * in_raw / (vs + in_raw)
    if tokens <= 0 or tokens > rt:
        return None
    impact = in_raw / (vs + in_raw)
    return {'token_raw': tokens, 'impact': impact, 'sol_in': sol_in}


def sell_quote(curve: dict, token_raw: float, fee_fraction: float = .01) -> dict | None:
    vt = curve.get('virtual_token_raw', 0)
    vs = curve.get('virtual_sol_lamports', 0)
    real_sol = curve.get('real_sol_lamports', 0)
    if min(vt, vs, real_sol) <= 0 or token_raw <= 0 or not 0 <= fee_fraction < 1 or curve.get('complete'):
        return None
    gross = vs * token_raw / (vt + token_raw)
    net = gross * (1 - fee_fraction)
    if net > real_sol:
        return None
    return {'sol_out': net / 1e9, 'impact': token_raw / (vt + token_raw)}
