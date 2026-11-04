import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
import msal
from config import (
    AZURE_CLIENT_ID,
    AZURE_TENANT_ID,
    AZURE_CLIENT_SECRET,
    AZURE_AUTH_MODE,
    DEFAULT_GRAPH_SCOPES,
    TOKEN_CACHE_FILE
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

class MicrosoftGraphClient:
    def __init__(self):
        self.client_id = AZURE_CLIENT_ID
        self.tenant_id = AZURE_TENANT_ID
        self.client_secret = AZURE_CLIENT_SECRET
        self.auth_mode = AZURE_AUTH_MODE
        self.scopes = DEFAULT_GRAPH_SCOPES
        self.cache = msal.SerializableTokenCache()
        self._load_cache()

    def _load_cache(self):
        if TOKEN_CACHE_FILE.exists():
            try:
                with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                    self.cache.deserialize(f.read())
            except Exception as e:
                logger.warning(f"Failed to load token cache: {e}")

    def _save_cache(self):
        if self.cache.has_state_changed:
            try:
                with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                    f.write(self.cache.serialize())
            except Exception as e:
                logger.warning(f"Failed to save token cache: {e}")

    def _build_app(self):
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        if self.client_secret:
            return msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=authority,
                token_cache=self.cache
            )
        else:
            return msal.PublicClientApplication(
                client_id=self.client_id,
                authority=authority,
                token_cache=self.cache
            )

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_id.strip())

    def get_token(self) -> Optional[str]:
        """Obtain a valid access token from cache or silent refresh."""
        if not self.is_configured():
            return None
        
        app = self._build_app()
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(self.scopes, account=accounts[0])
            if result and "access_token" in result:
                self._save_cache()
                return result["access_token"]
        
        # If client secret is provided, try client credentials
        if self.client_secret:
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if result and "access_token" in result:
                self._save_cache()
                return result["access_token"]
                
        return None

    def start_device_code_flow(self) -> Dict[str, Any]:
        """Start device code login flow for user authentication."""
        if not self.is_configured():
            return {
                "success": False,
                "error": "M365_CLIENT_ID is not configured in .env."
            }
        
        app = self._build_app()
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            return {
                "success": False,
                "error": flow.get("error_description", "Failed to initiate device flow.")
            }
        
        return {
            "success": True,
            "message": flow["message"],
            "user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "flow": flow
        }

    def complete_device_code_flow(self, flow: Dict[str, Any]) -> Dict[str, Any]:
        """Complete the device code flow once user approves in browser."""
        app = self._build_app()
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self._save_cache()
            return {
                "success": True,
                "message": "Authentication successful!",
                "user": result.get("id_token_claims", {}).get("name", "User")
            }
        else:
            return {
                "success": False,
                "error": result.get("error_description", "Authentication failed or timed out.")
            }

    def interactive_login(self) -> Dict[str, Any]:
        """Perform interactive browser login (spawns local browser)."""
        if not self.is_configured():
            return {
                "success": False,
                "error": "M365_CLIENT_ID is not configured in .env."
            }
        app = self._build_app()
        result = app.acquire_token_interactive(scopes=self.scopes, port=8400)
        if "access_token" in result:
            self._save_cache()
            return {
                "success": True,
                "message": "Interactive login successful!",
                "user": result.get("id_token_claims", {}).get("name", "User")
            }
        return {
            "success": False,
            "error": result.get("error_description", "Interactive login failed.")
        }

    def _headers(self) -> Dict[str, str]:
        token = self.get_token()
        if not token:
            raise PermissionError("Not authenticated with Microsoft Graph API. Please configure .env or log in.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def get_status(self) -> Dict[str, Any]:
        """Get current Graph API configuration and login status."""
        configured = self.is_configured()
        token = self.get_token() if configured else None
        user_info = None
        if token:
            try:
                res = requests.get(f"{GRAPH_BASE_URL}/me", headers={"Authorization": f"Bearer {token}"}, timeout=5)
                if res.status_code == 200:
                    user_info = res.json()
            except Exception:
                pass

        return {
            "configured": configured,
            "client_id": self.client_id[:8] + "..." if self.client_id else "Not set",
            "tenant_id": self.tenant_id,
            "authenticated": bool(token and user_info),
            "user_display_name": user_info.get("displayName") if user_info else None,
            "user_principal_name": user_info.get("userPrincipalName") if user_info else None
        }

    # ---------------- Teams ----------------
    def list_chats(self, top: int = 20) -> List[Dict[str, Any]]:
        """List recent 1:1 and group chats in Microsoft Teams."""
        res = requests.get(f"{GRAPH_BASE_URL}/me/chats?$top={top}&$expand=members", headers=self._headers())
        res.raise_for_status()
        return res.json().get("value", [])

    def get_chat_messages(self, chat_id: str, top: int = 20) -> List[Dict[str, Any]]:
        """Get messages from a specific Teams chat."""
        res = requests.get(f"{GRAPH_BASE_URL}/me/chats/{chat_id}/messages?$top={top}", headers=self._headers())
        res.raise_for_status()
        return res.json().get("value", [])

    def send_chat_message(self, chat_id: str, content: str) -> Dict[str, Any]:
        """Send a message to a Teams chat."""
        body = {
            "body": {
                "contentType": "html",
                "content": content
            }
        }
        res = requests.post(f"{GRAPH_BASE_URL}/me/chats/{chat_id}/messages", headers=self._headers(), json=body)
        res.raise_for_status()
        return res.json()

    def list_joined_teams(self) -> List[Dict[str, Any]]:
        """List all Microsoft Teams the user is a member of."""
        res = requests.get(f"{GRAPH_BASE_URL}/me/joinedTeams", headers=self._headers())
        res.raise_for_status()
        return res.json().get("value", [])

    def list_team_channels(self, team_id: str) -> List[Dict[str, Any]]:
        """List channels in a specific Microsoft Team."""
        res = requests.get(f"{GRAPH_BASE_URL}/teams/{team_id}/channels", headers=self._headers())
        res.raise_for_status()
        return res.json().get("value", [])

    def send_channel_message(self, team_id: str, channel_id: str, content: str) -> Dict[str, Any]:
        """Send a message to a channel in Microsoft Teams."""
        body = {
            "body": {
                "contentType": "html",
                "content": content
            }
        }
        res = requests.post(f"{GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages", headers=self._headers(), json=body)
        res.raise_for_status()
        return res.json()

    # ---------------- Cloud OneDrive & SharePoint ----------------
    def search_cloud_drive(self, query: str) -> List[Dict[str, Any]]:
        """Search files in Cloud OneDrive."""
        res = requests.get(f"{GRAPH_BASE_URL}/me/drive/root/search(q='{query}')", headers=self._headers())
        res.raise_for_status()
        return res.json().get("value", [])

    def list_sharepoint_sites(self, query: str = "") -> List[Dict[str, Any]]:
        """Search or list SharePoint sites accessible by the user."""
        endpoint = f"{GRAPH_BASE_URL}/sites?search={query}" if query else f"{GRAPH_BASE_URL}/sites/root"
        res = requests.get(endpoint, headers=self._headers())
        res.raise_for_status()
        val = res.json()
        return val.get("value", [val])

    def upload_file_to_onedrive(self, local_path: str, remote_folder: str = "") -> Dict[str, Any]:
        """Upload a local file to Cloud OneDrive."""
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        
        filename = p.name
        folder_prefix = f"/{remote_folder.strip('/')}" if remote_folder.strip('/') else ""
        upload_url = f"{GRAPH_BASE_URL}/me/drive/root:{folder_prefix}/{filename}:/content"
        
        headers = self._headers()
        headers["Content-Type"] = "application/octet-stream"
        
        with open(p, "rb") as f:
            res = requests.put(upload_url, headers=headers, data=f)
        res.raise_for_status()
        return res.json()
