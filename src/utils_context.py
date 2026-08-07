import re, os, fnmatch


def _max_backtick_run(text):
    """Find the longest consecutive run of backtick characters in text."""
    max_run = 0
    current = 0
    for ch in text:
        if ch == '`':
            current += 1
            if current > max_run:
                max_run = current
        else:
            current = 0
    return max_run


def stringify_context(context):
    context_string = ""
    for filename, content in context.items():
        # Choose a fence longer than any backtick run in content (CommonMark-style)
        fence_len = max(3, _max_backtick_run(content) + 1)
        fence = '`' * fence_len
        context_string += f"{fence}{filename}\n{content}{fence}\n\n"
    return context_string.strip()

def is_wildcard(filename):
    return "*" in filename or "?" in filename

def get_wildcard_extension(pattern):
    if pattern.startswith("*."):
        return pattern[2:]
    return None

def matches_wildcard(filename, pattern):
    return fnmatch.fnmatch(filename, pattern)

def _clean_file_name_tags(context_string):
    """Normalize <file_name> XML tags that some models emit instead of plain fenced filenames."""
    if "<file_name>" not in context_string:
        return context_string
    # Unfenced: <file_name>X at start of line -> ```X
    context_string = re.sub(
        r'^<file_name>[ \t]*(.+)',
        r'```\1',
        context_string,
        flags=re.MULTILINE,
    )
    # Fenced: ```<file_name>(whitespace) -> ```  (\s* also consumes newline when filename is on the next line)
    context_string = re.sub(r'```<file_name>\s*', '```', context_string)
    context_string = context_string.replace('</file_name>', '')
    # Trailing ``` on filename lines: ```foo.txt```  ->  ```foo.txt
    context_string = re.sub(
        r'```([^\n`]+)```[ \t]*$',
        r'```\1',
        context_string,
        flags=re.MULTILINE,
    )
    return context_string


def _normalize_fenced_blocks(context_string):
    """Normalize variant fenced-block formats some models produce.

    Handles three patterns observed in model outputs:
    1. ```\\nfilename.ext\\ncontent  →  ```filename.ext\\ncontent
    2. ```lang\\nfilename.ext\\ncontent  →  ```filename.ext\\ncontent
    3. ```\\nfilename.ext\\n```\\ncontent  →  ```filename.ext\\ncontent
    """
    # Pattern 3: ```\nfilename\n```\ncontent...\n```  (extra fence after filename)
    # Must come before pattern 1 to avoid partial match.
    context_string = re.sub(
        r'^(`{3,})[ \t]*\n([ \t]*\S+\.\S+)[ \t]*\n\1[ \t]*\n',
        r'\1\2\n',
        context_string,
        flags=re.MULTILINE,
    )
    # Pattern 1: ```\nfilename.ext\n  (bare fence, filename on next line)
    context_string = re.sub(
        r'^(`{3,})[ \t]*\n([ \t]*\S+\.\S+)[ \t]*\n',
        r'\1\2\n',
        context_string,
        flags=re.MULTILINE,
    )
    # Pattern 2: ```lang\nfilename.ext\n  (language hint, filename on next line)
    # Only when the hint is a pure lowercase word (not already a valid filename).
    context_string = re.sub(
        r'^(`{3,})[a-z]+[ \t]*\n([ \t]*\S+\.\S+)[ \t]*\n',
        r'\1\2\n',
        context_string,
        flags=re.MULTILINE,
    )
    return context_string


