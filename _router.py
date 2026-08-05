"""Model Router — maps message types to model configurations with fallback."""
import os, logging, httpx

logger = logging.getLogger("dududa20.router")

class Route:
    """A single model route."""
    def __init__(self, model: str, provider: str, base_url: str = "", api_key: str = ""):
        self.model = model
        self.provider = provider   # "deepseek" | "openai"
        self.base_url = base_url
        self.api_key = api_key


class ModelRouter:
    """Routes messages to the right model with fallback support."""

    def __init__(self):
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        openai_key   = os.environ.get("OPENAI_API_KEY", deepseek_key)
        openai_base  = os.environ.get("OPENAI_BASE_URL", "https://www.mhcoding.xyz")

        self.routes = {
            "text": [
                Route("deepseek-chat", "deepseek",
                      base_url="https://api.deepseek.com/v1", api_key=deepseek_key),
            ],
            "file": [
                Route("deepseek-chat", "deepseek",
                      base_url="https://api.deepseek.com/v1", api_key=deepseek_key),
            ],
            "image": [
                Route(os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001"),
                      "openai", base_url=openai_base, api_key=openai_key),
                Route("gemini-3.1-flash-image-preview",
                      "openai", base_url=openai_base, api_key=openai_key),
            ],
        }

    def resolve(self, msg_type: str) -> list[Route]:
        """Return ordered list of routes for a message type."""
        return self.routes.get(msg_type, self.routes.get("text", []))


# Singleton
router = ModelRouter()
