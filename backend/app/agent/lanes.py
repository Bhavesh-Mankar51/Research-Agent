from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Lane:
    key: str
    label: str
    question: str
    query_templates: tuple[str, ...]
    doc_path_hints: tuple[str, ...] = field(default_factory=tuple)


LANES: tuple[Lane, ...] = (
    Lane(
        key="identity",
        label="Identity & category",
        question=(
            "What is this product, who ships it, what category does it belong to, and "
            "where does its official developer documentation live? Find the canonical "
            "docs entry point — the URL an engineer would start from."
        ),
        query_templates=(
            "{app} developer documentation API reference",
            "{app} official website product overview",
        ),
        doc_path_hints=("/docs", "/developers", "/api"),
    ),
    Lane(
        key="auth_access",
        label="Auth & access tier",
        question=(
            "How does a developer authenticate against this product's public API "
            "(OAuth2, API key, bearer/personal access token, basic auth, JWT, HMAC "
            "signing, mTLS)? And can they obtain working credentials by themselves for "
            "free or on a trial, or does it require a paid plan, an admin's approval, "
            "acceptance into a partner program, or a sales conversation? Find the "
            "authentication docs and the page that states what plan API access needs."
        ),
        query_templates=(
            "{app} API authentication OAuth API key docs",
            "{app} API access pricing plan required developer account",
        ),
        doc_path_hints=("/docs/authentication", "/docs/auth", "/pricing"),
    ),
    Lane(
        key="api_mcp",
        label="API surface & MCP",
        question=(
            "What does the public API surface look like — REST, GraphQL, SOAP, gRPC, "
            "webhooks-only, SDK-only, or nothing public? Roughly how broad is it, and "
            "is it versioned and publicly documented? Separately: does a Model Context "
            "Protocol (MCP) server exist for this product, official or community, and "
            "where does it live?"
        ),
        query_templates=(
            "{app} REST API GraphQL endpoints reference",
            "{app} MCP server Model Context Protocol",
        ),
        doc_path_hints=("/docs/api", "/reference", "/graphql"),
    ),
)

LANES_BY_KEY = {lane.key: lane for lane in LANES}


def build_hints(lane: Lane, app: str, docs_urls: list[str]) -> list[str]:
    hints = [f"Known/likely docs URL: {u}" for u in docs_urls[:2]]
    hints += [tpl.format(app=app) for tpl in lane.query_templates]
    return hints
