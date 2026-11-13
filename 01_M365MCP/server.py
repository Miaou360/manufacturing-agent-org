import sys
import json
from typing import Optional, List, Dict, Any
from mcp.server.mcpserver import MCPServer
from local_office import LocalOutlookClient, LocalOneDriveClient, LocalExcelClient, LocalPowerPointClient
from graph_client import MicrosoftGraphClient

server = MCPServer("m365-hybrid-mcp")

outlook_client = LocalOutlookClient()
onedrive_client = LocalOneDriveClient()
excel_client = LocalExcelClient()
ppt_client = LocalPowerPointClient()
graph_client = MicrosoftGraphClient()

# ----------------- Status & Diagnostics -----------------

@server.tool(name="m365_get_status", description="Check connection status of local Outlook, local OneDrive, PowerPoint, and cloud Microsoft Graph API.")
def m365_get_status() -> str:
    """Return comprehensive connection status of M365 services."""
    ol_stat = outlook_client.get_status()
    od_stat = onedrive_client.get_status()
    ppt_stat = ppt_client.get_status()
    g_stat = graph_client.get_status()
    
    status = {
        "local_outlook": ol_stat,
        "local_onedrive": od_stat,
        "local_powerpoint": ppt_stat,
        "cloud_graph_api": g_stat,
        "mode": "hybrid",
        "ready_features": [
            "Outlook Email (Read/Search/Draft/Send)",
            "Outlook Calendar (View/Create Events)",
            "OneDrive Local File Search & List",
            "Excel Read/Write Automation",
            "PowerPoint Read/Create Automation"
        ]
    }
    if g_stat.get("authenticated"):
        status["ready_features"].extend([
            "Teams Chats & Channel Messages",
            "Cloud OneDrive / SharePoint Search & Upload"
        ])
    return json.dumps(status, ensure_ascii=False, indent=2)

# ----------------- Outlook Email Tools -----------------

@server.tool(name="outlook_get_unread_emails", description="Retrieve recent unread emails from Outlook inbox.")
def outlook_get_unread_emails(max_count: int = 10) -> str:
    """Retrieve unread emails."""
    try:
        results = outlook_client.get_unread_emails(max_count=max_count)
        return json.dumps({"count": len(results), "emails": results}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="outlook_search_emails", description="Search emails in Outlook by subject, sender, or content keyword.")
def outlook_search_emails(query: str, max_count: int = 10) -> str:
    """Search emails."""
    try:
        results = outlook_client.search_emails(query=query, max_count=max_count)
        return json.dumps({"query": query, "count": len(results), "emails": results}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="outlook_read_email", description="Read the full body text and attachment list of an email by entry_id.")
def outlook_read_email(entry_id: str) -> str:
    """Read email details."""
    try:
        details = outlook_client.read_email_details(entry_id)
        return json.dumps(details, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="outlook_create_draft", description="Create a draft email in Outlook without sending it immediately.")
def outlook_create_draft(to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> str:
    """Create email draft."""
    try:
        res = outlook_client.create_email_draft(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="outlook_send_email", description="Send an email via Outlook, or display it for review before sending.")
def outlook_send_email(to: str, subject: str, body: str, cc: str = "", bcc: str = "", display_first: bool = False) -> str:
    """Send email."""
    try:
        res = outlook_client.send_email(to=to, subject=subject, body=body, cc=cc, bcc=bcc, display_first=display_first)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ----------------- Outlook Calendar Tools -----------------

@server.tool(name="outlook_get_calendar_events", description="Retrieve upcoming calendar appointments and meetings from Outlook.")
def outlook_get_calendar_events(days_ahead: int = 7) -> str:
    """Get calendar events for next N days."""
    try:
        events = outlook_client.get_calendar_events(days_ahead=days_ahead)
        return json.dumps({"days_ahead": days_ahead, "count": len(events), "events": events}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="outlook_create_calendar_event", description="Create a new calendar meeting or event in Outlook.")
def outlook_create_calendar_event(subject: str, start_time: str, end_time: str, location: str = "", body: str = "", reminder_minutes: int = 15) -> str:
    """
    Create a new calendar appointment.
    start_time / end_time format: 'YYYY-MM-DD HH:MM' (e.g. '2026-10-25 14:00')
    """
    try:
        res = outlook_client.create_calendar_event(
            subject=subject,
            start_time=start_time,
            end_time=end_time,
            location=location,
            body=body,
            reminder_minutes=reminder_minutes
        )
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ----------------- Local OneDrive Tools -----------------

@server.tool(name="onedrive_list_files", description="List files and folders in local OneDrive directory.")
def onedrive_list_files(subpath: str = "", max_count: int = 50) -> str:
    """List OneDrive files."""
    try:
        files = onedrive_client.list_files(subpath=subpath, max_count=max_count)
        return json.dumps({"subpath": subpath, "count": len(files), "items": files}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="onedrive_search_files", description="Recursively search files in local OneDrive by keyword and optional extension.")
def onedrive_search_files(keyword: str, ext_filter: str = "", max_count: int = 30) -> str:
    """Search OneDrive files."""
    try:
        results = onedrive_client.search_files(keyword=keyword, ext_filter=ext_filter or None, max_count=max_count)
        return json.dumps({"keyword": keyword, "count": len(results), "matches": results}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ----------------- Excel Automation Tools -----------------

@server.tool(name="excel_list_sheets", description="List sheet names in an Excel file.")
def excel_list_sheets(file_path: str) -> str:
    """List sheets in Excel file."""
    try:
        sheets = excel_client.list_sheets(file_path)
        return json.dumps({"file_path": file_path, "sheets": sheets}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="excel_read_sheet", description="Read sheet data from an Excel workbook into JSON.")
def excel_read_sheet(file_path: str, sheet_name: str = "", max_rows: int = 50) -> str:
    """Read Excel sheet."""
    try:
        res = excel_client.read_sheet(file_path, sheet_name=sheet_name or None, max_rows=max_rows)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="excel_write_cell", description="Write a value to a cell (e.g. 'B2', 'D10') in an Excel file.")
def excel_write_cell(file_path: str, sheet_name: str, cell: str, value: str) -> str:
    """Write value to Excel cell."""
    try:
        res = excel_client.write_cell(file_path, sheet_name, cell, value)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ----------------- PowerPoint Automation Tools -----------------

@server.tool(name="ppt_read_presentation", description="Read slide titles, body text, tables, and speaker notes from a PowerPoint (.pptx) file.")
def ppt_read_presentation(file_path: str, max_slides: int = 50) -> str:
    """Read PowerPoint presentation."""
    try:
        res = ppt_client.read_presentation(file_path=file_path, max_slides=max_slides)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="ppt_create_presentation", description="Create a new PowerPoint presentation (.pptx) with title, subtitle, and bulleted slides.")
