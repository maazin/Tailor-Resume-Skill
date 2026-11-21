"""The LaTeX reader and writer."""

import pytest
from conftest import FIXTURES, needs_pdf_tools, needs_tex, rules

from ats_score import score_resume
from latex_io import (
    LatexFormatError,
    baseline_resume,
    bullet_index,
    detex,
    entex,
    merge_tailored,
    read_tex_string,
    split_top_level,
    template_findings,
    write_tex,
)

OLD_STYLE = r"""
\documentclass{article}
\newcommand{\resumeItem}[1]{\item\small #1}
\newcommand{\resumeSubheading}[4]{
  \item
  \begin{tabular*}{\linewidth}[t]{@{}l@{\extracolsep{\fill}}r@{}}
    \textbf{#1} & \small #2 \\
    \textit{\small #3} & \textit{\small #4} \\
  \end{tabular*}
}
\newcommand{\resumeProject}[2]{
  \Needspace{11\baselineskip}
  \item\small #1\par
}
\newcommand{\resumeItemListStart}{\begin{itemize}[leftmargin=0.17in,itemsep=2pt]}
\begin{document}
\begin{center}
  {\LARGE\bfseries Pat Example} \\ \vspace{2pt}
  \scriptsize
  \textit{Software Engineer | Applied AI \& Data Systems} $\vert$
  555-010-0142 $\vert$
  \href{mailto:pat@example.com}{pat@example.com} $\vert$
  \href{https://github.com/pat-example}{github.com/pat-example} $\vert$
  \href{https://linkedin.com/in/pat-example}{LinkedIn}
\end{center}
\section{EDUCATION}
\resumeSubHeadingListStart
  \resumeSubheading
    {Example State University}{Tampa, FL}
    {Bachelor of Science in Computer Science --- GPA: 3.9/4.0}{Aug 2022 -- May 2026}
\resumeSubHeadingListEnd
\section{TECHNICAL SKILLS}
\begin{itemize}[leftmargin=0.15in,label={}]
  \small\item
    \textbf{Languages:} Python, C\#, SQL \\
    \textbf{AI \& Retrieval:} Anthropic API (Claude), RAG, hybrid search
\end{itemize}
\section{EXPERIENCE}
\resumeSubHeadingListStart
  \resumeSubheading
    {Example Grocer}{Lakeland, FL}
    {Data Science \& ML Intern}{May 2025 -- July 2025}
    \resumeItemListStart
      \resumeItem{Trained a ranking model on 500{,}000+ transactions to power the ``Deals for You'' feature and raise click-through 12\%.}
      \resumeItem{Cut grading time from {4 minutes} to {72 seconds} with a \$0-cost router (Flash $\rightarrow$ Nano), saving $\sim$25 hours.}
    \resumeItemListEnd
\resumeSubHeadingListEnd
\section{PROJECTS}
\resumeSubHeadingListStart
  \resumeProject
    {\link{https://github.com/pat-example/overlap}{Overlap} $\vert$ \emph{Go, PostgreSQL, REST APIs}}{}
    \resumeItemListStart
      \resumeItem{Launched a scheduling platform.}
    \resumeItemListEnd
  % SWAP-INS: replace one above to match the posting.
  %
  % DEVOPS ROLES:
  % \resumeItem{Automated test coverage for 14 Go packages.}
  %
  % \resumeItem{A bullet that the comment wraps
  %   onto a second line.}
\resumeSubHeadingListEnd
\end{document}
"""


def test_reads_the_sample_master(master):
    info = master["personalInfo"]
    assert info["name"] == "Jordan Rivera" and info["title"] == "Software Engineer"
    assert info["location"] == "Denver, CO" and info["phone"] == "555-010-0199"
    assert master["_template"]["contact_order"] == ["location", "phone", "email", "website", "github", "linkedin"]
    assert [m["key"] for m in master["sectionMeta"]] == ["education", "additional", "workExperience", "personalProjects"]
    first = master["workExperience"][0]
    assert (first["company"], first["location"], first["title"], first["years"]) == (
        "Northwind Analytics", "Denver, CO", "Data Engineering Intern", "May 2025 – August 2025",
    )
    assert len(first["description"]) == 3 and len(first["alternates"]) == 2
    project = master["personalProjects"][0]
    assert project["name"] == "TrailCast" and project["url"].endswith("/trailcast")
    assert project["stack"] == ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "GitHub Actions"]
    assert "served 900" not in project["description"][0] and "$7 server" in project["description"][0]
    assert master["skillGroups"][2] == {
        "label": "Data & ML",
        "items": ["Airflow", "dbt", "PyTorch", "MLflow", "LangChain", "RAG", "pgvector", "Tableau"],
    }
    assert "Tableau" in master["additional"]["technicalSkills"]


