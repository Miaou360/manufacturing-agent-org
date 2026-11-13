import sys
import argparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from local_office import LocalOutlookClient, LocalOneDriveClient
from graph_client import MicrosoftGraphClient

def show_status():
    print("=" * 60)
    print("       M365 HYBRID MCP - SYSTEM STATUS")
    print("=" * 60)
    
    # 1. Local Outlook
    ol = LocalOutlookClient()
    ol_status = ol.get_status()
    print("\n[1] Local Outlook (Desktop COM):")
    if ol_status.get("available"):
        print(f"  - Status: CONNECTED")
        print(f"  - App: {ol_status.get('app_name')} (v{ol_status.get('version')})")
        print(f"  - Accounts: {', '.join(ol_status.get('accounts', []))}")
        print(f"  - Active User: {ol_status.get('default_account')}")
    else:
        print(f"  - Status: NOT AVAILABLE ({ol_status.get('error')})")

    # 2. Local OneDrive
    od = LocalOneDriveClient()
    od_status = od.get_status()
    print("\n[2] Local OneDrive:")
    if od_status.get("exists"):
        print(f"  - Status: CONNECTED")
        print(f"  - Path: {od_status.get('configured_path')}")
    else:
        print(f"  - Status: PATH NOT FOUND ({od_status.get('configured_path')})")

    # 3. Microsoft Graph Cloud
    graph = MicrosoftGraphClient()
    g_status = graph.get_status()
    print("\n[3] Microsoft Graph API (Cloud - Teams / SharePoint):")
    if not g_status.get("configured"):
        print("  - Status: UNCONFIGURED")
        print("  - Notice: To enable Teams & Cloud SharePoint, configure .env (M365_CLIENT_ID, M365_TENANT_ID).")
        print("            (Local Outlook/OneDrive/Excel tools work right now without this!)")
    elif g_status.get("authenticated"):
        print("  - Status: AUTHENTICATED")
        print(f"  - User: {g_status.get('user_display_name')} ({g_status.get('user_principal_name')})")
    else:
        print("  - Status: CONFIGURED BUT NOT LOGGED IN")
        print("  - Run: python auth_cli.py login")
    print("=" * 60)

def login():
    graph = MicrosoftGraphClient()
    if not graph.is_configured():
        print("Error: M365_CLIENT_ID is not configured in .env.")
        print("Please copy .env.example to .env and set your Azure Entra Application ID.")
        return

    print("Initiating Microsoft Graph Login...")
    if graph.auth_mode == "interactive":
        res = graph.interactive_login()
        if res.get("success"):
            print(f"Login successful! Welcome, {res.get('user')}.")
        else:
            print(f"Login failed: {res.get('error')}")
    else:
        # Default: Device Code Flow
        flow_res = graph.start_device_code_flow()
        if not flow_res.get("success"):
            print(f"Failed to start device code flow: {flow_res.get('error')}")
            return
        
        print("\n" + flow_res.get("message"))
        print("\nWaiting for browser authentication to complete...")
        complete_res = graph.complete_device_code_flow(flow_res.get("flow"))
        if complete_res.get("success"):
            print(f"\nAuthentication successful! Welcome, {complete_res.get('user')}.")
        else:
            print(f"\nAuthentication failed: {complete_res.get('error')}")

def main():
    parser = argparse.ArgumentParser(description="M365 MCP CLI Helper")
    parser.add_argument("command", choices=["status", "login"], nargs="?", default="status", help="Command to run")
    args = parser.parse_args()

    if args.command == "status":
        show_status()
    elif args.command == "login":
        login()

if __name__ == "__main__":
    main()
