import hashlib
from collections import OrderedDict, namedtuple
from enum import IntEnum, unique
from functools import total_ordering, wraps
from hmac import compare_digest
from itertools import groupby
from operator import attrgetter
from secrets import token_bytes, token_hex
from ssl import SSLObject, SSLSocket
from types import MappingProxyType, NoneType

from asn1crypto import pem
from asn1crypto.x509 import Certificate

from scramp.utils import (
    ScramException,
    ServerError,
    b64dec,
    b64enc,
    h,
    hi,
    hmac,
    saslprep,
    uenc,
    xor,
)

# https://tools.ietf.org/html/rfc5802
# https://www.rfc-editor.org/rfc/rfc7677.txt


@total_ordering
class IterationCount:
    MAX_ITERATION_COUNT = 10_000_000  # DoS guard
    MIN_ITERATION_COUNT = 4096  # Lowest recommended of any mechanism

    def __init__(self, value):
        if type(value) is not int:
            raise ScramException("The value must be an int", ServerError.OTHER_ERROR)
        if value > self.MAX_ITERATION_COUNT:
            raise ScramException(
                f"The value must not be > {self.MAX_ITERATION_COUNT}",
                ServerError.OTHER_ERROR,
            )
        if value < self.MIN_ITERATION_COUNT:
            raise ScramException(
                f"The value must not be < {self.MIN_ITERATION_COUNT}",
                ServerError.OTHER_ERROR,
            )
        self.value = value

    def __eq__(self, other):
        return isinstance(other, IterationCount) and self.value == other.value

    def __lt__(self, other):
        if not isinstance(other, IterationCount):
            return NotImplemented
        return self.value < other.value

    def __int__(self):
        return self.value

    def __str__(self):
        return str(self.value)

    @classmethod
    def from_str(cls, value_str):
        for c in value_str:
            if not (c.isascii() and c.isdigit()):
                raise ScramException(
                    "The iteration count can only contain ASCII digits",
                    ServerError.OTHER_ERROR,
                )
        if len(value_str) == 0:
            raise ScramException(
                "The iteration count must not be empty", ServerError.OTHER_ERROR
            )

        if value_str[0] == "0":
            raise ScramException(
                "The iteration count must not begin with a zero",
                ServerError.OTHER_ERROR,
            )
        try:
            value = int(value_str)
        except ValueError as e:
            raise ScramException("Server iteration count is not valid") from e

        return cls(value)


class Salt:
    MIN_LENGTH = 12

    def __init__(self, salt):
        if not type(salt) is bytes:
            raise ScramException(
                f"The 'salt' must be of type bytes, but found type {type(salt)}",
                ServerError.OTHER_ERROR,
            )
        len_salt = len(salt)
        if len_salt < self.MIN_LENGTH:
            raise ScramException(
                f"The 'salt' is {len_salt} bytes long, but it must be at least "
                f"{self.MIN_LENGTH} bytes long",
                ServerError.OTHER_ERROR,
            )

        self.salt = salt

    def __str__(self):
        return b64enc(self.salt)

    def __bytes__(self):
        return self.salt

    def __eq__(self, other):
        return isinstance(other, Salt) and self.salt == other.salt

    @classmethod
    def from_str(cls, s):
        return cls(b64dec(s))

    @classmethod
    def create(cls):
        return cls(token_bytes())


class Nonce:
    def __init__(self, nonce):
        if not isinstance(nonce, str):
            raise ScramException(
                f"The 'nonce' must be of type str, but found type {type(nonce)}",
                ServerError.OTHER_ERROR,
            )
        if not all(0x21 <= ord(c) <= 0x7E and c != "," for c in nonce):
            raise ScramException(
                "Nonce contains invalid characters", ServerError.OTHER_ERROR
            )

        if len(nonce) == 0:
            raise ScramException(
                "An empty string isn't allowed", ServerError.OTHER_ERROR
            )

        self.nonce = nonce

    def __str__(self):
        return self.nonce

    def __eq__(self, other):
        return isinstance(other, Nonce) and self.nonce == other.nonce

    def __add__(self, other):
        if not isinstance(other, Nonce):
            return NotImplemented
        return Nonce(self.nonce + other.nonce)

    def __len__(self):
        return len(self.nonce)

    def startswith(self, other):
        if not isinstance(other, Nonce):
            raise TypeError(f"expected Nonce, got {type(other).__name__}")
        return self.nonce.startswith(other.nonce)

    @classmethod
    def create(cls):
        return cls(token_hex())


