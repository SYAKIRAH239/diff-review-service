CHUNK_BYTES = 65536


def split_into_file_diffs(diff_text: str):
    """
    Splits a multi-file unified diff into a list of per-file diff strings.
    Each file's diff starts at its '--- ' line and runs up to (not including)
    the next '--- ' line, or the end of the text.
    """
    lines = diff_text.splitlines(keepends=True)
    file_diffs = []
    current = []

    for line in lines:
        if line.startswith('--- ') and current:
            file_diffs.append(''.join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        file_diffs.append(''.join(current))

    return file_diffs


def chunk_file_diffs(file_diffs):
    """
    Groups per-file diff strings into chunks of at most CHUNK_BYTES,
    never splitting a single file's diff across two chunks.
    A single file bigger than CHUNK_BYTES becomes its own oversized chunk.
    """
    chunks = []
    current_chunk_parts = []
    current_size = 0

    for file_diff in file_diffs:
        file_bytes = len(file_diff.encode('utf-8'))

        if file_bytes > CHUNK_BYTES:
            if current_chunk_parts:
                chunks.append(''.join(current_chunk_parts))
                current_chunk_parts = []
                current_size = 0
            chunks.append(file_diff)
            continue

        if current_chunk_parts and current_size + file_bytes > CHUNK_BYTES:
            chunks.append(''.join(current_chunk_parts))
            current_chunk_parts = []
            current_size = 0

        current_chunk_parts.append(file_diff)
        current_size += file_bytes

    if current_chunk_parts:
        chunks.append(''.join(current_chunk_parts))

    return chunks