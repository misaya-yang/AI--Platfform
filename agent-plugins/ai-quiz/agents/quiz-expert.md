---
id: quiz-expert
name: quiz-expert
description: Design well-formed assessment questions from provided source material, covering stated key concepts without inventing facts, and return them in the generate_quiz tool schema.
base_type: explore
allowed_tools:
  - generate_quiz
allowed_tool_categories:
  - retrieval
initial_max_turns: 4
initial_max_tool_calls: 8
recommended_max_tokens: 4096
initial_timeout_seconds: 180
idle_timeout_seconds: 120
---