class CbName:
    SUPPORTED_TYPES = frozenset(
        {"tls-server-end-point", "tls-unique", "tls-unique-for-telnet"}
    )

    UNSUPPORTED_TYPES = frozenset({"tls-exporter"})

    def __init__(self, cb_name):
        if not isinstance(cb_name, str):
            raise ScramException(
                f"The 'cb_name' must be of type str, but found type {type(cb_name)}",
                ServerError.OTHER_ERROR,
            )
        for c in cb_name:
            if not (c.isascii() and (c.isalpha() or c.isdigit() or c in ".-")):
                raise ScramException(
                    "The channel binding name must contain only ASCII characters that "
                    "are alphabetic or a digit or '.' or '-'",
                    ServerError.OTHER_ERROR,
                )
        if cb_name in self.UNSUPPORTED_TYPES:
            raise ScramException(
                f"The channel binding types {sorted(self.UNSUPPORTED_TYPES)} are not "
                f"supported by Scramp. Please see the README for details",
                ServerError.UNSUPPORTED_CHANNEL_BINDING_TYPE,
            )
        elif cb_name not in self.SUPPORTED_TYPES:
            raise ScramException(
                f"The channel binding type is not recognised by Scramp. The "
                f"recognised types are "
                f"{sorted(self.SUPPORTED_TYPES | self.UNSUPPORTED_TYPES)}",
                ServerError.UNSUPPORTED_CHANNEL_BINDING_TYPE,
            )
        self.cb_name = cb_name

    def __str__(self):
        return self.cb_name

    def __repr__(self):
        return f"'{self}'"

    def __eq__(self, other):
        return isinstance(other, CbName) and self.cb_name == other.cb_name


class CBindData:
    def __init__(self, data):
        if not isinstance(data, bytes):
            raise ScramException(
                f"The 'data' must be of type bytes, but found type {type(data)}",
                ServerError.OTHER_ERROR,
            )
        if len(data) == 0:
            raise ScramException(
                "The 'data' can't be of zero length.",
                ServerError.OTHER_ERROR,
            )

        self.data = data

    def __bytes__(self):
        return self.data

    def __eq__(self, other):
        return isinstance(other, CBindData) and self.data == other.data

    @classmethod
    def from_der(cls, der):
        try:
            cert = Certificate.load(der, strict=True)
        except ValueError as e:
            raise ScramException(
                "Problem loading certificate from DER",
                ServerError.OTHER_ERROR,
            ) from e

        try:
            hash_algo = cert.hash_algo
        except ValueError as e:
            raise ScramException(
                "Problem getting certificate algorithm",
                ServerError.OTHER_ERROR,
            ) from e

        if hash_algo in ("md5", "sha1"):
            hash_algo = "sha256"

        try:
            hash_obj = hashlib.new(hash_algo, der)
        except ValueError:
            raise ScramException(
                f"Hash algorithm {hash_algo} not supported by hashlib",
                ServerError.OTHER_ERROR,
            )
        cb_data = hash_obj.digest()
        return cls(cb_data)


@unique
class ClientStage(IntEnum):
    get_client_first = 1
    set_server_first = 2
    get_client_final = 3
    set_server_final = 4


@unique
class ServerStage(IntEnum):
    set_client_first = 1
    get_server_first = 2
    set_client_final = 3
    get_server_final = 4


def _check_stage(Stages, current_stage, next_stage):
    if current_stage is None:
        if next_stage != 1:
            raise ScramException(f"The method {Stages(1).name} must be called first.")
    elif current_stage == 4:
        raise ScramException("The authentication sequence has already finished.")
    elif next_stage != current_stage + 1:
        raise ScramException(
            f"The next method to be called is "
            f"{Stages(current_stage + 1).name}, not this method."
        )


Mech = namedtuple("Mech", ["hf", "use_binding", "iteration_count", "strength"])

MECHS = MappingProxyType(
    {
        "SCRAM-SHA-1": Mech(hashlib.sha1, False, IterationCount(4096), 0),
        "SCRAM-SHA-256": Mech(hashlib.sha256, False, IterationCount(4096), 1),
        "SCRAM-SHA-512": Mech(hashlib.sha512, False, IterationCount(4096), 2),
        "SCRAM-SHA3-512": Mech(hashlib.sha3_512, False, IterationCount(10000), 3),
        "SCRAM-SHA-1-PLUS": Mech(hashlib.sha1, True, IterationCount(4096), 4),
        "SCRAM-SHA-256-PLUS": Mech(hashlib.sha256, True, IterationCount(4096), 5),
        "SCRAM-SHA-512-PLUS": Mech(hashlib.sha512, True, IterationCount(4096), 6),
        "SCRAM-SHA3-512-PLUS": Mech(hashlib.sha3_512, True, IterationCount(10000), 7),
    }
)


