"""CLI: python scripts/parse_check resume.pdf [--resume resume.json|resume.tex]"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import dump_json, load_resume  # noqa: E402
from parse_check import parse_check_pdf, summary_line  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse-check a resume PDF with pdfminer and pdftotext.")
    parser.add_argument("pdf")
    parser.add_argument("--resume", help="resume .json or .tex to round-trip against")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--text", action="store_true", help="include the extracted text")
    args = parser.parse_args(argv)

    resume = load_resume(args.resume) if args.resume else None
    result = parse_check_pdf(args.pdf, resume, args.max_pages)
    if not args.text:
        for item in result["passes"]:
            item.pop("extracted_text", None)
    result["summary"] = summary_line(result)
    sys.stdout.write(dump_json(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
