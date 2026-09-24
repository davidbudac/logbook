"""Deterministic redaction of what leaves the on-prem box (ADR-0002).

No LLM in the loop. A `Vocabulary` is built per run from things this side
knows exactly (database/host page slugs and aliases, hostnames and IPs
harvested from those pages, TNS service names and the host names the
compactor observed on ES documents from the registries, ES index patterns
from config, operator-listed terms), a fixed set of patterns covers what
the vocabulary might miss (IPs, FQDN-ish tokens, `(HOST=…)`,
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
`leak_check` re-scans the finished payload with pseudonyms blanked, using
its own scanners rather than PATTERNS (`leak_scan`), so a gap in the
redactor is not also a gap in the check; any hit refuses the enqueue (fail
closed). `outbound` is the one call that does both, and truncation happens
after it, never before.

Deliberate choices:
- Vocabulary terms match as substrings, case-insensitively, not on word
  boundaries: `cdb1` inside `cdb1_tt00_2516.trc` is still the database name.
  Over-redaction of an unlucky English word is visible in `dbwiki redact
  --show` and is the safe direction. Terms shorter than 4 characters (`es`)
  are the exception and match whole words only, as do letters-only host
  names learned from ES documents alone (`mercury` must not eat
  "mercurial").
- IPv4-shaped tokens that look like an Oracle release (`19.27.0.0`,
  `12.2.0.1`: first octet 11/12/18/19/21/23 with a zero third component, or
  part of a five-part `19.27.0.0.250415`) are versions, not addresses, and are
  kept — product facts are what make research effective. `10.x` is always
  treated as an address (private range) even though 10g shares the shape.
- Loopback / wildcard addresses (`0.0.0.0`, `127.0.0.1`, `::1`, `localhost`)
  identify nothing and are kept.
- URLs whose host is a `sources/` page domain survive whole (citations must
  survive; external sites identify nothing); every other URL is swept.
- OS user names are only ever caught by the `USER=`/audit/e-mail patterns:
  adding `oracle` to the vocabulary would eat "Oracle Database".
- Any dotted alphanumeric token is a host unless it is shaped like something
  else (file extension, number, approved source domain, code namespace,
  `SCHEMA.OBJECT`, a `key=`): internal names live under suffixes no list can
  enumerate (`dbsrv77.corpx`). A name the vocabulary knows is a host
  whatever its shape (`DBSRV.CORPX` is not `SCHEMA.OBJECT` once `dbsrv.corpx`
  was observed).
- Host names are learned, not listed: every `host.name`, `host.hostname`,
  `agent.hostname`, listener/CRS diag-path host, `(HOST=…)` descriptor
  value and listener client host the compactor reads is recorded in the
  per-db registry (`normalize.observed_hosts`) and becomes a HOST term here
  with its first label, and its domain a known suffix (`learn_host`). A
  bare one-label name the pipeline has never seen on any document and no
  page or config term names (`on dbsrv77`) has no shape and stays the
  residual risk — `redact.terms` is the operator's lever for exactly those,
  `dbwiki redact --fuzz` the check (it reports the terms per source)."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .normalize import IDENTITY_FIELDS, host_name_like, service_name_like
from .pagetext import frontmatter
from .research import _source_pages, url_domain

if TYPE_CHECKING:
    from .config import Config

SCHEMA_VERSION = 1

CATEGORIES = ("DB", "HOST", "IP", "SVC", "USER", "PATH", "IDX", "TERM")
_PSEUDONYM_RE = re.compile(r"(?<![A-Za-z])(?:%s)_[A-Z]{1,3}(?![A-Za-z])" % "|".join(CATEGORIES))

# domain suffixes that make a dotted token an FQDN rather than a filename
# (configured `redact.domain_suffixes` are added to these, never replace them)
DEFAULT_DOMAIN_SUFFIXES = ("localdomain", "local", "lan", "internal", "corp",
                           "intranet", "intra", "int", "priv", "private", "home",
                           "lab", "world", "com", "net", "org", "io",
                           "de", "cz", "sk", "at", "ch", "uk", "eu")
KEEP_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::1", "::", "localhost"}
# container names every multitenant database has — not identifying
KEEP_NAMES = {"cdb$root", "pdb$seed"}
# database accounts every Oracle database has (audit `DATABASE USER:`); an
# application schema name is not in here and is masked
KEEP_DB_USERS = {"", "/", "sys", "system", "public", "sysdg", "sysbackup",
                 "syskm", "sysrac", "dbsnmp", "sysman"}
_VERSION_FIRST_OCTETS = {"11", "12", "18", "19", "21", "23"}
# client programs as `<program>@<host>` prints them; the program is kept
PROGRAMS = {"oracle", "sqlplus", "rman", "sqlldr", "expdp", "impdp", "exp", "imp",
            "dgmgrl", "emagent", "java", "jdbc", "perl", "python", "python3",
            "tnsping", "lsnrctl", "srvctl", "crsctl", "asmcmd", "orachk", "exachk",
            "sqlcl", "sql", "toad", "sqldeveloper", "ogg", "extract", "replicat"}

# which normalize() identity fields the vocabulary builder covers, and how
# (a test asserts this covers normalize.IDENTITY_FIELDS exactly)
IDENTITY_FIELD_COVERAGE = {
    "db": "registry file names + databases/*.md slugs",
    "index": "config index_patterns prefixes + IDX patterns",
    "id": "DOCID pattern; ids are dropped by the request builder anyway",
    "service": "registry services + db-derived service variants + SVCKV patterns",
    "client_host": "registry hosts (compactor, normalize.observed_hosts) + "
                   "page (HOST=…) values + HOSTKV/FQDN/IP patterns",
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


def _fqdn_like(tok: str, suffixes) -> bool:
    """The token ends in a known domain suffix, compared label-wise so a
    configured `corp.example` matches `dbsrv77.corp.example`; the last label
    may carry a trailing digit run (`localdomain1`, `lan2`)."""
    labels = tok.lower().split(".")
    if len(labels) < 2:
        return False
    labels[-1] = labels[-1].rstrip("0123456789")
    name = ".".join(labels)
    return any(name.endswith("." + s) for s in suffixes)


# a host token followed by `.label…` is a whole FQDN unless the tail is a
# file extension (`lab-dg1.trc` is a trace file, not a domain)
FILE_EXTS = {"md", "json", "jsonl", "log", "trc", "trm", "dat", "ctl", "dbf",
             "ora", "xml", "txt", "sql", "arc", "bak", "zip", "gz", "tar",
             "html", "htm", "py", "yaml", "yml", "csv", "pdf", "cfg", "ini", "sh",
             "out", "lst", "dmp", "aud", "lck", "pid", "tmp", "bkp", "rsp",
             "env", "conf", "properties", "jar", "war", "class", "java", "so",
             "dll", "exe", "bat", "cmd", "ps1", "pl", "pm", "rpm", "bin", "msg",
             "pem", "crt", "key", "sso", "p12", "jks", "wallet", "ocr", "dbc",
             "pls", "plb", "pks", "pkb", "trm", "cdmp", "mb", "hc", "ksh", "bash",
             "php", "js", "css", "png", "jpg", "gif", "svg", "zst", "xz", "bz2",
             "tgz", "list", "err", "orig", "old", "new", "prev", "sav", "bk", "incr",
             "aspx", "asp", "jsp", "shtml"}
# first labels that make a dotted token code or configuration, not a host:
# Java/Oracle packages, sysctl keys, sqlnet.ora / listener.ora parameters,
# and the schemas every Oracle database has
CODE_NAMESPACES = {"oracle", "java", "javax", "jdk", "sun", "com", "org",
                   "kernel", "vm", "fs", "net", "sqlnet", "names", "tcp", "ssl",
                   "wallet", "sys", "system", "public", "xdb", "dbsnmp", "outln",
                   "mdsys", "ctxsys", "audsys", "ordsys", "olapsys", "wmsys",
                   "apex", "dbms", "utl", "owa", "self", "this", "window",
                   "document", "os", "re", "json", "yaml", "np", "pd"}
# host terms may continue with letters and digits (`lab-dg1` in `lab-dg10`
# or `lab-dg1vip`, `localdomain` in `localdomain1`) and carry more labels
# behind them; the same holds for database and service names
# (`prodfin9.bank.cz`, `cdb1stby`)
_HOST_TAIL = r"[a-z0-9]*(?:\.[a-z0-9#-]+)*"
# terms this short (`es`, `db1`) match only as whole words — substring
# matching would eat "estate" or "pdb12"; longer terms match anywhere
SHORT_TERM = 4
# categories whose vocabulary terms swallow a glued tail (see _HOST_TAIL)
_TAILED = ("DB", "HOST", "SVC")


def _under(host: str, domains) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def dotted_host(tok: str, vocab: "Vocabulary") -> bool:
    """Is a dotted alphanumeric token a host name? Yes unless it is shaped
    like something else research needs: a file name (known extension), a
    number or release (no letter in the last label), an approved citation
    domain, code (`oracle.sysman.x`, `kernel.shmmax`, `SCOTT.MYPROC`) or an
    abbreviation (`e.g`). A known domain suffix always wins: internal names
    live under TLDs no list can enumerate (`dbsrv77.corpx`, `x.prod.intra`),
    so the default is host."""
    if _under(tok, vocab.source_domains):
        return False
    if _fqdn_like(tok, vocab.domain_suffixes):
        return True
    # a name the vocabulary knows is a host whatever its shape: an all-caps
    # `DBSRV.CORPX` would otherwise read as SCHEMA.OBJECT
    low = tok.lower()
    if low in vocab.hosts or low.split(".", 1)[0] in vocab.hosts:
        return True
    labels = tok.split(".")
    last = labels[-1]
    # a top-level label is letters, at most followed by digits
    # (`localdomain1`): `09.776Z`, `12-000003`, `T-1.S-1899` (an RMAN piece)
    # are timestamps, counters and file names
    if len(last) < 2 or not re.fullmatch(r"[A-Za-z]+[0-9]*", last):
        return False
    if all(len(lb) <= 1 for lb in labels[:-1]):
        return False
    if last.lower() in FILE_EXTS or last.lower().rstrip("0123456789") in FILE_EXTS:
        return False
    if labels[0].lower() in CODE_NAMESPACES:
        return False
    # SCHEMA.OBJECT, as ORA-06512 stacks print it
    if len(labels) == 2 and tok.isupper() and not any(c.isdigit() or c == "-" for c in tok):
        return False
    # CamelCase is code (`NullPointerException`, `emSDK`), never DNS
    if any(re.search(r"[a-z][A-Z]", lb) for lb in labels):
        return False
    return True


def _version_like(text: str, start: int, end: int) -> bool:
    """An IPv4-shaped token that is really an Oracle release number: inside a
    longer dotted number (`19.27.0.0.250415`) or a four-part release as
    Oracle prints them — `11.2.0.4`, `12.1.0.2`, `12.2.0.1`, and `N.M.0.0`
    for 18c onwards (`19.27.0.0`, `21.3.0.0`; 23ai also `23.4.0.24`). A
    public address that merely starts with 19 (`19.45.0.7`) is an address."""
    tok = text[start:end]
    if start > 1 and text[start - 1] == "." and text[start - 2].isdigit():
        return True
    if end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit():
        return True
    return _release_quad(tok.split("."))


def _release_quad(parts: list[str]) -> bool:
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b, c, d = (int(p) for p in parts)
    if parts[0] not in _VERSION_FIRST_OCTETS or c != 0:
        return False
    if a in (11, 12):
        return b in (1, 2) and d <= 5
    if b > 40:
        return False
    return d == 0 or (a == 23 and 20 <= d <= 40)


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
_IPV4_HARVEST_RE = re.compile(r"(?<![0-9])(?<![0-9]\.)(?:\d{1,3}\.){3}\d{1,3}(?![0-9]|\.[0-9])")
_HOST_KV_RE = re.compile(r"\(\s*HOST\s*=\s*([^)\s]+)\s*\)", re.I)
_HASHED_RE = re.compile(r"\d+")


# where a vocabulary term came from, in the order `dbwiki redact --fuzz`
# reports them: wiki pages, registry file names and services, host names the
# compactor observed on ES documents, config (redact.terms, ES url, index
# patterns), and spellings derived from those (service variants, `#` forms)
ORIGINS = ("wiki", "registry", "harvested", "config", "derived")


@dataclass
class Vocabulary:
    """Exact identifying terms the on-prem side knows, by category
    (lowercase; matching is case-insensitive). `words` are terms matched
    as whole words whatever their length (letters-only harvested host
    names, which may be English words); `origins` records which source
    contributed each term (a term may have several)."""
    dbs: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    services: set[str] = field(default_factory=set)
    indexes: set[str] = field(default_factory=set)
    terms: set[str] = field(default_factory=set)
    domain_suffixes: tuple[str, ...] = DEFAULT_DOMAIN_SUFFIXES
    source_domains: set[str] = field(default_factory=set)
    words: set[str] = field(default_factory=set)
    origins: dict[str, set[str]] = field(default_factory=dict)

    def categories(self) -> list[tuple[str, set[str]]]:
        return [("DB", self.dbs), ("HOST", self.hosts), ("IP", self.ips),
                ("SVC", self.services), ("IDX", self.indexes),
                ("TERM", self.terms)]

    def add(self, category: str, term: str, origin: str = "") -> None:
        term = (term or "").strip().lower()
        if len(term) < 2 or term in KEEP_ADDRESSES:
            return
        dict(self.categories())[category].add(term)
        if origin:
            self.origins.setdefault(origin, set()).add(term)

    def whole_word(self, term: str) -> bool:
        return len(term) < SHORT_TERM or term in self.words

    def summary(self) -> dict:
        return {c.lower(): len(s) for c, s in self.categories()}

    def sources(self) -> dict[str, int]:
        """Distinct live terms per origin (ORIGINS order, zeros included):
        what `dbwiki redact --fuzz` shows so an operator can see coverage."""
        live: set[str] = set().union(*(s for _c, s in self.categories()))
        return {o: len(self.origins.get(o, set()) & live) for o in ORIGINS}


def _all_source_domains(wiki: Path) -> set[str]:
    """Domains of every `sources/` page, whatever its status: external sites
    identify nothing about the estate, and citation URLs must survive whole."""
    out: set[str] = set()
    if (wiki / "sources").is_dir():
        for fm in _source_pages(wiki).values():
            out.update(str(d).strip().lower() for d in (fm.get("domains") or []))
    return out


def harvest_page(text: str, vocab: Vocabulary, origin: str = "wiki") -> None:
    """Pull IPs, FQDNs and `(HOST=…)` values out of a wiki page's prose into
    the vocabulary. Bare first labels of harvested FQDNs (`lab-dg1` of
    `lab-dg1.localdomain`) become host terms too: Occurrence notes often name
    a host without its domain."""
    for m in _IPV4_HARVEST_RE.finditer(text):
        tok = m.group(0)
        if _is_ipv4(tok) and not _version_like(text, m.start(), m.end()):
            vocab.add("IP", tok, origin)
    for m in _FQDN_HARVEST_RE.finditer(text):
        tok = m.group(1)
        if _is_ipv4(tok) or _under(tok, vocab.source_domains):
            continue
        if _fqdn_like(tok, vocab.domain_suffixes):
            vocab.add("HOST", tok, origin)
            first = tok.split(".", 1)[0]
            if not first.isdigit():
                vocab.add("HOST", first, origin)
    for m in _HOST_KV_RE.finditer(text):
        tok = m.group(1)
        vocab.add("IP" if _is_ipv4(tok) else "HOST", tok, origin)


# names the redactor's own tables already call non-identifying: a harvested
# host value equal to one of these would eat research text (`sqlplus`,
# `sys`, `trc`), so it is not learned
_NOT_HOSTS = PROGRAMS | CODE_NAMESPACES | FILE_EXTS | KEEP_NAMES | KEEP_DB_USERS


def _learnable(name: str, vocab: Vocabulary) -> bool:
    """Not a name the redactor's tables keep, not an approved source domain
    and not one of its labels (a client called `blog` must not mangle the
    citation `blog.example.org`)."""
    if not name or name in _NOT_HOSTS or _under(name, vocab.source_domains):
        return False
    return not any(name in d.split(".") for d in vocab.source_domains)


def learn_host(vocab: Vocabulary, value) -> None:
    """One host name the compactor observed (registry `hosts`) into the
    vocabulary: the name, its first label (`dbsrv77` of `dbsrv77.corpx`),
    and its domain as a known suffix so an unseen sibling under it
    (`OTHERSRV.CORPX`) reads as a host whatever its shape. Guarded twice:
    `normalize.host_name_like` (too short, numeric, addresses, releases,
    HOST_STOPLIST) and `_NOT_HOSTS`; a file-extension tail is a file name,
    not a domain. Letters-only single labels (`mercury`) match whole words
    only (`Vocabulary.words`), unless another source names them too."""
    name = host_name_like(value if isinstance(value, str) else "")
    if not _learnable(name, vocab):
        return
    labels = name.split(".")
    if len(labels) > 1 and labels[-1] in FILE_EXTS:
        return
    vocab.add("HOST", name, "harvested")
    if len(labels) == 1:
        return
    first = host_name_like(labels[0])
    if _learnable(first, vocab):
        vocab.add("HOST", first, "harvested")
    # the domain (`corpx` of `dbsrv77.corpx`) becomes a known suffix, unless
    # it is a name the redactor's tables keep or an approved source domain
    suffix = ".".join(labels[1:])
    if re.fullmatch(r"[a-z]+[0-9]*", labels[-1]) and "_" not in suffix \
            and suffix not in vocab.domain_suffixes and suffix not in _NOT_HOSTS \
            and not _under(suffix, vocab.source_domains):
        vocab.domain_suffixes = (*vocab.domain_suffixes, suffix)


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


def build_vocabulary(cfg: Config | None, wiki: Path,
                     state_dir: Path | None = None) -> Vocabulary:
    """Per-run vocabulary. `cfg` is None for a caller with only a wiki (a
    source-review request): no configured terms, sources or ES host, and no
    registry unless `state_dir` names one."""
    raw = cfg.raw if cfg is not None else {}
    rcfg = raw.get("redact") or {}
    wiki = Path(wiki)
    vocab = Vocabulary(
        # configured suffixes ADD to the defaults: an operator naming their
        # corporate suffix must not stop `.cz` from being redacted
        domain_suffixes=tuple(dict.fromkeys(
            [*DEFAULT_DOMAIN_SUFFIXES,
             *(str(s).strip().strip(".").lower()
               for s in rcfg.get("domain_suffixes") or [] if str(s).strip(". "))])),
        source_domains=_all_source_domains(wiki))

    for sub, cat in (("databases", "DB"), ("hosts", "HOST"), ("services", "SVC")):
        for p in sorted((wiki / sub).glob("*.md")):
            text = p.read_text()
            vocab.add(cat, p.stem, "wiki")
            for a in _aliases(frontmatter(text)):
                vocab.add("IP" if _is_ipv4(a) else cat, a, "wiki")
            harvest_page(text, vocab)

    sd = state_dir if state_dir is not None else (
        cfg.state_dir if cfg is not None else None)
    reg_dir = Path(sd) / "registry" if sd else None
    harvested: list = []
    if reg_dir and reg_dir.is_dir():
        for p in sorted(reg_dir.glob("*.json")):
            vocab.add("DB", p.stem, "registry")
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            for svc in (d.get("services") or {}):
                if service_name_like(svc):
                    vocab.add("SVC", svc, "registry")
            # machine names the compactor saw on this db's documents
            # (normalize.observed_hosts); a dict in the registry, a list
            # tolerated from a hand edit
            harvested.extend(d.get("hosts") or [])
    for h in sorted(set(x for x in harvested if isinstance(x, str))):
        learn_host(vocab, h)

    # service spelling variants the listener config generates for known dbs
    sources = cfg.sources if cfg is not None else {}
    domains: set[str] = set()
    for s in sources.values():
        if isinstance(s, dict):
            domains.update(str(d) for d in s.get("db_service_domains", ["world"]))
    for db in list(vocab.dbs):
        vocab.add("SVC", f"{db}_dgmgrl", "derived")
        for d in domains:
            vocab.add("SVC", f"{db}.{d}", "derived")
            vocab.add("SVC", f"{db}_dgmgrl.{d}", "derived")

    for s in sources.values():
        if not isinstance(s, dict):
            continue
        for pat in s.get("index_patterns") or []:
            prefix = str(pat).split("*", 1)[0].strip()
            if len(prefix) >= 4:
                vocab.add("IDX", prefix, "config")
    es_url = (raw.get("elasticsearch") or {}).get("url", "")
    host = urlparse(es_url).hostname if es_url else ""
    if host:
        vocab.add("IP" if _is_ipv4(host) else "HOST", host, "config")

    # a harvested "host" that is really a known db (`cdb1` out of the service
    # name `cdb1.world`) belongs to DB / SVC, not HOST
    for h in list(vocab.hosts):
        first = h.split(".", 1)[0]
        if first in vocab.dbs:
            vocab.hosts.discard(h)
            if "." in h:
                vocab.add("SVC", h)

    for t in rcfg.get("terms") or []:
        vocab.add("TERM", str(t), "config")

    # a letters-only harvested name may be an English word (`mercury`):
    # whole words only, unless a page or config term names it as well
    others = set().union(*(ts for o, ts in vocab.origins.items() if o != "harvested"))
    vocab.words = {t for t in vocab.origins.get("harvested", set())
                   if t.isalpha() and t not in others}

    # the compactor's message *templates* replace digit runs with `#`
    # (`cdb1_stby` -> `cdb#_stby`, `lab-dg1` -> `lab-dg#`); every term with
    # digits gets that spelling too so templates cannot slip past
    for cat, terms in vocab.categories():
        for t in list(terms):
            v = _HASHED_RE.sub("#", t)
            if v != t and len(v.replace("#", "").replace(".", "")) >= 3:
                terms.add(v)
                vocab.origins.setdefault("derived", set()).add(v)
    return vocab


# (kind, regex). Kinds with inner capture groups pseudonymize only those
# groups, each replaced by its span — never by searching the token for the
# value, which can also occur inside the key (`(USER=U)`). The combined
# alternation tries vocabulary terms first, then these, at every position.
# No pattern may use a numbered backreference: the alternation renumbers
# every group.
_Q = r"[\"']?"
# absolute paths under these roots are the OS's, not the estate's
BENIGN_PATH_ROOTS = ("etc", "usr", "bin", "sbin", "lib", "lib64", "proc", "dev",
                     "sys", "run", "boot")
PATTERNS: list[tuple[str, str]] = [
    ("URL", r"https?://[^\s<>()\[\]\"'`;,]+"),
    ("EMAIL", r"(?<![\w.%+-])[A-Za-z0-9._%+-]*[A-Za-z0-9]@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"),
    # an address may be glued to a word (`addr_10.1.2.3`) or end a sentence
    # (`from 10.1.2.3.`); only a longer dotted number makes it something else
    ("IP4", r"(?<![0-9])(?<![0-9]\.)(?:\d{1,3}\.){3}\d{1,3}(?![0-9]|\.[0-9])"),
    ("IP6", r"(?<![\w:.])(?:[0-9a-f]{0,4}:){2,6}(?:\d{1,3}\.){3}\d{1,3}(?![0-9]|\.[0-9])"),
    ("IP6", r"(?<![\w:.])(?:(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}"
            r"|(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?)"
            r"(?![\w:]|\.[0-9])"),
    # descriptors as tnsnames.ora and the listener log print them, spaces or not
    ("HOSTKV", r"\(\s*HOST\s*=\s*([^)\s]*)\s*\)"),
    ("SVCKV", r"\(\s*(?:SERVICE_NAME|SID|INSTANCE_NAME|GLOBAL_DBNAME|SID_NAME|PDBNAME"
              r"|DB_UNIQUE_NAME|DB_NAME)\s*=\s*([^)\s]*)\s*\)"),
    # compound keys are spelled out (`ORACLE_SID=`, where `\b` fails after
    # the `_`); the generic ones must not be the tail of another key
    # (`STATIC_SERVICE=TRUE`, `CURRENT_USER=`)
    ("USERKV", r"(?<![A-Za-z0-9_])USER\s*=\s*" + _Q + r"([^)\s,;\"']*)"),
    # unified/OS audit records: CLIENT USER:[4] 'jdoe'
    ("AUDIT", r"(?:CLIENT USER|DATABASE USER|OS_USER|CLIENT_USER|USERID|USERHOST|DBID)"
              r"\s*:\s*\[\d+\]\s*(?:'([^']*)'|\"([^\"]*)\")"),
    # not SERVER= (DEDICATED/SHARED)
    ("HOSTKV", r"(?<![A-Za-z0-9_])(?:host|hostname|userhost|client_host)\s*=\s*" + _Q
               + r"([A-Za-z0-9._#-]+)"),
    ("DBKV", r"(?<![A-Za-z0-9_])(?:oracle_sid|oracle_unqname|db_unique_name|db_name|sid"
             r"|instance_name|dbid)\s*=\s*" + _Q + r"([A-Za-z0-9._$#+-]+)"),
    ("SVCKV", r"(?<![A-Za-z0-9_])(?:service_names?|service)\s*=\s*" + _Q + r"([A-Za-z0-9._$#,-]+)"),
    ("PATH", r"(?<![\w/$.~-])/(?!(?:%s)(?:/|\b))[A-Za-z0-9._+-]+/[^\s\"'`)>,;|]+"
             % "|".join(BENIGN_PATH_ROOTS)),
    ("PATH", r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'`<>|,;]*"),
    # Oracle names that embed a database, SID or host: ASM file names, ADR
    # homes, alert logs and trace files
    ("ASM", r"(?<![\w+])\+[A-Za-z][A-Za-z0-9_]*/([^/\s\"'`),;]+)"),
    ("DIAG", r"(?<![\w-])diag/(?:rdbms|tnslsnr|crs|asm|clients?)/([^/\s\"'`),;]+)"
             r"(?:/([^/\s\"'`),;]+))?"),
    ("ALERT", r"(?<![A-Za-z0-9$#])alert_([A-Za-z0-9$#+_-]+?)\.log"),
    ("TRACE", r"(?<![A-Za-z0-9$#_+])([A-Za-z][A-Za-z0-9$#]*)_[a-z][a-z0-9]{1,5}_\d+(?:_\d+)?\.tr[cm]"),
    ("IDX", r"\.ds-[a-z0-9_.-]+-\d{4}\.\d{2}\.\d{2}-\d{6}"),
    ("IDX", r"\b(?:oracle-logs|logs-oracle)[a-z0-9_.-]*"),
    ("DOCID", r"(?<![\w-])[A-Za-z0-9_-]{20}(?![\w-])"),
    # any dotted alphanumeric token; `dotted_host` decides whether it is one.
    # `#` allowed inside labels: compactor templates spell `lab-dg#.localdomain`;
    # `$` around it is a fixed view or dictionary table (`v$session.sid`)
    ("FQDN", r"(?<![\w#.$-])[a-z0-9#](?:[a-z0-9#-]{0,62}[a-z0-9#])?"
             r"(?:\.[a-z0-9#](?:[a-z0-9#-]{0,62}[a-z0-9#])?)+(?![\w#$-]|\.[\w#]|\s*=)"),
    # the same with `_` in a label: never DNS, but a service name carries
    # one (`payroll_rw.world`); identifying only under a known domain suffix
    ("SVCDOM", r"(?<![\w#.$-])[a-z0-9#][a-z0-9_#-]*(?:\.[a-z0-9_#-]+)+(?![\w#$-]|\.[\w#]|\s*=)"),
]


def _peel_exts(tok: str) -> tuple[str, str]:
    """`dbsrv77.corpx.log` -> (`dbsrv77.corpx`, `.log`); every trailing label
    that is a file extension is peeled (`tnsnames.ora.bak`)."""
    core, ext = tok, ""
    while "." in core:
        head, last = core.rsplit(".", 1)
        if not head or last.lower() not in FILE_EXTS:
            break
        core, ext = head, "." + last + ext
    return core, ext


def _compile(vocab: Vocabulary, patterns: list[tuple[str, str]]):
    """One alternation: every vocabulary term (longest first, escaped) then
    the given patterns, each as a named group `g<i>`. Returns the regex and
    the group-name -> (kind, is_vocab, inner group count) table."""
    terms: list[tuple[str, str]] = []
    for cat, s in vocab.categories():
        terms.extend((cat, t) for t in s)
    terms.sort(key=lambda ct: (-len(ct[1]), ct[1]))
    entries: list[tuple[str, str]] = []          # (regex, category)
    for cat, t in terms:
        rx = re.escape(t)
        if vocab.whole_word(t):
            rx = rf"(?<![a-z0-9]){rx}(?![a-z0-9])"
        entries.append((rx + _HOST_TAIL if cat in _TAILED else rx, cat))
    # digit families: siblings of known names share the shape (`ol9-19-dg1`
    # -> `ol\d+-\d+-dg\d+` also catches the never-seen `ol9-19-dg3`;
    # `cdb1` -> `cdb\d+`). After the exact terms so those win where they apply.
    families: dict[str, str] = {}                # regex -> category (DB wins ties)
    for cat, t in terms:                          # terms iterate DB, HOST, SVC in order
        if cat in _TAILED and any(c.isdigit() for c in t) and "#" not in t:
            stem = _HASHED_RE.sub("#", t).replace("#", "")
            if len(stem) < 3:
                continue
            fam = _HASHED_RE.sub(r"(?:\\d+|#)", re.escape(t))
            families.setdefault(fam + _HOST_TAIL, cat)
    entries.extend(sorted(families.items(), key=lambda e: (-len(e[0]), e[0])))
    parts, table = [], {}
    for i, (rx, cat) in enumerate(entries):
        parts.append(f"(?P<g{i}>{rx})")
        table[f"g{i}"] = (cat, True, 0)
    for j, (kind, pat) in enumerate(patterns):
        name = f"g{len(entries) + j}"
        parts.append(f"(?P<{name}>{pat})")
        table[name] = (kind, False, re.compile(pat).groups)
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
        # what a dotted token that is not a host is swept with: every pattern
        # but the dotted-token one itself (`abc.10.1.2.3` still holds an IP)
        self._inner, self._table_inner = _compile(
            vocab, [p for p in PATTERNS if p[0] not in ("URL", "FQDN", "SVCDOM", "DOCID")])
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

    def _sweep_inner(self, tok: str) -> str:
        return self._inner.sub(lambda m: self._replace(m, self._table_inner), tok)

    @staticmethod
    def _group_category(kind: str, whole: str, nth: int, inner: str) -> str | None:
        """Pseudonym category for the `nth` inner group of a key/value or
        Oracle-name match, or None when the value identifies nothing."""
        low = inner.lower()
        if not inner or low in KEEP_ADDRESSES or low in KEEP_NAMES:
            return None
        if kind == "HOSTKV":
            return "IP" if _is_ipv4(inner) else "HOST"
        if kind == "SVCKV":
            return "SVC"
        if kind == "DBKV":
            return None if low.startswith("+") else "DB"
        if kind == "USERKV":
            return "USER"
        if kind == "AUDIT":
            key = whole.split(":", 1)[0].strip().upper()
            if key == "USERHOST":
                return "IP" if _is_ipv4(inner) else "HOST"
            if key == "DBID":
                return "DB"
            if key == "DATABASE USER" and low in KEEP_DB_USERS:
                return None
            return "USER"
        # ASM, DIAG, ALERT, TRACE: ASM/CRS instances and default names are
        # the same on every system
        if low.startswith("+") or low in ("asm", "crs", "listener"):
            return None
        if kind == "DIAG":
            sub = whole.split("/")[1].lower()
            if sub == "rdbms":
                return "DB"
            if sub == "tnslsnr":
                return "HOST" if nth == 0 else "SVC"
            if sub == "crs":
                return "HOST" if nth == 0 else None
            return None
        return "DB"

    def _replace_groups(self, m: re.Match, kind: str, ngroups: int) -> str:
        """Pseudonymize exactly the spans of the inner groups; everything
        between them is swept with the vocabulary only."""
        base = m.re.groupindex[m.lastgroup]
        tok, start = m.group(0), m.start()
        out, pos = [], 0
        for nth in range(ngroups):
            s, e = m.span(base + 1 + nth)
            if s < 0:
                continue
            inner = m.group(base + 1 + nth)
            cat = self._group_category(kind, tok, nth, inner)
            out.append(self._sweep_vocab(tok[pos:s - start]))
            out.append(self.pseudonym(cat, inner) if cat else self._sweep_vocab(inner))
            pos = e - start
        out.append(self._sweep_vocab(tok[pos:]))
        return "".join(out)

    def _replace(self, m: re.Match, table: dict) -> str:
        kind, is_vocab, ngroups = table[m.lastgroup]
        tok = m.group(0)
        if is_vocab:
            if kind in _TAILED and "." in tok:
                core, ext = _peel_exts(tok)
                if ext:
                    return self.pseudonym(kind, core) + ext
            return self.pseudonym(kind, tok)
        text = m.string
        if kind == "URL":
            host = url_domain(tok)
            if host and _under(host, self.vocab.source_domains):
                return self._sweep_vocab(tok)
            return self._sweep_nourl(tok)
        if kind == "EMAIL":
            local, dom = tok.split("@", 1)
            if local.lower() in PROGRAMS:
                # `sqlplus@dbsrv77`: a client program on a host, as
                # v$session and the audit trail print it (a program name is
                # never a person's mailbox)
                keep = dom.lower() in KEEP_ADDRESSES
                return f"{local}@{dom if keep else self.pseudonym('HOST', dom)}"
            return self.pseudonym("USER", tok)
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
        if ngroups:
            return self._replace_groups(m, kind, ngroups)
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
            core, ext = _peel_exts(tok)
            if "." in core and not _is_ipv4(core) and dotted_host(core, self.vocab) \
                    and (not ext or _fqdn_like(core, self.vocab.domain_suffixes)
                         or any(c.isdigit() for c in core)):
                return self.pseudonym("HOST", core) + ext
            return self._sweep_inner(tok)
        if kind == "SVCDOM":
            core, ext = _peel_exts(tok)
            if "." in core and _fqdn_like(core, self.vocab.domain_suffixes) \
                    and not _under(core, self.vocab.source_domains):
                return self.pseudonym("SVC", core) + ext
            return self._sweep_inner(tok)
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

    def outbound(self, obj):
        """The one way a payload crosses the boundary: redact every string,
        then run the independent leak check over the result. Returns the
        redacted payload; raises RedactionLeak (fail closed) on any hit.
        Truncate *after* this, never before: a name cut in half no longer
        matches its vocabulary term."""
        out = self.redact_obj(obj)
        self.check(out)
        return out

    def demap(self, text: str) -> str:
        """Replace pseudonyms the researcher echoed back with the originals.
        Longest pseudonym first so `DB_AB` is not eaten by `DB_A`. A
        pseudonym glued to lower-case letters or digits (`DB_Astby`,
        `xDB_A`) is still one — the redactor keeps the neighbours' case —
        but one followed by an upper-case letter is an Oracle name
        (`DB_BLOCK_SIZE`) and is left alone."""
        if not text or not self.mapping:
            return text
        pat = re.compile(r"(?<![A-Z])(?:%s)(?![A-Z])" % "|".join(
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
        """Independent re-scan of an already-redacted payload (`leak_scan`):
        pseudonyms are blanked and every string is searched for vocabulary
        terms, addresses, host-shaped tokens, key=value names, audit users,
        e-mail addresses and Oracle file/path names — with its own scanners,
        not the redactor's regexes, so a gap in PATTERNS is not also a gap
        here. Returns the offending tokens (empty = clean)."""
        hits: list[str] = []
        for s in _strings(obj):
            hits.extend(leak_scan(s, self.vocab))
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


# ---- the independent leak check ---------------------------------------------------
#
# Written against the *policy* (what counts as identifying: vocabulary terms,
# addresses, host names, key=value names, …), not against PATTERNS: its own
# scanners and a token-based host check instead of the redactor's single
# alternation, so a lookaround, an ordering effect or a replacement bug in the
# redactor is not silently repeated here. Shared with the redactor are only the
# policy tables (FILE_EXTS, KEEP_*, domain suffixes, `dotted_host`,
# `_release_quad`): if the two ever disagree the check is the stricter one,
# and a disagreement is a refused request, never a leak.

_BLANK = "\x00"
_NUM_RUN_RE = re.compile(r"(?<![0-9])(?<![0-9]\.)[0-9]+(?:\.[0-9]+)+")
_V6_RUN_RE = re.compile(r"[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*")
_IP_PSEUDO_TAIL_RE = re.compile(r"(?<![A-Za-z])IP_[A-Z]{1,3}((?:\.[0-9]{1,3}){1,3})(?![0-9])")
_KV_SCAN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(host|hostname|userhost|client_host|service_names?|service|sid"
    r"|oracle_sid|oracle_unqname|db_unique_name|db_name|instance_name|global_dbname"
    r"|sid_name|pdbname|dbid|user)\s*=\s*[\"']?([^\s()\"',;]+)", re.I)
_AUDIT_SCAN_RE = re.compile(
    r"(client user|database user|os_user|client_user|userid|userhost|dbid)"
    r"\s*:\s*\[[0-9]+\]\s*['\"]([^'\"]*)['\"]", re.I)
_EMAIL_SCAN_RE = re.compile(r"[A-Za-z0-9._%+-]*[A-Za-z0-9]@[A-Za-z0-9\x00]")
_ASM_SCAN_RE = re.compile(r"\+[A-Za-z][A-Za-z0-9_]*/([^/\s\"'`),;]+)")
_DIAG_SCAN_RE = re.compile(
    r"diag/(rdbms|tnslsnr|crs|asm|clients?)/([^/\s\"'`),;]+)(?:/([^/\s\"'`),;]+))?", re.I)
_ALERT_SCAN_RE = re.compile(r"alert_([^./\s\"'`]+)\.log", re.I)
_TRACE_SCAN_RE = re.compile(
    r"(?<![A-Za-z0-9$#_+\x00])([A-Za-z][A-Za-z0-9$#]*)_[a-z][a-z0-9]{1,5}_[0-9]+"
    r"(?:_[0-9]+)?\.tr[cm]")
_WIN_SCAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\+[A-Za-z0-9]")
_TOKEN_SPLIT_RE = re.compile(r"[\s()\[\]{}<>\"'`,;|=*!?]+")
_PIECE_SPLIT_RE = re.compile(r"[/\\:@]+")
_DOTTED_RE = re.compile(r"[A-Za-z0-9#\x00][A-Za-z0-9#\x00-]*(?:\.[A-Za-z0-9#\x00-]+)+")
_KV_KEEP = KEEP_ADDRESSES | KEEP_NAMES | {"dedicated", "shared", "pooled", "*", "%"}


def _redacted(value: str) -> bool:
    """Nothing but pseudonyms, digits and punctuation left in a value."""
    return not re.search(r"[A-Za-z]", value.replace(_BLANK, ""))


def _scan_vocab(s: str, low: str, vocab: Vocabulary):
    for _cat, terms in vocab.categories():
        for t in terms:
            if vocab.whole_word(t):
                for m in re.finditer(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(t), low):
                    yield s[m.start():m.end()]
                continue
            i = low.find(t)
            while i >= 0:
                yield s[i:i + len(t)]
                i = low.find(t, i + 1)
    for cat, terms in vocab.categories():
        if cat not in _TAILED:
            continue
        for t in terms:
            if "#" in t or not any(c.isdigit() for c in t):
                continue
            if len(re.sub(r"[0-9]+", "", t)) < 3:
                continue
            fam = re.sub(r"[0-9]+", lambda _m: r"[0-9]+", re.escape(t))
            for m in re.finditer(fam, low):
                yield s[m.start():m.end()]


_UNDERSCORE_DOTTED_RE = re.compile(r"[A-Za-z0-9_#\x00-]+(?:\.[A-Za-z0-9_#\x00-]+)+")


def _scan_host(piece: str, vocab: Vocabulary) -> bool:
    piece = piece.strip(".-")
    if "_" in piece:
        if not _UNDERSCORE_DOTTED_RE.fullmatch(piece):
            return False
        core = _peel_exts(piece)[0].replace(_BLANK, "x")
        return "." in core and _fqdn_like(core, vocab.domain_suffixes) \
            and not _under(core, vocab.source_domains)
    if not _DOTTED_RE.fullmatch(piece) or not piece.replace(_BLANK, "").strip(".-"):
        return False
    core, ext = _peel_exts(piece)
    if "." not in core:
        return False
    probe = core.replace(_BLANK, "x")
    if ext and not (_fqdn_like(probe, vocab.domain_suffixes)
                    or any(c.isdigit() for c in core)):
        return False
    if not any(c.isalpha() for c in probe.rsplit(".", 1)[-1]):
        return False                       # numbers are the address scan's job
    return dotted_host(probe, vocab)


_KEY_BEFORE_EQ_RE = re.compile(r"[A-Za-z0-9_#.$-]+(?=\s*=)")


def _scan_tokens(s: str, vocab: Vocabulary):
    # `blksz.grain=4096.8`: a dotted name in front of `=` is a key
    s = _KEY_BEFORE_EQ_RE.sub(" ", s)
    for tok in _TOKEN_SPLIT_RE.split(s):
        tok = tok.rstrip(".:")
        if not tok:
            continue
        scheme = re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", tok)
        if scheme:
            rest = tok[scheme.end():]
            host = re.split(r"[/:?#]", rest, maxsplit=1)[0].lower()
            if host and _under(host, vocab.source_domains):
                continue                  # approved citation: kept whole on purpose
            tok = rest
        elif tok.startswith("/"):
            segs = [x for x in tok.split("/") if x]
            if len(segs) >= 2 and segs[0] not in BENIGN_PATH_ROOTS \
                    and not tok.startswith("//"):
                yield tok
                continue
        for piece in _PIECE_SPLIT_RE.split(tok):
            if piece and _scan_host(piece, vocab):
                yield piece.replace(_BLANK, "…")


def leak_scan(text: str, vocab: Vocabulary) -> list[str]:
    """Identifying tokens left in `text` (see the section comment above).
    Pseudonyms (`DB_A`, `HOST_B`, …) are blanked first; a pseudonym still
    followed by `.octets` or a domain is itself a hit."""
    if not text:
        return []
    hits: list[str] = []
    hits += [m.group(0) for m in _IP_PSEUDO_TAIL_RE.finditer(text)]
    s = _PSEUDONYM_RE.sub(_BLANK, text)
    low = s.lower()
    hits += list(_scan_vocab(s, low, vocab))
    for m in _NUM_RUN_RE.finditer(s):
        tok = m.group(0)
        parts = tok.split(".")
        if len(parts) == 4 and _is_ipv4(tok) and tok not in KEEP_ADDRESSES \
                and not _release_quad(parts):
            hits.append(tok)
    for m in _V6_RUN_RE.finditer(s):
        tok = m.group(0).rstrip(".")
        if tok.count(":") < 2 or tok in KEEP_ADDRESSES:
            continue
        try:
            ipaddress.IPv6Address(tok)
        except ValueError:
            continue
        hits.append(tok)
    for m in _KV_SCAN_RE.finditer(s):
        key, val = m.group(1).lower(), m.group(2)
        if val.lower() in _KV_KEEP or val.startswith("+"):
            continue
        if not _redacted(val) or (key == "dbid" and re.search(r"[0-9]", val)):
            hits.append(val)
    for m in _AUDIT_SCAN_RE.finditer(s):
        key, val = m.group(1).lower(), m.group(2)
        if key == "database user" and val.lower() in KEEP_DB_USERS:
            continue
        if val.replace(_BLANK, "").strip():
            hits.append(val)
    for m in _EMAIL_SCAN_RE.finditer(s):
        local = m.group(0).split("@", 1)[0]
        if local.lower() in PROGRAMS and s[m.end() - 1] == _BLANK:
            continue
        hits.append(m.group(0))
    for m in _ASM_SCAN_RE.finditer(s):
        val = m.group(1)
        if not _redacted(val) and not val.startswith("+") and val.lower() != "asm":
            hits.append(val)
    for m in _DIAG_SCAN_RE.finditer(s):
        sub = m.group(1).lower()
        for nth, val in enumerate((m.group(2), m.group(3))):
            if not val or _redacted(val) or val.startswith("+") \
                    or val.lower() in ("asm", "crs", "listener"):
                continue
            if sub == "rdbms" or sub == "tnslsnr" or (sub == "crs" and nth == 0):
                hits.append(val)
    for m in _ALERT_SCAN_RE.finditer(s):
        if not _redacted(m.group(1)) and not m.group(1).startswith("+"):
            hits.append(m.group(1))
    hits += [m.group(1) for m in _TRACE_SCAN_RE.finditer(s)]
    hits += [m.group(0) for m in _WIN_SCAN_RE.finditer(s)]
    hits += list(_scan_tokens(s, vocab))
    return [h.replace(_BLANK, "…") for h in hits]


# ---- `dbwiki redact --fuzz` -----------------------------------------------------------

# identifying shapes, filled with live vocabulary terms and with synthetic
# names no vocabulary knows ({db}, {host}, {ip}, {svc}; upper-cased variants
# as {DB}, {HOST}); every filled value must be gone from the output
FUZZ_LEAKS = [
    "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST = {host})(PORT = 1521))"
    "(CONNECT_DATA = (SERVICE_NAME = {svc})))",
    "ORACLE_SID={db} ORACLE_UNQNAME={db}_stby DB_UNIQUE_NAME = {db}",
    "connection from {ip}. addr_{ip} refused; client ::ffff:{ip} dropped",
    "http://{host}:1158/em and https://{ip}/x",
    "service {db}.bank.cz is down; {svc}.world refused",
    "Trace file /u01/app/oracle/diag/rdbms/{db}/{db}/trace/{db}_ora_123.trc",
    "diag/rdbms/{db}/{db}, alert_{db}.log and {db}_lgwr_7.trm",
    "+DATA/{db}/DATAFILE/system.256.1",
    "D:\\app\\oracle\\oradata\\{db}\\system01.dbf",
    "CLIENT USER:[6] 'jdoe' USERHOST:[9] '{host}' DBID:[10] '1234567890'",
    "mail from oracle@{host} to jan.novak@bank.cz",
]
# bare names: only the vocabulary can know these, so they are filled with
# live terms only (the synthetic names would be a guaranteed miss)
FUZZ_VOCAB_LEAKS = [
    "{db}stby on {host}vip and {host}-vip",
    "{HOST}:1521 and {DB} are down; {svc} refused",
]
# public text research needs: must come out byte-identical and pass the check
FUZZ_KEEPS = [
    "ORA-12541: TNS:no listener; TNS-12560, ORA-00600 [kdsgrp1]",
    "Version 19.27.0.0.0, RU 19.27.0.0.250415, 12.2.0.1, 11.2.0.4, 21.3.0.0",
    "check sqlnet.ora, listener.ora, tnsnames.ora and alert.log",
    'ORA-06512: at "SYS.DBMS_STATS", line 123',
    "query v$session.sid and x$ksppi.ksppinm",
    "e.g. set it, i.e. the default",
    "$ORACLE_HOME/network/admin/sqlnet.ora and /etc/oratab",
    "from 00:00:09.776Z on 0.0.0.0, 127.0.0.1, localhost and ::1",
    "(CONNECT_DATA=(SERVER=DEDICATED)) (PDBNAME=CDB$ROOT)",
    "DB_BLOCK_SIZE and DB_RECOVERY_FILE_DEST_SIZE",
]
_FUZZ_SYNTHETIC = {"db": "payroll2", "host": "dbsrv77.corpx", "ip": "10.1.2.3",
                   "svc": "payroll_rw"}


def print_fuzz(rows: list[dict], out, vocab: Vocabulary | None = None) -> int:
    """`dbwiki redact --fuzz` output: every failing probe with its output and
    problems, how many vocabulary terms each source contributed (when
    `vocab` is given: `harvested 0` means the compactor has not recorded
    host names yet), then a one-line tally. Exit code 0 when all pass, 2
    when any fails (the same code a leak gets from `dbwiki redact`)."""
    bad = [r for r in rows if not r["ok"]]
    for r in bad:
        print(f"FAIL [{r['kind']}] {r['input']}\n  -> {r['output']}\n  "
              + "; ".join(r["problems"]), file=out)
    if vocab is not None:
        print("vocabulary sources: " + ", ".join(
            f"{o} {n}" for o, n in vocab.sources().items()), file=out)
    print(f"fuzz: {len(rows) - len(bad)}/{len(rows)} probes clean", file=out)
    return 2 if bad else 0


def fuzz(vocab: Vocabulary, per_category: int = 3) -> list[dict]:
    """The acceptance probe (`dbwiki redact --fuzz`): every FUZZ_LEAKS shape
    filled with up to `per_category` live vocabulary terms per category, and
    with synthetic names, redacted and leak-checked; every FUZZ_KEEPS line
    must survive unchanged. One row per probe: kind, input, output, ok,
    problems."""
    def live(cat: str) -> list[str]:
        terms = dict(vocab.categories()).get(cat, set())
        # whole-word terms cannot be probed glued (`{host}vip`)
        return sorted(t for t in terms
                      if "#" not in t and not vocab.whole_word(t))[:per_category]

    fills = [dict(_FUZZ_SYNTHETIC)]
    pools = {"db": live("DB"), "host": live("HOST"), "ip": live("IP"), "svc": live("SVC")}
    for i in range(max((len(v) for v in pools.values()), default=0)):
        fills.append({k: (v[i % len(v)] if v else _FUZZ_SYNTHETIC[k])
                      for k, v in pools.items()})
    rows: list[dict] = []
    for n, fill in enumerate(fills):
        values = {**fill, **{k.upper(): v.upper() for k, v in fill.items()}}
        for tmpl in FUZZ_LEAKS + (FUZZ_VOCAB_LEAKS if n else []):
            text = tmpl.format(**values)
            secrets = [v for k, v in values.items() if "{%s}" % k in tmpl]
            secrets += [s for s in ("bank.cz", "jdoe", "jan.novak")
                        if s in text]
            red = Redactor(vocab, "fuzz")
            out = red.redact(text)
            problems = [f"{s!r} survived" for s in dict.fromkeys(secrets)
                        if s.lower() in out.lower()]
            problems += [f"leak check: {h!r}" for h in red.leak_check(out)]
            rows.append({"kind": "leak", "input": text, "output": out,
                         "ok": not problems, "problems": problems})
    for text in FUZZ_KEEPS:
        red = Redactor(vocab, "fuzz")
        out = red.redact(text)
        problems = [] if out == text else ["changed"]
        problems += [f"leak check: {h!r}" for h in red.leak_check(text)]
        rows.append({"kind": "keep", "input": text, "output": out,
                     "ok": not problems, "problems": problems})
    return rows


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