class ChannelBinding:
    def __init__(self, name, data):
        if not isinstance(name, CbName):
            raise ScramException("The channel binding name must be a CbName object.")
        self.name = name

        if not isinstance(data, CBindData):
            raise ScramException("The channel binding data must be a CBindData object.")
        self.data = data

    def __eq__(self, other):
        return (
            isinstance(other, ChannelBinding)
            and self.name == other.name
            and self.data == other.data
        )


class ScramMechanism:
    def __init__(self, mechanism="SCRAM-SHA-256"):
        if not isinstance(mechanism, str):
            raise ScramException("The mechanism name must be a str")

        try:
            mech = MECHS[mechanism]
        except KeyError:
            raise ScramException(
                f"The mechanism name is not supported. The supported mechanisms are "
                f"{sorted(MECHS.keys())}"
            )
        self.name = mechanism
        self.hf = mech.hf
        self.use_binding = mech.use_binding
        self.iteration_count = mech.iteration_count
        self.strength = mech.strength

    def make_auth_info(self, password, iteration_count=None, salt=None):
        if salt is not None:
            salt = Salt(salt)

        password = Password(password)

        if iteration_count is None:
            i = self.iteration_count
        else:
            i = IterationCount(iteration_count)
            if i < self.iteration_count:
                raise ScramException(
                    f"The iteration count must not be < {self.iteration_count}",
                    ServerError.OTHER_ERROR,
                )

        salt, stored_key, server_key = _make_auth_info(self.hf, password, i, salt=salt)
        return bytes(salt), stored_key, server_key, int(i)

    def make_server(self, auth_fn, channel_binding=None, s_nonce=None):
        if s_nonce is not None:
            s_nonce = Nonce(s_nonce)

        if channel_binding is None:
            cb = None
        else:
            try:
                cb_name_str, d_so_st = channel_binding
            except ValueError:
                raise ScramException(
                    "Channel binding must be a tuple with two items",
                    ServerError.OTHER_ERROR,
                )

            cb_name = CbName(cb_name_str)
            cb_data = None
            if isinstance(d_so_st, bytes):
                cb_data = CBindData(d_so_st)
            elif cb_name == CbName("tls-unique"):
                if not isinstance(d_so_st, (SSLSocket, SSLObject)):
                    raise ScramException(
                        "If the channel type is 'tls-unique', the type of the second "
                        "item of the channel_binding tuple must be bytes or "
                        "ssl.SSLSocket",
                        ServerError.OTHER_ERROR,
                    )
                ssl_socket = d_so_st
                cb_data = CBindData(ssl_socket.get_channel_binding(str(cb_name)))

            elif cb_name == CbName("tls-server-end-point"):
                if not isinstance(d_so_st, str):
                    raise ScramException(
                        "If the channel type is 'tls-server-end-point', the type of "
                        "the second item of the channel_binding tuple must be bytes "
                        "or a str of the PEM certificate file",
                        ServerError.OTHER_ERROR,
                    )
                try:
                    pem_bin = d_so_st.encode("ascii")
                except UnicodeError:
                    raise ScramException(
                        "The PEM encoded certificate str must only contain ASCII "
                        "characters",
                        ServerError.OTHER_ERROR,
                    )
                der = None
                try:
                    for type_name, _headers, cand_der in pem.unarmor(  # pyright: ignore[reportGeneralTypeIssues]
                        pem_bin, multiple=True
                    ):
                        if type_name == "CERTIFICATE":
                            der = cand_der
                            break
                except ValueError as e:
                    raise ScramException(
                        "The PEM encoded certificate str doesn't seem to be valid",
                        ServerError.OTHER_ERROR,
                    ) from e

                if der is None:
                    raise ScramException(
                        "No CERTIFICATE block found in PEM data",
                        ServerError.OTHER_ERROR,
                    )

                cb_data = CBindData.from_der(der)

            else:
                raise ScramException(
                    "Channel binding name not recognized", ServerError.OTHER_ERROR
                )

            cb = ChannelBinding(cb_name, cb_data)

        return ScramServer(self, AuthFn(auth_fn), cb=cb, s_nonce=s_nonce)


