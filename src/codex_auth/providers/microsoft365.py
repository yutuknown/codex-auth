import asyncio
import json
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any, AsyncGenerator, Dict, Iterable

from curl_cffi.const import CurlWsFlag
from curl_cffi.requests import Session

from ..config import (
    load_m365_auth_data,
    load_m365_graph_data,
    load_m365_graph_oauth_data,
    load_m365_oauth_data,
    load_provider_cookie_text,
    provider_cookies_are_configured,
    save_m365_auth_data,
    save_m365_graph_data,
    save_m365_graph_oauth_data,
    save_m365_oauth_data,
)
from .base import BaseProvider, ProviderCapabilities
from .cookies import parse_netscape_cookies
from .errors import (
    ProviderBusyError,
    ProviderNotConfiguredError,
    ProviderNotFoundError,
    ProviderUpstreamError,
)

logger = logging.getLogger("codex_auth")

WEB_URL = "https://m365.cloud.microsoft/chat"
CHAT_HUB = "wss://substrate.office.com/m365Copilot/Chathub"
OAUTH_HOST = "login.microsoftonline.com"
GRAPH_ME_URL = (
    "https://graph.microsoft.com/v1.0/me"
    "?$select=id,displayName,userPrincipalName,mail,jobTitle,officeLocation,preferredLanguage,usageLocation"
)
RECORD_SEPARATOR = "\x1e"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "update_textdoc_response_after_streaming",
    "deepleo_networking_timeout_10minutes_canmore",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "enable_msa_user",
    "cwcgptv",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
    "pdnascan",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "cwc_code_interpreter_interactive_charts_inline_image",
    "code_interpreter_matplotlib_patching",
    "cwc_fileupload_odb",
    "update_memory_plugin",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
    "flux_v3_references",
    "flux_v3_references_entities",
    "flux_v3_image_gen_enable_non_watermarked_storage",
    "flux_v3_image_gen_enable_story",
    "rich_responses",
]
ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GeneratedCode",
    "RenderCardRequest",
    "AdsQuery",
    "SemanticSerp",
    "GenerateContentQuery",
    "GenerateGraphicArt",
    "SearchQuery",
    "ConfirmationCard",
    "AuthError",
    "DeveloperLogs",
    "TriggerPlugin",
    "HintInvocation",
    "MemoryUpdate",
    "EndOfRequest",
    "TriggerConfirmation",
    "ResumeInvokeAction",
    "ResumeUserInputRequest",
    "TriggerUserInputRequest",
    "EscapeHatch",
    "TriggerPluginAuth",
    "ResumePluginAuth",
    "SideBySide",
    "ReferencesListComplete",
    "SwitchRespondingEndpoint",
]
MODEL_TONES = {
    "auto": "Magic",
    "quick-response": "Chat",
    "think-deeper": "Reasoning",
    "gpt-5.5-quick-response": "Gpt_5_5_Chat",
    "gpt-5.5-think-deeper": "Gpt_5_5_Reasoning",
}
MODEL_TITLES = {
    "auto": "Auto",
    "quick-response": "Quick Response",
    "think-deeper": "Think Deeper",
    "gpt-5.5-quick-response": "GPT 5.5 Quick Response",
    "gpt-5.5-think-deeper": "GPT 5.5 Think Deeper",
}
MODEL_CATALOG_CACHE_SECONDS = 15 * 60
MODEL_METADATA_MARKER = "modelSelectorMetadata"
MODEL_METADATA_MAX_CHARS = 128 * 1024


def _slug_for_tone(tone: str) -> str:
    for slug, known_tone in MODEL_TONES.items():
        if known_tone == tone:
            return slug
    match = re.fullmatch(r"Gpt_(\d+)_(\d+)_(Auto|Chat|Reasoning)", tone)
    if match:
        version = f"{match.group(1)}.{match.group(2)}"
        suffix = {
            "Auto": "auto",
            "Chat": "quick-response",
            "Reasoning": "think-deeper",
        }[match.group(3)]
        return f"gpt-{version}-{suffix}"
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", tone.lower())).strip("-")


