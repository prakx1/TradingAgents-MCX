"""MCX contract specifications and symbol registry.

The original Chinese system supported 50+ commodities across SHFE/DCE/CZCE/INE.
This version focuses on MCX energy contracts, primarily Crude Oil.
"""

CONTRACTS = {
    "CRUDEOIL": {
        "name": "MCX Crude Oil",
        "exchange": "MCX",
        "category": "Energy",
        "underlying": "NYMEX WTI Light Sweet Crude Oil",
        "lot_size_bbl": 100,
        "quote_unit": "INR per barrel",
        "tick_size_inr": 1.0,            # Rs 1/bbl => Rs 100 per lot per tick
        "tick_value_inr": 100.0,
        "settlement": "Cash settled: NYMEX WTI front-month settlement on expiry x RBI USDINR reference rate",
        "expiry_rule": "Approx. 19th-20th of contract month (one business day before NYMEX WTI expiry)",
        "trading_hours_ist": "Mon-Fri 09:00-23:30 (US DST) / 09:00-23:55 (US winter)",
        "typical_margin_pct": 15.0,      # SPAN + exposure, varies; crude often 12-20%
        "daily_price_limit_pct": 9.0,    # base DPR, expandable per MCX rules
        "proxy_symbol_yf": "CL=F",       # WTI front month on Yahoo Finance
        "fx_symbol_yf": "USDINR=X",
    },
    "CRUDEOILM": {
        "name": "MCX Crude Oil Mini",
        "exchange": "MCX",
        "category": "Energy",
        "underlying": "NYMEX WTI Light Sweet Crude Oil",
        "lot_size_bbl": 10,
        "quote_unit": "INR per barrel",
        "tick_size_inr": 1.0,
        "tick_value_inr": 10.0,
        "settlement": "Cash settled like CRUDEOIL",
        "expiry_rule": "Same cycle as CRUDEOIL",
        "trading_hours_ist": "Mon-Fri 09:00-23:30 (US DST) / 09:00-23:55 (US winter)",
        "typical_margin_pct": 30.0,
        "daily_price_limit_pct": 9.0,
        "proxy_symbol_yf": "CL=F",
        "fx_symbol_yf": "USDINR=X",
    },
}

DEFAULT_SYMBOL = "CRUDEOIL"


def contract(symbol: str) -> dict:
    try:
        return CONTRACTS[symbol.upper()]
    except KeyError:
        raise KeyError(f"Unknown symbol {symbol!r}. Supported: {', '.join(CONTRACTS)}")


def fair_value_inr(wti_usd: float, usdinr: float) -> float:
    """MCX crude fair value: WTI (USD/bbl) x USDINR. MCX settles at exactly this on expiry."""
    return wti_usd * usdinr
