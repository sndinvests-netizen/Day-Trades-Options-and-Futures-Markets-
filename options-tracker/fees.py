"""Robinhood trading fees for opening and closing trades (rates as of 2026).

Commission is $0 for stock and ETF options. What Robinhood passes on, per its Help Center:
  Options, every order:   $0.04 per contract (OCC/exchange regulatory fees, combined)
                          CAT fee $0.0003 per contract (under 1 cent rounds to $0)
  Options, sell orders:   SEC fee $20.60 per $1M of premium, rounded up to the penny
                          FINRA TAF $0.00329 per contract, rounded to the penny, max $9.79
  Index options (SPX, NDX, XSP, ...): $0.50 per contract ($0.35 with Gold) plus the $0.04
                          and CAT fees, no SEC fee or TAF. Per-symbol exchange index fees
                          are not included.
  Stock, sell orders:     SEC fee (waived at $500 or less), TAF $0.000195 per share
                          (waived at 50 shares or less, max $9.79); CAT $0.000003 per share.
Options that expire worthless have no closing fees.
"""
import math
from decimal import ROUND_HALF_UP, Decimal

SEC_RATE = 20.60 / 1_000_000
TAF_OPTION = 0.00329
TAF_SHARE = 0.000195
TAF_MAX = 9.79
OPTION_REG = 0.04
CAT_OPTION = 0.0003
CAT_SHARE = 0.000003
INDEX_CONTRACT = 0.50
INDEX_CONTRACT_GOLD = 0.35
INDEX_OPTIONS = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "XND", "RUT", "RUTW", "MRUT", "VIX", "VIXW",
                 "DJX", "OEX", "XEO"}
GOLD = False  # set True if you have Robinhood Gold (index option contract fee $0.35)


def penny(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def penny_up(x):
    return math.ceil(round(x * 100, 6)) / 100


def cat(x):
    return 0.0 if x < 0.01 else penny(x)


def leg_fees(ticker, type, action, price, contracts):
    """Fees for one leg of an order. action is "buy" or "sell" (the order side now,
    so closing a short option is a buy). Returns (total, {fee name: amount})."""
    n = contracts
    parts = {}
    if type == "stock":
        shares = n * 100
        notional = price * shares
        if action == "sell":
            if notional > 500:
                parts["SEC"] = penny_up(notional * SEC_RATE)
            if shares > 50:
                parts["TAF"] = min(penny(shares * TAF_SHARE), TAF_MAX)
        parts["CAT"] = cat(shares * CAT_SHARE)
    else:
        if ticker.upper() in INDEX_OPTIONS:
            parts["Index contract"] = penny(n * (INDEX_CONTRACT_GOLD if GOLD else INDEX_CONTRACT))
        elif action == "sell":
            parts["SEC"] = penny_up(price * 100 * n * SEC_RATE)
            parts["TAF"] = min(penny(n * TAF_OPTION), TAF_MAX)
        parts["OCC/exchange"] = penny(n * OPTION_REG)
        parts["CAT"] = cat(n * CAT_OPTION)
    parts = {k: v for k, v in parts.items() if v}
    return round(sum(parts.values()), 2), parts


def order_fees(ticker, legs):
    """Total fees for an order: legs = [{type, action, price, contracts}].
    Returns (total, {fee name: amount}) with the same fees added across legs."""
    total, parts = 0.0, {}
    for leg in legs:
        t, p = leg_fees(ticker, leg["type"], leg["action"], leg["price"] or 0, leg["contracts"])
        total += t
        for k, v in p.items():
            parts[k] = round(parts.get(k, 0) + v, 2)
    return round(total, 2), parts
