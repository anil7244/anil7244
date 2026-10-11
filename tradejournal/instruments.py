"""Turn the many broker symbol formats into one canonical instrument.

Handles:
  NIFTY24OCT24000CE      Zerodha/NSE monthly option
  NIFTY2410324000CE      Zerodha/NSE weekly option (YY + month code 1-9/O/N/D + DD)
  BANKNIFTY24DECFUT      monthly future
  NIFTY 26 DEC 24000 CALL, NIFTY 24000 CE 26 DEC 24, BANKNIFTY-Dec2024-50000-CE
                         spaced / hyphenated formats used by Dhan and Upstox
plus explicit expiry/strike/option-type columns when a broker provides them.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
WEEKLY_MONTH_CODE = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
_MON = "|".join(MONTHS)

COMPACT_MONTHLY = re.compile(
    rf"^(?P<u>[A-Z][A-Z0-9&_\-]*?)(?P<yy>\d{{2}})(?P<mon>{_MON})"
    r"(?:(?P<fut>FUT)|(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE))$"
)
COMPACT_WEEKLY = re.compile(
    r"^(?P<u>[A-Z][A-Z&_\-]*?)(?P<yy>\d{2})(?P<m>[1-9OND])(?P<dd>\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<opt>CE|PE)$"
)
SPACED_DATE = re.compile(
    rf"(?:(?P<dd>\d{{1,2}})[\s\-/]*)?(?P<mon>{_MON})[A-Z]*[\s\-/]*(?P<yy>\d{{4}}|\d{{2}})?(?!\d)"
)
KIND_WORDS = {"CE": "CE", "CALL": "CE", "C": "CE", "PE": "PE", "PUT": "PE", "P": "PE",
              "FUT": "FUT", "FUTURE": "FUT", "FUTURES": "FUT", "FUTIDX": "FUT", "FUTSTK": "FUT",
              "FUTCOM": "FUT", "FUTCUR": "FUT"}
EQ_SUFFIXES = ("-EQ", "-BE", "-BZ", "-SM", "-ST")
# NSE moved monthly expiry from Thursday to Tuesday from Sep 2025.
EXPIRY_WEEKDAY_SWITCH = date(2025, 9, 1)


@dataclass(frozen=True)
class Instrument:
    underlying: str
    kind: str                    # EQ, FUT, CE, PE
    expiry: date | None = None
    strike: float | None = None
    expiry_exact: bool = True    # False when only the expiry month was known

    @property
    def is_option(self) -> bool:
        return self.kind in ("CE", "PE")

    @property
    def is_derivative(self) -> bool:
        return self.kind in ("CE", "PE", "FUT")

    @property
    def key(self) -> str:
        exp = self.expiry.isoformat() if self.expiry else ""
        return f"{self.underlying}|{self.kind}|{exp}|{fmt_strike(self.strike)}"

    @property
    def display(self) -> str:
        if self.kind == "EQ":
            return self.underlying
        exp = self.expiry.strftime("%d-%b-%Y") if self.expiry else "?"
        if self.kind == "FUT":
            return f"{self.underlying} {exp} FUT"
        return f"{self.underlying} {exp} {fmt_strike(self.strike)} {self.kind}"

    def with_expiry(self, expiry: date, exact: bool = True) -> "Instrument":
        return Instrument(self.underlying, self.kind, expiry, self.strike, exact)


def fmt_strike(strike: float | None) -> str:
    if strike is None:
        return ""
    return ("%f" % float(strike)).rstrip("0").rstrip(".")


def monthly_expiry(year: int, month: int) -> date:
    """Estimated monthly expiry (last Thursday, last Tuesday from Sep 2025). Ignores holidays."""
    weekday = 1 if date(year, month, 1) >= EXPIRY_WEEKDAY_SWITCH else 3
    last = date(year, month, calendar.monthrange(year, month)[1])
    return date(year, month, last.day - ((last.weekday() - weekday) % 7))


def _year(yy: str) -> int:
    y = int(yy)
    return y + 2000 if y < 100 else y


def parse_date(value) -> date | None:
    """Parse the date formats Indian brokers use; day-first when ambiguous."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.upper() in ("NA", "N/A", "NAN", "NONE", "NAT", "-", "0"):
        return None
    dt = parse_datetime(s)
    return dt.date() if dt else None