def test_commented_alternates_count_as_master_bullets(master):
    rows = bullet_index(master)
    northwind = [r for r in rows if r["entry"] == "Northwind Analytics"]
    assert [r["alternate"] for r in northwind] == [False, False, False, True, True]
    assert northwind[3]["id"] == "northwind-analytics-data-engineering-intern.3"
    assert northwind[3]["text"].startswith("Modeled 9 dbt tables")
    assert len(rows) == 28


def test_reads_the_old_two_row_style_and_its_latex():
    resume = read_tex_string(OLD_STYLE)
    info = resume["personalInfo"]
    assert info["title"] == "Software Engineer | Applied AI & Data Systems"
    assert info["location"] == "" and info["linkedin"] == "LinkedIn"
    assert resume["education"][0]["degree"] == "Bachelor of Science in Computer Science — GPA: 3.9/4.0"
    assert resume["education"][0]["years"] == "Aug 2022 – May 2026"
    assert resume["skillGroups"][0]["items"] == ["Python", "C#", "SQL"]
    assert resume["skillGroups"][1] == {"label": "AI & Retrieval", "items": ["Anthropic API (Claude)", "RAG", "hybrid search"]}
    bullets = resume["workExperience"][0]["description"]
    assert bullets[0] == 'Trained a ranking model on 500,000+ transactions to power the "Deals for You" feature and raise click-through 12%.'
    assert bullets[1] == "Cut grading time from 4 minutes to 72 seconds with a $0-cost router (Flash → Nano), saving ~25 hours."
    overlap = resume["personalProjects"][0]
    # Alternates written after \resumeItemListEnd still belong to the entry above them.
    assert overlap["alternates"] == [
        "Automated test coverage for 14 Go packages.",
        "A bullet that the comment wraps onto a second line.",
    ]
    assert overlap["stack"] == ["Go", "PostgreSQL", "REST APIs"]


def test_old_style_template_is_flagged():
    found = rules(template_findings(OLD_STYLE), "block")
    assert {"D10", "L6", "L8"} <= found
    assert "D12" in rules(template_findings(OLD_STYLE), "warn")  # tabular heading is replaced on write
    assert "L13" in rules(template_findings(OLD_STYLE), "info")


def test_sample_template_is_clean(master_tex):
    assert template_findings(master_tex) == []


def test_jake_argument_order_is_detected():
    jake = (FIXTURES / "jake_resume.tex").read_text(encoding="utf-8")
    resume = read_tex_string(jake)
    first = resume["workExperience"][0]
    assert first["title"] == "Undergraduate Research Assistant" and first["company"] == "Texas A&M University"
    assert first["years"] == "June 2020 – Present" and first["location"] == "College Station, TX"
    assert resume["education"][0]["institution"] == "Southwestern University"
    assert resume["personalProjects"][0]["name"] == "Gitlytics"
    assert "Python" in resume["additional"]["technicalSkills"]


def test_refuses_unknown_macros(master_tex):
    broken = master_tex.replace(r"\resumeSubHeadingListEnd" + "\n" + r"\end{document}",
                                r"\resumeSubSubheading{Lead}{2024}" + "\n" + r"\resumeSubHeadingListEnd" + "\n" + r"\end{document}")
    with pytest.raises(LatexFormatError) as excinfo:
        read_tex_string(broken)
    assert r"\resumeSubSubheading" in str(excinfo.value)


def test_refuses_unknown_sections(master_tex):
    with pytest.raises(LatexFormatError):
        read_tex_string(master_tex.replace(r"\section{PROJECTS}", r"\section{HOBBIES}"))


