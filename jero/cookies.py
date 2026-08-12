"""Cookies: :class:`SetCookie` (the response side) and the request-side header codec.

The response vocabulary (``SetCookie``, ``encode_set_cookie``) and the request-side
parser (``parse_cookie_header``) live together in this leaf module, mirroring
:mod:`jero.headers`'s split between the opaque request bag and its own leaf module.
Neither ``core`` nor any Struct crosses through here — ``SetCookie`` is a plain
dataclass, never a wire model.
"""

from dataclasses import KW_ONLY, dataclass
from datetime import UTC, datetime
from typing import Literal

# RFC 2616 token separators, plus space and tab — characters a cookie *name* may not
# contain. Control characters are rejected by the printable-ASCII range check alongside
# this set, so the set itself only needs to list the printable exceptions.
_NAME_SEPARATORS = frozenset('()<>@,;:\\"/[]?={} \t')

# Characters RFC 6265 excludes from a cookie *value* even though they are otherwise
# printable ASCII: space, DQUOTE, comma, semicolon, backslash.
_VALUE_EXCLUDED = frozenset(' ",;\\')

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _is_token_char(char: str) -> bool:
    return 0x21 <= ord(char) <= 0x7E and char not in _NAME_SEPARATORS


def _is_cookie_octet(char: str) -> bool:
    return 0x21 <= ord(char) <= 0x7E and char not in _VALUE_EXCLUDED


def _is_path_char(char: str) -> bool:
    """RFC 6265's ``path-value``: any character but a control character or ``;``.
    Unlike a cookie name/value, space is allowed — a real path segment may need one."""
    return 0x20 <= ord(char) <= 0x7E and char != ";"


def _is_domain_char(char: str) -> bool:
    """ASCII hostname characters only. Not a full RFC 1123 label-length validation —
    just enough to keep a domain from being anything but a hostname, which is what
    stops it from being usable as a header-injection vector (embedded CR/LF, another
    ``Set-Cookie``, and so on)."""
    return char.isascii() and (char.isalnum() or char in "-.")


def _validate_name(name: str) -> None:
    if not name:
        raise ValueError("SetCookie: name must be non-empty")
    if not all(_is_token_char(char) for char in name):
        raise ValueError(f"SetCookie: name {name!r} contains characters not allowed by RFC 6265")


def _validate_value(value: str) -> None:
    if not all(_is_cookie_octet(char) for char in value):
        raise ValueError(f"SetCookie: value {value!r} contains characters not allowed by RFC 6265")


def _validate_path(path: str) -> None:
    if not path.startswith("/"):
        raise ValueError(f"SetCookie: path {path!r} must start with '/'")
    if not all(_is_path_char(char) for char in path):
        raise ValueError(f"SetCookie: path {path!r} contains characters not allowed by RFC 6265")


def _validate_domain(domain: str) -> None:
    if not domain:
        raise ValueError("SetCookie: domain must be non-empty (or None)")
    if not all(_is_domain_char(char) for char in domain):
        raise ValueError(
            f"SetCookie: domain {domain!r} contains characters not allowed by RFC 6265"
        )


# The only three values RFC 6265bis defines for SameSite. An allow-list, not a
# character check, because it's a closed set — anything outside it is simply wrong,
# not merely differently-shaped, and a caller building it from a non-literal source
# (config, an env var, a `cast` to satisfy the type checker) gets the same construction-
# time rejection as a hand-typed typo. It also closes the header-injection angle a
# character check would (same_site is interpolated verbatim into the Set-Cookie value).
_SAME_SITE_VALUES = frozenset({"strict", "lax", "none"})


def _validate_same_site(same_site: str) -> None:
    if same_site not in _SAME_SITE_VALUES:
        raise ValueError(f"SetCookie: same_site {same_site!r} must be 'strict', 'lax', or 'none'")


def _imf_fixdate(moment: datetime) -> str:
    """``moment`` as an RFC 9110 IMF-fixdate in GMT (``Wdy, DD Mon YYYY HH:MM:SS GMT``).

    Spelled out with lookup tables rather than ``strftime("%a, %d %b %Y ...")``: ``%a``/``%b``
    are locale-dependent, and a cookie's ``Expires`` attribute must always read in English.
    """
    moment = moment.astimezone(UTC)
    weekday = _WEEKDAYS[moment.weekday()]
    month = _MONTHS[moment.month - 1]
    time = moment.strftime("%H:%M:%S")
    return f"{weekday}, {moment.day:02d} {month} {moment.year:04d} {time} GMT"


