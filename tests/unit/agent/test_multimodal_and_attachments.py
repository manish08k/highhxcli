"""Images and files reaching the model: each provider's wire format for images, the newest-images
window, stored history without pixels, attachments extracted (text, Office, PDF) and given with
the request (`@file`), and the agent's tools for attachments."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from highhx.agent.messages import ImageBlock, Message, TextBlock, ToolResultBlock, limit_images, without_images
from highhx.agent.model.anthropic import to_anthropic_messages
from highhx.agent.model.capabilities import capabilities_for
from highhx.agent.model.openai import to_openai_messages
from highhx.attachments import AttachmentError, AttachmentStore, context_blocks, read_text

IMAGE = ImageBlock("image/png", "aGk=", "screenshot c1")


def test_every_provider_carries_images_after_the_tool_results() -> None:
    message = Message("user", [ToolResultBlock("t1", "ok", False, "computer_act"), IMAGE])
    anthropic = to_anthropic_messages([message])[0]["content"]
    assert [b["type"] for b in anthropic] == ["tool_result", "text", "image"]
    assert anthropic[2]["source"] == {"type": "base64", "media_type": "image/png", "data": "aGk="}
    openai = to_openai_messages("sys", [message])
    assert [m["role"] for m in openai] == ["system", "tool", "user"]  # images only in user content
    assert openai[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,aGk="
    genai = pytest.importorskip("google.genai.types")
    from highhx.agent.model.gemini import to_gemini_contents

    parts = to_gemini_contents([message], genai)[0].parts
    assert parts[-1].inline_data.data == b"hi" and parts[-1].inline_data.mime_type == "image/png"


def test_only_the_newest_images_are_sent_and_none_are_stored() -> None:
    messages = [Message("user", [TextBlock(f"step {n}"), ImageBlock("image/png", "aGk=", f"c{n}")]) for n in range(6)]
    kept = limit_images(messages, keep=2)
    assert [len(m.images) for m in kept] == [0, 0, 0, 0, 1, 1]
    assert "earlier image no longer shown: c0" in kept[0].text
    stored = without_images(messages[5])
    assert not stored.images and "[image: c5]" in stored.text


def test_capabilities_say_who_can_see() -> None:
    assert capabilities_for("highhx", None).vision and not capabilities_for("highhx", None).local
    local = capabilities_for("local", "qwen2.5-vl-7b", config={"base_url": "http://127.0.0.1:11434/v1"})
    assert local.vision and local.local and local.action_format == "json"
    tars = capabilities_for("local", "ui-tars-1.5-7b", config={"base_url": "http://10.0.0.5:8000/v1"})
    assert tars.action_format == "uitars" and tars.coordinates == "relative1000" and not tars.local
    assert not capabilities_for("local", "llama3-8b", config={}).vision


# -------------------------------------------------------------- attachments
@pytest.fixture
def files(tmp_path: Path) -> Path:
    (tmp_path / "notes.txt").write_text("Revenue grew 12%.\n")
    (tmp_path / "shot.png").write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\0" * 64)
    (tmp_path / "blob.bin").write_bytes(b"\0\1\2\3")
    if shutil.which("textutil"):
        subprocess.run(["textutil", "-convert", "docx", str(tmp_path / "notes.txt")], check=True, capture_output=True)
    return tmp_path


def test_mentions_attach_existing_files_only(files: Path) -> None:
    store = AttachmentStore()
    added, problems = store.mentions("compare @notes.txt, with @shot.png and mail @someone", files)
    assert [a.name for a in added] == ["notes.txt", "shot.png"] and problems == []
    assert store.get("f1").kind == "text" and store.get("shot.png").kind == "image"
    assert store.mentions("again @notes.txt", files)[0][0].id == "f1"  # the same file keeps its id
    with pytest.raises(AttachmentError, match="No attachment"):
        store.get("f9")


def test_what_the_model_is_given(files: Path) -> None:
    store = AttachmentStore()
    for name in ("notes.txt", "shot.png", "blob.bin"):
        store.add(files / name)
    seeing = context_blocks(store.take_pending(), vision=True)
    assert "Revenue grew 12%." in "".join(b.text for b in seeing if isinstance(b, TextBlock))
    assert any(isinstance(b, ImageBlock) for b in seeing)
    assert any("binary file" in b.text for b in seeing if isinstance(b, TextBlock))
    blind = context_blocks(list(store.items.values()), vision=False)
    assert not any(isinstance(b, ImageBlock) for b in blind)


@pytest.mark.skipif(shutil.which("textutil") is None, reason="needs macOS textutil to make a .docx")
def test_office_documents_are_read(files: Path) -> None:
    store = AttachmentStore()
    assert "Revenue grew 12%." in read_text(store.add(files / "notes.docx"))


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs poppler's pdftotext")
def test_pdf_text_and_pages(tmp_path: Path) -> None:
    from highhx.attachments import page_images

    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(_pdf("Invoice total: 1,284.50 EUR"))
    attachment = AttachmentStore().add(pdf)
    assert attachment.pages == 1 and "1,284.50" in read_text(attachment)
    if shutil.which("pdftoppm"):
        assert page_images(attachment)[0].label.endswith("page 1")


def test_a_request_carries_its_attachments(agent_project: Path, make_session: Any, files: Path) -> None:
    from tests.unit.agent.conftest import reply

    session, provider, _ = make_session(agent_project, [reply("The revenue grew 12%.")])
    (agent_project / "notes.txt").write_text("Revenue grew 12%.\n")
    session.run_turn("summarise @notes.txt")
    sent = provider.requests[0].messages[0]
    assert "Attached file f1: notes.txt" in sent.text and "Revenue grew 12%." in sent.text
    assert session.attachments.get("f1").name == "notes.txt"


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 30 120 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