def ppt_create_presentation(file_path: str, title: str, subtitle: str = "", slides_data_json: str = "[]") -> str:
    """
    Create a new PowerPoint presentation.
    slides_data_json should be a JSON array of objects: [{'title': 'Slide Title', 'bullets': ['Point 1', 'Point 2']}]
    """
    try:
        slides_data = json.loads(slides_data_json) if slides_data_json else []
        res = ppt_client.create_presentation(file_path=file_path, title=title, subtitle=subtitle, slides_data=slides_data)
        return json.dumps(res, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ----------------- Microsoft Graph Cloud Tools (Teams / SharePoint) -----------------

@server.tool(name="teams_list_chats", description="List recent Teams chats (Requires Microsoft Graph authentication).")
def teams_list_chats(top: int = 20) -> str:
    """List Teams chats."""
    try:
        chats = graph_client.list_chats(top=top)
        return json.dumps({"count": len(chats), "chats": chats}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "tip": "To use Teams tools, configure .env with M365_CLIENT_ID and run 'python auth_cli.py login'."
        }, ensure_ascii=False)

@server.tool(name="teams_get_messages", description="Get messages from a specific Teams chat by chat_id.")
def teams_get_messages(chat_id: str, top: int = 20) -> str:
    """Get Teams chat messages."""
    try:
        messages = graph_client.get_chat_messages(chat_id=chat_id, top=top)
        return json.dumps({"chat_id": chat_id, "count": len(messages), "messages": messages}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="teams_send_chat_message", description="Send a message to a Teams chat by chat_id.")
def teams_send_chat_message(chat_id: str, content: str) -> str:
    """Send Teams chat message."""
    try:
        res = graph_client.send_chat_message(chat_id=chat_id, content=content)
        return json.dumps({"success": True, "result": res}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="teams_list_joined_teams", description="List all Microsoft Teams the user belongs to.")
def teams_list_joined_teams() -> str:
    """List joined teams."""
    try:
        teams = graph_client.list_joined_teams()
        return json.dumps({"count": len(teams), "teams": teams}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="teams_list_channels", description="List channels in a team by team_id.")
def teams_list_channels(team_id: str) -> str:
    """List channels in team."""
    try:
        channels = graph_client.list_team_channels(team_id=team_id)
        return json.dumps({"team_id": team_id, "channels": channels}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="teams_send_channel_message", description="Send a message to a Teams channel by team_id and channel_id.")
def teams_send_channel_message(team_id: str, channel_id: str, content: str) -> str:
    """Send channel message."""
    try:
        res = graph_client.send_channel_message(team_id=team_id, channel_id=channel_id, content=content)
        return json.dumps({"success": True, "result": res}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="sharepoint_search_sites", description="Search or list SharePoint sites accessible by the user.")
def sharepoint_search_sites(query: str = "") -> str:
    """Search SharePoint sites."""
    try:
        sites = graph_client.list_sharepoint_sites(query=query)
        return json.dumps({"query": query, "sites": sites}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="onedrive_cloud_search", description="Search files in Cloud OneDrive via Graph API.")
def onedrive_cloud_search(query: str) -> str:
    """Search Cloud OneDrive."""
    try:
        results = graph_client.search_cloud_drive(query=query)
        return json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

@server.tool(name="onedrive_cloud_upload", description="Upload a local file to Cloud OneDrive root or subfolder.")
def onedrive_cloud_upload(local_path: str, remote_folder: str = "") -> str:
    """Upload file to Cloud OneDrive."""
    try:
        res = graph_client.upload_file_to_onedrive(local_path=local_path, remote_folder=remote_folder)
        return json.dumps({"success": True, "result": res}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

if __name__ == "__main__":
    # Run the MCP server over stdio
    server.run(transport="stdio")
