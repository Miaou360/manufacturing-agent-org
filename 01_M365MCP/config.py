import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from 01_M365MCP directory if it exists
CURRENT_DIR = Path(__file__).resolve().parent
ENV_PATH = CURRENT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# Azure Entra ID / Microsoft Graph API Configuration
# Default Client ID uses Microsoft's standard public client ID for Entra ID
DEFAULT_PUBLIC_CLIENT_ID = "00000000-0000-0000-0000-000000000000"
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"

AZURE_CLIENT_ID = os.getenv("M365_CLIENT_ID", DEFAULT_PUBLIC_CLIENT_ID)
AZURE_TENANT_ID = os.getenv("M365_TENANT_ID", DEFAULT_TENANT_ID)
AZURE_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
AZURE_AUTH_MODE = os.getenv("M365_AUTH_MODE", "device_code")  # 'device_code', 'interactive', 'client_credentials'

# Default Graph Scopes for User Delegated Flow
DEFAULT_GRAPH_SCOPES = [
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Chat.ReadWrite",
    "ChatMessage.Send",
    "ChannelMessage.Send",
    "Files.ReadWrite.All",
    "Sites.Read.All"
]

# Local OneDrive Paths
LOCAL_ONEDRIVE_PATH = os.getenv("LOCAL_ONEDRIVE_PATH", "")
if not LOCAL_ONEDRIVE_PATH:
    candidate = os.environ.get("OneDrive") or os.environ.get("OneDriveCommercial") or os.environ.get("OneDriveConsumer")
    if candidate and os.path.exists(candidate):
        LOCAL_ONEDRIVE_PATH = candidate
    else:
        user_home = Path.home()
        fallback = user_home / "OneDrive"
        if fallback.exists():
            LOCAL_ONEDRIVE_PATH = str(fallback)

TOKEN_CACHE_FILE = CURRENT_DIR / ".token_cache.bin"
