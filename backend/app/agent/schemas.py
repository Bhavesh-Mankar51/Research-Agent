from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    CRM_AND_SALES = "crm_and_sales"
    SUPPORT_AND_HELPDESK = "support_and_helpdesk"
    COMMUNICATIONS = "communications"
    MARKETING_ADS_EMAIL_SOCIAL = "marketing_ads_email_social"
    ECOMMERCE = "ecommerce"
    DATA_SEO_SCRAPING = "data_seo_scraping"
    DEVELOPER_INFRA_DATA = "developer_infra_data"
    PRODUCTIVITY_AND_PM = "productivity_and_pm"
    FINANCE_AND_FINTECH = "finance_and_fintech"
    AI_RESEARCH_MEDIA = "ai_research_media"
    OTHER = "other"


class AuthMethod(StrEnum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    PERSONAL_ACCESS_TOKEN = "personal_access_token"
    BASIC_AUTH = "basic_auth"
    JWT = "jwt"
    HMAC_SIGNATURE = "hmac_signature"
    MTLS = "mtls"
    SAML_SSO = "saml_sso"
    NO_AUTH = "no_auth"
    UNKNOWN = "unknown"


class AccessTier(StrEnum):
    SELF_SERVE_FREE = "self_serve_free"
    SELF_SERVE_TRIAL = "self_serve_trial"
    PAID_PLAN_REQUIRED = "paid_plan_required"
    ADMIN_APPROVAL_REQUIRED = "admin_approval_required"
    PARTNER_GATED = "partner_gated"
    CONTACT_SALES = "contact_sales"
    UNKNOWN = "unknown"


class ApiStyle(StrEnum):
    REST = "rest"
    GRAPHQL = "graphql"
    SOAP = "soap"
    GRPC = "grpc"
    WEBHOOKS_ONLY = "webhooks_only"
    SDK_ONLY = "sdk_only"
    NO_PUBLIC_API = "no_public_api"
    UNKNOWN = "unknown"


class ApiBreadth(StrEnum):
    NARROW = "narrow"
    MODERATE = "moderate"
    BROAD = "broad"
    UNKNOWN = "unknown"


class McpStatus(StrEnum):
    OFFICIAL = "official"
    COMMUNITY = "community"
    NONE = "none"
    UNKNOWN = "unknown"


class Verdict(StrEnum):
    BUILDABLE_TODAY = "buildable_today"
    BUILDABLE_WITH_FRICTION = "buildable_with_friction"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Evidence(BaseModel):
    url: str = Field(description="Exact URL the claim came from.")
    title: str | None = Field(default=None, description="Page or document title.")
    quote: str = Field(
        max_length=400,
        description="Verbatim snippet from the source that supports the claim.",
    )


class ResolvedApp(BaseModel):
    canonical_name: str = Field(description="Official product name, e.g. 'Zoho CRM'.")
    vendor: str | None = Field(default=None, description="Company that ships the product.")
    homepage: str | None = Field(default=None, description="Best-guess marketing site URL.")
    likely_docs_urls: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Best-guess developer documentation URLs, most likely first.",
    )
    category: Category = Field(description="Category the product belongs to.")
    one_liner: str = Field(max_length=160, description="What the product does, in one line.")
    ambiguous: bool = Field(
        default=False,
        description="True if the name plausibly refers to more than one product.",
    )
    disambiguation_note: str | None = Field(
        default=None, description="If ambiguous, which product was assumed and why."
    )


class Finding(BaseModel):
    claim: str = Field(max_length=400, description="A single factual statement.")
    evidence: Evidence
    confidence: Confidence


class LaneFindings(BaseModel):
    findings: list[Finding] = Field(
        default_factory=list,
        max_length=8,
        description="Evidence-backed claims relevant to this lane's question only.",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Questions this lane could not answer from the sources it saw.",
    )


class DimensionConfidence(BaseModel):
    auth: Confidence
    access: Confidence
    api: Confidence
    mcp: Confidence
    verdict: Confidence


class AppResearchReport(BaseModel):
    canonical_name: str
    vendor: str | None = None
    homepage: str | None = None
    docs_url: str | None = Field(default=None, description="Primary developer docs entry point.")

    category: Category
    one_liner: str = Field(max_length=160)

    auth_methods: list[AuthMethod] = Field(
        default_factory=list, description="Every auth scheme the public API accepts."
    )
    auth_notes: str = Field(
        max_length=600, description="How auth actually works, including scopes or app-review steps."
    )

    access_tier: AccessTier
    access_notes: str = Field(
        max_length=600,
        description="What a developer must do to obtain working credentials, and any cost.",
    )

    api_styles: list[ApiStyle] = Field(default_factory=list)
    api_breadth: ApiBreadth
    api_notes: str = Field(
        max_length=600, description="Shape of the API surface: resource families, versioning, docs."
    )

    mcp_status: McpStatus
    mcp_url: str | None = Field(default=None, description="MCP server URL or repo, if one exists.")

    verdict: Verdict
    blocker: str | None = Field(
        default=None,
        max_length=400,
        description="The single main obstacle to shipping a toolkit today. Null if none.",
    )
    integration_notes: str = Field(
        max_length=800,
        description="What building an agent toolkit for this app would actually involve.",
    )

    evidence: list[Evidence] = Field(
        default_factory=list,
        max_length=10,
        description="Sources backing this report. Prefer vendor docs over blogs.",
    )
    confidence: DimensionConfidence
    unknowns: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="What could not be established. Say so plainly rather than guessing.",
    )
    human_review_needed: bool = Field(
        description="True when a person should check this before it is trusted."
    )
    human_review_reason: str | None = Field(default=None, max_length=300)


class FieldIssue(BaseModel):
    field: str = Field(description="Report field the issue applies to, e.g. 'access_tier'.")
    issue: str = Field(max_length=300, description="What is wrong or unsupported.")
    severity: Confidence = Field(
        description="high = claim is unsupported or contradicted; low = cosmetic."
    )


class VerificationResult(BaseModel):
    passed: bool = Field(description="True if every material claim is supported by its evidence.")
    issues: list[FieldIssue] = Field(default_factory=list, max_length=6)
    followup_queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Targeted searches that would resolve the issues found.",
    )
    summary: str = Field(max_length=400, description="One-paragraph verification verdict.")
