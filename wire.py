"""wire.py -- the only crypto the inbox needs. PyNaCl, nothing else.

Deliberately identical in format to toragents.crypto so an identity created by
either implementation is readable by the other: same seed/box JSON, same
blake2b(verify||box, 16) fingerprint. PyNaCl does all the real work; this file
just names things.
"""
import base64

import nacl.hash
from nacl.encoding import RawEncoder
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

b64e = lambda b: base64.b64encode(b).decode("ascii")
b64d = lambda s: base64.b64decode(s)


class PublicIdentity:
    """Someone else's public half: verify their signatures, seal boxes to them."""

    def __init__(self, verify_key: bytes, box_key: bytes):
        self._verify = VerifyKey(verify_key)
        self._box = PublicKey(box_key)

    @property
    def verify_key(self) -> bytes:
        return bytes(self._verify)

    @property
    def box_key(self) -> bytes:
        return bytes(self._box)

    @property
    def fingerprint(self) -> str:
        return nacl.hash.blake2b(
            self.verify_key + self.box_key, digest_size=16, encoder=RawEncoder
        ).hex()

    def verify(self, message: bytes, signature: bytes) -> bool:
        # False means ONE thing: this signature is not valid for this message.
        # Anything else -- a TypeError, an AttributeError, a broken key object --
        # is our bug and must not be laundered into "your signature is bad", which
        # is what a bare except does. Callers are expected to catch and name it.
        from nacl.exceptions import BadSignatureError
        try:
            self._verify.verify(message, signature)
            return True
        except BadSignatureError:
            return False

    def to_dict(self) -> dict:
        return {"verify": b64e(self.verify_key), "box": b64e(self.box_key)}

    @classmethod
    def from_dict(cls, d: dict) -> "PublicIdentity":
        return cls(b64d(d["verify"]), b64d(d["box"]))

    def __eq__(self, other):
        return isinstance(other, PublicIdentity) and self.to_dict() == other.to_dict()

    def __hash__(self):
        return hash((self.verify_key, self.box_key))


class AgentKeys:
    """Our private identity. Never leaves this machine."""

    def __init__(self, signing_key: SigningKey, box_key: PrivateKey):
        self._signing = signing_key
        self._box = box_key

    @classmethod
    def generate(cls) -> "AgentKeys":
        return cls(SigningKey.generate(), PrivateKey.generate())

    def public(self) -> PublicIdentity:
        return PublicIdentity(bytes(self._signing.verify_key),
                              bytes(self._box.public_key))

    def sign(self, message: bytes) -> bytes:
        return self._signing.sign(message).signature

    def to_secret_dict(self) -> dict:
        return {"seed": b64e(self._signing.encode()), "box": b64e(bytes(self._box))}

    @classmethod
    def from_secret_dict(cls, d: dict) -> "AgentKeys":
        return cls(SigningKey(b64d(d["seed"])), PrivateKey(b64d(d["box"])))

    def seal(self, recipient: PublicIdentity, plaintext: bytes) -> bytes:
        return SealedBox(recipient._box).encrypt(plaintext)

    def open_sealed(self, sealed: bytes) -> bytes:
        return SealedBox(self._box).decrypt(sealed)


def seal_to(recipient: PublicIdentity, plaintext: bytes) -> bytes:
    return SealedBox(recipient._box).encrypt(plaintext)


def open_sealed(keys: AgentKeys, sealed: bytes) -> bytes:
    return SealedBox(keys._box).decrypt(sealed)
