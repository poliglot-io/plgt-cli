"""Unit tests for crypto module.

Tests cover E2E encryption utilities for secret management including:
- Key generation
- Base64 encoding
- Response parsing
- Decryption
"""

import base64

import pytest
from nacl.public import PrivateKey
from plgt.core.crypto import (
    EncryptedSecretRequest,
    EncryptedSecretResponse,
    decrypt_secret_value,
    derive_symmetric_key,
    encrypt_secret_value,
    generate_keypair,
    parse_encrypted_response,
    public_key_to_base64,
)


class TestGenerateKeypair:
    """Test keypair generation."""

    def test_generates_private_and_public_key(self):
        """Test that generate_keypair returns valid keys."""
        private_key, public_key = generate_keypair()

        assert private_key is not None
        assert public_key is not None
        assert isinstance(private_key, PrivateKey)

    def test_generates_unique_keys_each_call(self):
        """Test that each call generates different keys."""
        private1, public1 = generate_keypair()
        private2, public2 = generate_keypair()

        assert bytes(private1) != bytes(private2)
        assert bytes(public1) != bytes(public2)


class TestPublicKeyToBase64:
    """Test public key base64 encoding."""

    def test_encodes_to_base64_string(self):
        """Test that public key is encoded to valid base64."""
        _, public_key = generate_keypair()

        result = public_key_to_base64(public_key)

        assert isinstance(result, str)
        # Verify it's valid base64
        decoded = base64.b64decode(result)
        assert decoded == bytes(public_key)

    def test_encoded_key_is_32_bytes_decoded(self):
        """Test that encoded key decodes to 32 bytes (X25519 key size)."""
        _, public_key = generate_keypair()

        result = public_key_to_base64(public_key)
        decoded = base64.b64decode(result)

        assert len(decoded) == 32


class TestParseEncryptedResponse:
    """Test encrypted response parsing."""

    def test_parses_valid_response(self):
        """Test parsing a valid encrypted response."""
        data = {
            "encryptedValue": base64.b64encode(b"encrypted-data").decode(),
            "nonce": base64.b64encode(b"nonce-24-bytes-long!!!!").decode(),
            "serverPublicKey": base64.b64encode(b"x" * 32).decode(),
            "algorithm": "X25519-XChaCha20-Poly1305",
        }

        result = parse_encrypted_response(data)

        assert isinstance(result, EncryptedSecretResponse)
        assert result.encrypted_value == b"encrypted-data"
        assert result.nonce == b"nonce-24-bytes-long!!!!"
        assert result.server_public_key == b"x" * 32
        assert result.algorithm == "X25519-XChaCha20-Poly1305"

    def test_raises_on_missing_encrypted_value(self):
        """Test that missing encryptedValue raises ValueError."""
        data = {
            "nonce": base64.b64encode(b"nonce").decode(),
            "serverPublicKey": base64.b64encode(b"x" * 32).decode(),
            "algorithm": "X25519-XChaCha20-Poly1305",
        }

        with pytest.raises(ValueError, match="Missing required field"):
            parse_encrypted_response(data)

    def test_raises_on_missing_nonce(self):
        """Test that missing nonce raises ValueError."""
        data = {
            "encryptedValue": base64.b64encode(b"encrypted").decode(),
            "serverPublicKey": base64.b64encode(b"x" * 32).decode(),
            "algorithm": "X25519-XChaCha20-Poly1305",
        }

        with pytest.raises(ValueError, match="Missing required field"):
            parse_encrypted_response(data)

    def test_raises_on_invalid_base64(self):
        """Test that invalid base64 raises ValueError."""
        data = {
            "encryptedValue": "not-valid-base64!!!",
            "nonce": base64.b64encode(b"nonce").decode(),
            "serverPublicKey": base64.b64encode(b"x" * 32).decode(),
            "algorithm": "X25519-XChaCha20-Poly1305",
        }

        with pytest.raises(ValueError, match="Failed to parse"):
            parse_encrypted_response(data)


class TestDecryptSecretValue:
    """Test secret value decryption."""

    def test_raises_on_unsupported_algorithm(self):
        """Test that unsupported algorithm raises ValueError."""
        encrypted = EncryptedSecretResponse(
            encrypted_value=b"data",
            nonce=b"nonce",
            server_public_key=b"x" * 32,
            algorithm="UNSUPPORTED_ALGORITHM",
        )
        private_key, _ = generate_keypair()

        with pytest.raises(ValueError, match="Unsupported algorithm"):
            decrypt_secret_value(encrypted, private_key)

    def test_raises_on_decryption_failure(self):
        """Test that invalid ciphertext raises ValueError."""
        # Create invalid encrypted data that will fail decryption
        encrypted = EncryptedSecretResponse(
            encrypted_value=b"invalid-ciphertext",
            nonce=b"x" * 24,  # XChaCha20-Poly1305 uses 24-byte nonce
            server_public_key=b"y" * 32,
            algorithm="X25519-XChaCha20-Poly1305",
        )
        private_key, _ = generate_keypair()

        with pytest.raises(ValueError, match="Decryption failed"):
            decrypt_secret_value(encrypted, private_key)


