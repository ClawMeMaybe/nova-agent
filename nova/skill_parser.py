"""Nova Skill Parser — parse .md skill files with YAML frontmatter.

Handles external skill formats (Claude Code, OpenClaw, OMC) by extracting
known fields from frontmatter and preserving the full body as contract content.
Uses simple string-based parsing to avoid adding a yaml dependency.
"""


def parse_skill_markdown(content: str, filename: str = '') -> dict:
    """Parse a skill .md file with optional YAML frontmatter.

    Returns a dict with: name, description, triggers, tags, contract (body content).
    If no frontmatter is found, uses filename as name and entire content as contract.

    Supported frontmatter fields (mapped to Nova schema):
        name -> name
        description -> description
        triggers -> triggers
        tags -> tags
    All other fields (level, pipeline, handoff, etc.) are ignored.
    """
    if not content.strip():
        return {
            'name': filename or 'unknown',
            'description': '',
            'triggers': '',
            'tags': '',
            'contract': '',
        }

    # Check for YAML frontmatter (--- delimiters)
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            # parts[0] is empty (before first ---)
            # parts[1] is the frontmatter block
            # parts[2] is the body content
            frontmatter_text = parts[1].strip()
            body = '---'.join(parts[2:]) if len(parts) > 3 else parts[2]
            body = body.strip()

            metadata = _parse_simple_yaml(frontmatter_text)
            return {
                'name': metadata.get('name', filename or 'unknown'),
                'description': metadata.get('description', ''),
                'triggers': metadata.get('triggers', ''),
                'tags': metadata.get('tags', ''),
                'contract': body,
            }

    # No frontmatter — use entire content as contract
    return {
        'name': filename or 'unknown',
        'description': '',
        'triggers': '',
        'tags': '',
        'contract': content.strip(),
    }


def _parse_simple_yaml(text: str) -> dict:
    """Parse simple key: value YAML without requiring yaml library.

    Handles:
        key: value
        key: "quoted value"
        key: 'quoted value'
    Does NOT handle nested structures, arrays, or multi-line values.
    """
    result = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Find first colon that separates key from value
        colon_idx = line.find(':')
        if colon_idx < 1:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1:].strip()

        # Remove surrounding quotes
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]

        # Only store known Nova fields
        if key in ('name', 'description', 'triggers', 'tags'):
            result[key] = value

    return result