@pytest.mark.parametrize(
    "plain",
    [
        "R&D cut cost 50% to $7 on issue #4 in my_table {x} a~b a^b c\\d",
        'Moved "Deals" from 4 → 2 steps, May 2025 – Aug 2025',
        "C#, C++, Node.js, CI/CD, precision@10, 2.29:1, 500K+",
    ],
)
def test_escape_round_trip(plain):
    assert detex(entex(plain)) == plain


def test_split_top_level():
    assert split_top_level("Anthropic API (Claude, Haiku), RAG, hybrid search") == [
        "Anthropic API (Claude, Haiku)", "RAG", "hybrid search",
    ]


def test_write_is_clean_and_keeps_the_preamble(master, master_tex, tailored):
    resume = merge_tailored(tailored, master, projects=3)
    tex = write_tex(resume, entrygap=7.5, sectiongap=10)
    preamble = tex.split(r"\begin{document}")[0]
    assert r"\setlength{\entrygap}{7.5pt}" in preamble and r"\setlength{\sectiongap}{10pt}" in preamble
    assert "pdfsubject={Data Engineer II}" in preamble
    assert "pdfkeywords={Python, SQL, TypeScript" in preamble
    body = tex.split(r"\begin{document}")[1]
    assert "%" not in body.replace(r"\%", "") and r"\vspace" not in body and "Needspace" not in tex
    assert "{\\small Data Engineer II}" in body and "Relocating to Austin, TX \\textbar{}" in body
    assert "on a single \\$7 server" in body and "93\\% accuracy" in body
    assert "May 2025 -- August 2025" in body
    assert body.count(r"\resumeProject") == 3 and "LedgerLite" not in body
    # Only the two lengths and the PDF metadata differ from the master's preamble.
    from layout_check import _preamble_settings
    assert _preamble_settings(tex) == _preamble_settings(master_tex)


def test_merge_fills_dates_and_links_from_the_master(resume):
    assert resume["workExperience"][0]["years"] == "May 2025 – August 2025"
    assert resume["workExperience"][0]["title"] == "Data Engineering Intern"
    assert resume["personalProjects"][0]["url"].endswith("/trailcast")
    assert resume["education"][0]["institution"] == "Front Range University"
    assert resume["education"][0]["degree"] == "Bachelor of Science in Computer Science"
    assert resume["education"][0]["gpa"] == "GPA: 3.8/4.0"
    assert resume["personalInfo"]["email"] == "jordan.rivera@example.com"


def test_baseline_is_first_three_bullets(master):
    baseline = baseline_resume(master)
    for kind in ("workExperience", "personalProjects"):
        for entry, source in zip(baseline[kind], master[kind]):
            assert entry["description"] == source["description"][:3] and "alternates" not in entry
    assert len(baseline["personalProjects"]) == 5


def test_ats_score_runs_on_the_reader_output(master, job):
    result = score_resume(baseline_resume(master), master, job)
    assert result["sub_scores"]["date_consistency"] == 100.0
    assert result["missing_not_in_master"] == ["Snowflake"]


@needs_pdf_tools
def test_read_then_write_gives_the_same_pdf_text(tmp_path, master_tex):
    """Build step 4: reading then writing the sample with no changes gives the same PDF text."""
    import subprocess

    from compile import compile_tex

    original = tmp_path / "original.tex"
    original.write_text(master_tex, encoding="utf-8")
    rewritten = tmp_path / "rewritten.tex"
    rewritten.write_text(write_tex(baseline_resume(read_tex_string(master_tex))), encoding="utf-8")
    texts = []
    for path in (original, rewritten):
        result = compile_tex(path)
        assert result.ok, result.errors
        out = subprocess.run(["pdftotext", "-layout", result.pdf, "-"], capture_output=True, check=True)
        texts.append(" ".join(out.stdout.decode("utf-8").split()))
    assert texts[0] == texts[1]


@needs_tex
def test_written_file_compiles(tmp_path, master, tailored):
    from compile import compile_tex

    path = tmp_path / "resume.tex"
    path.write_text(write_tex(merge_tailored(tailored, master, projects=3)), encoding="utf-8")
    result = compile_tex(path)
    assert result.ok and result.pages == 1, result.errors
