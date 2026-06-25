"""E2E cryptography utilities for secret management.

This module provides X25519 key exchange and XChaCha20-Poly1305 decryption
for securely retrieving secret values from the Platform API.

The Platform encrypts secret values using:
1. Ephemeral X25519 keypair (client sends its public key + the server keyId
   as query params, having first fetched the server pubkey via POST /pubkey)
2. ECDH shared secret derivation
3. XChaCha20-Poly1305 authenticated encryption

This module handles the client-side decryption.
"""

import base64
import logging
from dataclasses import dataclass

from nacl.bindings import crypto_scalarmult
from nacl.encoding import RawEncoder
from nacl.hash import blake2b
from nacl.public import PrivateKey, PublicKey
from nacl.secret import Aead
from nacl.utils import random

logger = logging.getLogger(__name__)


@dataclass
class EncryptedSecretResponse:
    """Encrypted secret value response from Platform API."""

    encrypted_value: bytes
    nonce: bytes
    server_public_key: bytes
    algorithm: str


def generate_keypair() -> tuple[PrivateKey, PublicKey]:
    """Generate an ephemeral X25519 keypair for E2E encryption.

    Returns:
        Tuple of (private_key, public_key) for ECDH key exchange.
    """
    private_key = PrivateKey.generate()
    public_key = private_key.public_key
    return private_key, public_key


def public_key_to_base64(public_key: PublicKey) -> str:
    """Encode public key to base64 for HTTP header.

    Args:
        public_key: The X25519 public key to encode.

    Returns:
        Base64-encoded public key string.
    """
    return base64.b64encode(bytes(public_key)).decode("ascii")


def derive_symmetric_key(private_key: PrivateKey, peer_public_key: bytes) -> bytes:
    """Derive a symmetric key from ECDH shared secret using BLAKE2b.

    Matches the server-side protocol which uses BLAKE2b(raw_ecdh_output, 32)
    via libsodium's crypto_generichash.

    Args:
        private_key: Our X25519 private key.
        peer_public_key: Peer's 32-byte X25519 public key.

    Returns:
        32-byte symmetric key for XChaCha20-Poly1305.
    """
    raw_shared_secret = crypto_scalarmult(bytes(private_key), peer_public_key)
    return blake2b(raw_shared_secret, digest_size=32, encoder=RawEncoder)


def decrypt_secret_value(
    encrypted_response: EncryptedSecretResponse,
    private_key: PrivateKey,
) -> str:
    """Decrypt secret value using X25519 + BLAKE2b + XChaCha20-Poly1305.

    Performs ECDH key exchange with server's public key, derives symmetric
    key via BLAKE2b, then decrypts using XChaCha20-Poly1305.

    Args:
        encrypted_response: The encrypted response from Platform API.
        private_key: Client's ephemeral private key.

    Returns:
        Decrypted secret value as string.

    Raises:
        ValueError: If algorithm is not supported or decryption fails.
    """
    if encrypted_response.algorithm != "X25519-XChaCha20-Poly1305":
        msg = f"Unsupported algorithm: {encrypted_response.algorithm}"
        raise ValueError(msg)

    symmetric_key = derive_symmetric_key(
        private_key, encrypted_response.server_public_key
    )

    aead = Aead(symmetric_key)

    try:
        plaintext = aead.decrypt(
            encrypted_response.encrypted_value,
            nonce=encrypted_response.nonce,
        )
        return plaintext.decode("utf-8")
    except Exception as e:
        msg = f"Decryption failed: {e}"
        raise ValueError(msg) from e


@dataclass
class EncryptedSecretRequest:
    """Encrypted secret value to send to Platform API."""

    encrypted_value: bytes
    nonce: bytes
    client_public_key: bytes


def encrypt_secret_value(
    plaintext: str,
    server_public_key: bytes,
) -> tuple[EncryptedSecretRequest, PrivateKey]:
    """Encrypt a secret value for E2E transport to the Platform.

    Generates an ephemeral client keypair, derives a symmetric key via
    ECDH + BLAKE2b, and encrypts using XChaCha20-Poly1305.

    Args:
        plaintext: The secret value to encrypt.
        server_public_key: Server's 32-byte ephemeral X25519 public key.

    Returns:
        Tuple of (EncryptedSecretRequest, client_private_key).

    Raises:
        ValueError: If encryption fails.
    """
    private_key, public_key = generate_keypair()

    symmetric_key = derive_symmetric_key(private_key, server_public_key)

    aead = Aead(symmetric_key)

    try:
        nonce = random(Aead.NONCE_SIZE)
        # Aead.encrypt returns nonce + ciphertext; strip the nonce
        # since we send it separately in the request
        raw_output = aead.encrypt(
            plaintext.encode("utf-8"),
            nonce=nonce,
        )
        ciphertext = raw_output[Aead.NONCE_SIZE :]
        return (
            EncryptedSecretRequest(
                encrypted_value=ciphertext,
                nonce=nonce,
                client_public_key=bytes(public_key),
            ),
            private_key,
        )
    except Exception as e:
        msg = f"Encryption failed: {e}"
        raise ValueError(msg) from e