_DT_FORMATS = [
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %b %y",
    "%d%b%Y", "%d%b%y", "%b %d, %Y", "%d %B %Y", "%d-%B-%Y", "%Y%m%d",
]


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"(\+\d{2}:?\d{2}|Z)$", "", s).strip()   # drop timezone suffix
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Excel serial numbers
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        from datetime import timedelta
        return datetime(1899, 12, 30) + timedelta(days=float(s))
    return None


def parse_time(value) -> tuple[int, int, int] | None:
    if value is None:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?", str(value))
    if not m:
        return None
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if m.group(4):
        h = h % 12 + (12 if m.group(4).upper() == "PM" else 0)
    return h, mi, se


def norm_kind(value) -> str | None:
    if value is None:
        return None
    s = re.sub(r"[^A-Z]", "", str(value).upper())
    if s.startswith("OPT"):
        return None  # OPTIDX/OPTSTK: option, but side comes from another column
    return KIND_WORDS.get(s)


def _parse_compact(sym: str) -> Instrument | None:
    m = COMPACT_MONTHLY.match(sym)
    if m:
        year, month = _year(m["yy"]), MONTH_NUM[m["mon"]]
        exp = monthly_expiry(year, month)
        if m["fut"]:
            return Instrument(m["u"], "FUT", exp, None, expiry_exact=False)
        return Instrument(m["u"], m["opt"], exp, float(m["strike"]), expiry_exact=False)
    m = COMPACT_WEEKLY.match(sym)
    if m:
        try:
            exp = date(_year(m["yy"]), WEEKLY_MONTH_CODE[m["m"]], int(m["dd"]))
        except ValueError:
            return None
        return Instrument(m["u"], m["opt"], exp, float(m["strike"]), expiry_exact=True)
    return None


def _parse_spaced(sym: str, trade_date: date | None) -> Instrument | None:
    tokens = sym.split()
    if len(tokens) == 1 and "-" in sym:
        tokens = sym.split("-")
    if len(tokens) < 2:
        return None

    def is_marker(tok: str) -> bool:
        return (tok in KIND_WORDS and len(tok) > 1) or bool(re.match(r"^\d", tok)) or \
            bool(re.match(rf"^({_MON})[A-Z]*\d*$", tok))

    lead = []
    for tok in tokens:
        if lead and is_marker(tok):
            break
        lead.append(tok)
    underlying = "-".join(lead) if "-" in sym and len(sym.split()) == 1 else " ".join(lead)
    rest_tokens = tokens[len(lead):]
    if not rest_tokens:
        return None

    kind = None
    others = []
    for tok in rest_tokens:
        k = KIND_WORDS.get(tok) if len(tok) > 1 else None
        if k and kind is None:
            kind = k
        else:
            others.append(tok)
    if kind is None:
        return None

    rest = " ".join(others)
    expiry, exact = None, False
    dm = SPACED_DATE.search(rest)
    if dm:
        month = MONTH_NUM[dm["mon"]]
        if dm["yy"]:
            year = _year(dm["yy"])
        elif trade_date:
            year = trade_date.year + (1 if month < trade_date.month else 0)
        else:
            year = None
        if year:
            if dm["dd"]:
                try:
                    expiry, exact = date(year, month, int(dm["dd"])), True
                except ValueError:
                    expiry = None
            if expiry is None:
                expiry, exact = monthly_expiry(year, month), False
        rest = rest[:dm.start()] + " " + rest[dm.end():]

    strike = None
    if kind in ("CE", "PE"):
        nums = re.findall(r"\d+(?:\.\d+)?", rest)
        if nums:
            strike = float(max(nums, key=lambda n: float(n)))
    return Instrument(underlying, kind, expiry, strike, exact)


