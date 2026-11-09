"""Вложения: разбор @файл и сообщение с inline-данными."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import attachments  # noqa: E402
from llm import LLM  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class TestAttachments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "a.png").write_bytes(PNG)
        (self.dir / "b.JPG").write_bytes(b"jpg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_files(self):
        text, files, notes = attachments.extract("сравни @a.png и @b.JPG, пожалуйста", self.dir)
        self.assertEqual(text, "сравни [file 1: a.png] и [file 2: b.JPG], пожалуйста")
        self.assertEqual(files, [("image/png", PNG), ("image/jpeg", b"jpg")])
        self.assertEqual([n[0] for n in notes], ["✓", "✓"])

    def test_pdf_and_text_supported(self):
        (self.dir / "r.pdf").write_bytes(b"%PDF")
        (self.dir / "n.txt").write_text("hi")
        _, files, _ = attachments.extract("@r.pdf @n.txt", self.dir)
        self.assertEqual([m for m, _ in files], ["application/pdf", "text/plain"])

    def test_unsupported_type_gives_feedback_and_is_not_sent(self):
        (self.dir / "x.xyz").write_bytes(b"?")
        text, files, notes = attachments.extract("глянь @x.xyz", self.dir)
        self.assertEqual((text, files), ("глянь @x.xyz", []))
        self.assertEqual(len(notes), 1)
        self.assertIn("не поддерживается", notes[0])

    def test_non_files_stay_text(self):
        for t in ("пиши на me@a.png.example", "используй @dataclass", "@ссылка"):
            self.assertEqual(attachments.extract(t, self.dir), (t, [], []))

    def test_missing_file_with_known_type(self):
        with self.assertRaises(attachments.AttachmentError):
            attachments.extract("@nope.png", self.dir)

    def test_too_many(self):
        with self.assertRaises(attachments.AttachmentError):
            attachments.extract(" ".join(["@a.png"] * (attachments.MAX_FILES + 1)), self.dir)

    def test_too_big(self):
        (self.dir / "big.png").write_bytes(b"0" * (attachments.MAX_BYTES + 1))
        with self.assertRaises(attachments.AttachmentError):
            attachments.extract("@big.png", self.dir)

    def test_message_roundtrip_keeps_file(self):
        msg = LLM.user_message(None, "что тут?", [("image/png", PNG)])
        back = LLM.load_message(json.loads(json.dumps(LLM.dump_message(msg))))
        self.assertEqual(back, msg)
        self.assertEqual(back.parts[1].inline_data.data, PNG)


if __name__ == "__main__":
    unittest.main()
