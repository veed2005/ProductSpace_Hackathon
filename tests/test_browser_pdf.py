"""Reading a PDF tab in full: text extraction, scanned pages, protection, masking, and the hub path."""

import asyncio
import base64
from pathlib import Path

import pymupdf

from app.agent.render import page_text
from app.browser import pdf
from app.browser.hub import BrowserConnection
from app.browser.protocol import PageState

LEASE = Path(__file__).resolve().parent.parent / "demo_sites" / "testbench" / "lease.pdf"


def make_pdf(pages: list[str], *, password: str = "", image_only: bool = False) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if image_only:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
            pix.clear_with(200)
            page.insert_image(pymupdf.Rect(72, 72, 472, 272), pixmap=pix)
        else:
            page.insert_textbox(pymupdf.Rect(72, 72, 540, 760), text, fontsize=11)
    kw = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password} if password else {}
    return doc.tobytes(**kw)


def test_reads_every_page_of_the_demo_lease():
    d = pdf.read(LEASE.read_bytes(), "https://files.example/lease.pdf")
    assert d.pages == 6 and not d.scanned and not d.truncated and d.error is None
    assert d.title.startswith("Residential Lease Agreement")
    assert "[Page 4 of 6]" in d.text and "one dog weighing under 40 pounds" in d.text
    assert "[Page 6 of 6]" in d.text and "Dana Whitfield" in d.text  # the last page too


def test_masks_secrets_in_pdf_text():
    d = pdf.read(make_pdf(["Tenant SSN: 123-45-6789. Card 4111 1111 1111 1111."]), "https://x/a.pdf")
    assert "123-45-6789" not in d.text and "4111" not in d.text and "[redacted SSN]" in d.text


def test_scanned_pdf_pages_become_images():
    d = pdf.read(make_pdf(["", ""], image_only=True), "https://x/scan.pdf")
    assert d.scanned and len(d.images) == 2
    assert "scanned" in pdf.describe(d)
    assert "images" not in d.model_dump()  # page pictures never go to the dashboard


def test_password_protected_and_broken_pdfs_explain_themselves():
    assert "password" in pdf.read(make_pdf(["secret"], password="pw"), "https://x/p.pdf").error
    assert pdf.read(b"not a pdf", "https://x/bad.pdf").error


def test_title_falls_back_to_the_file_name():
    assert pdf.read(make_pdf(["hello"]), "https://x/My_Utility-Bill.pdf").title == "My Utility Bill"


def test_hub_reads_a_pdf_tab_once_and_shows_its_text_to_the_agent():
    calls = []

    class Conn(BrowserConnection):
        async def request(self, action, *, tab_id=None, timeout=20.0, **args):
            calls.append(action)
            if action == "get_page_state":
                return {"ok": True, "data": {"doc_id": "d1", "tab_id": 3, "url": "https://files.example/lease.pdf",
                                             "title": "", "elements": [], "content_type": "application/pdf"}}
            return {"ok": True, "data": {"pdf_base64": base64.b64encode(LEASE.read_bytes()).decode()}}

    async def scenario():
        conn = Conn(None, "br_x", 1)
        return await conn.page_state(fresh=True), await conn.page_state(fresh=True)

    first, again = asyncio.run(scenario())
    assert calls.count("read_pdf") == 1  # cached by address
    assert first.site_name.startswith("the PDF")
    text = page_text(first)
    assert "6 pages" in text and "pet deposit of $300.00" in text and again.document.pages == 6


def test_a_pdf_that_cannot_be_downloaded_is_explained():
    class Conn(BrowserConnection):
        async def request(self, action, *, tab_id=None, timeout=20.0, **args):
            if action == "get_page_state":
                return {"ok": True, "data": {"doc_id": "d1", "url": "file:///C:/docs/lease.pdf", "elements": [],
                                             "content_type": "application/pdf"}}
            return {"ok": False, "error": "file_access",
                    "detail": "To read files on this computer, turn on \"Allow access to file URLs\" for Formline."}

    state = asyncio.run(Conn(None, "br_y", 1).page_state(fresh=True))
    assert "Allow access to file URLs" in page_text(state)


def test_page_state_without_a_document_is_unchanged():
    assert "PDF" not in page_text(PageState(doc_id="d", url="https://a.test/", title="A"))


def test_expired_pdf_falls_back_to_what_is_on_screen():
    class Conn(BrowserConnection):
        async def request(self, action, *, tab_id=None, timeout=20.0, **args):
            if action == "get_page_state":
                return {"ok": True, "data": {"doc_id": "d1", "tab_id": 3, "elements": [], "content_type": "application/pdf",
                                             "url": "https://bucket.s3.amazonaws.com/9f8e7d6c5b4a39281706f5e4d3c2b1a0.pdf?X-Amz-Expires=60"}}
            return {"ok": True, "data": {"screenshot_base64": "aGVsbG8=", "reason": "The PDF's link has expired (HTTP 403)."}}

    state = asyncio.run(Conn(None, "br_z", 1).page_state(fresh=True))
    assert state.document.on_screen_only and state.document.images == ["aGVsbG8="]
    text = page_text(state)
    assert "visible on screen is attached" in text and "open it again from the website" in text
    assert state.site_name == "a PDF"  # a random S3 file name isn't read out as a title
