"""Run yt-dlp with public-network validation for every HTTP request.

yt-dlp follows extractor, manifest, and CDN redirects inside its own process,
outside the aiohttp checks used by the bot.  This entry point keeps yt-dlp's
normal CLI intact while validating each request and manually following
redirects so a supported source cannot turn into an SSRF primitive.
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .network import is_public_address, validate_public_url_sync

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


def _configured_pot_provider() -> tuple[str, str, int] | None:
    """Return the exact host/port allowed for the local PO-token provider.

    The provider is an operator-managed sidecar, so it intentionally runs on
    a private Docker address.  All other yt-dlp requests remain restricted to
    public addresses by the normal SSRF guard.
    """

    raw_url = os.environ.get("FISHIE_YOUTUBE_POT_PROVIDER_URL", "").strip()
    if not raw_url:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return (
        parsed.scheme.casefold(),
        hostname,
        port or (443 if parsed.scheme.casefold() == "https" else 80),
    )


_POT_PROVIDER = _configured_pot_provider()


def _is_pot_provider_url(url: str) -> bool:
    if _POT_PROVIDER is None:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme.casefold(),
        hostname,
        port or (443 if parsed.scheme.casefold() == "https" else 80),
    ) == _POT_PROVIDER


def _is_pot_provider_host(hostname: object, port: object = None) -> bool:
    if _POT_PROVIDER is None or not isinstance(hostname, str):
        return False
    if hostname.casefold().rstrip(".") != _POT_PROVIDER[1]:
        return False
    return port is None or str(port) == str(_POT_PROVIDER[2])


def _guard_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as error:
        raise urllib.error.URLError("Invalid URL") from error
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise urllib.error.URLError("Only public HTTP(S) URLs are allowed")
    if _is_pot_provider_url(url):
        return url
    try:
        return validate_public_url_sync(url)
    except ValueError as error:
        raise urllib.error.URLError(str(error)) from error


def _guard_getaddrinfo(original: Callable[..., Any]) -> Callable[..., Any]:
    """Reject private DNS answers used by Python HTTP handlers."""

    def guarded(*args: Any, **kwargs: Any) -> Any:
        hostname = args[0] if args else kwargs.get("host")
        port = args[1] if len(args) > 1 else kwargs.get("port")
        if _is_pot_provider_host(hostname, port):
            return original(*args, **kwargs)
        results = original(*args, **kwargs)
        addresses = {str(item[4][0]) for item in results if len(item) > 4}
        if any(not is_public_address(address) for address in addresses):
            raise socket.gaierror("private and local network addresses are not allowed")
        return results

    return guarded


def _guard_session_request(original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a requests-compatible session and validate redirects manually."""

    def guarded(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        allow_redirects = kwargs.get("allow_redirects", True)
        if allow_redirects is False:
            _guard_url(str(url))
            return original(self, method, url, *args, **kwargs)

        kwargs["allow_redirects"] = False
        current = str(url)
        response = None
        for _ in range(_MAX_REDIRECTS + 1):
            _guard_url(current)
            response = original(self, method, current, *args, **kwargs)
            if getattr(response, "status_code", 0) not in _REDIRECT_STATUSES:
                return response
            location = getattr(response, "headers", {}).get("Location")
            if not location:
                return response
            try:
                next_url = urllib.parse.urljoin(current, location)
                _guard_url(next_url)
            except (TypeError, ValueError, urllib.error.URLError):
                response.close()
                raise
            response.close()
            current = next_url

        # Return the final response rather than following an unbounded chain.
        # yt-dlp will report the non-2xx response normally.
        return response

    return guarded


def _install_guards() -> None:
    if getattr(_install_guards, "installed", False):
        return
    _install_guards.installed = True  # type: ignore[attr-defined]

    socket.getaddrinfo = _guard_getaddrinfo(socket.getaddrinfo)  # type: ignore[assignment]

    try:
        from yt_dlp import YoutubeDL

        original_urlopen = YoutubeDL.urlopen

        def guarded_urlopen(self: Any, request: Any) -> Any:
            url = request if isinstance(request, str) else getattr(request, "url", "")
            if url:
                _guard_url(str(url))
            return original_urlopen(self, request)

        YoutubeDL.urlopen = guarded_urlopen  # type: ignore[method-assign]

        from yt_dlp.networking._urllib import RedirectHandler

        original_redirect = RedirectHandler.redirect_request

        def guarded_redirect(
            self: Any,
            request: Any,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> Any:
            _guard_url(str(newurl))
            return original_redirect(self, request, fp, code, msg, headers, newurl)

        RedirectHandler.redirect_request = guarded_redirect  # type: ignore[method-assign]
    except ImportError:
        return

    try:
        import requests

        requests.sessions.Session.request = _guard_session_request(  # type: ignore[method-assign]
            requests.sessions.Session.request
        )
    except ImportError:
        pass

    try:
        from curl_cffi import requests as curl_requests

        curl_requests.Session.request = _guard_session_request(  # type: ignore[method-assign]
            curl_requests.Session.request
        )
    except ImportError:
        pass


def main() -> None:
    _install_guards()
    from yt_dlp import main as yt_dlp_main

    yt_dlp_main()


if __name__ == "__main__":
    main()
