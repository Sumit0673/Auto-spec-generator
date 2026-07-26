"""Utility functions."""


def extract_cvl_section(content: str, section_marker: str = "```cvl") -> str:
    """Extract CVL code block from LLM response.
    
    Args:
        content: Full LLM response
        section_marker: Marker for CVL code block
        
    Returns:
        str: Extracted CVL code
    """
    if section_marker not in content:
        return content
    
    start = content.find(section_marker) + len(section_marker)
    end = content.find("```", start)
    
    if end == -1:
        return content[start:]
    
    return content[start:end].strip()


def parse_property_sections(content: str) -> dict:
    """Parse properties overview and spec sections from LLM response.
    
    Args:
        content: Full LLM response
        
    Returns:
        dict: Parsed sections
    """
    sections = {
        "overview": None,
        "specification": None,
        "raw": content
    }
    
    # Find SECTION 1
    if "SECTION 1:" in content:
        start = content.find("SECTION 1:") + len("SECTION 1:")
        end = content.find("SECTION 2:") if "SECTION 2:" in content else len(content)
        sections["overview"] = content[start:end].strip()
    
    # Find SECTION 2
    if "SECTION 2:" in content:
        start = content.find("SECTION 2:") + len("SECTION 2:")
        sections["specification"] = content[start:].strip()
    
    return sections