def _make_auth_info(hf, password, i, salt=None):
    if salt is None:
        salt = Salt.create()

    salted_password = hi(hf, bytes(password), bytes(salt), i)
    _, stored_key, server_key = _c_key_stored_key_s_key(hf, salted_password)
    return salt, stored_key, server_key


class ScramClient:
    def __init__(
        self, mechanisms, username, password, channel_binding=None, c_nonce=None
    ):
        if not isinstance(mechanisms, (list, tuple)):
            raise ScramException(
                f"'{mechanisms}' The 'mechanisms' parameter must be a list or tuple of "
                f"mechanism names."
            )
        if not len(mechanisms) > 0:
            raise ScramException(
                "The 'mechanisms' parameter must contain at least one mechanism name"
            )

        if channel_binding is None:
            cb = None
        else:
            try:
                cb_name_str, d_s = channel_binding
            except ValueError:
                raise ScramException(
                    "Channel binding must be a tuple with two items",
                    ServerError.OTHER_ERROR,
                )

            cb_name = CbName(cb_name_str)
            cb_data = None
            if isinstance(d_s, bytes):
                cb_data = CBindData(d_s)
            elif isinstance(d_s, (SSLSocket, SSLObject)):
                ssl_socket = d_s
                if cb_name == CbName("tls-unique"):
                    cb_data = CBindData(ssl_socket.get_channel_binding(str(cb_name)))

                elif cb_name == CbName("tls-server-end-point"):
                    der = ssl_socket.getpeercert(binary_form=True)
                    if der is None:
                        raise ScramException(
                            "There is no peer certificate for the SSL socket",
                            ServerError.OTHER_ERROR,
                        )
                    cb_data = CBindData.from_der(der)

                else:
                    raise ScramException(
                        "Channel binding name not recognized", ServerError.OTHER_ERROR
                    )
            else:
                raise ScramException(
                    "The second item of the channel_binding tuple must be of type "
                    "bytes or ssl.SSLSocket",
                    ServerError.OTHER_ERROR,
                )

            cb = ChannelBinding(cb_name, cb_data)

        ms = (ScramMechanism(m) for m in mechanisms)
        mechs = [m for m in ms if not (cb is None and m.use_binding)]
        if len(mechs) == 0:
            raise ScramException(
                "The mechanisms in the 'mechanisms' parameter all require channel "
                "binding but a 'channel_binding' parameter hasn't been provided"
            )

        mech = max(mechs, key=attrgetter("strength"))
        self.hf, use_binding = mech.hf, mech.use_binding
        self.mechanism_name = mech.name

        self.c_nonce = Nonce.create() if c_nonce is None else Nonce(c_nonce)
        self.username = Username(username)
        self.password = Password(password)
        self.cb = cb
        self.stage = None
        self.gs2_header = Gs2Header.from_binding(self.cb, use_binding)

    def _set_stage(self, next_stage):
        _check_stage(ClientStage, self.stage, next_stage)
        self.stage = next_stage

    def get_client_first(self):
        self._set_stage(ClientStage.get_client_first)
        self.client_first_bare, client_first = _get_client_first(
            self.username, self.c_nonce, self.gs2_header
        )
        return client_first

    def set_server_first(self, message):
        self._set_stage(ClientStage.set_server_first)
        self.server_first = message
        self.nonce, self.salt, self.iterations = _set_server_first(
            ServerFirst(message), self.c_nonce
        )

    def get_client_final(self):
        self._set_stage(ClientStage.get_client_final)
        self.server_signature, cfinal = _get_client_final(
            self.hf,
            self.password,
            self.salt,
            self.iterations,
            self.nonce,
            self.client_first_bare,
            self.server_first,
            self.cb,
            self.gs2_header,
        )
        return cfinal

    def set_server_final(self, message):
        self._set_stage(ClientStage.set_server_final)
        _set_server_final(ServerFinal(message), self.server_signature)


def set_error(f):
    @wraps(f)
    def wrapper(self, *args, **kwds):
        try:
            return f(self, *args, **kwds)
        except ScramException as e:
            if e.server_error is not None:
                self.error = e.server_error
                self.stage = ServerStage.set_client_final
            raise

    return wrapper