@dataclass(frozen=True, slots=True)
class SetCookie:
    """One ``Set-Cookie`` response header, secure by default.

    A bare ``SetCookie("session", token)`` is already ``Path=/; Secure; HttpOnly;
    SameSite=Lax`` — loosening any of that (``http_only=False`` for a JS-readable
    cookie, ``secure=False``) is explicit and visible in review. Modern browsers treat
    ``http://localhost`` as a trustworthy origin, so ``Secure`` cookies still work in
    local dev.

    Validation runs at construction (``ValueError`` on the offending attribute), not at
    emission — a rejected cookie never becomes a route's problem.
    """

    name: str
    value: str = ""
    _: KW_ONLY
    max_age: int | None = None
    expires: datetime | None = None
    path: str | None = "/"
    domain: str | None = None
    secure: bool = True
    http_only: bool = True
    same_site: Literal["strict", "lax", "none"] | None = "lax"
    partitioned: bool = False

    def __post_init__(self) -> None:
        _validate_name(self.name)
        _validate_value(self.value)
        if self.max_age is not None and (
            isinstance(self.max_age, bool) or not isinstance(self.max_age, int)
        ):
            raise ValueError("SetCookie: max_age must be an int")
        if self.expires is not None and self.expires.tzinfo is None:
            raise ValueError("SetCookie: expires must be timezone-aware")
        if self.path is not None:
            _validate_path(self.path)
        if self.domain is not None:
            _validate_domain(self.domain)
        if self.same_site is not None:
            _validate_same_site(self.same_site)
        if self.same_site == "none" and not self.secure:
            raise ValueError("SetCookie: same_site='none' requires secure=True")
        if self.partitioned and not self.secure:
            raise ValueError("SetCookie: partitioned=True requires secure=True")
        if self.name.startswith("__Host-") and not (
            self.secure and self.path == "/" and self.domain is None
        ):
            raise ValueError(
                "SetCookie: a '__Host-' cookie requires secure=True, path='/', domain=None"
            )
        if self.name.startswith("__Secure-") and not self.secure:
            raise ValueError("SetCookie: a '__Secure-' cookie requires secure=True")

    @classmethod
    def expire(cls, name: str, *, path: str | None = "/", domain: str | None = None) -> "SetCookie":
        """A cookie that clears ``name`` on the browser: empty value, ``Max-Age=0``, and
        ``Expires`` at the Unix epoch (belt and braces for clients that ignore ``Max-Age``).

        ``path``/``domain`` must match the cookie being cleared — a browser only removes a
        cookie whose scope matches exactly.
        """
        return cls(
            name,
            max_age=0,
            expires=datetime.fromtimestamp(0, tz=UTC),
            path=path,
            domain=domain,
        )


def encode_set_cookie(cookie: SetCookie) -> str:
    """``cookie`` as one ``Set-Cookie`` header value, attributes in a fixed order,
    ``None``/``False`` attributes omitted."""
    parts = [f"{cookie.name}={cookie.value}"]
    if cookie.max_age is not None:
        parts.append(f"Max-Age={cookie.max_age}")
    if cookie.expires is not None:
        parts.append(f"Expires={_imf_fixdate(cookie.expires)}")
    if cookie.domain is not None:
        parts.append(f"Domain={cookie.domain}")
    if cookie.path is not None:
        parts.append(f"Path={cookie.path}")
    if cookie.secure:
        parts.append("Secure")
    if cookie.http_only:
        parts.append("HttpOnly")
    if cookie.same_site is not None:
        parts.append(f"SameSite={cookie.same_site.capitalize()}")
    if cookie.partitioned:
        parts.append("Partitioned")
    return "; ".join(parts)


def parse_cookie_header(value: str) -> dict[str, str]:
    """The ``Cookie`` request header as a name -> value dict.

    Lenient by design: a browser sends every cookie scoped to the domain/path, including
    other applications' garbage, so a malformed *fragment* is skipped rather than failing
    the whole header. Split on ``;``, each fragment on the first ``=``; surrounding
    whitespace and one pair of surrounding double quotes are stripped from the value; the
    first occurrence of a duplicated name wins. No percent-decoding — RFC 6265 defines
    none, and jero does not guess at app-level encoding conventions.
    """
    cookies: dict[str, str] = {}
    for fragment in value.split(";"):
        name, sep, raw_value = fragment.partition("=")
        if not sep:
            continue  # no '=' at all: a bare token, not a name=value pair
        name = name.strip()
        if not name or name in cookies:
            continue
        raw_value = raw_value.strip()
        if len(raw_value) >= 2 and raw_value[0] == '"' and raw_value[-1] == '"':
            raw_value = raw_value[1:-1]
        cookies[name] = raw_value
    return cookies
