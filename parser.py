import re

def parse_diff(diff_text: str):
    """
    Parses a unified diff into a list of added lines.
    Each entry: {"path": str, "line": int, "content": str}
    'line' is the line number in the NEW file.
    """
    added_lines = []
    current_file = None
    current_line_num = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith('+++'):
            match = re.match(r'^\+\+\+ b/(.+)$', raw_line)
            current_file = match.group(1) if match else raw_line[4:].strip()
            continue

        if raw_line.startswith('---'):
            continue

        if raw_line.startswith('@@'):
            match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw_line)
            if match:
                current_line_num = int(match.group(1))
            continue

        if raw_line.startswith('+'):
            if current_file is not None and current_line_num is not None:
                added_lines.append({
                    "path": current_file,
                    "line": current_line_num,
                    "content": raw_line[1:]
                })
                current_line_num += 1

        elif raw_line.startswith('-'):
            continue  # removed lines don't exist in the new file, don't advance counter

        else:
            # context line (unchanged) — still exists in new file, advance counter
            if current_line_num is not None:
                current_line_num += 1

    return added_lines