class TestDeriveSymmetricKey:
    """Test ECDH + BLAKE2b symmetric key derivation."""

    def test_derive_symmetric_key_is_32_bytes(self):
        """Test that derived key is 32 bytes."""
        private_key, _ = generate_keypair()
        _, peer_public = generate_keypair()

        key = derive_symmetric_key(private_key, bytes(peer_public))

        assert len(key) == 32

    def test_derive_symmetric_key_is_deterministic(self):
        """Test that same inputs produce same key."""
        private_key, _ = generate_keypair()
        _, peer_public = generate_keypair()

        key1 = derive_symmetric_key(private_key, bytes(peer_public))
        key2 = derive_symmetric_key(private_key, bytes(peer_public))

        assert key1 == key2

    def test_derive_symmetric_key_differs_from_raw_ecdh(self):
        """Test that BLAKE2b hashing changes the raw ECDH output."""
        from nacl.bindings import crypto_scalarmult

        private_key, _ = generate_keypair()
        _, peer_public = generate_keypair()

        raw = crypto_scalarmult(bytes(private_key), bytes(peer_public))
        derived = derive_symmetric_key(private_key, bytes(peer_public))

        assert raw != derived

    def test_derive_symmetric_key_both_sides_agree(self):
        """Test that both sides derive the same symmetric key."""
        alice_private, alice_public = generate_keypair()
        bob_private, bob_public = generate_keypair()

        alice_key = derive_symmetric_key(alice_private, bytes(bob_public))
        bob_key = derive_symmetric_key(bob_private, bytes(alice_public))

        assert alice_key == bob_key


class TestEndToEndCrypto:
    """Test full E2E encryption/decryption round-trip."""

    def test_encrypt_decrypt_round_trip(self):
        """Test that we can decrypt what was encrypted with matching keys.

        Simulates the server-side protocol: ECDH + BLAKE2b + XChaCha20-Poly1305.
        """
        from nacl.bindings import (
            crypto_aead_xchacha20poly1305_ietf_encrypt,
            crypto_scalarmult,
        )
        from nacl.encoding import RawEncoder
        from nacl.hash import blake2b

        plaintext = "my-secret-value"

        # Client generates keypair
        client_private, client_public = generate_keypair()

        # Server generates ephemeral keypair
        server_private, server_public = generate_keypair()

        # Server derives symmetric key (ECDH + BLAKE2b)
        raw_shared = crypto_scalarmult(bytes(server_private), bytes(client_public))
        server_symmetric_key = blake2b(raw_shared, digest_size=32, encoder=RawEncoder)

        # Server encrypts
        nonce = b"x" * 24
        ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext.encode(),
            aad=None,
            nonce=nonce,
            key=server_symmetric_key,
        )

        # Create response as it would come from API
        encrypted = EncryptedSecretResponse(
            encrypted_value=ciphertext,
            nonce=nonce,
            server_public_key=bytes(server_public),
            algorithm="X25519-XChaCha20-Poly1305",
        )

        # Client decrypts
        result = decrypt_secret_value(encrypted, client_private)

        assert result == plaintext


class TestEncryptSecretValue:
    """Test secret value encryption for set_secret_value."""

    def test_encrypt_returns_encrypted_request(self):
        """Test that encrypt_secret_value returns EncryptedSecretRequest."""
        _, server_public = generate_keypair()

        encrypted, _ = encrypt_secret_value("test-value", bytes(server_public))

        assert isinstance(encrypted, EncryptedSecretRequest)
        assert len(encrypted.nonce) == 24
        assert len(encrypted.client_public_key) == 32
        assert len(encrypted.encrypted_value) > 0

    def test_encrypt_decrypt_round_trip(self):
        """Test that server can decrypt what client encrypted.

        Simulates the full set_secret_value protocol.
        """
        from nacl.bindings import (
            crypto_aead_xchacha20poly1305_ietf_decrypt,
            crypto_scalarmult,
        )
        from nacl.encoding import RawEncoder
        from nacl.hash import blake2b

        plaintext = "my-secret-value-to-set"

        # Server generates keypair (POST /pubkey)
        server_private, server_public = generate_keypair()

        # Client encrypts
        encrypted, _ = encrypt_secret_value(plaintext, bytes(server_public))

        # Server derives symmetric key (ECDH + BLAKE2b)
        raw_shared = crypto_scalarmult(
            bytes(server_private), encrypted.client_public_key
        )
        server_symmetric_key = blake2b(raw_shared, digest_size=32, encoder=RawEncoder)

        # Server decrypts
        decrypted = crypto_aead_xchacha20poly1305_ietf_decrypt(
            encrypted.encrypted_value,
            aad=None,
            nonce=encrypted.nonce,
            key=server_symmetric_key,
        )

        assert decrypted.decode("utf-8") == plaintext
