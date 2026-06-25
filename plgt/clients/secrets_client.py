"""Secrets API client for Platform Service integration.

This module provides a client for interacting with the secrets API endpoints.
It handles E2E encryption for retrieving secret values securely.
"""

import base64
import logging
from datetime import UTC, datetime

import requests

from plgt.core.crypto import (
    EncryptedSecretResponse,
    decrypt_secret_value,
    encrypt_secret_value,
    generate_keypair,
    public_key_to_base64,
)
from plgt.core.exceptions import ServiceError
from plgt.core.sessions import APISession
from plgt.models.secret import Secret

logger = logging.getLogger(__name__)

# Scope tokens a secret value can be written/read at.
SCOPE_WORKSPACE = "workspace"
SCOPE_PRINCIPAL = "principal"


class SecretsClient:
    """Client for secrets API operations."""

    def __init__(self, session: APISession):
        """Initialize the secrets client with an API session.

        Args:
            session: Authenticated API session for making requests.
        """
        self.session = session

    def list_secrets(
        self,
        workspace: str,
        prefix: str | None = None,
    ) -> list[Secret]:
        """List all secrets in a workspace.

        Args:
            workspace: The workspace slug.
            prefix: Optional prefix to filter secrets by URI.

        Returns:
            List of Secret objects with metadata.

        Raises:
            ServiceError: If the request fails.
        """
        logger.debug("Listing secrets in workspace %s", workspace)

        try:
            params = {}
            if prefix:
                params["prefix"] = prefix

            response = self.session.get(
                f"/api/v1/secrets/{workspace}",
                params=params if params else None,
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            # Handle PagedResponse wrapper
            if isinstance(data, dict) and "items" in data:
                data = data["items"]

            secrets_data = data if isinstance(data, list) else []
            return [self._parse_secret(s) for s in secrets_data]

        except requests.exceptions.RequestException as e:
            logger.exception("Failed to list secrets in workspace %s", workspace)
            msg = f"Failed to list secrets: {e}"
            raise ServiceError(msg) from e

    def get_secret(self, workspace: str, secret_id: str) -> Secret:
        """Get a single secret's metadata by ID.

        Args:
            workspace: The workspace slug.
            secret_id: The secret ID (e.g., "matrix:SecretName").

        Returns:
            Secret object with metadata.

        Raises:
            ServiceError: If the request fails.
        """
        logger.debug(
            "Fetching secret %s in workspace %s",
            secret_id,
            workspace,
        )

        try:
            response = self.session.get(f"/api/v1/secrets/{workspace}/{secret_id}")

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            return self._parse_secret(data)

        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch secret %s in workspace %s",
                secret_id,
                workspace,
            )
            msg = f"Failed to fetch secret {secret_id}: {e}"
            raise ServiceError(msg) from e

    def get_secret_value(
        self,
        workspace: str,
        secret_id: str,
        scope: str = SCOPE_WORKSPACE,
        scope_entity_id: str | None = None,
    ) -> str:
        """Get a secret's decrypted value at a scope, with E2E encryption.

        Protocol (mirrors ``set_secret_value`` in reverse):
        1. POST /pubkey to get the server's ephemeral public key and keyId.
        2. Generate a client ephemeral keypair.
        3. GET /value with ``clientPublicKey`` + ``keyId`` (+ scope) query params.
        4. Re-derive the shared secret via ECDH against the server pubkey from
           step 1 and decrypt with XChaCha20-Poly1305.

        Args:
            workspace: The workspace slug.
            secret_id: The secret ID (e.g., "matrix:SecretName").
            scope: Which scope to read the value from — ``"workspace"`` (shared
                by every principal in the workspace) or ``"principal"`` (private
                to one principal). Defaults to ``"workspace"``.
            scope_entity_id: The principal id to read for, required when
                ``scope="principal"`` and ignored otherwise.

        Returns:
            Decrypted secret value as string.

        Raises:
            ServiceError: If the request or decryption fails.
        """
        logger.debug(
            "Fetching secret value %s in workspace %s at scope %s",
            secret_id,
            workspace,
            scope,
        )

        try:
            # Step 1: Get the server's ephemeral public key + keyId.
            pubkey_response = self.session.post(
                f"/api/v1/secrets/{workspace}/pubkey",
            )
            pubkey_data = pubkey_response.json()
            if "data" in pubkey_data:
                pubkey_data = pubkey_data["data"]

            server_public_key = base64.b64decode(pubkey_data["serverPublicKey"])
            key_id = pubkey_data["keyId"]

            # Step 2: Generate the client ephemeral keypair.
            private_key, public_key = generate_keypair()
            public_key_b64 = public_key_to_base64(public_key)

            # Step 3: GET /value with the handshake as query params.
            params = {
                "clientPublicKey": public_key_b64,
                "keyId": key_id,
                "scope": scope,
            }
            if scope_entity_id is not None:
                params["scopeEntityId"] = scope_entity_id

            response = self.session.get(
                f"/api/v1/secrets/{workspace}/{secret_id}/value",
                params=params,
            )

            data = response.json()

            # Handle API response wrapper
            if "data" in data:
                data = data["data"]

            # Step 4: Decrypt. The response envelope carries ciphertext + nonce
            # + algorithm only; the server pubkey comes from the /pubkey step.
            encrypted = EncryptedSecretResponse(
                encrypted_value=base64.b64decode(data["encryptedValue"]),
                nonce=base64.b64decode(data["nonce"]),
                server_public_key=server_public_key,
                algorithm=data["algorithm"],
            )
            return decrypt_secret_value(encrypted, private_key)

        except (ValueError, KeyError) as e:
            logger.exception(
                "Failed to decrypt secret value %s in workspace %s",
                secret_id,
                workspace,
            )
            msg = f"Failed to decrypt secret value: {e}"
            raise ServiceError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to fetch secret value %s in workspace %s",
                secret_id,
                workspace,
            )
            msg = f"Failed to fetch secret value: {e}"
            raise ServiceError(msg) from e

    def set_secret_value(
        self,
        workspace: str,
        secret_id: str,
        value: str,
        scope: str = SCOPE_WORKSPACE,
        scope_entity_id: str | None = None,
    ) -> None:
        """Set a secret's value at a scope, with E2E encryption.

        Protocol:
        1. POST /pubkey to get server's ephemeral public key and keyId
        2. Generate client ephemeral keypair
        3. Derive shared secret via ECDH + BLAKE2b
        4. Encrypt value with XChaCha20-Poly1305
        5. PUT encrypted payload with keyId + scope

        Args:
            workspace: The workspace slug.
            secret_id: The secret ID (e.g., "matrix:SecretName").
            value: The secret value to set.
            scope: Which scope to write the value at — ``"workspace"`` (shared
                by every principal in the workspace) or ``"principal"`` (private
                to one principal). Defaults to ``"workspace"``.
            scope_entity_id: The principal id to write for, required when
                ``scope="principal"`` and ignored otherwise.

        Raises:
            ServiceError: If the request or encryption fails.
        """
        logger.debug(
            "Setting secret value %s in workspace %s at scope %s",
            secret_id,
            workspace,
            scope,
        )

        try:
            # Step 1: Get server's ephemeral public key
            pubkey_response = self.session.post(
                f"/api/v1/secrets/{workspace}/pubkey",
            )
            pubkey_data = pubkey_response.json()
            if "data" in pubkey_data:
                pubkey_data = pubkey_data["data"]

            server_public_key = base64.b64decode(pubkey_data["serverPublicKey"])
            key_id = pubkey_data["keyId"]

            # Step 2-4: Generate keypair, derive shared secret, encrypt
            encrypted, _ = encrypt_secret_value(value, server_public_key)

            # Step 5: Send encrypted payload at the requested scope
            body = {
                "encryptedValue": base64.b64encode(encrypted.encrypted_value).decode(
                    "ascii"
                ),
                "nonce": base64.b64encode(encrypted.nonce).decode("ascii"),
                "clientPublicKey": base64.b64encode(encrypted.client_public_key).decode(
                    "ascii"
                ),
                "keyId": key_id,
                "scope": scope,
            }
            if scope_entity_id is not None:
                body["scopeEntityId"] = scope_entity_id

            self.session.put(
                f"/api/v1/secrets/{workspace}/{secret_id}/value",
                json=body,
            )

        except ValueError as e:
            logger.exception(
                "Failed to encrypt secret value %s in workspace %s",
                secret_id,
                workspace,
            )
            msg = f"Failed to encrypt secret value: {e}"
            raise ServiceError(msg) from e
        except requests.exceptions.RequestException as e:
            logger.exception(
                "Failed to set secret value %s in workspace %s",
                secret_id,
                workspace,
            )
            msg = f"Failed to set secret value: {e}"
            raise ServiceError(msg) from e

    def _parse_secret(self, data: dict) -> Secret:
        """Parse secret data from API response.

        Args:
            data: Raw secret data from API.

        Returns:
            Secret object.
        """
        matrix = data.get("matrix") or {}
        return Secret(
            id=data["id"],
            uri=data["uri"],
            description=data.get("description", ""),
            created_at=self._parse_datetime(data["createdAt"]),
            updated_at=self._parse_datetime(data["updatedAt"]),
            allowed_scopes=data.get("allowedScopes") or [],
            matrix_uri=matrix.get("uri"),
            matrix_name=matrix.get("name"),
        )

    def _parse_datetime(self, value: str) -> datetime:
        """Parse datetime string from API response.

        Args:
            value: ISO format datetime string.

        Returns:
            datetime object with UTC timezone.
        """
        # Handle various ISO formats
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        # If naive datetime (no timezone), assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        return dt
