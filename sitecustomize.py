"""Local runtime compatibility shims for DDR_Bench tools."""

import sys
import types

try:
    import mcp.client.streamable_http as _streamable_http

    if (
        not hasattr(_streamable_http, "streamable_http_client")
        and hasattr(_streamable_http, "streamablehttp_client")
    ):
        _streamable_http.streamable_http_client = _streamable_http.streamablehttp_client
except Exception:
    pass

try:
    import mcp.client.auth as _auth

    if not hasattr(_auth, "__path__"):
        _auth.__path__ = []

    if "mcp.client.auth.utils" not in sys.modules:
        _utils = types.ModuleType("mcp.client.auth.utils")

        def _unsupported(*_args, **_kwargs):
            raise RuntimeError(
                "mcp.client.auth.utils is unavailable in this MCP SDK version. "
                "OAuth MCP auth is not supported by the DDR_Bench compatibility shim."
            )

        _utils.build_oauth_authorization_server_metadata_discovery_urls = _unsupported
        _utils.build_protected_resource_metadata_discovery_urls = _unsupported
        _utils.create_oauth_metadata_request = _unsupported
        _utils.handle_auth_metadata_response = _unsupported
        _utils.handle_protected_resource_response = _unsupported
        sys.modules["mcp.client.auth.utils"] = _utils
except Exception:
    pass