def _flatten_model_options(options: list[Any]) -> Iterable[dict[str, Any]]:
    for option in options:
        if not isinstance(option, dict):
            continue
        group = option.get("itemGroup")
        if isinstance(group, list):
            yield from _flatten_model_options(group)
        elif option.get("id"):
            yield option


def parse_model_catalog(html: str) -> tuple[list[dict[str, Any]], str | None]:
    """Extract the service-driven model picker from the authenticated chat shell."""
    catalogs: list[dict[str, Any]] = []
    default_tone = None
    offset = 0
    decoder = json.JSONDecoder()
    while True:
        marker = html.find(MODEL_METADATA_MARKER, offset)
        if marker < 0:
            break
        offset = marker + len(MODEL_METADATA_MARKER)
        fragment = html[marker : marker + MODEL_METADATA_MAX_CHARS].replace('\\"', '"')
        object_start = fragment.find("{")
        if object_start < 0:
            continue
        try:
            metadata, _ = decoder.raw_decode(fragment[object_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict):
            continue
        if default_tone is None and metadata.get("defaultModelSelectionId"):
            default_tone = str(metadata["defaultModelSelectionId"])
        options = metadata.get("availableModelSelectionOptions")
        if not isinstance(options, list):
            continue
        for option in _flatten_model_options(options):
            tone = str(option["id"])
            if any(item["tone"] == tone for item in catalogs):
                continue
            title = str(option.get("menuItemTitle") or option.get("shortTitle") or tone)
            catalogs.append(
                {
                    "slug": _slug_for_tone(tone),
                    "tone": tone,
                    "title": title,
                    "description": str(option.get("menuItemDescription") or ""),
                    "section": int(option.get("sectionNumber") or 0),
                }
            )
    return catalogs, default_tone


class Microsoft365CopilotProvider(BaseProvider):
    provider_id = "m365-copilot"
    display_name = "Microsoft 365 Copilot Chat"
    auth_kind = "Microsoft 365 cookies plus short-lived Copilot bearer"
    capabilities = ProviderCapabilities(text=True, streaming=True, web_search=True)

    def __init__(self) -> None:
        self.session: Session | None = None
        self.access_token = ""
        self.access_token_expires_at = 0.0
        self.identity = ""
        self.variants = ""
        self.cookie_count = 0
        self.web_session_valid = False
        self.initialized = False
        self.started_at = time.time()
        self.lock = asyncio.Lock()
        self.admission = asyncio.Semaphore(4)
        self._models_cache: list[Dict[str, Any]] = []
        self._models_cache_time = 0.0
        self._model_tones = dict(MODEL_TONES)
        self.default_model = "auto"
        self.model_catalog_source = "fallback"

    def is_configured(self) -> bool:
        return provider_cookies_are_configured(self.provider_id)

    def _initialize_sync(self, cookie_text: str | None = None) -> None:
        cookie_text = cookie_text or load_provider_cookie_text(self.provider_id)
        cookies = parse_netscape_cookies(cookie_text)
        session = Session(impersonate="chrome")
        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
            )
        response = session.get(WEB_URL, allow_redirects=True, timeout=30)
        if response.status_code != 200 or urllib.parse.urlsplit(response.url).path != "/chat":
            response.close()
            session.close()
            raise ProviderUpstreamError("Microsoft 365 cookies did not open the authenticated Copilot chat shell")
        shell_html = response.text
        signed_in = "Sign in" not in shell_html and "Copilot" in shell_html
        response.close()
        if not signed_in:
            session.close()
            raise ProviderUpstreamError(
                "Microsoft 365 returned the chat shell without an authenticated Copilot session"
            )

        auth = load_m365_auth_data()
        self.session = session
        self.cookie_count = len(cookies)
        self.web_session_valid = True
        self._apply_model_catalog(shell_html)
        self.access_token = str(auth.get("access_token") or "")
        self.access_token_expires_at = float(auth.get("expires_at") or 0)
        self.identity = str(auth.get("identity") or "")
        self.variants = str(auth.get("variants") or "")
        self.initialized = True
        if self.refresh_configured and (
            not self.access_token
            or not self.access_token_expires_at
            or self.access_token_expires_at <= time.time() + 300
        ):
            self._refresh_access_token_sync()

    @property
    def refresh_configured(self) -> bool:
        oauth = load_m365_oauth_data()
        form = oauth.get("form")
        return bool(isinstance(form, dict) and form.get("refresh_token") and oauth.get("token_endpoint"))

    def _refresh_access_token_sync(self) -> None:
        if not self.session:
            raise ProviderUpstreamError("Microsoft 365 session is not initialized")
        oauth = load_m365_oauth_data()
        endpoint = str(oauth.get("token_endpoint") or "")
        parsed_endpoint = urllib.parse.urlsplit(endpoint)
        if (
            parsed_endpoint.scheme != "https"
            or parsed_endpoint.hostname != OAUTH_HOST
            or not parsed_endpoint.path.endswith("/oauth2/v2.0/token")
        ):
            raise ProviderNotConfiguredError("Microsoft 365 OAuth token endpoint is missing or invalid")
        form = oauth.get("form")
        if not isinstance(form, dict) or not form.get("refresh_token"):
            raise ProviderNotConfiguredError("Microsoft 365 OAuth refresh token is not configured")
        query = dict(oauth.get("query") or {})
        query["client-request-id"] = str(uuid.uuid4())
        response = self.session.post(
            endpoint,
            params=query,
            data=form,
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "Origin": "https://m365.cloud.microsoft",
                "Referer": "https://m365.cloud.microsoft/",
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
        try:
            if response.status_code != 200:
                raise ProviderUpstreamError(f"Microsoft 365 OAuth refresh returned HTTP {response.status_code}")
            token_data = response.json()
        finally:
            response.close()
        access_token = str(token_data.get("access_token") or "")
        refresh_token = str(token_data.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise ProviderUpstreamError("Microsoft 365 OAuth refresh response omitted required tokens")

        captured_at = int(time.time())
        expires_in = int(token_data.get("expires_in") or 0)
        self.access_token = access_token
        self.access_token_expires_at = captured_at + expires_in

        updated_oauth = dict(oauth)
        updated_form = dict(form)
        updated_form["refresh_token"] = refresh_token
        updated_oauth["form"] = updated_form
        updated_oauth["captured_at"] = captured_at
        if token_data.get("refresh_token_expires_in") is not None:
            updated_oauth["refresh_token_expires_in"] = token_data["refresh_token_expires_in"]

        updated_auth = load_m365_auth_data()
        updated_auth.update(
            {
                "access_token": access_token,
                "captured_at": captured_at,
                "expires_in": expires_in,
                "expires_at": self.access_token_expires_at,
            }
        )
        save_m365_oauth_data(updated_oauth)
        save_m365_auth_data(updated_auth)
        logger.info("[Microsoft 365] OAuth access and refresh tokens rotated")

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        logger.info(
            "[Microsoft 365] Cookie session authenticated; generation bearer %s",
            "available" if self.generation_ready else "missing",
        )

    async def replace_cookies(self, cookie_text: str) -> dict[str, Any]:
        candidate = Microsoft365CopilotProvider()
        await asyncio.to_thread(candidate._initialize_sync, cookie_text)
        async with self.lock:
            old_session = self.session
            self.session = candidate.session
            candidate.session = None
            self.access_token = candidate.access_token
            self.access_token_expires_at = candidate.access_token_expires_at
            self.identity = candidate.identity
            self.variants = candidate.variants
            self.cookie_count = candidate.cookie_count
            self.web_session_valid = candidate.web_session_valid
            self.initialized = candidate.initialized
            self._models_cache = candidate._models_cache
            self._models_cache_time = candidate._models_cache_time
            self._model_tones = candidate._model_tones
            self.default_model = candidate.default_model
            self.model_catalog_source = candidate.model_catalog_source
        if old_session:
            await asyncio.to_thread(old_session.close)
        return self.runtime_status()

    @property
    def generation_ready(self) -> bool:
        return bool(self.access_token and self.identity)

    def runtime_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "initialized": self.initialized,
            "configured": self.is_configured(),
            "web_session_valid": self.web_session_valid,
            "generation_ready": self.generation_ready,
            "refresh_configured": self.refresh_configured,
            "access_token_expires_in_seconds": max(
                0,
                round(self.access_token_expires_at - time.time()),
            )
            if self.access_token_expires_at
            else None,
            "auth_mode": "cookies_plus_bearer" if self.generation_ready else "cookies_only",
            "transport": "curl-cffi websocket",
            "browser_process": False,
            "cookie_count": self.cookie_count,
            "model_count": len(self._models_cache),
            "default_model": self.default_model,
            "model_catalog_source": self.model_catalog_source,
            "model_catalog_age_seconds": (
                max(0, round(time.time() - self._models_cache_time)) if self._models_cache_time else None
            ),
            "uptime_seconds": max(0, round(time.time() - self.started_at)),
            "proxy_capabilities": {
                "text": self.generation_ready,
                "streaming": self.generation_ready,
                "web_search": self.generation_ready,
                "image_input": False,
                "file_uploads": False,
                "function_tools": False,
            },
        }

    @staticmethod
    def _is_valid_oauth_endpoint(endpoint: str) -> bool:
        parsed_endpoint = urllib.parse.urlsplit(endpoint)
        return (
            parsed_endpoint.scheme == "https"
            and parsed_endpoint.hostname == OAUTH_HOST
            and parsed_endpoint.path.endswith("/oauth2/v2.0/token")
        )

    @property
    def graph_refresh_configured(self) -> bool:
        oauth = load_m365_graph_oauth_data()
        form = oauth.get("form")
        return bool(isinstance(form, dict) and form.get("refresh_token") and oauth.get("token_endpoint"))

    def _refresh_graph_access_token_sync(self) -> dict[str, Any]:
        oauth = load_m365_graph_oauth_data()
        endpoint = str(oauth.get("token_endpoint") or "")
        form = oauth.get("form")
        if not self._is_valid_oauth_endpoint(endpoint) or not isinstance(form, dict) or not form.get("refresh_token"):
            raise ProviderNotConfiguredError("Microsoft Graph refresh configuration is missing or invalid")
        session = Session(impersonate="chrome")
        try:
            response = session.post(
                endpoint,
                params={**dict(oauth.get("query") or {}), "client-request-id": str(uuid.uuid4())},
                data=form,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "User-Agent": USER_AGENT,
                },
                timeout=30,
            )
            try:
                if response.status_code != 200:
                    raise ProviderUpstreamError(f"Microsoft Graph token refresh returned HTTP {response.status_code}")
                token_data = response.json()
            finally:
                response.close()
        finally:
            session.close()
        access_token = str(token_data.get("access_token") or "")
        if not access_token:
            raise ProviderUpstreamError("Microsoft Graph token refresh omitted an access token")
        captured_at = int(time.time())
        expires_in = int(token_data.get("expires_in") or 0)
        graph_data = load_m365_graph_data()
        graph_data.update({
            "access_token": access_token,
            "captured_at": captured_at,
            "expires_in": expires_in,
            "expires_at": captured_at + expires_in,
        })
        refreshed_oauth = dict(oauth)
        refreshed_form = dict(form)
        if token_data.get("refresh_token"):
            refreshed_form["refresh_token"] = str(token_data["refresh_token"])
        refreshed_oauth["form"] = refreshed_form
        refreshed_oauth["captured_at"] = captured_at
        save_m365_graph_data(graph_data)
        save_m365_graph_oauth_data(refreshed_oauth)
        return graph_data

    def _fetch_graph_profile_sync(self) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        graph_data = load_m365_graph_data()
        expires_at = float(graph_data.get("expires_at") or 0)
        if (not graph_data.get("access_token") or (expires_at and expires_at <= time.time() + 60)) and self.graph_refresh_configured:
            try:
                graph_data = self._refresh_graph_access_token_sync()
            except ProviderUpstreamError as exc:
                return {}, {"state": "error", "source": "Microsoft Graph", "message": str(exc)}, [{"source": "Microsoft Graph", "state": "error", "required": False}]

        access_token = str(graph_data.get("access_token") or "")
        if not access_token:
            return {}, {"state": "not_connected", "source": "Microsoft Graph", "message": "Connect a read-only Microsoft Graph profile token to show account details."}, [{"source": "Microsoft Graph", "state": "not_connected", "required": False}]

        session = Session(impersonate="chrome")
        try:
            response = session.get(
                GRAPH_ME_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=30,
            )
            try:
                status = response.status_code
                payload = response.json() if status == 200 else {}
            finally:
                response.close()
        finally:
            session.close()

        if status != 200:
            state = "expired" if status in {401, 403} else "error"
            message = "Microsoft Graph profile token needs reconnection." if state == "expired" else f"Microsoft Graph returned HTTP {status}"
            return {}, {"state": state, "source": "Microsoft Graph", "status": status, "message": message}, [{"source": "Microsoft Graph", "state": state, "status": status, "required": False}]

        profile = {
            "id": payload.get("id"),
            "name": payload.get("displayName"),
            "email": payload.get("mail") or payload.get("userPrincipalName"),
            "identity_level": "identified",
        }
        account = {
            "user_principal_name": payload.get("userPrincipalName"),
            "job_title": payload.get("jobTitle"),
            "office_location": payload.get("officeLocation"),
            "preferred_language": payload.get("preferredLanguage"),
            "usage_location": payload.get("usageLocation"),
        }
        return {"profile": profile, "account": account}, {"state": "available", "source": "Microsoft Graph", "status": status}, [{"source": "Microsoft Graph", "state": "available", "status": status, "required": False}]

    async def fetch_account_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        if self.is_configured() and not self.initialized:
            try:
                await self.initialize()
            except ProviderUpstreamError as exc:
                runtime = self.runtime_status()
                base = await super().fetch_account_snapshot(refresh=refresh)
                base["connection"]["state"] = "error"
                base["diagnostics"] = [{"source": "Microsoft 365 Copilot", "state": "error", "message": str(exc), "required": True}]
                base["runtime"] = self._safe_runtime_status(runtime)
                return base
        graph, graph_connection, diagnostics = await asyncio.to_thread(self._fetch_graph_profile_sync)
        runtime = self.runtime_status()
        connection_state = "active" if runtime.get("initialized") and runtime.get("generation_ready") else "configured" if runtime.get("configured") else "not_configured"
        return {
            "provider": self.descriptor(),
            "connection": {
                "state": connection_state,
                "configured": bool(runtime.get("configured")),
                "initialized": bool(runtime.get("initialized")),
                "generation_ready": bool(runtime.get("generation_ready")),
                "auth_mode": runtime.get("auth_mode"),
                "profile_connection": graph_connection,
            },
            "profile": graph.get("profile") or {},
            "account": graph.get("account") or {},
            "entitlement": {},
            "privacy": {},
            "models": {
                "count": runtime.get("model_count", 0),
                "default_model": runtime.get("default_model"),
                "catalog_source": runtime.get("model_catalog_source"),
            },
            "credentials": {
                "cookie_session": "active" if runtime.get("web_session_valid") else "unavailable",
                "generation_bearer": "active" if runtime.get("generation_ready") else "required",
                "generation_refresh": "configured" if self.refresh_configured else "manual",
                "graph_profile": graph_connection.get("state"),
                "graph_refresh": "configured" if self.graph_refresh_configured else "manual",
            },
            "runtime": self._safe_runtime_status(runtime),
            "diagnostics": diagnostics,
            "warnings": ([graph_connection["message"]] if graph_connection.get("message") else []),
        }

    async def close(self) -> None:
        if self.session:
            await asyncio.to_thread(self.session.close)
        self.session = None
        self.initialized = False
        self.web_session_valid = False

    async def reset_session(self, model: str):
        return None

    @staticmethod
    def _fallback_models() -> list[Dict[str, Any]]:
        return [
            {
                "slug": slug,
                "title": title,
                "description": f"Microsoft 365 Copilot mode using tone {MODEL_TONES[slug]}",
                "max_tokens": 32768,
                "tags": ["web-search"],
                "enabled_tools": ["search"],
                "product_features": {},
            }
            for slug, title in MODEL_TITLES.items()
        ]

    def _apply_model_catalog(self, html: str) -> bool:
        discovered, default_tone = parse_model_catalog(html)
        if not discovered:
            if not self._models_cache:
                self._models_cache = self._fallback_models()
                self._models_cache_time = time.time()
                self.model_catalog_source = "fallback"
            return False
        models = []
        tones = {}
        for item in discovered:
            slug = item["slug"]
            tone = item["tone"]
            tones[slug] = tone
            description = item["description"] or (f"Microsoft 365 Copilot mode using upstream tone {tone}")
            models.append(
                {
                    "slug": slug,
                    "title": item["title"],
                    "description": description,
                    "max_tokens": 32768,
                    "tags": ["web-search"],
                    "enabled_tools": ["search"],
                    "product_features": {},
                    "upstream_id": tone,
                    "catalog_section": item["section"],
                }
            )
        self._models_cache = models
        self._models_cache_time = time.time()
        self._model_tones = tones
        self.default_model = _slug_for_tone(default_tone) if default_tone else "auto"
        self.model_catalog_source = "authenticated_chat_shell"
        return True

    def _refresh_models_sync(self) -> None:
        if not self.session:
            raise ProviderUpstreamError("Microsoft 365 provider is not initialized")
        response = self.session.get(WEB_URL, allow_redirects=True, timeout=30)
        try:
            if response.status_code != 200:
                raise ProviderUpstreamError(f"Microsoft 365 model discovery returned HTTP {response.status_code}")
            if not self._apply_model_catalog(response.text):
                raise ProviderUpstreamError("Microsoft 365 chat shell did not contain model selector metadata")
        finally:
            response.close()

    async def fetch_models(self, *, refresh: bool = False) -> list[Dict[str, Any]]:
        cache_fresh = self._models_cache and time.time() - self._models_cache_time < MODEL_CATALOG_CACHE_SECONDS
        if refresh or not cache_fresh:
            try:
                await asyncio.to_thread(self._refresh_models_sync)
            except ProviderUpstreamError:
                if not self._models_cache:
                    raise
                logger.warning("[Microsoft 365] Model refresh failed; using the last known catalog")
        return [dict(model) for model in self._models_cache or self._fallback_models()]

    @staticmethod
    def _client_info(session_id: str) -> dict[str, str]:
        return {
            "clientPlatform": "mcmcopilot-web",
            "clientAppName": "Office",
            "clientEntrypoint": "mcmcopilot-officeweb",
            "clientSessionId": session_id,
            "ProductCategory": "Chat",
            "clientAppType": "Web",
            "productEntryPoint": "ChatPanel",
            "deviceOS": "Windows",
            "deviceType": "Desktop",
            "clientPlatformVersion": "10",
        }

    def _endpoint(self, session_id: str, conversation_id: str, request_id: str) -> str:
        query = {
            "chatsessionid": request_id,
            "XRoutingParameterSessionKey": request_id,
            "clientrequestid": request_id,
            "X-SessionId": session_id,
            "ConversationId": conversation_id,
            "access_token": self.access_token,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "licenseType": "Starter",
            "isEdu": "false",
            "agent": "web",
            "scenario": "OfficeWebPaidConsumerCopilot",
        }
        if self.variants:
            query["variants"] = self.variants
        return f"{CHAT_HUB}/{self.identity}?{urllib.parse.urlencode(query)}"

    @staticmethod
    def _request_payload(
        prompt: str,
        session_id: str,
        request_id: str,
        trace_id: str,
        model: str = "auto",
        tone: str | None = None,
    ) -> dict[str, Any]:
        if tone is None:
            try:
                tone = MODEL_TONES[model]
            except KeyError as exc:
                raise ProviderNotFoundError(f"Unknown Microsoft 365 Copilot model '{model}'") from exc
        client_info = Microsoft365CopilotProvider._client_info(session_id)
        return {
            "source": "officeweb",
            "clientCorrelationId": request_id,
            "sessionId": session_id,
            "optionsSets": OPTIONS_SETS,
            "streamingMode": "ConciseWithPadding",
            "options": {},
            "extraExtensionParameters": {},
            "allowedMessageTypes": ALLOWED_MESSAGE_TYPES,
            "sliceIds": [],
            "threadLevelGptId": {},
            "traceId": trace_id,
            "isStartOfSession": False,
            "clientInfo": client_info,
            "message": {
                "author": "user",
                "inputMethod": "Keyboard",
                "text": prompt,
                "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
                "requestId": request_id,
                "locationInfo": {
                    "timeZoneOffset": 5.5,
                    "timeZone": "Asia/Calcutta",
                },
                "locale": "en-gb",
                "messageType": "Chat",
                "experienceType": "Default",
                "adaptiveCards": [],
                "clientPreferences": {},
                "clientInfo": client_info,
            },
            "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            "isSbsSupported": True,
            "tone": tone,
            "renderReferencesBehindEOS": True,
            "disconnectBehavior": "continue",
        }

    @staticmethod
    def _json_frames(payload: bytes) -> Iterable[dict[str, Any]]:
        text = payload.decode("utf-8", errors="replace")
        for item in text.split(RECORD_SEPARATOR):
            if not item:
                continue
            try:
                value = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value

    def _generate_sync(self, prompt: str, model: str = "auto") -> str:
        if not self.generation_ready:
            raise ProviderNotConfiguredError(
                "Microsoft 365 cookies are valid, but Copilot generation requires a "
                "short-lived bearer token. Refresh CODEX_AUTH_M365_AUTH_JSON."
            )
        if not self.session:
            raise ProviderUpstreamError("Microsoft 365 provider is not initialized")
        tone = self._model_tones.get(model)
        if not tone:
            available = ", ".join(sorted(self._model_tones))
            raise ProviderNotFoundError(f"Unknown Microsoft 365 Copilot model '{model}'. Available models: {available}")
        if (
            self.refresh_configured
            and self.access_token_expires_at
            and self.access_token_expires_at <= time.time() + 300
        ):
            self._refresh_access_token_sync()

        session_id = str(uuid.uuid4())
        conversation_id = str(uuid.uuid4())
        request_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        endpoint = self._endpoint(session_id, conversation_id, request_id)
        websocket = None
        try:
            websocket = self.session.ws_connect(
                endpoint,
                headers={
                    "Origin": "https://m365.cloud.microsoft",
                    "User-Agent": USER_AGENT,
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                timeout=30,
            )
            websocket.send(
                json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR,
                CurlWsFlag.TEXT,
            )
            handshake, _ = websocket.recv()
            if not any(True for _ in self._json_frames(handshake)):
                raise ProviderUpstreamError("Microsoft 365 SignalR handshake failed")
            websocket.send(json.dumps({"type": 6}) + RECORD_SEPARATOR, CurlWsFlag.TEXT)
            invocation = {
                "arguments": [
                    self._request_payload(
                        prompt,
                        session_id,
                        request_id,
                        trace_id,
                        model,
                        tone,
                    )
                ],
                "invocationId": "0",
                "target": "chat",
                "type": 4,
            }
            websocket.send(json.dumps(invocation) + RECORD_SEPARATOR, CurlWsFlag.TEXT)

            final_text = ""
            while True:
                payload, _ = websocket.recv()
                for frame in self._json_frames(payload):
                    if frame.get("type") == 3:
                        if not final_text:
                            raise ProviderUpstreamError("Microsoft 365 completed without assistant text")
                        return final_text
                    if frame.get("target") != "update":
                        continue
                    for argument in frame.get("arguments") or []:
                        for message in argument.get("messages") or []:
                            if message.get("author") != "bot":
                                continue
                            text = message.get("text")
                            if isinstance(text, str) and text:
                                final_text = text
        except ProviderUpstreamError:
            raise
        except Exception as exc:
            raise ProviderUpstreamError(f"Microsoft 365 chat transport failed: {type(exc).__name__}") from exc
        finally:
            if websocket:
                websocket.close()

    @staticmethod
    def _chunks(text: str, size: int = 512) -> Iterable[str]:
        for offset in range(0, len(text), size):
            yield text[offset : offset + size]

    async def generate_stream(
        self,
        prompt: str,
        files: list | None = None,
        web_search: bool = False,
        model: str | None = None,
        realtime: bool = False,
    ) -> AsyncGenerator[str, None]:
        if files:
            raise ProviderNotConfiguredError("Microsoft 365 file inputs are not implemented by this adapter")
        try:
            await asyncio.wait_for(self.admission.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise ProviderBusyError("Microsoft 365 provider is busy") from exc
        try:
            async with self.lock:
                response = await asyncio.to_thread(
                    self._generate_sync,
                    prompt,
                    model or "auto",
                )
            for chunk in self._chunks(response):
                yield chunk
        finally:
            self.admission.release()
