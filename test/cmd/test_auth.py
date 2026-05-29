"""Unit tests for auth command.

Tests cover authentication command functionality.
"""

from unittest.mock import Mock, patch

from plgt.cmd.auth import create_key, list_keys, login, revoke_key, sync


class TestLogin:
    """Test login command."""

    @patch("plgt.cmd.auth._sync")
    @patch("plgt.cmd.auth.OAuthClient")
    def test_login_success(self, mock_oauth_class, mock_sync):
        """Test successful login flow."""
        mock_oauth = Mock()
        mock_oauth_class.return_value = mock_oauth

        login()

        mock_oauth.auth_code_flow.assert_called_once()
        mock_sync.assert_called_once()

    @patch("plgt.cmd.auth._sync")
    @patch("plgt.cmd.auth.OAuthClient")
    def test_login_calls_oauth_flow(self, mock_oauth_class, mock_sync):
        """Test that login initiates OAuth flow."""
        mock_oauth = Mock()
        mock_oauth_class.return_value = mock_oauth

        login()

        mock_oauth.auth_code_flow.assert_called_once()


class TestSync:
    """Test workspace sync command."""

    @patch("plgt.cmd.auth._sync")
    def test_sync_calls_internal_sync(self, mock_sync):
        """Test that sync command calls _sync function."""
        sync()

        mock_sync.assert_called_once()


class TestSyncInternal:
    """Test internal _sync functionality."""

    @patch("plgt.cmd.auth.config")
    def test_sync_fetches_workspaces(self, mock_config):
        """Test that _sync fetches workspaces from API."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "slug": "workspace-1",
                    "id": "ws-id-1",
                    "description": "Test workspace 1",
                },
                {
                    "slug": "workspace-2",
                    "id": "ws-id-2",
                    "description": "Test workspace 2",
                },
            ]
        }
        mock_session.get.return_value = mock_response

        mock_config.defaults.get.return_value = None

        from plgt.cmd.auth import _sync

        _sync()

        mock_session.get.assert_called_once_with("/api/v1/workspaces")
        assert mock_config.add_workspace.call_count == 2

    @patch("plgt.cmd.auth.config")
    def test_sync_sets_default_workspace(self, mock_config):
        """Test that _sync sets first workspace as default if none exists."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [{"slug": "first-workspace", "id": "ws-1"}]
        }
        mock_session.get.return_value = mock_response

        mock_config.defaults.get.return_value = None  # No default set

        from plgt.cmd.auth import _sync

        _sync()

        mock_config.set_defaults.assert_called_once_with(workspace="first-workspace")

    @patch("plgt.cmd.auth.config")
    def test_sync_no_workspaces(self, mock_config):
        """Test _sync handles empty workspace list."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        from plgt.cmd.auth import _sync

        _sync()

        mock_config.add_workspace.assert_not_called()
        mock_config.set_defaults.assert_not_called()


class TestCreateKey:
    """Test create-key command."""

    @patch("plgt.cmd.auth.config")
    def test_create_key_posts_to_correct_endpoint(self, mock_config):
        """Test that create-key sends POST to /api/v1/users/me/keys with NEVER expiration."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": {
                "id": "key-id-123",
                "name": "test-key",
                "key": "plgt_abcdefghijklmnopqrstuvwxyz1234567890ab",
                "prefix": "plgt_abcd",
            }
        }
        mock_session.post.return_value = mock_response

        create_key("test-key", expires=None)

        mock_session.post.assert_called_once_with(
            "/api/v1/users/me/keys",
            json={"name": "test-key", "expiresIn": "NEVER"},
        )

    @patch("plgt.cmd.auth.config")
    def test_create_key_with_30d_expiry(self, mock_config):
        """Test that create-key maps 30d to THIRTY_DAYS enum."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {"key": "plgt_test123"}}
        mock_session.post.return_value = mock_response

        create_key("expiring-key", expires="30d")

        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["expiresIn"] == "THIRTY_DAYS"

    @patch("plgt.cmd.auth.config")
    def test_create_key_with_7d_expiry(self, mock_config):
        """Test that create-key maps 7d to SEVEN_DAYS enum."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {"key": "plgt_test123"}}
        mock_session.post.return_value = mock_response

        create_key("weekly-key", expires="7d")

        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["expiresIn"] == "SEVEN_DAYS"

    @patch("plgt.cmd.auth.config")
    def test_create_key_with_14d_expiry(self, mock_config):
        """Test that create-key maps 14d to FOURTEEN_DAYS enum."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {"key": "plgt_test123"}}
        mock_session.post.return_value = mock_response

        create_key("biweekly-key", expires="14d")

        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["expiresIn"] == "FOURTEEN_DAYS"

    @patch("plgt.cmd.auth.config")
    def test_create_key_with_90d_expiry(self, mock_config):
        """Test that create-key maps 90d to NINETY_DAYS enum."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {"key": "plgt_test123"}}
        mock_session.post.return_value = mock_response

        create_key("quarterly-key", expires="90d")

        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["expiresIn"] == "NINETY_DAYS"

    @patch("plgt.cmd.auth.config")
    def test_create_key_with_never_expiry(self, mock_config):
        """Test that create-key maps 'never' to NEVER enum."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {"key": "plgt_test123"}}
        mock_session.post.return_value = mock_response

        create_key("permanent-key", expires="never")

        call_args = mock_session.post.call_args
        assert call_args[1]["json"]["expiresIn"] == "NEVER"

    @patch("plgt.cmd.auth.config")
    def test_create_key_empty_response_logs_error(self, mock_config):
        """Test that create-key handles missing key in response."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": {}}
        mock_session.post.return_value = mock_response

        # Should not raise — logs error and returns
        create_key("test-key", expires=None)

        mock_session.post.assert_called_once()


class TestListKeys:
    """Test list-keys command."""

    @patch("plgt.cmd.auth.config")
    def test_list_keys_gets_correct_endpoint(self, mock_config):
        """Test that list-keys sends GET to /api/v1/users/me/keys."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {"data": []}
        mock_session.get.return_value = mock_response

        list_keys()

        mock_session.get.assert_called_once_with("/api/v1/users/me/keys")

    @patch("plgt.cmd.auth.config")
    def test_list_keys_with_populated_data(self, mock_config):
        """Test that list-keys correctly accesses all fields from key data."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "name": "ci-pipeline",
                    "prefix": "plgt_abcd",
                    "createdAt": "2025-01-15T10:30:00Z",
                    "lastUsedAt": "2025-01-16T08:00:00Z",
                    "expiresAt": "2025-04-15T10:30:00Z",
                },
                {
                    "id": "650e8400-e29b-41d4-a716-446655440000",
                    "name": "dev-key-no-expiry",
                    "prefix": "plgt_efgh",
                    "createdAt": "2025-01-10T00:00:00Z",
                    "lastUsedAt": None,
                    "expiresAt": None,
                },
            ]
        }
        mock_session.get.return_value = mock_response

        # Should not raise — all fields accessed correctly
        list_keys()

        mock_session.get.assert_called_once_with("/api/v1/users/me/keys")


class TestRevokeKey:
    """Test revoke-key command."""

    @patch("plgt.cmd.auth.config")
    def test_revoke_key_deletes_correct_endpoint(self, mock_config):
        """Test that revoke-key sends DELETE to /api/v1/users/me/keys/{keyId}."""
        mock_session = Mock()
        mock_config.get_session.return_value = mock_session

        mock_response = Mock()
        mock_session.delete.return_value = mock_response

        revoke_key("key-uuid-123")

        mock_session.delete.assert_called_once_with(
            "/api/v1/users/me/keys/key-uuid-123"
        )
