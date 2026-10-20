from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


APP_DIR = Path(__file__).resolve().parents[1]   # apps/api
REPO_ROOT = Path(__file__).resolve().parents[3]  # monorepo root (used for a shared local .env)


class Settings(BaseSettings):
    app_name: str = "OpenLivery API"
    database_url: str = "postgresql+psycopg://openlivery:openlivery@localhost:5432/openlivery"
    secret_key: str = "dev-local-change-this-key-please"
    encryption_key: str = "dev-local-change-this-key-too"
    frontend_url: str = "http://localhost:3000"
    access_token_minutes: int = 60 * 24 * 7
    # Session cookie flags. Defaults suit local HTTP; set cookie_secure=true (and
    # cookie_samesite=none when the frontend and API are on different sites)
    # behind HTTPS in production.
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    # Rate limiting on public/unauthenticated endpoints (per client IP). Disable
    # only for tests or when a proxy in front already enforces limits.
    rate_limit_enabled: bool = True
    # SSRF guard for agent HTTP tools: URLs resolving to private/loopback
    # addresses are rejected. Enable only on self-hosted deployments that need
    # tools to reach internal services.
    tools_allow_private_urls: bool = False
    storage_dir: Path = APP_DIR / "storage"
    backend_url: str = "http://localhost:8000"
    whatsapp_bridge_url: str = "http://localhost:3101"
    whatsapp_bridge_token: str = "dev-local-change-this-bridge-token"
    # Meta Graph API root used by the WhatsApp Cloud API channel; override to
    # point at a mock server in tests.
    meta_graph_base_url: str = "https://graph.facebook.com/v23.0"
    # The Meta app the WhatsApp Cloud API tokens belong to. Only needed to
    # upload the sample file of a template header: Meta files uploads under
    # the app, not the business account.
    whatsapp_app_id: str = ""
    # Speech-to-text models offered for the audio capability. OpenRouter serves
    # them through its audio endpoint but lists them nowhere its API exposes,
    # so the offer is declared here; every other model is read live.
    transcription_models: str = "openai/gpt-4o-mini-transcribe,openai/gpt-4o-transcribe,openai/gpt-transcribe"
    # Models a new agent starts with for audio and for its knowledge base. An
    # install that uses vendor keys directly points these at models those
    # vendors serve (e.g. google/gemini-embedding-001).
    default_audio_model: str = "openai/gpt-4o-mini-transcribe"
    default_embedding_model: str = "openai/text-embedding-3-small"
    # Models to fall over to, in order, when a direct vendor fails a call for a
    # passing reason (overloaded, rate limited, timed out, model withdrawn).
    # Only models of the failing model's vendor are tried, with the same key.
    # Transcription falls over along TRANSCRIPTION_MODELS the same way.
    model_fallbacks: str = ""
    # Seconds one attempt may take while a fallback is still left to try; the
    # last attempt keeps the usual timeout.
    model_attempt_timeout: float = 25.0
    # Reasoning effort sent to Gemini (minimal, low, medium, high); empty
    # leaves the model's default.
    gemini_reasoning_effort: str = "minimal"
    # Thinking mode sent to Claude ("between_tools" keeps current models from
    # thinking before they answer); empty leaves the model's default.
    anthropic_thinking: str = "between_tools"
    # WhatsApp replies keep "typing..." on for as long as a person would take
    # to type them, at this many characters per second (0 turns it off),
    # never longer than TYPING_MAX_SECONDS. Counts on top of the reply delay.
    typing_chars_per_second: float = 0
    typing_max_seconds: float = 20
    # Debounced WhatsApp replies go out as up to this many messages, one per
    # paragraph, each after its own "typing..."; 1 sends the reply whole.
    reply_max_messages: int = 1
    # Official messaging APIs. App credentials remain on the server.
    social_graph_version: str = "v25.0"
    social_worker_enabled: bool = True
    social_worker_interval_seconds: float = 2.0
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_webhook_verify_token: str = ""
    instagram_human_agent_enabled: bool = False
    messenger_app_id: str = ""
    messenger_app_secret: str = ""
    messenger_webhook_verify_token: str = ""
    messenger_human_agent_enabled: bool = False
    messenger_login_config_id: str = ""
    # Public HTTPS origin; defaults to frontend_url when left empty.
    social_public_url: str = ""
    social_oauth_state_minutes: int = 10

    # Conversations the AI is answering resolve themselves after this many
    # hours without a message from either side. Conversations a person took
    # over are never closed automatically: only that person decides. 0 disables.
    auto_resolve_after_hours: float = 24.0
    # How often due inactivity follow-ups are sent, in seconds. They are an
    # agent setting; this only paces the timer that sends them. 0 disables it.
    follow_up_sweep_seconds: float = 60.0

    # Push notifications for the mobile app. "none" (the default) sends nothing
    # and needs no account with anyone; "webhook" POSTs each event to
    # push_webhook_url so you can route it through whatever you already use.
    # Deployments may register further providers at startup — see
    # app/services/notifications.py and docs/push-notifications.md.
    push_provider: str = "none"
    push_webhook_url: str = ""
    push_webhook_secret: str = ""
    # Web push for the portal needs no provider, only a VAPID key pair the
    # deployment owns: `python -m app.webpush_keys` prints one. Empty leaves
    # browsers out; the mobile provider above is independent of it. The
    # subject is who the browser vendors may contact about this sender.
    push_webpush_private_key: str = ""
    push_webpush_subject: str = ""

    # Where the portal's people can get a mobile app for it, if the deployment
    # publishes one: the store pages, shown as the stores' own badges on the
    # portal's sign-in page and under Preferences. Empty shows nothing.
    portal_ios_app_url: str = ""
    portal_android_app_url: str = ""
    # What a client's own portal domain must point at on this deployment: the
    # host its gateway answers on, e.g. "app.youragency.com". The client page
    # shows it as the value of the CNAME record; empty shows a placeholder and
    # the note that the multi-domain gateway has to be enabled first.
    portal_domain_cname_target: str = ""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", APP_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