def parse_instrument(symbol: str, *, segment: str | None = None, expiry=None, strike=None,
                     option_type=None, instrument_type=None, trade_date: date | None = None) -> Instrument:
    """Best-effort canonical instrument from a symbol plus any explicit columns."""
    sym = re.sub(r"\s+", " ", str(symbol or "").upper()).strip()
    exp_hint = parse_date(expiry)
    try:
        strike_hint = float(str(strike).replace(",", "")) if strike not in (None, "") else None
    except ValueError:
        strike_hint = None
    if strike_hint is not None and strike_hint <= 0:
        strike_hint = None
    kind_hint = norm_kind(option_type) or norm_kind(instrument_type)
    if kind_hint is None and instrument_type and str(instrument_type).upper().startswith("FUT"):
        kind_hint = "FUT"

    inst = _parse_compact(sym.replace(" ", "")) or _parse_spaced(sym, trade_date)

    if inst is None:
        derivative_hint = kind_hint is not None or exp_hint is not None or \
            (segment or "").upper() in ("FO", "COM", "CD")
        underlying = sym.split(" ")[0] if derivative_hint else sym
        for suf in EQ_SUFFIXES:
            if underlying.endswith(suf):
                underlying = underlying[: -len(suf)]
        if not derivative_hint:
            return Instrument(underlying or "UNKNOWN", "EQ")
        kind = kind_hint or ("CE" if strike_hint else "FUT")
        inst = Instrument(underlying or "UNKNOWN", kind, None, strike_hint if kind != "FUT" else None, False)

    # Explicit columns beat what we inferred from the symbol.
    if kind_hint and kind_hint != inst.kind and not (inst.kind == "FUT" and strike_hint is None):
        inst = Instrument(inst.underlying, kind_hint, inst.expiry, inst.strike, inst.expiry_exact)
    if strike_hint is not None and inst.is_option:
        inst = Instrument(inst.underlying, inst.kind, inst.expiry, strike_hint, inst.expiry_exact)
    if exp_hint is not None and inst.is_derivative:
        inst = inst.with_expiry(exp_hint, exact=True)
    return inst


def norm_exchange_segment(exchange: str | None, segment: str | None, inst: Instrument) -> tuple[str, str]:
    """Return (exchange, segment) as e.g. ('NSE', 'FO')."""
    ex = (exchange or "").upper().replace(" ", "")
    seg = (segment or "").upper().replace(" ", "")
    mapping = {
        "NFO": ("NSE", "FO"), "BFO": ("BSE", "FO"), "NSE_FNO": ("NSE", "FO"), "BSE_FNO": ("BSE", "FO"),
        "NSEFO": ("NSE", "FO"), "BSEFO": ("BSE", "FO"), "NSE_FO": ("NSE", "FO"), "BSE_FO": ("BSE", "FO"),
        "NSE_EQ": ("NSE", "EQ"), "BSE_EQ": ("BSE", "EQ"), "CDS": ("NSE", "CD"), "BCD": ("BSE", "CD"),
        "NSE_CURRENCY": ("NSE", "CD"), "BSE_CURRENCY": ("BSE", "CD"), "NCO": ("NSE", "COM"),
        "MCX": ("MCX", "COM"), "MCX_COMM": ("MCX", "COM"), "MCX_FO": ("MCX", "COM"),
        "NCDEX": ("NCDEX", "COM"),
    }
    if ex in mapping:
        out_ex, out_seg = mapping[ex]
    else:
        out_ex = ex if ex in ("NSE", "BSE") else (ex or "NSE")
        out_seg = ""
    if not out_seg:
        if seg in ("FO", "FNO", "F&O", "NFO", "DERIVATIVES", "FUTURESANDOPTIONS", "EQUITYDERIVATIVES"):
            out_seg = "FO"
        elif seg in ("EQ", "EQUITY", "CASH", "CM"):
            out_seg = "EQ"
        elif seg in ("CD", "CDS", "CURRENCY", "CUR"):
            out_seg = "CD"
        elif seg in ("COM", "COMMODITY", "MCX"):
            out_seg = "COM"
        else:
            out_seg = "FO" if inst.is_derivative else "EQ"
    if out_seg == "EQ" and inst.is_derivative:
        out_seg = "FO"
    return out_ex, out_seg
