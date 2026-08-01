import re
from parser import parse_diff
from parser import parse_diff
from chunker import split_into_file_diffs, chunk_file_diffs

MOCK_RULES = [
    {
        "ruleId": "MOCK-001",
        "severity": "critical",
        "category": "security",
        "title": "eval usage",
        "check": lambda line: "eval(" in line,
    },
    {
        "ruleId": "MOCK-002",
        "severity": "critical",
        "category": "security",
        "title": "hardcoded credential",
        "check": lambda line: bool(re.search(
            r"(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
            line, re.IGNORECASE
        )),
    },
    {
        "ruleId": "MOCK-003",
        "severity": "high",
        "category": "security",
        "title": "SQL string concatenation",
        "check": lambda line: bool(re.search(
            r"(SELECT|INSERT|UPDATE|DELETE)\b.*['\"]?\s*\+", line, re.IGNORECASE
        )),
    },
    {
        "ruleId": "MOCK-005",
        "severity": "medium",
        "category": "correctness",
        "title": "loose null comparison",
        "check": lambda line: bool(re.search(r"(==|!=)\s*null", line)),
    },
    {
        "ruleId": "MOCK-006",
        "severity": "medium",
        "category": "performance",
        "title": "deep-clone via JSON",
        "check": lambda line: "JSON.parse(JSON.stringify(" in line,
    },
    {
        "ruleId": "MOCK-007",
        "severity": "low",
        "category": "style",
        "title": "console.log left in",
        "check": lambda line: "console.log(" in line,
    },
    {
        "ruleId": "MOCK-008",
        "severity": "low",
        "category": "style",
        "title": "unresolved marker",
        "check": lambda line: "TODO" in line or "FIXME" in line,
    },
    {
        "ruleId": "MOCK-INJ",
        "severity": "critical",
        "category": "security",
        "title": "prompt-injection content",
        "check": lambda line: bool(re.search(
            r"ignore previous instructions|disregard all prior|you are now",
            line, re.IGNORECASE
        )),
    },
]

def find_empty_catch_blocks(lines_in_hunk):
    """
    lines_in_hunk: list of dicts from parse_diff (only added lines).
    Detects a 'catch' line immediately followed by an added line
    that closes the block with nothing meaningful inside.
    Simplified heuristic: catch(...) { immediately followed by }
    possibly across consecutive added lines.
    """
    findings = []
    for i, entry in enumerate(lines_in_hunk):
        content = entry["content"]
        if re.search(r"catch\s*\([^)]*\)\s*{", content):
            # look ahead at the next added line(s) for an immediate close
            remainder = content.split("{", 1)[1].strip()
            if remainder == "}" or remainder == "":
                # check next line too, in case '}' is on its own added line
                if remainder == "}":
                    findings.append(entry)
                elif i + 1 < len(lines_in_hunk) and lines_in_hunk[i + 1]["content"].strip() == "}":
                    findings.append(entry)
    return findings

def finalize_findings(findings, max_findings=100):
    """
    Dedupes by id, sorts by path/line/ruleId, then truncates to max_findings.
    Returns (ordered_findings, total_count_before_truncation).
    """
    seen = {}
    for f in findings:
        seen[f["id"]] = f  # last write wins, but ids should be unique anyway

    deduped = list(seen.values())
    deduped.sort(key=lambda f: (f["path"], f["line"], f["ruleId"]))

    total = len(deduped)
    truncated = deduped[:max_findings]

    return truncated, total

def review_diff(diff_text: str, max_findings: int = 100):
    file_diffs = split_into_file_diffs(diff_text)
    chunks = chunk_file_diffs(file_diffs)

    all_findings = []

    for chunk_text in chunks:
        added_lines = parse_diff(chunk_text)

        for entry in added_lines:
            for rule in MOCK_RULES:
                if rule["check"](entry["content"]):
                    all_findings.append({
                        "id": f"{rule['ruleId']}:{entry['path']}:{entry['line']}",
                        "ruleId": rule["ruleId"],
                        "path": entry["path"],
                        "line": entry["line"],
                        "severity": rule["severity"],
                        "category": rule["category"],
                        "title": rule["title"],
                        "evidence": entry["content"],
                    })

        for entry in find_empty_catch_blocks(added_lines):
            all_findings.append({
                "id": f"MOCK-004:{entry['path']}:{entry['line']}",
                "ruleId": "MOCK-004",
                "path": entry["path"],
                "line": entry["line"],
                "severity": "high",
                "category": "correctness",
                "title": "swallowed exception",
                "evidence": entry["content"],
            })

    findings, total_findings = finalize_findings(all_findings, max_findings)
    return findings, total_findings, len(chunks)