class ScramServer:
    def __init__(self, mechanism, auth_fn, cb=None, s_nonce=None):
        self.cb = cb
        self.s_nonce = Nonce.create() if s_nonce is None else s_nonce
        self.auth_fn = auth_fn
        self.stage = None
        self.server_signature = None
        self.error = None
        self.nonce = None

        self._set_mechanism(mechanism)

    def _set_mechanism(self, mechanism):
        if mechanism.use_binding and self.cb is None:
            raise ScramException(
                "The mechanism requires channel binding, and so channel_binding can't "
                "be None."
            )
        self.m = mechanism

    def _set_stage(self, next_stage):
        _check_stage(ServerStage, self.stage, next_stage)
        self.stage = next_stage

    @set_error
    def set_client_first(self, client_first):

        if not isinstance(client_first, str):
            raise ScramException(
                "The client_first message must be a str",
                ServerError.OTHER_ERROR,
            )
        self._set_stage(ServerStage.set_client_first)
        (
            self.nonce,
            self.client_first_bare,
            upgrade_mechanism,
            self.gs2_header,
            self.salt,
            self.stored_key,
            self.server_key,
            self.i,
        ) = _set_client_first(
            client_first,
            self.s_nonce,
            self.cb,
            self.m.use_binding,
            self.auth_fn,
        )

        if upgrade_mechanism:
            mech = ScramMechanism(f"{self.m.name}-PLUS")
            self._set_mechanism(mech)

    @set_error
    def get_server_first(self):
        self._set_stage(ServerStage.get_server_first)
        self.server_first = _get_server_first(
            self.nonce,
            self.salt,
            self.i,
        )
        return self.server_first

    @set_error
    def set_client_final(self, client_final):
        self._set_stage(ServerStage.set_client_final)
        self.server_signature = _set_client_final(
            self.m.hf,
            ClientFinal(client_final),
            self.nonce,
            self.stored_key,
            self.server_key,
            self.client_first_bare,
            self.server_first,
            self.cb,
            self.gs2_header,
        )

    @set_error
    def get_server_final(self):
        self._set_stage(ServerStage.get_server_final)
        return _get_server_final(self.server_signature, self.error)


def _make_auth_message(client_first_bare, server_first, client_final_without_proof):
    msg = client_first_bare, server_first, client_final_without_proof
    return uenc(",".join(msg))


def _c_key_stored_key_s_key(hf, salted_password):
    client_key = hmac(hf, salted_password, b"Client Key")
    stored_key = h(hf, client_key)
    server_key = hmac(hf, salted_password, b"Server Key")

    return client_key, stored_key, server_key


def _check_client_key(hf, stored_key, auth_msg, proof):
    client_signature = hmac(hf, stored_key, auth_msg)
    try:
        client_key = xor(client_signature, proof)
    except ValueError as e:
        raise ScramException(
            "Can't create client key.", ServerError.INVALID_PROOF
        ) from e

    key = h(hf, client_key)

    if not compare_digest(key, stored_key):
        raise ScramException("The client keys don't match.", ServerError.INVALID_PROOF)


class Gs2Header:
    # gs2-header      = gs2-cbind-flag "," [ authzid ] ","
    def __init__(self, gs2_char, cb_name):
        self.gs2_char = gs2_char
        if not isinstance(cb_name, (NoneType, CbName)):
            raise ScramException("must be CbName or None")
        self.cb_name = cb_name

    def __str__(self):
        if self.gs2_char == "n":
            return "n,,"
        elif self.gs2_char == "y":
            return "y,,"
        elif self.gs2_char == "p":
            return f"p={self.cb_name},,"
        else:
            raise ScramException("Invalid GS2 char")

    def __bytes__(self):
        return str(self).encode("ascii")

    def __eq__(self, other):
        return (
            isinstance(other, Gs2Header)
            and self.gs2_char == other.gs2_char
            and self.cb_name == other.cb_name
        )

    def make_cbind_input(self, cb):
        gs2_header_bin = bytes(self)

        if self.gs2_char in ("y", "n"):
            return gs2_header_bin
        elif self.gs2_char == "p":
            return gs2_header_bin + bytes(cb.data)

    @classmethod
    def from_str(cls, s):
        try:
            gs2_cbind_flag, authzid, trail = s.split(",")
        except ValueError:
            raise ScramException(
                "The gs2-header must contain exactly two commas",
                ServerError.OTHER_ERROR,
            )

        if gs2_cbind_flag == "y":
            gs2_char, cb_name = "y", None

        elif gs2_cbind_flag == "n":
            gs2_char, cb_name = "n", None

        elif gs2_cbind_flag.startswith("p="):
            gs2_char = "p"

            cb_name = CbName(gs2_cbind_flag[2:])

        else:
            raise ScramException(
                "GS2 flag must be one of ('y', 'n', 'p=')", ServerError.OTHER_ERROR
            )

        if authzid != "":
            raise ScramException(
                "The GS2 authzid must be empty", ServerError.OTHER_ERROR
            )

        if trail != "":
            raise ScramException(
                "The gs2-header can't have characters after the last comma",
                ServerError.OTHER_ERROR,
            )
        return cls(gs2_char, cb_name)

    @classmethod
    def from_binding(cls, cb, use_binding):
        if cb is None:
            gs2_char, cb_name = "n", None
        else:
            if use_binding:
                gs2_char, cb_name = "p", cb.name
            else:
                gs2_char, cb_name = "y", None
        return cls(gs2_char, cb_name)


