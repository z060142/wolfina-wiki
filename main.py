"""Entry point that reads SERVER_HOST / SERVER_PORT from settings."""
import uvicorn
from core.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "api.app:app",
        host=settings.server_host,
        port=settings.server_port,
        reload=False,
    )
