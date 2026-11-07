"""Market news and geopolitics from public RSS feeds, each headline linked to the sectors,
stocks or crypto it can move.

The link is rule-based (keywords -> sectors/stocks), so it is consistent and never invented:
it says what the story is ABOUT and who usually reacts, not which way prices will go.
Feeds that fail are skipped; the rest still work.
"""
from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PAGE = 10
MAX_ITEMS = 50
MAX_AGE_H = 48
UA = {"User-Agent": "Mozilla/5.0 (compatible; ShivamTraderBot/1.0; +https://github.com)"}
LINE = "━━━━━━━━━━━━━━━━━━"


def google_news(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"


FEEDS = {
    "india": [
        ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
        ("Economic Times", "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
        ("Mint", "https://www.livemint.com/rss/markets"),
        ("Google News", google_news("(Sensex OR Nifty OR RBI OR SEBI) when:1d")),
    ],
    "world": [
        ("Google News", google_news("(geopolitics OR war OR sanctions OR tariffs OR OPEC OR \"Federal Reserve\") "
                                    "markets when:1d")),
        ("Google News", google_news("(\"crude oil\" OR \"US Fed\" OR \"dollar index\" OR \"bond yields\") when:1d")),
        ("Economic Times", "https://economictimes.indiatimes.com/news/international/rssfeeds/858478126.cms"),
        ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ],
    "crypto": [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Economic Times", "https://economictimes.indiatimes.com/markets/cryptocurrency/rssfeeds/82519373.cms"),
    ],
}
CATEGORIES = {"all": "🌍 All", "india": "🇮🇳 India", "world": "🌐 World", "crypto": "🪙 Crypto"}


# ---------------------------------------------------------------- what moves what

@dataclass(frozen=True)
class Rule:
    key: str
    pattern: str
    link: str          # short: who reacts
    why: str           # one-sentence explanation for the detail view
    weight: int
    stocks: tuple = ()
    crypto: tuple = ()


RULES = [
    Rule("crude", r"crude|brent|\bwti\b|\bopec\+?|oil prices?|oil output", "oil & gas (ONGC, Reliance), OMCs (BPCL, IOC), "
         "airlines, paints and the rupee", "Oil prices change costs for fuel retailers, airlines and paint makers and "
         "India's import bill (and so the rupee); oil producers such as ONGC gain when prices rise.", 3,
         ("ONGC", "BPCL", "IOC", "RELIANCE")),
    Rule("rbi", r"\brbi\b|repo rate|reserve bank of india|\bmpc\b|governor malhotra",
         "banks & NBFCs (HDFC Bank, SBI, Bajaj Finance), BANKNIFTY, realty and autos",
         "Interest-rate and liquidity decisions change borrowing costs, bank margins and loan demand, so banks, NBFCs "
         "and rate-sensitive sectors react first.", 3, ("HDFCBANK", "SBIN", "BAJFINANCE")),
    Rule("fed", r"\bfed\b|federal reserve|\bfomc\b|powell|treasury yields?|bond yields?|us inflation|us cpi|"
         r"us jobs|payrolls", "foreign flows into NIFTY, IT stocks and crypto (BTC, ETH)",
         "US interest rates set the global cost of money: higher US yields tend to pull foreign money out of Indian "
         "stocks and risk assets like crypto; lower yields tend to do the opposite.", 3, ("INFY", "TCS"),
         ("BTC", "ETH")),
    Rule("war", r"\bwar\b|missiles?|air ?strikes?|drone attack|military|troops|ceasefire|invasion|conflict|"
         r"israel|iran|gaza|hamas|hezbollah|houthi|red sea|strait of hormuz|russia|ukraine|taiwan|north korea|"
         r"geopolitic\w*", "risk sentiment: NIFTY volatility (India VIX), crude, gold, defence stocks (HAL, BEL) "
         "and crypto", "Conflicts raise uncertainty: investors often cut risky positions, oil and gold can jump, and "
         "defence stocks get attention; a ceasefire usually eases this.", 3, ("HAL", "BEL"), ("BTC",)),
    Rule("tariff", r"tariffs?|trade war|trade deal|trade talks|sanctions?|export curbs?|import duty|customs duty|"
         r"\bwto\b", "exporters (IT, pharma, textiles, auto parts), metals and global risk sentiment",
         "Tariffs and sanctions change what exporters earn and what imports cost, and trade fights hit global growth "
         "expectations.", 3, ("TCS", "SUNPHARMA", "TATASTEEL")),
    Rule("crypto", r"bitcoin|\bbtc\b|crypto\w*|ethereum|\bether\b|\beth\b|solana|\bxrp\b|binance|coinbase|"
         r"stablecoins?|blockchain|altcoins?|memecoins?|spot etf", "the crypto market (BTC, ETH, altcoins)",
         "Crypto prices react quickly to regulation, ETF flows, exchange news and global risk appetite.", 2, (),
         ("BTC", "ETH")),
    Rule("rupee", r"\brupee\b|\binr\b|dollar index|\bdxy\b|forex reserves", "IT and pharma exporters (TCS, Infosys, "
         "Sun Pharma) vs importers (oil, electronics)", "A weaker rupee lifts exporters' earnings in rupee terms and "
         "makes imports such as oil costlier; a stronger rupee does the opposite.", 2,
         ("TCS", "INFY", "SUNPHARMA")),
    Rule("flows", r"\bfiis?\b|\bfpis?\b|foreign investors?|foreign portfolio|\bdiis?\b|institutional investors?|"
         r"fund flows?|outflows?|inflows?", "NIFTY and BANKNIFTY direction (large-cap flows)",
         "Large fund flows move big stocks and the indices, so steady buying or selling shows up in NIFTY.", 2),
    Rule("gold", r"\bgold\b|\bsilver\b|bullion", "gold-linked stocks (Titan, Muthoot Finance) and gold ETFs",
         "Gold and silver prices move jewellers and gold-loan companies; a gold rally often signals investors "
         "seeking safety.", 2, ("TITAN", "MUTHOOTFIN")),
    Rule("macro", r"\bgdp\b|\bcpi\b|\bwpi\b|inflation|\biip\b|\bpmi\b|fiscal deficit|union budget|\bbudget\b|\bgst\b",
         "the broad market (NIFTY), banks and consumer stocks", "Growth, inflation and tax data shape the RBI's next "
         "move and company earnings, which feeds into the whole market.", 2),
    Rule("sebi", r"\bsebi\b|f&o|derivatives?|futures and options|weekly expiry|lot size",
         "F&O traders, brokers and exchanges (BSE, Angel One, CDSL)", "Rule changes for trading change volumes, "
         "which matter most for brokers and exchanges and for F&O traders' costs.", 2,
         ("BSE", "ANGELONE", "CDSL")),
    Rule("it", r"\bit (services|stocks?|sector|firms?|companies)\b|nifty it|tech spending|h-?1b|outsourcing|"
         r"accenture|\bai spending|artificial intelligence", "IT stocks (TCS, Infosys, Wipro, HCLTech)",
         "Indian IT earns mostly from US and European clients, so their tech budgets, visa rules and AI spending "
         "drive these stocks.", 2, ("TCS", "INFY", "WIPRO", "HCLTECH")),
    Rule("pharma", r"pharma\w*|usfda|\bfda\b|drug|vaccine|hospitals?", "pharma & healthcare (Sun Pharma, Dr Reddy's, "
         "Cipla)", "US FDA decisions, drug pricing and approvals directly change pharma companies' sales.", 2,
         ("SUNPHARMA", "DRREDDY", "CIPLA")),
    Rule("auto", r"auto sales|car sales|two-wheelers?|vehicle sales|\bevs?\b|electric vehicles?|automakers?|"
         r"carmakers?|passenger vehicles?", "auto stocks (Maruti, Tata Motors, M&M)",
         "Monthly sales, EV policy and input costs drive carmakers' earnings expectations.", 2,
         ("MARUTI", "TATAMOTORS", "M&M")),
    Rule("metal", r"\bsteel\b|\bmetals?\b|iron ore|copper|alumini?um|\bzinc\b|china stimulus|china demand",
         "metal stocks (Tata Steel, JSW Steel, Hindalco)", "Metal prices are global and depend heavily on Chinese "
         "demand, so they move Indian metal producers directly.", 2, ("TATASTEEL", "JSWSTEEL", "HINDALCO")),
    Rule("bank", r"\bbanks?\b|banking|\bnpas?\b|bad loans|credit growth|deposits?|lenders?|\bnbfcs?\b",
         "banks (HDFC Bank, ICICI Bank, SBI) and BANKNIFTY", "Loan growth, bad loans and deposit costs decide bank "
         "profits, and banks are the biggest weight in NIFTY.", 2, ("HDFCBANK", "ICICIBANK", "SBIN")),
    Rule("monsoon", r"monsoon|rainfall|kharif|\brabi\b|crops?|farm|agri\w*|food inflation",
         "FMCG (HUL, ITC), tractors (M&M, Escorts) and rural demand", "Rain and crop output decide rural incomes, "
         "which drive sales of FMCG products, two-wheelers and tractors.", 2, ("HINDUNILVR", "ITC", "M&M")),
    Rule("telecom", r"telecom|5g|\bjio\b|airtel|vodafone idea|tariff hike", "telecom (Bharti Airtel, Vodafone Idea, "
         "Reliance)", "Tariff changes and spectrum costs directly change telecom companies' revenue per user.", 2,
         ("BHARTIARTL", "IDEA", "RELIANCE")),
    Rule("power", r"power demand|electricity|\bcoal\b|renewables?|solar|green energy|power sector",
         "power & energy (NTPC, Power Grid, Tata Power, Coal India)", "Power demand and fuel costs drive earnings of "
         "power producers, grids and coal miners.", 2, ("NTPC", "POWERGRID", "TATAPOWER", "COALINDIA")),
    Rule("defence", r"defen[cs]e|fighter jets?|\bdrdo\b|military orders?", "defence stocks (HAL, BEL, BDL)",
         "Defence budgets and orders go mainly to a few listed companies, so they react strongly.", 2,
         ("HAL", "BEL", "BDL")),
    Rule("realty", r"real estate|realty|housing|home sales|property prices", "realty (DLF, Godrej Properties) and "
         "home lenders", "Home sales and loan rates drive developers' bookings and housing-finance demand.", 1,
         ("DLF", "GODREJPROP")),
    Rule("infra", r"infrastructure|\bcapex\b|cement|highways?|railways?", "infra & cement (L&T, UltraTech)",
         "Government and private capital spending turns into orders for builders and cement makers.", 1,
         ("LT", "ULTRACEMCO")),
    Rule("ipo", r"\bipos?\b|listing|public issue|\bqip\b|\bofs\b|block deal", "the primary market, exchanges and the "
         "stock being issued", "Big share sales can pull money from other stocks for a while and show how strong "
         "investor demand is.", 1),
    Rule("global", r"wall street|nasdaq|s&p 500|dow jones|us stocks|asian markets|global markets",
         "global cues for NIFTY's opening, IT stocks and crypto", "Overseas markets set the mood for Indian markets' "
         "opening and for global risk assets such as crypto.", 1, ("INFY", "TCS"), ("BTC",)),
]
_RULES_RX = [(r, re.compile(r.pattern, re.I)) for r in RULES]

# Company names that appear in headlines -> NSE symbol
COMPANIES = {
    "reliance": "RELIANCE", "tcs": "TCS", "tata consultancy": "TCS", "infosys": "INFY", "hdfc bank": "HDFCBANK",
    "icici bank": "ICICIBANK", "state bank of india": "SBIN", "sbi": "SBIN", "bharti airtel": "BHARTIARTL",
    "airtel": "BHARTIARTL", "itc": "ITC", "larsen": "LT", "l&t": "LT", "axis bank": "AXISBANK",
    "kotak": "KOTAKBANK", "maruti": "MARUTI", "tata motors": "TATAMOTORS", "tata steel": "TATASTEEL",
    "jsw steel": "JSWSTEEL", "hindalco": "HINDALCO", "wipro": "WIPRO", "hcltech": "HCLTECH", "hcl tech": "HCLTECH",
    "tech mahindra": "TECHM", "sun pharma": "SUNPHARMA", "dr reddy": "DRREDDY", "cipla": "CIPLA",
    "bajaj finance": "BAJFINANCE", "bajaj finserv": "BAJAJFINSV", "bajaj auto": "BAJAJ-AUTO",
    "mahindra": "M&M", "hindustan unilever": "HINDUNILVR", "hul": "HINDUNILVR", "nestle india": "NESTLEIND",
    "asian paints": "ASIANPAINT", "titan": "TITAN", "ultratech": "ULTRACEMCO", "ongc": "ONGC", "bpcl": "BPCL",
    "indian oil": "IOC", "ioc": "IOC", "coal india": "COALINDIA", "ntpc": "NTPC", "power grid": "POWERGRID",
    "tata power": "TATAPOWER", "adani enterprises": "ADANIENT", "adani ports": "ADANIPORTS", "adani": "ADANIENT",
    "hal": "HAL", "hindustan aeronautics": "HAL", "bel": "BEL", "bharat electronics": "BEL", "dlf": "DLF",
    "zomato": "ETERNAL", "paytm": "PAYTM", "nykaa": "NYKAA", "indigo": "INDIGO",
    "interglobe": "INDIGO", "vodafone idea": "IDEA", "yes bank": "YESBANK", "lic": "LICI", "hdfc life": "HDFCLIFE",
    "sbi life": "SBILIFE", "ambuja": "AMBUJACEM", "vedanta": "VEDL", "jio financial": "JIOFIN",
    "bse ltd": "BSE", "angel one": "ANGELONE", "cdsl": "CDSL", "muthoot": "MUTHOOTFIN",
}
_COMPANY_RX = [(name, sym, re.compile(r"(?<![\w&])" + re.escape(name) + r"(?![\w&])", re.I))
               for name, sym in sorted(COMPANIES.items(), key=lambda kv: -len(kv[0]))]
CRYPTO_NAMES = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "xrp": "XRP",
                "bnb": "BNB", "dogecoin": "DOGE", "cardano": "ADA"}
_CRYPTO_RX = [(sym, re.compile(r"\b" + re.escape(n) + r"\b", re.I)) for n, sym in CRYPTO_NAMES.items()]


@dataclass
class Item:
    title: str
    link: str
    source: str
    cat: str
    ts: float | None = None
    summary: str = ""
    rules: list = field(default_factory=list)     # rule keys
    stocks: list = field(default_factory=list)    # NSE symbols mentioned
    coins: list = field(default_factory=list)     # crypto mentioned
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Item":
        return Item(**{k: d.get(k) for k in Item.__dataclass_fields__ if k in d})


# ---------------------------------------------------------------- parsing

def _clean(text: str | None) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_feed(xml_text: str | bytes, source: str, cat: str) -> list[Item]:
    """RSS 2.0 or Atom -> items. Google News titles end with ' - Publisher', which becomes the source."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.rsplit("}", 1)[-1]: c for c in el}
        title = _clean(get["title"].text if "title" in get else "")
        if not title:
            continue
        link = ""
        if "link" in get:
            link = (get["link"].text or get["link"].get("href") or "").strip()
        desc = next((get[k] for k in ("description", "summary", "content") if k in get), None)
        when = next((get[k] for k in ("pubDate", "published", "updated") if k in get), None)
        src = source
        if "source" in get and (get["source"].text or "").strip():
            src = get["source"].text.strip()
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"\s*$", "", title)
        summary = _clean(desc.text if desc is not None else "")
        if summary.lower().startswith(title.lower()[:40]) and len(summary) < len(title) + 40:
            summary = ""  # Google News repeats the title as the description
        out.append(Item(title=title, link=link, source=src, cat=cat,
                        ts=_ts(when.text if when is not None else None), summary=summary[:800]))
    return out


def fetch_feed(url: str, source: str, cat: str) -> list[Item]:
    try:
        r = requests.get(url, headers=UA, timeout=12)
        if r.status_code != 200:
            log.info("News feed %s: HTTP %s", source, r.status_code)
            return []
        return parse_feed(r.content, source, cat)
    except requests.RequestException as exc:
        log.info("News feed %s failed: %s", source, exc.__class__.__name__)
        return []


# ---------------------------------------------------------------- linking + ranking

def tag(item: Item) -> Item:
    text = f"{item.title} {item.summary[:300]}"
    item.rules = [r.key for r, rx in _RULES_RX if rx.search(text)]
    seen, used = [], []
    for name, sym, rx in _COMPANY_RX:  # longest names first; a shorter name inside a longer match is skipped
        for m in rx.finditer(item.title):
            if any(a <= m.start() < b for a, b in used):
                continue
            used.append(m.span())
            if sym not in seen:
                seen.append(sym)
    item.stocks = seen[:3]
    item.coins = list(dict.fromkeys(sym for sym, rx in _CRYPTO_RX if rx.search(item.title)))[:3]
    return item


def rules_of(item: Item) -> list[Rule]:
    by_key = {r.key: r for r in RULES}
    return sorted((by_key[k] for k in item.rules if k in by_key), key=lambda r: -r.weight)


def link_sentence(item: Item, themes: int = 1) -> str:
    """'links to …' text: named companies first, then the strongest theme(s)."""
    parts = []
    if item.stocks:
        parts.append(", ".join(item.stocks) + " directly")
    if item.coins and "crypto" not in item.rules:
        parts.append(", ".join(item.coins))
    for r in rules_of(item)[:themes]:
        parts.append(r.link)
    if not parts:
        parts.append({"crypto": "the crypto market (BTC, ETH)", "world": "global risk sentiment"}.get(
            item.cat, "the broad Indian market (NIFTY)"))
    return "; ".join(parts)


def rank(items: list[Item], now: float | None = None, max_age_h: float = MAX_AGE_H) -> list[Item]:
    now = now or time.time()
    kept = []
    for it in items:
        tag(it)
        age_h = (now - it.ts) / 3600 if it.ts else None
        if age_h is not None and age_h > max_age_h:
            continue
        rs = rules_of(it)
        if it.cat == "world" and not rs and not it.stocks:
            continue  # general world news with no market angle
        recency = 0 if age_h is None else (3 if age_h <= 3 else 2 if age_h <= 8 else 1 if age_h <= 24 else 0)
        it.score = (rs[0].weight * 2 if rs else 0) + len(rs) + 2 * bool(it.stocks) + recency
        kept.append(it)
    kept.sort(key=lambda i: (i.score, i.ts or 0), reverse=True)
    out, sigs = [], []
    for it in kept:  # drop near-duplicate headlines from different feeds
        words = set(re.findall(r"[a-z0-9]{3,}", it.title.lower()))
        if any(len(words & s) / max(len(words | s), 1) >= 0.6 for s in sigs):
            continue
        sigs.append(words)
        out.append(it)
    return out[:MAX_ITEMS]


def collect(cat: str = "all", query: str | None = None) -> list[Item]:
    """Fetch and rank. cat: all / india / world / crypto, or a stock symbol's news when query is given."""
    if query:
        q = query.upper()
        items = fetch_feed(google_news(f"\"{q}\" (share OR stock OR results OR NSE) when:7d"), "Google News", "india")
        ranked = rank(items, max_age_h=7 * 24)
        for it in ranked:
            if q not in it.stocks:
                it.stocks = [q] + it.stocks[:2]
        return ranked
    cats = list(FEEDS) if cat == "all" else [cat]
    jobs = [(url, source, c) for c in cats for source, url in FEEDS.get(c, [])]
    items: list[Item] = []
    with ThreadPoolExecutor(max_workers=8) as pool:   # one slow site must not delay the reply
        for got in pool.map(lambda j: fetch_feed(*j), jobs):
            items += got
    return rank(items)


# ---------------------------------------------------------------- messages

def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{max(mins, 1)}m ago"
    if mins < 48 * 60:
        return f"{mins // 60}h ago"
    return datetime.fromtimestamp(ts, IST).strftime("%d %b")


def pages(items: list) -> int:
    return max(1, -(-len(items) // PAGE))


def page_text(items: list[Item], page: int, cat: str, title: str | None = None) -> str:
    total = pages(items)
    page = min(max(page, 1), total)
    start = (page - 1) * PAGE
    head = title or f"📰 <b>MARKET NEWS</b> · {CATEGORIES.get(cat, cat)}"
    now = datetime.now(IST).strftime("%d %b, %H:%M IST")
    lines = [f"{head} · page {page}/{total}", f"<i>{now} · {len(items)} stories, most market-moving first</i>", LINE]
    if not items:
        lines.append("No fresh stories could be loaded right now. Try 🔄 Refresh in a few minutes.")
    for n, it in enumerate(items[start:start + PAGE], start + 1):
        ago = _ago(it.ts)
        lines.append(f"<b>{n}.</b> {html.escape(it.title[:140])} — <i>links to {html.escape(link_sentence(it))}</i>"
                     + (f" <code>{ago}</code>" if ago else ""))
    lines += [LINE, "👇 Tap a number (or send it, e.g. <code>3</code>) for the full story and why it matters."]
    return "\n".join(lines)


def detail_text(it: Item, n: int, total: int, extra_summary: str = "", ai_take: str | None = None) -> str:
    when = datetime.fromtimestamp(it.ts, IST).strftime("%d %b %H:%M IST") if it.ts else "time unknown"
    lines = [f"📰 <b>STORY {n} of {total}</b>", f"<b>{html.escape(it.title)}</b>",
             f"🗞 {html.escape(it.source)} · {_ago(it.ts) or ''} ({when})", LINE]
    summary = it.summary if len(it.summary) >= len(extra_summary) else extra_summary
    lines += ["📝 <b>What happened</b>", html.escape(summary) if summary else
              "The feed gave only the headline — tap 🔗 Read full article for details."]
    lines += ["", "🔗 <b>Market link</b>"]
    rs = rules_of(it)
    if it.stocks:
        lines.append(f"• <b>{html.escape(', '.join(it.stocks))}</b> — named in the story, so these react most "
                     "directly.")
    for r in rs[:3]:
        lines.append(f"• <b>{html.escape(r.link)}</b> — {html.escape(r.why)}")
    if not rs and not it.stocks:
        lines.append(f"• {html.escape(link_sentence(it, 2))} — general market news.")
    watch_st = list(dict.fromkeys(it.stocks + [s for r in rs[:2] for s in r.stocks]))[:6]
    watch_cr = list(dict.fromkeys(it.coins + [c for r in rs[:2] for c in r.crypto]))[:3]
    if watch_st or watch_cr:
        lines.append("")
        if watch_st:
            lines.append("📌 Stocks to watch: " + html.escape(", ".join(watch_st)))
        if watch_cr:
            lines.append("🪙 Crypto to watch: " + html.escape(", ".join(watch_cr)))
    if ai_take:
        lines += ["", f"🤖 <b>AI take</b> (Gemini, from the headline and summary only)\n{html.escape(ai_take)}"]
    lines += ["", "<i>Headlines can be incomplete or already priced in. The link shows who usually reacts, not the "
                  "direction. Not financial advice.</i>"]
    return "\n".join(lines)


def watch_symbols(it: Item) -> tuple[list[str], list[str]]:
    rs = rules_of(it)
    st = list(dict.fromkeys(it.stocks + [s for r in rs[:2] for s in r.stocks]))[:3]
    cr = list(dict.fromkeys(it.coins + [c for r in rs[:2] for c in r.crypto]))[:2]
    return st, cr


def article_description(url: str) -> str:
    """The article page's own short description (og:description), for feeds that give only a headline."""
    if not url.startswith("http") or "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers=UA, timeout=8, stream=True)
        raw = r.raw.read(300_000, decode_content=True) if r.status_code == 200 else b""
        r.close()
    except (requests.RequestException, OSError):
        return ""
    page = raw.decode("utf-8", "ignore")
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, page, re.I)
        if m:
            return _clean(m.group(1))[:600]
    return ""

