from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\"'<>\\)\]}]+")

_USEFUL_SLUG_FRAGMENTS = (
    "SEARCH",
    "SCRAPE",
    "EXTRACT",
    "GET_CONTENTS",
    "ANSWER",
    "CRAWL_URL",
)

_IRRELEVANT_SLUG_FRAGMENTS = (
    "AMAZON",
    "FLIGHTS",
    "HOTELS",
    "TRIP_ADVISOR",
    "SHOPPING",
    "GOOGLE_MAPS",
    "NPPESNPI",
    "IMAGE",
    "EVENT",
    "FINANCE",
    "TRENDS",
    "GROQ_CHAT",
    "SEC_FILINGS",
)


def _toolkit_is_reachable(client: Any, toolkit: str, connected: set[str]) -> tuple[bool, str]:
    if toolkit in connected:
        return True, ""
    try:
        meta = client.toolkits.get(slug=toolkit.lower())
    except Exception as exc:
        logger.warning("Could not check reachability of %s: %s", toolkit, exc)
        return True, ""

    modes = {
        str(getattr(scheme, "mode", "")).upper()
        for scheme in (getattr(meta, "auth_config_details", None) or [])
    }
    if not modes or modes <= {"NO_AUTH"}:
        return True, ""
    return False, (
        f"requires {'/'.join(sorted(modes))} but no connected account exists for "
        f"user; connect it in the Composio dashboard"
    )


def _connected_toolkits(client: Any) -> set[str]:
    try:
        listing = client.connected_accounts.list()
    except Exception as exc:
        logger.warning("Could not list connected accounts: %s", exc)
        return set()
    items = getattr(listing, "items", None) or []
    slugs: set[str] = set()
    for account in items:
        status = str(getattr(account, "status", "")).upper()
        if status and status not in {"ACTIVE", "INITIATED"}:
            continue
        toolkit = getattr(account, "toolkit", None)
        slug = getattr(toolkit, "slug", None) or toolkit
        if slug:
            slugs.add(str(slug).upper())
    return slugs


class ToolProvider(Protocol):
    @property
    def tools(self) -> list[Any]: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass
class ComposioToolProvider:
    user_id: str
    toolkits: list[str]
    _tools: list[Any] | None = field(default=None, init=False, repr=False)
    _resolved: list[str] = field(default_factory=list, init=False)
    _skipped: dict[str, str] = field(default_factory=dict, init=False)

    @classmethod
    def from_settings(cls) -> ComposioToolProvider:
        s = get_settings()
        return cls(user_id=s.composio_user_id, toolkits=s.toolkit_list)

    @property
    def tools(self) -> list[Any]:
        if self._tools is None:
            self._tools = self._discover()
        return self._tools

    def _discover(self) -> list[Any]:
        try:
            from composio import Composio
            from composio_langchain import LangchainProvider
        except ImportError as exc:
            raise RuntimeError(
                "composio and composio-langchain must be installed to run research. "
                "pip install 'composio' 'composio-langchain'"
            ) from exc

        settings = get_settings()
        if not settings.composio_api_key:
            raise RuntimeError(
                "COMPOSIO_API_KEY is not set; the agent has no way to reach the web."
            )

        client = Composio(provider=LangchainProvider(), api_key=settings.composio_api_key)

        connected = _connected_toolkits(client)

        collected: list[Any] = []
        seen: set[str] = set()
        for toolkit in self.toolkits:
            reachable, reason = _toolkit_is_reachable(client, toolkit, connected)
            if not reachable:
                self._skipped[toolkit] = reason
                logger.warning("Composio toolkit %s skipped: %s", toolkit, reason)
                continue
            try:
                found = client.tools.get(user_id=self.user_id, toolkits=[toolkit])
            except Exception as exc:
                self._skipped[toolkit] = str(exc)[:200]
                logger.warning("Composio toolkit %s unavailable: %s", toolkit, exc)
                continue

            for tool in found or []:
                name = getattr(tool, "name", "")
                if not name or name in seen:
                    continue
                upper = name.upper()
                if not any(fragment in upper for fragment in _USEFUL_SLUG_FRAGMENTS):
                    continue
                if any(fragment in upper for fragment in _IRRELEVANT_SLUG_FRAGMENTS):
                    continue
                seen.add(name)
                collected.append(tool)
                self._resolved.append(name)

        if not collected:
            raise RuntimeError(
                "No usable Composio search/scrape tools were found across toolkits "
                f"{self.toolkits}. Connect one in the Composio dashboard, or adjust "
                "COMPOSIO_TOOLKITS."
            )
        logger.info("Composio tools resolved: %s", self._resolved)
        return collected

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "composio",
            "user_id": self.user_id,
            "toolkits_requested": self.toolkits,
            "tools_resolved": list(self._resolved),
            "toolkits_skipped": dict(self._skipped),
        }


@dataclass
class ToolResult:
    tool: str
    args: dict[str, Any]
    content: str
    urls: list[str]
    truncated: bool
    cached: bool = False
    error: str | None = None


class EvidencePool:
    def __init__(self, max_chars: int | None = None) -> None:
        self.max_chars = max_chars or get_settings().max_source_chars
        self._cache: dict[str, ToolResult] = {}
        self.seen_urls: set[str] = set()
        self.call_log: list[dict[str, Any]] = []

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> str:
        blob = json.dumps(args, sort_keys=True, default=str)
        return f"{tool}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"

    async def run(self, tool: Any, args: dict[str, Any]) -> ToolResult:
        name = getattr(tool, "name", "unknown_tool")
        key = self._key(name, args)
        if key in self._cache:
            hit = self._cache[key]
            self.call_log.append({"tool": name, "args": args, "cached": True})
            return ToolResult(**{**hit.__dict__, "cached": True})

        try:
            raw = await tool.ainvoke(args)
            error = None
        except Exception as exc:
            logger.warning("Tool %s failed: %s", name, exc)
            raw, error = f"Tool call failed: {exc}", str(exc)[:300]

        text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        urls = list(dict.fromkeys(_URL_RE.findall(text)))
        self.seen_urls.update(urls)

        truncated = len(text) > self.max_chars
        result = ToolResult(
            tool=name,
            args=args,
            content=text[: self.max_chars],
            urls=urls,
            truncated=truncated,
            error=error,
        )
        self._cache[key] = result
        self.call_log.append(
            {
                "tool": name,
                "args": args,
                "cached": False,
                "chars": len(text),
                "truncated": truncated,
                "urls": len(urls),
                "error": error,
            }
        )
        return result

    def unknown_urls(self, urls: list[str]) -> list[str]:
        return [u for u in urls if _normalise(u) not in {_normalise(s) for s in self.seen_urls}]

    def stats(self) -> dict[str, Any]:
        return {
            "tool_calls": len(self.call_log),
            "cache_hits": sum(1 for c in self.call_log if c.get("cached")),
            "distinct_urls_seen": len(self.seen_urls),
        }


def _normalise(url: str) -> str:
    return url.rstrip("/.,);").lower()
