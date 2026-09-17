"""Deterministic redaction of what leaves the on-prem box (ADR-0002).

No LLM in the loop. A `Vocabulary` is built per run from things this side
knows exactly (database/host page slugs and aliases, hostnames and IPs
harvested from those pages, TNS service names from the registries, ES index
patterns from config, operator-listed terms), a fixed set of patterns covers
what the vocabulary might miss (IPs, FQDN-ish tokens, `(HOST=…)`,
`(SERVICE_NAME=…)`, `SID=`, `USER=`, ES index/doc-id shapes, home/oracle
paths), and a `Redactor` replaces every hit with a pseudonym that is
consistent within one request (`DB_A`, `HOST_B`, `IP_A`, …) so the researcher
can still tell "the primary" from "the standby".

Everything is done in one combined-regex pass — vocabulary terms longest
first, then patterns — and replacement text is never rescanned, so a
pseudonym can never be mangled by a later rule. When a pattern branch decides
a token is *not* identifying (a version number, a filename, an approved
citation URL) the token is still swept with the vocabulary-only regex, so a
database name embedded in `dr1cdb1.dat` cannot hide behind a benign shape.
`leak_check` re-scans the finished payload with pseudonyms blanked; any hit is
a bug or an ordering effect and refuses the enqueue (fail closed).

Deliberate choices:
- Vocabulary terms match as substrings, case-insensitively, not on word
  boundaries: `cdb1` inside `cdb1_tt00_2516.trc` is still the database name.
  Over-redaction of an unlucky English word is visible in `dbwiki redact
  --show` and is the safe direction. Terms shorter than 4 characters (`es`)
  are the exception and match whole words only.
- IPv4-shaped tokens that look like an Oracle release (`19.27.0.0`,
  `12.2.0.1`: first octet 11/12/18/19/21/23 with a zero third component, or
  part of a five-part `19.27.0.0.250415`) are versions, not addresses, and are
  kept — product facts are what make research effective. `10.x` is always
  treated as an address (private range) even though 10g shares the shape.
- Loopback / wildcard addresses (`0.0.0.0`, `127.0.0.1`, `::1`, `localhost`)
  identify nothing and are kept.
- URLs whose host is a `sources/` page domain survive whole (citations must
  survive; external sites identify nothing); every other URL is swept.
- OS user names are only ever caught by the `USER=` pattern: adding `oracle`
  to the vocabulary would eat "Oracle Database"."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .normalize import IDENTITY_FIELDS, service_name_like
from .pagetext import frontmatter
from .research import _source_pages, url_domain

SCHEMA_VERSION = 1

CATEGORIES = ("DB", "HOST", "IP", "SVC", "USER", "PATH", "IDX", "TERM")
_PSEUDONYM_RE = re.compile(r"(?<![A-Za-z])(?:%s)_[A-Z]{1,3}(?![A-Za-z])" % "|".join(CATEGORIES))

# domain suffixes that make a dotted token an FQDN rather than a filename
DEFAULT_DOMAIN_SUFFIXES = ("localdomain", "local", "lan", "internal", "corp",
                           "intranet", "world", "com", "net", "org", "io",
                           "de", "cz", "sk", "at", "ch", "uk", "eu")
KEEP_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::1", "::", "localhost"}
# container names every multitenant database has — not identifying
KEEP_NAMES = {"cdb$root", "pdb$seed"}
_VERSION_FIRST_OCTETS = {"11", "12", "18", "19", "21", "23"}

# which normalize() identity fields the vocabulary builder covers, and how
# (a test asserts this covers normalize.IDENTITY_FIELDS exactly)
IDENTITY_FIELD_COVERAGE = {
    "db": "registry file names + databases/*.md slugs",
    "index": "config index_patterns prefixes + IDX patterns",
    "id": "DOCID pattern; ids are dropped by the request builder anyway",
    "service": "registry services + db-derived service variants + SVCKV patterns",
    "client_host": "harvested (HOST=…) values + HOSTKV/FQDN/IP patterns",
    "os_user": "USER= pattern",
    "member": "db vocabulary (a Data Guard member is a db_unique_name)",
    "metric_target": "tablespace names ride the db vocabulary; not sent by the request builder",
}
assert set(IDENTITY_FIELD_COVERAGE) == set(IDENTITY_FIELDS), \
    "redact.IDENTITY_FIELD_COVERAGE out of sync with normalize.IDENTITY_FIELDS"


class RedactionLeak(RuntimeError):
    """The leak check found identifying content in an outgoing payload."""

    def __init__(self, hits: list[str]):
        super().__init__(f"redaction leak: {', '.join(sorted(set(hits)))[:300]}")
        self.hits = hits


def _is_ipv4(tok: str) -> bool:
    try:
        ipaddress.IPv4Address(tok)
        return True
    except ValueError:
        return False


def _fqdn_like(tok: str, suffixes: tuple[str, ...]) -> bool:
    """Last label is a known domain suffix (optionally with a trailing digit
    run: `localdomain1`, `lan2`)."""
    if "." not in tok:
        return False
    last = tok.rsplit(".", 1)[-1].lower().rstrip("0123456789")
    return last in suffixes


# a host token followed by `.label…` is a whole FQDN unless the tail is a
# file extension (`lab-dg1.trc` is a trace file, not a domain)
FILE_EXTS = {"md", "json", "jsonl", "log", "trc", "trm", "dat", "ctl", "dbf",
             "ora", "xml", "txt", "sql", "arc", "bak", "zip", "gz", "tar",
             "html", "py", "yaml", "yml", "csv", "pdf", "cfg", "ini", "sh",
             "out", "lst", "dmp", "aud", "lck", "pid", "tmp"}
# host terms may continue with digits (`lab-dg1` in `lab-dg10`,
# `localdomain` in `localdomain1`) and carry more labels behind them
_HOST_TAIL = r"\d*(?:\.[a-z0-9#-]+)*"
# terms this short (`es`, `db1`) match only as whole words — substring
# matching would eat "estate" or "pdb12"; longer terms match anywhere
SHORT_TERM = 4


def _under(host: str, domains) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _version_like(text: str, start: int, end: int) -> bool:
    """An IPv4-shaped token that is really an Oracle release number: inside a
    longer dotted number (`19.27.0.0.250415`) or a known-major four-part
    release with a zero third component (`19.0.0.0`, `12.2.0.1`)."""
    tok = text[start:end]
    if start > 1 and text[start - 1] == "." and text[start - 2].isdigit():
        return True
    if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
        return True
    parts = tok.split(".")
    return parts[0] in _VERSION_FIRST_OCTETS and parts[2] == "0"


def _label(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_FQDN_HARVEST_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+)\b",
    re.I)
_IPV4_HARVEST_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_HOST_KV_RE = re.compile(r"\(HOST=([^)\s]+)\)", re.I)
_HASHED_RE = re.compile(r"\d+")


@dataclass
class Vocabulary:
    """Exact identifying terms the on-prem side knows, by category
    (lowercase; matching is case-insensitive)."""
    dbs: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    indexes: set[str] = field(default_factory=set)
    terms: set[str] = field(default_factory=set)
    domain_suffixes: tuple[str, ...] = DEFAULT_DOMAIN_SUFFIXES
    source_domains: set[str] = field(default_factory=set)

    def categories(self) -> list[tuple[str, set[str]]]:
        return [("DB", self.dbs), ("HOST", self.hosts), ("IP", self.ips),
                ("SVC", self.services), ("IDX", self.indexes),
                ("TERM", self.terms)]

    def add(self, category: str, term: str) -> None:
        term = (term or "").strip().lower()
        if len(term) < 2 or term in KEEP_ADDRESSES:
            return
        dict(self.categories())[category].add(term)

    def summary(self) -> dict:
        return {c.lower(): len(s) for c, s in self.categories()}


def _all_source_domains(wiki: Path) -> set[str]:
    """Domains of every `sources/` page, whatever its status: external sites
    identify nothing about the estate, and citation URLs must survive whole."""
    out: set[str] = set()
    if (wiki / "sources").is_dir():
        for fm in _source_pages(wiki).values():
            out.update(str(d).strip().lower() for d in (fm.get("domains") or []))
    return out


def harvest_page(text: str, vocab: Vocabulary) -> None:
    """Pull IPs, FQDNs and `(HOST=…)` values out of a wiki page's prose into
    the vocabulary. Bare first labels of harvested FQDNs (`lab-dg1` of
    `lab-dg1.localdomain`) become host terms too: Occurrence notes often name
    a host without its domain."""
    for m in _IPV4_HARVEST_RE.finditer(text):
        tok = m.group(0)
        if _is_ipv4(tok) and not _version_like(text, m.start(), m.end()):
            vocab.add("IP", tok)
    for m in _FQDN_HARVEST_RE.finditer(text):
        tok = m.group(1)
        if _is_ipv4(tok) or _under(tok, vocab.source_domains):
            continue
        if _fqdn_like(tok, vocab.domain_suffixes):
            vocab.add("HOST", tok)
            first = tok.split(".", 1)[0]
            if not first.isdigit():
                vocab.add("HOST", first)
    for m in _HOST_KV_RE.finditer(text):
        tok = m.group(1)
        vocab.add("IP" if _is_ipv4(tok) else "HOST", tok)


def _aliases(fm: dict) -> list[str]:
    out = []
    for key in ("db", "host", "hostname", "aliases", "services", "ips",
                "db_unique_name", "sid"):
        v = fm.get(key)
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(str(x) for x in v)
    return out


def build_vocabulary(cfg, wiki: Path, state_dir: Path | None = None) -> Vocabulary:
    """Per-run vocabulary. `cfg` may be a bare test double: only `.raw`,
    `.sources` and `.state_dir` are read, all defensively."""
    raw = getattr(cfg, "raw", None) or {}
    rcfg = raw.get("redact") or {}
    wiki = Path(wiki)
    vocab = Vocabulary(
        domain_suffixes=tuple(str(s).lower() for s in
                              rcfg.get("domain_suffixes") or DEFAULT_DOMAIN_SUFFIXES),
        source_domains=_all_source_domains(wiki))

    for sub, cat in (("databases", "DB"), ("hosts", "HOST"), ("services", "SVC")):
        for p in sorted((wiki / sub).glob("*.md")):
            text = p.read_text()
            vocab.add(cat, p.stem)
            for a in _aliases(frontmatter(text)):
                vocab.add("IP" if _is_ipv4(a) else cat, a)
            harvest_page(text, vocab)

    sd = state_dir if state_dir is not None else getattr(cfg, "state_dir", None)
    reg_dir = Path(sd) / "registry" if sd else None
    if reg_dir and reg_dir.is_dir():
        for p in sorted(reg_dir.glob("*.json")):
            vocab.add("DB", p.stem)
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            for svc in (d.get("services") or {}):
                if service_name_like(svc):
                    vocab.add("SVC", svc)

    # service spelling variants the listener config generates for known dbs
    sources = getattr(cfg, "sources", None) or {}
    domains: set[str] = set()
    for s in sources.values():
        if isinstance(s, dict):
            domains.update(str(d) for d in s.get("db_service_domains", ["world"]))
    for db in list(vocab.dbs):
        vocab.add("SVC", f"{db}_dgmgrl")
        for d in domains:
            vocab.add("SVC", f"{db}.{d}")
            vocab.add("SVC", f"{db}_dgmgrl.{d}")

    for s in sources.values():
        if not isinstance(s, dict):
            continue
        for pat in s.get("index_patterns") or []:
            prefix = str(pat).split("*", 1)[0].strip()
            if len(prefix) >= 4:
                vocab.add("IDX", prefix)
    es_url = (raw.get("elasticsearch") or {}).get("url", "")
    host = urlparse(es_url).hostname if es_url else ""
    if host:
        vocab.add("IP" if _is_ipv4(host) else "HOST", host)

    # a harvested "host" that is really a known db (`cdb1` out of the service
    # name `cdb1.world`) belongs to DB / SVC, not HOST
    for h in list(vocab.hosts):
        first = h.split(".", 1)[0]
        if first in vocab.dbs:
            vocab.hosts.discard(h)
            if "." in h:
                vocab.add("SVC", h)

    for t in rcfg.get("terms") or []:
        vocab.add("TERM", str(t))

    # the compactor's message *templates* replace digit runs with `#`
    # (`cdb1_stby` -> `cdb#_stby`, `lab-dg1` -> `lab-dg#`); every term with
    # digits gets that spelling too so templates cannot slip past
    for cat, terms in vocab.categories():
        for t in list(terms):
            v = _HASHED_RE.sub("#", t)
            if v != t and len(v.replace("#", "").replace(".", "")) >= 3:
                terms.add(v)
    return vocab


# (kind, regex). Kinds with an inner capture group pseudonymize only that
# group. The combined alternation tries vocabulary terms first, then these,
# at every position.
PATTERNS: list[tuple[str, str]] = [
    ("URL", r"https?://[^\s<>()\[\]\"'`;,]+"),
    ("IP4", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    ("IP6", r"(?<![\w:.])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
            r"|(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?)"
            r"(?![\w:])"),
    ("HOSTKV", r"\(HOST=([^)\s]*)\)"),
    ("SVCKV", r"\((?:SERVICE_NAME|SID|INSTANCE_NAME|GLOBAL_DBNAME|SID_NAME|PDBNAME)=([^)\s]*)\)"),
    ("USERKV", r"\bUSER=([^)\s,;]*)"),
    ("HOSTKV", r"\b(?:host|hostname)=\"?([A-Za-z0-9._-]+)\"?"),      # not SERVER= (DEDICATED/SHARED)
    ("SVCKV", r"\b(?:service_name|service|sid|db_unique_name|db_name)=\"?([A-Za-z0-9._$-]+)\"?"),
    ("PATH", r"(?<![\w/])/(?:home|u0\d|opt|export/home)/[^\s\"'`)>,;|]+"),
    ("IDX", r"\.ds-[a-z0-9_.-]+-\d{4}\.\d{2}\.\d{2}-\d{6}"),
    ("IDX", r"\b(?:oracle-logs|logs-oracle)[a-z0-9_.-]*"),
    ("DOCID", r"(?<![\w-])[A-Za-z0-9_-]{20}(?![\w-])"),
    # `#` allowed inside labels: compactor templates spell `lab-dg#.localdomain`
    ("FQDN", r"(?<![\w#.-])[a-z0-9#](?:[a-z0-9#-]{0,62}[a-z0-9#])?(?:\.[a-z0-9#](?:[a-z0-9#-]{0,62}[a-z0-9#])?)+(?![\w#-])"),
]


def _compile(vocab: Vocabulary, patterns: list[tuple[str, str]]):
    """One alternation: every vocabulary term (longest first, escaped) then
    the given patterns, each as a named group `g<i>`. Returns the regex and
    the group-name -> (kind, is_vocab) table."""
    terms: list[tuple[str, str]] = []
    for cat, s in vocab.categories():
        terms.extend((cat, t) for t in s)
    terms.sort(key=lambda ct: (-len(ct[1]), ct[1]))
    entries: list[tuple[str, str]] = []          # (regex, category)
    for cat, t in terms:
        rx = re.escape(t)
        if len(t) < SHORT_TERM:
            rx = rf"(?<![a-z0-9]){rx}(?![a-z0-9])"
        entries.append((rx + _HOST_TAIL if cat == "HOST" else rx, cat))
    # digit families: siblings of known names share the shape (`ol9-19-dg1`
    # -> `ol\d+-\d+-dg\d+` also catches the never-seen `ol9-19-dg3`;
    # `cdb1` -> `cdb\d+`). After the exact terms so those win where they apply.
    families: dict[str, str] = {}                # regex -> category (DB wins ties)
    for cat, t in terms:                          # terms iterate DB, HOST, SVC in order
        if cat in ("DB", "HOST", "SVC") and any(c.isdigit() for c in t) and "#" not in t:
            stem = _HASHED_RE.sub("#", t).replace("#", "")
            if len(stem) < 3:
                continue
            fam = _HASHED_RE.sub(r"(?:\\d+|#)", re.escape(t))
            families.setdefault(fam + _HOST_TAIL if cat == "HOST" else fam, cat)
    entries.extend(sorted(families.items(), key=lambda e: (-len(e[0]), e[0])))
    parts, table = [], {}
    for i, (rx, cat) in enumerate(entries):
        parts.append(f"(?P<g{i}>{rx})")
        table[f"g{i}"] = (cat, True)
    for j, (kind, pat) in enumerate(patterns):
        name = f"g{len(entries) + j}"
        parts.append(f"(?P<{name}>{pat})")
        table[name] = (kind, False)
    if not parts:
        return re.compile(r"(?!x)x"), table
    return re.compile("|".join(parts), re.I), table


class Redactor:
    """Consistent pseudonyms for one request. `mapping` (pseudonym ->
    original) is what `.state/redaction/<run_id>.json` holds and what
    `demap` uses on the way back in."""

    def __init__(self, vocab: Vocabulary, run_id: str = "", key: str | None = None):
        self.vocab = vocab
        self.run_id = run_id
        # one mapping file per *request*: several pages share a run_id
        self.key = key or run_id
        self.mapping: dict[str, str] = {}
        self._by_original: dict[tuple[str, str], str] = {}
        self._full, self._table = _compile(vocab, PATTERNS)
        self._nourl, self._table_nourl = _compile(
            vocab, [p for p in PATTERNS if p[0] != "URL"])
        self._vocab_only, self._table_vocab = _compile(vocab, [])

    def _category_of(self, tok: str, default: str) -> str:
        """A token caught by a generic pattern may be a known vocabulary term
        (`db_unique_name="cdb1"`): file it under its vocabulary category so the
        pseudonym is the same wherever the term appears."""
        low = tok.lower()
        for cat, terms in self.vocab.categories():
            if low in terms:
                return cat
        return default

    def pseudonym(self, category: str, original: str) -> str:
        category = self._category_of(original, category)
        key = (category, original.lower())
        if key in self._by_original:
            return self._by_original[key]
        n = sum(1 for c, _ in self._by_original if c == category)
        name = f"{category}_{_label(n)}"
        self._by_original[key] = name
        self.mapping[name] = original
        return name

    def _sweep_vocab(self, tok: str) -> str:
        return self._vocab_only.sub(
            lambda m: self.pseudonym(self._table_vocab[m.lastgroup][0], m.group(0)), tok)

    def _sweep_nourl(self, tok: str) -> str:
        return self._nourl.sub(lambda m: self._replace(m, self._table_nourl), tok)

    def _replace(self, m: re.Match, table: dict) -> str:
        kind, is_vocab = table[m.lastgroup]
        tok = m.group(0)
        if is_vocab:
            if kind == "HOST" and "." in tok:
                core, ext = tok.rsplit(".", 1)
                if ext.lower() in FILE_EXTS:
                    return self.pseudonym("HOST", core) + "." + ext
            return self.pseudonym(kind, tok)
        text = m.string
        if kind == "URL":
            host = url_domain(tok)
            if host and _under(host, self.vocab.source_domains):
                return self._sweep_vocab(tok)
            return self._sweep_nourl(tok)
        if kind == "IP4":
            if tok in KEEP_ADDRESSES:
                return tok
            if not _is_ipv4(tok) or _version_like(text, m.start(), m.end()):
                return self._sweep_vocab(tok)
            return self.pseudonym("IP", tok)
        if kind == "IP6":
            if tok in KEEP_ADDRESSES:
                return tok
            try:
                ipaddress.IPv6Address(tok)
            except ValueError:
                return self._sweep_vocab(tok)
            return self.pseudonym("IP", tok)
        if kind in ("HOSTKV", "SVCKV", "USERKV"):
            inner = m.group(m.lastindex + 1) if m.lastindex else ""
            if not inner or inner in KEEP_ADDRESSES or inner.lower() in KEEP_NAMES:
                return self._sweep_vocab(tok)
            if kind == "HOSTKV":
                cat = "IP" if _is_ipv4(inner) else "HOST"
            else:
                cat = "SVC" if kind == "SVCKV" else "USER"
            return tok.replace(inner, self.pseudonym(cat, inner), 1)
        if kind == "PATH":
            core = tok.rstrip(".:")
            return self.pseudonym("PATH", core) + tok[len(core):]
        if kind == "IDX":
            return self.pseudonym("IDX", tok)
        if kind == "DOCID":
            # ES auto ids are mixed-case base64url; a plain word/number/date is not one
            if not (any(c.isupper() for c in tok) and any(c.islower() for c in tok)):
                return self._sweep_vocab(tok)
            return self.pseudonym("IDX", tok)
        if kind == "FQDN":
            if _is_ipv4(tok) or not _fqdn_like(tok, self.vocab.domain_suffixes) \
                    or _under(tok, self.vocab.source_domains):
                return self._sweep_vocab(tok)
            return self.pseudonym("HOST", tok)
        return self._sweep_vocab(tok)

    def redact(self, text: str) -> str:
        if not text:
            return text
        return self._full.sub(lambda m: self._replace(m, self._table), text)

    def redact_obj(self, obj):
        """Recursively redact every string in a JSON-ish structure. Keys are
        schema, not data, and are left alone."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self.redact_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        return obj

    def demap(self, text: str) -> str:
        """Replace pseudonyms the researcher echoed back with the originals.
        Longest pseudonym first so `DB_AB` is not eaten by `DB_A`."""
        if not text or not self.mapping:
            return text
        pat = re.compile(r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % "|".join(
            re.escape(k) for k in sorted(self.mapping, key=len, reverse=True)))
        return pat.sub(lambda m: self.mapping[m.group(0)], text)

    def demap_obj(self, obj):
        if isinstance(obj, str):
            return self.demap(obj)
        if isinstance(obj, list):
            return [self.demap_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.demap_obj(v) for k, v in obj.items()}
        return obj

    def leak_check(self, obj) -> list[str]:
        """Independent re-scan of an already-redacted payload: pseudonyms are
        blanked, then a fresh redactor over the same vocabulary must change
        nothing. Returns the offending original tokens (empty = clean)."""
        hits: list[str] = []
        for s in _strings(obj):
            blank = _PSEUDONYM_RE.sub("", s)
            probe = Redactor(self.vocab)
            if probe.redact(blank) != blank:
                hits.extend(sorted(set(probe.mapping.values())) or ["<pattern hit>"])
        return hits

    def check(self, obj) -> None:
        """Raise RedactionLeak instead of returning hits (fail closed)."""
        hits = self.leak_check(obj)
        if hits:
            raise RedactionLeak(hits)

    def save(self, state_dir: Path) -> Path:
        d = Path(state_dir) / "redaction"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.key}.json"
        p.write_text(json.dumps({"schema_version": SCHEMA_VERSION,
                                 "run_id": self.run_id, "key": self.key,
                                 "mapping": self.mapping}, indent=1, sort_keys=True))
        return p

    @staticmethod
    def mapping_path(state_dir: Path, key: str) -> Path:
        return Path(state_dir) / "redaction" / f"{key}.json"

    @classmethod
    def load(cls, state_dir: Path, key: str,
             vocab: Vocabulary | None = None) -> "Redactor":
        p = cls.mapping_path(state_dir, key)
        d = json.loads(p.read_text())
        r = cls(vocab or Vocabulary(), str(d.get("run_id") or key), key)
        r.mapping = dict(d.get("mapping") or {})
        for name, orig in r.mapping.items():
            r._by_original[(name.rsplit("_", 1)[0], orig.lower())] = name
        return r


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _strings(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)


_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
_DIGEST_PATH_RE = re.compile(r"\bdigests/[^\s|)\]]+")
_PROVENANCE_RE = re.compile(r"\((?:observed|derived)\s+—[^)]*\)", re.S)


def strip_wiki_refs(text: str) -> str:
    """Remove what research does not need and must not see: wiki links,
    digest paths, provenance parentheticals. Removed, not pseudonymized."""
    text = _WIKILINK_RE.sub("", text)
    text = _DIGEST_PATH_RE.sub("", text)
    text = _PROVENANCE_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
