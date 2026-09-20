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
        deepseek_base = os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

        self.routes = {
            "text": [
                Route(model, "deepseek",
                      base_url=deepseek_base, api_key=deepseek_key),
            ],
            "file": [
                Route(model, "deepseek",
                      base_url=deepseek_base, api_key=deepseek_key),
            ],
            "image": [
                Route(os.environ.get("VISION_MODEL", model), "deepseek",
                      base_url=deepseek_base, api_key=deepseek_key),
            ],
        }

    def resolve(self, msg_type: str) -> list[Route]:
        """Return ordered list of routes for a message type."""
        return self.routes.get(msg_type, self.routes.get("text", []))


# Singleton
router = ModelRouter()
