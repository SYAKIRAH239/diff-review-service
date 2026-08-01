import os
import json
from google import genai as google_genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

REVIEW_PROMPT_TEMPLATE = """You are a code reviewer. Analyze the following unified diff and identify issues in the added lines only (lines starting with +, excluding the +++ header).

For each issue found, output a JSON object with these exact fields:
- ruleId: a short identifier you choose, e.g. "LLM-001"
- path: the file path
- line: the line number in the new file
- severity: one of "critical", "high", "medium", "low"
- category: one of "security", "correctness", "performance", "style"
- title: a short title
- evidence: the exact offending line content

Return ONLY a JSON array of these objects, nothing else. If there are no issues, return an empty array [].

Diff:
{diff}
"""


def llm_review_diff(diff_text: str, max_findings: int = 100):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured on this server")

    client = google_genai.Client(api_key=GEMINI_API_KEY)

    prompt = REVIEW_PROMPT_TEMPLATE.format(diff=diff_text)

    response = client.models.generate_content(
    model="gemini-2.0-flash-lite",
    contents=prompt,
    config={"response_mime_type": "application/json"}
)

    raw_findings = json.loads(response.text)

    findings = []
    for item in raw_findings:
        finding_id = f"{item['ruleId']}:{item['path']}:{item['line']}"
        findings.append({
            "id": finding_id,
            "ruleId": item["ruleId"],
            "path": item["path"],
            "line": item["line"],
            "severity": item["severity"],
            "category": item["category"],
            "title": item["title"],
            "evidence": item["evidence"],
        })

    seen = {}
    for f in findings:
        seen[f["id"]] = f
    deduped = list(seen.values())
    deduped.sort(key=lambda f: (f["path"], f["line"], f["ruleId"]))

    total = len(deduped)
    truncated = deduped[:max_findings]

    return truncated, total, 1