class Message:
    PROTO_ATTRS = frozenset({"a", "n", "m", "r", "c", "s", "i", "p", "v", "e"})

    def __init__(self, msg, desc, *expected_attr_strs):
        if not isinstance(msg, str):
            raise ScramException(
                "A message must be of type str", ServerError.OTHER_ERROR
            )
        m = OrderedDict()
        for p in msg.split(","):
            if len(p) < 2 or p[1] != "=":
                raise ScramException(
                    f"Malformed {desc} message. Attributes must be separated by a ',' "
                    f"and each attribute must start with a letter followed by a '='",
                    ServerError.OTHER_ERROR,
                )
            k = p[0]
            if not (k.isalpha() and k.isascii()):
                raise ScramException(
                    f"Malformed {desc} message. Attributes must be US-ASCII alpha "
                    f"characters.",
                    ServerError.OTHER_ERROR,
                )
            elif k == "m":
                raise ScramException(
                    "The 'm' attribute isn't supported by this version of the "
                    "protocol.",
                    ServerError.EXTENSIONS_NOT_SUPPORTED,
                )
            elif k in m:
                raise ScramException(
                    "Duplicate attributes not allowed in message",
                    ServerError.OTHER_ERROR,
                )

            v = p[2:]
            if v == "":
                raise ScramException(
                    f"Malformed {desc} message. Attribute values must at "
                    f"least one character long",
                    ServerError.OTHER_ERROR,
                )
            elif "\x00" in v:
                raise ScramException(
                    f"Malformed {desc} message. Attribute values can't "
                    f"contain the NUL character",
                    ServerError.OTHER_ERROR,
                )
            elif "," in v:
                raise ScramException(
                    f"Malformed {desc} message. Attribute values can't "
                    f"contain the ',' character",
                    ServerError.OTHER_ERROR,
                )

            m[k] = v

        attr_list = [k if k in Message.PROTO_ATTRS else "*" for k in m]
        attr_str = "".join(k for k, _ in groupby(attr_list))
        if "*" in attr_str:
            exp_strs = list(expected_attr_strs)
        else:
            exp_strs = [s.replace("*", "") for s in expected_attr_strs]

        if attr_str not in exp_strs:
            star_exp = (
                " (where '*' is unrecognised attributes)" if "*" in attr_str else ""
            )
            raise ScramException(
                f"Malformed {desc} message. Expected the attribute str to be one of "
                f"{exp_strs} but found '{attr_str}'{star_exp}",
                ServerError.OTHER_ERROR,
            )

        self._msg = OrderedDict()
        for k, v_str in m.items():
            match k:
                case "n":
                    v = Username.from_escaped(v_str)
                case "r":
                    v = Nonce(v_str)
                case "s":
                    v = Salt.from_str(v_str)
                case "i":
                    v = IterationCount.from_str(v_str)
                case "p":
                    v = b64dec(v_str)
                case "v":
                    v = b64dec(v_str)
                case "e":
                    try:
                        v = ServerError(v_str)
                    except ValueError:
                        raise ScramException("The server returned an invalid error")
                case _:
                    v = v_str

            self._msg[k] = v

    def __getitem__(self, key):
        return self._msg[key]

    def items(self):
        return self._msg.items()

    def __contains__(self, key):
        return key in self._msg

    def __len__(self):
        return len(self._msg)

    def _iter__(self):
        return iter(self._msg)


