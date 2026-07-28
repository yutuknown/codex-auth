class ProviderError(RuntimeError):
    """Base error for failures that can be mapped consistently by API routes."""

    error_type = "provider_error"
    status_code = 502


class ProviderNotFoundError(ProviderError):
    error_type = "provider_not_found"
    status_code = 404


class ProviderNotConfiguredError(ProviderError):
    error_type = "provider_not_configured"
    status_code = 503


class ProviderUnsupportedError(ProviderError):
    error_type = "unsupported_feature"
    status_code = 501


class ProviderUpstreamError(ProviderError):
    error_type = "upstream_error"
    status_code = 502


class ProviderBusyError(ProviderError):
    error_type = "rate_limit_error"
    status_code = 429
