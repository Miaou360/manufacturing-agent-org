import os
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
import pythoncom
import win32com.client
from config import LOCAL_ONEDRIVE_PATH

# Folder enum constants in Outlook
OL_FOLDER_INBOX = 6
OL_FOLDER_CALENDAR = 9
OL_FOLDER_DRAFTS = 16
OL_FOLDER_SENT = 5

class LocalOutlookClient:
    def __init__(self):
        pass

    def _get_namespace(self):
        pythoncom.CoInitialize()
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = getattr(ol, "Session", None)
        if ns is None:
            try:
                ns = ol.GetNamespace("MAPI")
            except AttributeError:
                ns = ol.GetNameSpace("MAPI")
        return ol, ns

    def get_status(self) -> Dict[str, Any]:
        """Check Outlook status and connected accounts."""
        try:
            ol, ns = self._get_namespace()
            accounts = [acc.DisplayName for acc in ol.Session.Accounts]
            return {
                "available": True,
                "app_name": ol.Name,
                "version": ol.Version,
                "accounts": accounts,
                "default_account": accounts[0] if accounts else "Unknown"
            }
        except Exception as e:
            return {
                "available": False,
                "error": str(e)
            }
        finally:
            pythoncom.CoUninitialize()

    def get_unread_emails(self, max_count: int = 10) -> List[Dict[str, Any]]:
        """Retrieve recent unread emails from Inbox."""
        try:
            ol, ns = self._get_namespace()
            inbox = ns.GetDefaultFolder(OL_FOLDER_INBOX)
            items = inbox.Items
            items.Sort("[ReceivedTime]", True)
            
            # Restrict to unread
            unread_items = items.Restrict("[UnRead] = True")
            
            results = []
            count = 0
            for item in unread_items:
                if count >= max_count:
                    break
                try:
                    results.append({
                        "subject": getattr(item, "Subject", "(No Subject)"),
                        "sender_name": getattr(item, "SenderName", ""),
                        "sender_email": getattr(item, "SenderEmailAddress", ""),
                        "received_time": str(getattr(item, "ReceivedTime", "")),
                        "has_attachments": getattr(item, "Attachments", None) is not None and item.Attachments.Count > 0,
                        "entry_id": getattr(item, "EntryID", ""),
                        "body_preview": getattr(item, "Body", "")[:200]
                    })
                    count += 1
                except Exception:
                    continue
            return results
        finally:
            pythoncom.CoUninitialize()

    def search_emails(self, query: str, max_count: int = 10, folder_name: str = "Inbox", scan_limit: int = 300) -> List[Dict[str, Any]]:
        """Search emails by subject, sender, or body text within recent items."""
        try:
            ol, ns = self._get_namespace()
            folder = ns.GetDefaultFolder(OL_FOLDER_INBOX)
            items = folder.Items
            items.Sort("[ReceivedTime]", True)
            
            results = []
            count = 0
            scanned = 0
            query_lower = query.lower()
            
            for item in items:
                if count >= max_count or scanned >= scan_limit:
                    break
                scanned += 1
                try:
                    subject = str(getattr(item, "Subject", ""))
                    body = str(getattr(item, "Body", ""))
                    sender = str(getattr(item, "SenderName", ""))
                    
                    if query_lower in subject.lower() or query_lower in sender.lower() or query_lower in body.lower():
                        results.append({
                            "subject": subject,
                            "sender_name": sender,
                            "sender_email": getattr(item, "SenderEmailAddress", ""),
                            "received_time": str(getattr(item, "ReceivedTime", "")),
                            "has_attachments": getattr(item, "Attachments", None) is not None and item.Attachments.Count > 0,
                            "entry_id": getattr(item, "EntryID", ""),
                            "body_preview": body[:200]
                        })
                        count += 1
                except Exception:
                    continue
            return results
        finally:
            pythoncom.CoUninitialize()

    def read_email_details(self, entry_id: str) -> Dict[str, Any]:
        """Fetch full details and body of an email by its EntryID."""
        try:
            ol, ns = self._get_namespace()
            item = ns.GetItemFromID(entry_id)
            
            attachments = []
            if getattr(item, "Attachments", None):
                for i in range(1, item.Attachments.Count + 1):
                    att = item.Attachments.Item(i)
                    attachments.append({
                        "name": att.FileName,
                        "size_bytes": getattr(att, "Size", 0)
                    })
            
            return {
                "entry_id": entry_id,
                "subject": getattr(item, "Subject", ""),
                "sender_name": getattr(item, "SenderName", ""),
                "sender_email": getattr(item, "SenderEmailAddress", ""),
                "to": getattr(item, "To", ""),
                "cc": getattr(item, "CC", ""),
                "received_time": str(getattr(item, "ReceivedTime", "")),
                "attachments": attachments,
                "body": getattr(item, "Body", "")
            }
        finally:
            pythoncom.CoUninitialize()

    def create_email_draft(self, to: str, subject: str, body: str, cc: str = "", bcc: str = "", attachments: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create a draft email in Outlook without sending."""
        try:
            ol, ns = self._get_namespace()
            mail = ol.CreateItem(0)  # 0 = olMailItem
            mail.To = to
            if cc:
                mail.CC = cc
            if bcc:
                mail.BCC = bcc
            mail.Subject = subject
            if "<html" in body.lower() or "<div" in body.lower() or "<table" in body.lower():
                mail.HTMLBody = body
            else:
                mail.Body = body
            
            if attachments:
                for att_path in attachments:
                    if os.path.exists(att_path):
                        mail.Attachments.Add(os.path.abspath(att_path))
                    else:
                        raise FileNotFoundError(f"Attachment file not found: {att_path}")
            
            mail.Save()
            return {
                "success": True,
                "message": "Draft created successfully in Outlook Drafts folder.",
                "entry_id": mail.EntryID,
                "subject": subject,
                "to": to
            }
        finally:
            pythoncom.CoUninitialize()

    def send_email(self, to: str, subject: str, body: str, cc: str = "", bcc: str = "", attachments: Optional[List[str]] = None, display_first: bool = False) -> Dict[str, Any]:
        """Send an email via Outlook, or display it for user approval."""
        try:
            ol, ns = self._get_namespace()
            mail = ol.CreateItem(0)  # 0 = olMailItem
            mail.To = to
            if cc:
                mail.CC = cc
            if bcc:
                mail.BCC = bcc
            mail.Subject = subject
            if "<html" in body.lower() or "<div" in body.lower() or "<table" in body.lower():
                mail.HTMLBody = body
            else:
                mail.Body = body
            
            if attachments:
                for att_path in attachments:
                    if os.path.exists(att_path):
                        mail.Attachments.Add(os.path.abspath(att_path))
                    else:
                        raise FileNotFoundError(f"Attachment file not found: {att_path}")
            
            if display_first:
                mail.Display()
                return {
                    "success": True,
                    "message": "Email opened in Outlook for review.",
                    "subject": subject,
                    "to": to
                }
            else:
                mail.Send()
                return {
                    "success": True,
                    "message": "Email sent successfully via Outlook.",
                    "subject": subject,
                    "to": to
                }
        finally:
            pythoncom.CoUninitialize()

    def get_calendar_events(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Retrieve upcoming calendar events for the specified number of days."""
        try:
            ol, ns = self._get_namespace()
            calendar = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            items = calendar.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            
            now = datetime.datetime.now()
            end_date = now + datetime.timedelta(days=days_ahead)
            
            # Outlook filter format: [Start] >= 'MM/DD/YYYY HH:MM AM/PM'
            start_str = now.strftime("%m/%d/%Y 00:00 AM")
            end_str = end_date.strftime("%m/%d/%Y 11:59 PM")
            filter_query = f"[Start] >= '{start_str}' AND [Start] <= '{end_str}'"
            
            filtered = items.Restrict(filter_query)
            
            events = []
            for appt in filtered:
                try:
                    events.append({
                        "subject": getattr(appt, "Subject", "(No Title)"),
                        "start": str(getattr(appt, "Start", "")),
                        "end": str(getattr(appt, "End", "")),
                        "location": getattr(appt, "Location", ""),
                        "organizer": getattr(appt, "Organizer", ""),
                        "is_recurring": getattr(appt, "IsRecurring", False),
                        "body_preview": getattr(appt, "Body", "")[:150]
                    })
                except Exception:
                    continue
            return events
        finally:
            pythoncom.CoUninitialize()

    def create_calendar_event(self, subject: str, start_time: str, end_time: str, location: str = "", body: str = "", reminder_minutes: int = 15) -> Dict[str, Any]:
        """
        Create a new appointment in Outlook calendar.
        Format for start_time / end_time: 'YYYY-MM-DD HH:MM' or ISO format.
        """
        try:
            ol, ns = self._get_namespace()
            appt = ol.CreateItem(1)  # 1 = olAppointmentItem
            appt.Subject = subject
            appt.Start = start_time
            appt.End = end_time
            if location:
                appt.Location = location
            if body:
                appt.Body = body
            appt.ReminderMinutesBeforeStart = reminder_minutes
            appt.ReminderSet = True
            appt.Save()
            
            return {
                "success": True,
                "message": "Calendar event created successfully in Outlook.",
                "subject": subject,
                "start": str(appt.Start),
                "end": str(appt.End),
                "location": location
            }
        finally:
            pythoncom.CoUninitialize()


class LocalOneDriveClient:
    def __init__(self, root_path: Optional[str] = None):
        self.root_path = Path(root_path or LOCAL_ONEDRIVE_PATH)

    def get_status(self) -> Dict[str, Any]:
        exists = self.root_path.exists()
        return {
            "configured_path": str(self.root_path),
            "exists": exists,
            "is_dir": self.root_path.is_dir() if exists else False
        }

    def list_files(self, subpath: str = "", max_count: int = 50) -> List[Dict[str, Any]]:
        """List files and folders in the local OneDrive directory."""
        target_dir = self.root_path / subpath.strip("/\\")
        if not target_dir.exists():
            raise FileNotFoundError(f"OneDrive folder not found: {target_dir}")
        
        items = []
        for entry in target_dir.iterdir():
            if len(items) >= max_count:
                break
            try:
                stat = entry.stat()
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size_bytes": stat.st_size if not entry.is_dir() else 0,
                    "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "relative_path": str(entry.relative_to(self.root_path)),
                    "full_path": str(entry)
                })
            except Exception:
                continue
        return items

    def search_files(self, keyword: str, ext_filter: Optional[str] = None, max_count: int = 30) -> List[Dict[str, Any]]:
        """Search files recursively in local OneDrive by keyword and optional extension."""
        if not self.root_path.exists():
            raise FileNotFoundError(f"OneDrive path not found: {self.root_path}")
        
        matches = []
        keyword_lower = keyword.lower()
        ext_filter_clean = ext_filter.lower().lstrip(".") if ext_filter else None
        
        for root, dirs, files in os.walk(self.root_path):
            if len(matches) >= max_count:
                break
            for f in files:
                if len(matches) >= max_count:
                    break
                if keyword_lower in f.lower():
                    if ext_filter_clean and not f.lower().endswith("." + ext_filter_clean):
                        continue
                    full_p = Path(root) / f
                    try:
                        stat = full_p.stat()
                        matches.append({
                            "name": f,
                            "relative_path": str(full_p.relative_to(self.root_path)),
                            "full_path": str(full_p),
                            "size_bytes": stat.st_size,
                            "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                        })
                    except Exception:
                        continue
        return matches


class LocalExcelClient:
    def list_sheets(self, file_path: str) -> List[str]:
        """List sheets in an Excel workbook."""
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True)
        sheets = wb.sheetnames
        wb.close()
        return sheets

    def read_sheet(self, file_path: str, sheet_name: Optional[str] = None, max_rows: int = 100) -> Dict[str, Any]:
        """Read rows and columns from an Excel file into a JSON structure."""
        import pandas as pd
        import numpy as np
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Excel file not found: {file_path}")
        
        df = pd.read_excel(file_path, sheet_name=sheet_name or 0)
        total_rows = len(df)
        preview_df = df.head(max_rows).replace([np.nan, np.inf, -np.inf], None)
        records = preview_df.to_dict(orient="records")
        return {
            "file_path": file_path,
            "sheet_name": sheet_name or "First Sheet",
            "total_rows": total_rows,
            "returned_rows": len(records),
            "columns": [str(c) for c in df.columns],
            "data": records
        }

    def write_cell(self, file_path: str, sheet_name: str, cell_coordinate: str, value: Any) -> Dict[str, Any]:
        """Write a value to a specific cell (e.g. 'A1', 'C5') in an Excel file."""
        import openpyxl
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Excel file not found: {file_path}")
        
        wb = openpyxl.load_workbook(file_path)
        if sheet_name not in wb.sheetnames:
            sheet = wb.active
        else:
            sheet = wb[sheet_name]
            
        sheet[cell_coordinate] = value
        wb.save(file_path)
        wb.close()
        return {
            "success": True,
            "message": f"Successfully updated cell {cell_coordinate} in {file_path}",
            "cell": cell_coordinate,
            "value": value
        }


class LocalPowerPointClient:
    def get_status(self) -> Dict[str, Any]:
        """Check if PowerPoint is available."""
        try:
            pythoncom.CoInitialize()
            ppt = win32com.client.Dispatch("PowerPoint.Application")
            version = ppt.Version
            return {
                "available": True,
                "app": "Microsoft PowerPoint",
                "version": version
            }
        except Exception as e:
            return {
                "available": False,
                "error": str(e)
            }
        finally:
            pythoncom.CoUninitialize()

    def read_presentation(self, file_path: str, max_slides: int = 50) -> Dict[str, Any]:
        """Read slide contents, texts, tables, and notes from a PowerPoint (.pptx) file."""
        from pptx import Presentation
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"PowerPoint file not found: {file_path}")

        prs = Presentation(file_path)
        total_slides = len(prs.slides)
        slides_info = []

        for i, slide in enumerate(prs.slides):
            if i >= max_slides:
                break
            slide_texts = []
            notes_text = ""

            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            slide_texts.append(text)
                elif shape.has_table:
                    table_rows = []
                    for row in shape.table.rows:
                        row_vals = [cell.text.strip() for cell in row.cells]
                        table_rows.append(" | ".join(row_vals))
                    if table_rows:
                        slide_texts.append("\n".join(table_rows))

            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes_text = slide.notes_slide.notes_text_frame.text.strip()

            title = slide.shapes.title.text.strip() if (slide.shapes.title and slide.shapes.title.text) else f"Slide {i+1}"

            slides_info.append({
                "slide_number": i + 1,
                "title": title,
                "texts": slide_texts,
                "notes": notes_text
            })

        return {
            "file_path": file_path,
            "total_slides": total_slides,
            "returned_slides": len(slides_info),
            "slides": slides_info
        }

    def create_presentation(self, file_path: str, title: str, subtitle: str = "", slides_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Create a new PowerPoint presentation (.pptx) with title and content slides."""
        from pptx import Presentation
        from pptx.util import Inches

        prs = Presentation()
        # Title slide
        title_slide_layout = prs.slide_layouts[0]
        slide = prs.slides.add_slide(title_slide_layout)
        slide.shapes.title.text = title
        if subtitle and slide.placeholders and len(slide.placeholders) > 1:
            slide.placeholders[1].text = subtitle

        # Content slides
        bullet_slide_layout = prs.slide_layouts[1]
        if slides_data:
            for s_data in slides_data:
                s = prs.slides.add_slide(bullet_slide_layout)
                s.shapes.title.text = s_data.get("title", "")
                tf = s.shapes.placeholders[1].text_frame
                content_items = s_data.get("bullets", [])
                for idx, item in enumerate(content_items):
                    if idx == 0:
                        tf.text = item
                    else:
                        p = tf.add_paragraph()
                        p.text = item

        prs.save(file_path)
        return {
            "success": True,
            "message": f"Successfully created PowerPoint presentation at {file_path}",
            "file_path": file_path,
            "total_slides": len(prs.slides)
        }