class ClientFirstBare(Message):
    def __init__(self, message):
        super().__init__(message, "client_first_bare", "nr*")


class ServerFirst(Message):
    def __init__(self, message):
        super().__init__(message, "server first", "rsi*", "e*")


class ClientFinal(Message):
    def __init__(self, message):
        super().__init__(message, "client final", "cr*p")


class ServerFinal(Message):
    def __init__(self, message):
        super().__init__(message, "server final", "v*", "e*")


class Username:
    def __init__(self, username):
        try:
            self.username = saslprep(username)
        except ScramException as e:
            raise ScramException(e.args[0], ServerError.INVALID_USERNAME_ENCODING)

        if len(self.username) == 0:
            raise ScramException(
                "The username cannot be empty", ServerError.OTHER_ERROR
            )

    def __str__(self):
        return self.username

    def __eq__(self, other):
        return isinstance(other, Username) and self.username == other.username

    def escape(self):
        return _username_escape(self.username)

    @classmethod
    def from_escaped(cls, username):
        return cls(_username_unescape(username))


ESCAPE_EQUALS = "=3D"
ESCAPE_COMMA = "=2C"


def _username_escape(username):
    return username.replace("=", ESCAPE_EQUALS).replace(",", ESCAPE_COMMA)


def _username_unescape(username):
    for i in (i for i, c in enumerate(username) if c == "="):
        if username[i : i + 3] not in (ESCAPE_EQUALS, ESCAPE_COMMA):
            raise ScramException(
                "An '=' in a username must be followed by '3D', or  '2C'",
                ServerError.INVALID_USERNAME_ENCODING,
            )
    return username.replace(ESCAPE_COMMA, ",").replace(ESCAPE_EQUALS, "=")


class AuthFn:
    def __init__(self, auth_fn):
        if not callable(auth_fn):
            raise ScramException(
                "The 'auth_fn' must be callable", ServerError.OTHER_ERROR
            )
        self.auth_fn = auth_fn

    def __call__(self, username):
        try:
            result = self.auth_fn(str(username))  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as e:
            raise ScramException("auth_fn exception", ServerError.NO_RESOURCES) from e

        if result is None:
            raise ScramException("Unknown user", ServerError.UNKNOWN_USER)

        if not isinstance(result, tuple):
            raise ScramException(
                "auth_fun must return None or a tuple", ServerError.OTHER_ERROR
            )

        if len(result) != 4:
            raise ScramException(
                "auth_fun must return a tuple with exactly four items",
                ServerError.OTHER_ERROR,
            )

        salt, stored_key, server_key, i = result

        if not isinstance(stored_key, bytes):
            raise ScramException(
                f"The 'stored_key' must be of type bytes, "
                f"but found type {type(stored_key)}",
                ServerError.OTHER_ERROR,
            )

        if not isinstance(server_key, bytes):
            raise ScramException(
                f"The 'server_key' must be of type bytes, "
                f"but found type {type(server_key)}",
                ServerError.OTHER_ERROR,
            )

        return (
            Salt(salt),
            stored_key,
            server_key,
            IterationCount(i),
        )


class Password:
    def __init__(self, password):
        try:
            self.password = saslprep(password)
        except ScramException as e:
            raise ScramException(e.args[0], ServerError.OTHER_ERROR)

    def __bytes__(self):
        return uenc(self.password)


def _make_message(**kwargs):
    return ",".join((f"{k}={v}" for k, v in kwargs.items()))


def _get_client_first(username, c_nonce, gs2_header):
    bare = _make_message(n=username.escape(), r=c_nonce)
    return bare, f"{gs2_header}{bare}"


