"""Offline device signature verification for salinity manifests.

The trust store is a fixed mapping of device ids to RSA public parameters
(``device_keys.DEVICE_KEYS``).  Nothing here talks to the network and there is
no certificate authority: a manifest either verifies under the key pinned for
the device named inside it, or it is rejected.

RSA PKCS#1 v1.5 with SHA-256 is implemented directly so the package has no
third-party dependency.  The same primitives are reused by the fixture
generator/tests to *sign* synthetic packets; the package itself only needs
verification, and only the public parameters are part of the trust store.
"""
import hashlib
from .device_keys import DEVICE_KEYS

_SHA256_DIGEST_INFO = bytes.fromhex('3031300d060960864801650304020105000420')


class SignatureError(ValueError):
    pass


def _emsa_pkcs1_v15_sha256(message, length):
    digest = hashlib.sha256(message).digest()
    encoded = _SHA256_DIGEST_INFO + digest
    if length < len(encoded) + 11:
        raise SignatureError('signature_invalid')
    padding = b'\xff' * (length - len(encoded) - 3)
    return b'\x00\x01' + padding + b'\x00' + encoded


def _verify_digest(message, signature_hex, public):
    n, e = public['n'], public['e']
    try:
        signature = int(signature_hex, 16)
    except (TypeError, ValueError):
        return False
    if signature <= 0 or signature >= n:
        return False
    length = (n.bit_length() + 7) // 8
    candidate = pow(signature, e, n).to_bytes(length, 'big')
    return candidate == _emsa_pkcs1_v15_sha256(message, length)


def verify_signature(message, signature_hex, device_id):
    """Verify a hex RSA signature over ``message`` for a pinned device."""
    public = DEVICE_KEYS.get(device_id)
    if not public or not isinstance(signature_hex, str):
        raise SignatureError('signature_device_unknown')
    if not _verify_digest(message, signature_hex, public):
        raise SignatureError('signature_invalid')
    return True


def sign_message(message, private):
    """Sign with ``{'n': ..., 'd': ...}``.  Used by fixture builders only.

    Not imported by the runtime protocol; private parameters never enter the
    package's trust store.
    """
    n, d = private['n'], private['d']
    length = (n.bit_length() + 7) // 8
    encoded = _emsa_pkcs1_v15_sha256(message, length)
    return format(pow(int.from_bytes(encoded, 'big'), d, n), 'x').zfill(length * 2)


def manifest_body(manifest):
    """Canonical bytes covered by the device signature."""
    from .salinity import canonical_bytes
    return canonical_bytes({k: v for k, v in manifest.items() if k != 'signature'})


def verify_manifest(manifest):
    """Verify a parsed manifest's signature and return its signed body bytes."""
    body = manifest_body(manifest)
    verify_signature(body, manifest.get('signature'), manifest.get('device_id'))
    return body


__all__ = ['SignatureError', 'verify_signature', 'sign_message', 'verify_manifest',
           'manifest_body', 'DEVICE_KEYS']
