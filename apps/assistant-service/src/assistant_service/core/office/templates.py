"""Office workflow templates."""

MEETING_MINUTES_TEMPLATE = {
    "goal": "Generate meeting minutes with action items",
    "tasks": [
        {
            "id": "extract_key_points",
            "tool": "analyze",
            "description": "Extract key points",
        },
        {
            "id": "generate_minutes",
            "tool": "generate_document",
            "description": "Generate minutes",
        },
    ],
}