def _set_client_first(client_first, s_nonce, cb, use_binding, auth_fn):
    try:
        first_comma = client_first.index(",")
        second_comma = client_first.index(",", first_comma + 1)
    except ValueError:
        raise ScramException(
            "The client sent a malformed first message", ServerError.OTHER_ERROR
        )
    gs2_header = Gs2Header.from_str(client_first[: second_comma + 1])
    upgrade_mechanism = False

    if gs2_header.gs2_char == "y":
        if cb is not None:
            raise ScramException(
                "Received GS2 flag 'y' which indicates that the client doesn't think "
                "the server supports channel binding, but in fact it does",
                ServerError.SERVER_DOES_SUPPORT_CHANNEL_BINDING,
            )

    elif gs2_header.gs2_char == "n":
        if use_binding:
            raise ScramException(
                "Received GS2 flag 'n' which indicates that the client doesn't require "
                "channel binding, but the server does",
                ServerError.SERVER_DOES_SUPPORT_CHANNEL_BINDING,
            )

    elif gs2_header.gs2_char == "p":
        if cb is None:
            raise ScramException(
                "Received GS2 flag 'p' which indicates that the client requires "
                "channel binding, but the server does not",
                ServerError.CHANNEL_BINDING_NOT_SUPPORTED,
            )
        if not use_binding:
            upgrade_mechanism = True

        channel_type = cb.name
        cb_name = gs2_header.cb_name
        if cb_name != channel_type:
            raise ScramException(
                f"Received channel binding name doesn't match the channel binding "
                f"name of the server: {channel_type}",
                ServerError.UNSUPPORTED_CHANNEL_BINDING_TYPE,
            )

    client_first_bare = client_first[second_comma + 1 :]
    msg = ClientFirstBare(client_first_bare)

    c_nonce = msg["r"]
    nonce = c_nonce + s_nonce
    user = msg["n"]

    salt, stored_key, server_key, i = auth_fn(user)

    return (
        nonce,
        client_first_bare,
        upgrade_mechanism,
        gs2_header,
        salt,
        stored_key,
        server_key,
        i,
    )


def _get_server_first(nonce, salt, iterations):
    return _make_message(r=nonce, s=salt, i=iterations)


def _set_server_first(msg, c_nonce):
    if "e" in msg:
        raise ScramException("The server returned an error", msg["e"])

    nonce = msg["r"]
    salt = msg["s"]
    i = msg["i"]

    if not nonce.startswith(c_nonce):
        raise ScramException("Client nonce doesn't match.", ServerError.OTHER_ERROR)

    if not len(nonce) > len(c_nonce):
        raise ScramException(
            "The nonce must be longer than the client nonce", ServerError.OTHER_ERROR
        )

    return nonce, salt, i


def _get_client_final(
    hf,
    password,
    salt,
    iterations,
    nonce,
    client_first_bare,
    server_first,
    channel_binding,
    gs2_header,
):
    salted_password = hi(hf, bytes(password), bytes(salt), iterations)
    client_key, stored_key, server_key = _c_key_stored_key_s_key(hf, salted_password)

    cbind_input = gs2_header.make_cbind_input(channel_binding)
    client_final_without_proof = _make_message(c=b64enc(cbind_input), r=nonce)
    auth_msg = _make_auth_message(
        client_first_bare, server_first, client_final_without_proof
    )

    client_signature = hmac(hf, stored_key, auth_msg)
    client_proof = xor(client_key, client_signature)
    server_signature = hmac(hf, server_key, auth_msg)
    client_final = f"{client_final_without_proof},p={b64enc(client_proof)}"
    return server_signature, client_final


def _set_client_final(
    hf,
    msg,
    nonce,
    stored_key,
    server_key,
    client_first_bare,
    server_first,
    channel_binding,
    gs2_header,
):
    chan_binding = msg["c"]
    try:
        chan_binding_bin = b64dec(chan_binding)
    except ScramException as e:
        raise ScramException(
            f"The channel binding isn't correctly encoded: {e}",
            ServerError.INVALID_ENCODING,
        ) from e
    msg_nonce = msg["r"]
    proof = msg["p"]
    if not compare_digest(
        chan_binding_bin,
        gs2_header.make_cbind_input(channel_binding),
    ):
        raise ScramException(
            "The channel bindings don't match.",
            ServerError.CHANNEL_BINDINGS_DONT_MATCH,
        )

    if not nonce == msg_nonce:
        raise ScramException("Server nonce doesn't match.", ServerError.OTHER_ERROR)

    client_final_without_proof = ",".join(
        [f"{k}={v}" for k, v in msg.items() if k != "p"]
    )
    auth_msg = _make_auth_message(
        client_first_bare, server_first, client_final_without_proof
    )
    _check_client_key(hf, stored_key, auth_msg, proof)

    return hmac(hf, server_key, auth_msg)


def _get_server_final(server_signature, error):
    if error is None:
        return _make_message(v=b64enc(server_signature))
    else:
        return _make_message(e=error.value)


def _set_server_final(msg, server_signature):
    if "e" in msg:
        raise ScramException("The server returned an error", msg["e"])

    if not compare_digest(server_signature, msg["v"]):
        raise ScramException(
            "The server signature doesn't match.", ServerError.OTHER_ERROR
        )