def parse_context_string(context_string):
    """Parse serialized context with fence-length-aware code block detection.

    Uses backreferences to match opening and closing fences of the same length.
    For fences longer than 3 backticks, content cannot contain the fence pattern
    (guaranteed by stringify_context using max_backtick_run + 1), so the
    non-greedy match correctly identifies the closing fence.  For 3-backtick
    fences this degrades to the same behaviour as the original regex parser.
    """
    context_string = _clean_file_name_tags(context_string)
    context_string = _normalize_fenced_blocks(context_string)
    parsed_context = {}
    for m in re.finditer(r'(`{3,})([^\n]+)\n(.*?)\1', context_string, re.DOTALL):
        filename = m.group(2).strip()
        # Strip angle brackets some models wrap around filenames (e.g. <foo.txt>)
        if filename.startswith('<') and filename.endswith('>') and '/' not in filename:
            filename = filename[1:-1]
        if filename not in parsed_context:
            parsed_context[filename] = m.group(3)
    return parsed_context

def expand_context(context, folder_path, allow_overwrite=False):
    for filename, content in context.items():
        if os.path.exists(os.path.join(folder_path, filename)) and not allow_overwrite:
            raise FileExistsError(f"File {filename} already exists in {folder_path}")
        with open(os.path.join(folder_path, filename), "w") as f:
            f.write(content)

def is_context_complete(generated_context, target_context):
    for filename in target_context:
        if is_wildcard(filename):
            # For wildcards, check that at least one generated file matches
            matching_files = [f for f in generated_context if matches_wildcard(f, filename)]
            if not matching_files:
                return False
        else:
            if filename not in generated_context:
                return False
    return True

def validate_wildcard_context(generated_context, target_context):
    # Check that all generated files match one of the target patterns
    for gen_filename in generated_context:
        matched = False
        for target_pattern in target_context:
            if is_wildcard(target_pattern):
                if matches_wildcard(gen_filename, target_pattern):
                    matched = True
                    break
            else:
                if gen_filename == target_pattern:
                    matched = True
                    break
        if not matched:
            return False, f"Generated file '{gen_filename}' does not match any target pattern"
    return True, None

def format_file_names_for_prompt(target_context):
    explicit_files = []
    wildcard_descriptions = []
    for entry in target_context:
        if is_wildcard(entry):
            ext = get_wildcard_extension(entry)
            if ext:
                wildcard_descriptions.append(f"any number of files with extension `.{ext}`")
            else:
                wildcard_descriptions.append(f"files matching pattern `{entry}`")
        else:
            explicit_files.append(f"`{entry}`")

    parts = []
    if explicit_files:
        parts.append(", ".join(explicit_files))
    if wildcard_descriptions:
        parts.append(" and ".join(wildcard_descriptions))
    return ", ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")

_BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif", ".ico"}

def build_context_from_folder(folder_path):
    import base64 as _b64
    context = {}
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isdir(file_path):
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext in _BINARY_EXTENSIONS:
            with open(file_path, "rb") as f:
                context[filename] = _b64.b64encode(f.read()).decode("ascii")
        else:
            try:
                with open(file_path, "r") as f:
                    context[filename] = f.read()
            except UnicodeDecodeError:
                with open(file_path, "rb") as f:
                    context[filename] = _b64.b64encode(f.read()).decode("ascii")
    return context

def calculate_basic_stats(context):
    num_lines, num_tokens, num_chars = 0, 0, 0
    for filename, content in context.items():
        num_lines += len(content.splitlines())
        num_tokens += len(content.split())
        num_chars += len(content)
    return num_lines, num_tokens, num_chars

def calculate_context_stats(candidate_context, reference_context):
    num_candidate_lines, num_candidate_tokens, num_candidate_chars = calculate_basic_stats(candidate_context)
    num_reference_lines, num_reference_tokens, num_reference_chars = calculate_basic_stats(reference_context)

    line_bloat = 100.0 * (num_candidate_lines - num_reference_lines) / num_reference_lines
    token_bloat = 100.0 * (num_candidate_tokens - num_reference_tokens) / num_reference_tokens
    char_bloat = 100.0 * (num_candidate_chars - num_reference_chars) / num_reference_chars

    return {"num_lines": num_candidate_lines, "num_tokens": num_candidate_tokens, "num_chars": num_candidate_chars, "line_bloat": line_bloat, "token_bloat": token_bloat, "char_bloat": char_bloat}
