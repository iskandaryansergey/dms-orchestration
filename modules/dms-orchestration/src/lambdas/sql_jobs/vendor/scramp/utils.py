import hashlib
import hmac as hmaca
import unicodedata
from base64 import b64decode, b64encode
from enum import Enum
from stringprep import (
    in_table_a1,
    in_table_b1,
    in_table_c3,
    in_table_c4,
    in_table_c5,
    in_table_c6,
    in_table_c7,
    in_table_c8,
    in_table_c9,
    in_table_c12,
    in_table_c21_c22,
    in_table_d1,
    in_table_d2,
)
from types import NoneType


class ServerError(Enum):
    INVALID_ENCODING = "invalid-encoding"
    EXTENSIONS_NOT_SUPPORTED = "extensions-not-supported"
    INVALID_PROOF = "invalid-proof"
    CHANNEL_BINDINGS_DONT_MATCH = "channel-bindings-dont-match"
    SERVER_DOES_SUPPORT_CHANNEL_BINDING = "server-does-support-channel-binding"
    CHANNEL_BINDING_NOT_SUPPORTED = "channel-binding-not-supported"
    UNSUPPORTED_CHANNEL_BINDING_TYPE = "unsupported-channel-binding-type"
    UNKNOWN_USER = "unknown-user"
    INVALID_USERNAME_ENCODING = "invalid-username-encoding"
    NO_RESOURCES = "no-resources"
    OTHER_ERROR = "other-error"


class ScramException(Exception):
    def __init__(self, message, server_error=None):
        super().__init__(message)
        if not isinstance(server_error, (NoneType, ServerError)):
            raise ScramException(
                "Server error must be None or of type ServerError",
                ServerError.OTHER_ERROR,
            )
        self.server_error = server_error

    def __str__(self):
        s_str = "" if self.server_error is None else f": {self.server_error.value}"
        return super().__str__() + s_str


def hmac(hf, key, msg):
    return hmaca.new(key, msg=msg, digestmod=hf).digest()


def pbkdf2_hmac_pure_python(hf, password, salt, iterations):
    u = ui = hmac(hf, password, salt + b"\x00\x00\x00\x01")
    for i in range(int(iterations) - 1):
        ui = hmac(hf, password, ui)
        u = xor(u, ui)
    return u


def pbkdf2_hmac_hashlib(hf, password, salt, iterations):
    return hashlib.pbkdf2_hmac(hf().name, password, salt, int(iterations))


def hi(hf, password, salt, iterations):
    if hasattr(hashlib, "pbkdf2_hmac"):
        pbkdf2_hmac = pbkdf2_hmac_hashlib
    else:
        pbkdf2_hmac = pbkdf2_hmac_pure_python
    return hi_pbkdf2_hmac(pbkdf2_hmac, hf, password, salt, iterations)


def hi_pbkdf2_hmac(pbkdf2_hmac, hf, password, salt, iterations):
    return pbkdf2_hmac(hf, password, salt, iterations)


def h(hf, msg):
    return hf(msg).digest()


def xor(bytes1, bytes2):
    return bytes(a ^ b for a, b in zip(bytes1, bytes2, strict=True))


def b64enc(binary):
    return b64encode(binary).decode("utf8")


def b64dec(string):
    try:
        return b64decode(string, validate=True)
    except ValueError:
        raise ScramException("Invalid base 64 encoding", ServerError.INVALID_ENCODING)


def uenc(string):
    return string.encode("utf-8")


def saslprep(source):
    # mapping stage
    #   - map non-ascii spaces to U+0020 (stringprep C.1.2)
    #   - strip 'commonly mapped to nothing' chars (stringprep B.1)
    data = "".join(" " if in_table_c12(c) else c for c in source if not in_table_b1(c))

    # normalize to KC form
    data = unicodedata.ucd_3_2_0.normalize("NFKC", data)
    if not data:
        return ""

    # check for invalid bi-directional strings.
    # stringprep requires the following:
    #   - chars in C.8 must be prohibited.
    #   - if any R/AL chars in string:
    #       - no L chars allowed in string
    #       - first and last must be R/AL chars
    # this checks if start/end are R/AL chars. if so, prohibited loop
    # will forbid all L chars. if not, prohibited loop will forbid all
    # R/AL chars instead. in both cases, prohibited loop takes care of C.8.
    is_ral_char = in_table_d1
    if is_ral_char(data[0]):
        if not is_ral_char(data[-1]):
            raise ScramException(
                "malformed bidi sequence", ServerError.INVALID_ENCODING
            )
        # forbid L chars within R/AL sequence.
        is_forbidden_bidi_char = in_table_d2
    else:
        # forbid R/AL chars if start not setup correctly; L chars allowed.
        is_forbidden_bidi_char = is_ral_char

    # check for prohibited output
    # stringprep tables A.1, B.1, C.1.2, C.2 - C.9
    for c in data:
        for f, msg in (
            # check for chars mapping stage should have removed
            (in_table_b1, "failed to strip B.1 in mapping stage"),
            (in_table_c12, "failed to replace C.1.2 in mapping stage"),
            # check for forbidden chars
            (in_table_a1, "unassigned code points forbidden"),
            (in_table_c21_c22, "control characters forbidden"),
            (in_table_c3, "private use characters forbidden"),
            (in_table_c4, "non-char code points forbidden"),
            (in_table_c5, "surrogate codes forbidden"),
            (in_table_c6, "non-plaintext chars forbidden"),
            (in_table_c7, "non-canonical chars forbidden"),
            (in_table_c8, "display-modifying/deprecated chars forbidden"),
            (in_table_c9, "tagged characters forbidden"),
            (is_forbidden_bidi_char, "forbidden bidi character"),
        ):
            if f(c):
                raise ScramException(msg, ServerError.INVALID_ENCODING)

    return data
