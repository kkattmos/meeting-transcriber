# Input Data

===TRANSCRIPT===
{transcript}
===END_TRANSCRIPT===

===FRAME_MANIFEST===
{frame_manifest}
===END_FRAME_MANIFEST===

You are an expert academic tutor and note-taker. Based on the preceding transcript and frame manifest, your task is to analyze the lecture and create a comprehensive, Notion-compatible structured study guide. 

# Output Format

Return a single Markdown document using the exact headers below:

## Important Announcements & Admin
* **Exams / Quizzes**: Upcoming test dates, scope, or formats.
* **Assignments & Deadlines**: Homework, milestones, and submissions.
* **Office Hours / Help**: Schedule changes or contact info.

## Core Concepts & Definitions
| Concept / Term | Timestamp | Definition | Key Takeaway |
| :--- | :--- | :--- | :--- |
| [Term] | `[mm:ss]` | Rigorous definition | Broader context / relevance |

## Technical Implementation & Theory
* List equations, formal logic, theorems, or code constructs presented.
* Explicitly state any algorithmic time and space complexities ($O(N)$, etc.) or hardware pipeline stages if mentioned.
* Format formulas using clear pseudo-code or standard notation.
* Define all variables and operational conditions.

## Detailed Breakdown by Topic
Organize into logical learning units using clean nested bullet hierarchies:
### 1. [Topic Name] (`[mm:ss - mm:ss]`)
* **Summary**: Narrative overview of the concept.
* **Instructor Emphasis**: Flag anything explicitly called out as "important" or "frequently tested."
* **Visual Reference**: 
  * Note corresponding slides/board work (e.g., *Slide shown at frame 12 @ 410s*).

## Visual & Board Work Index
| Frame / Index | Timestamp | Slide / Board Content | Related Topic |
| :--- | :--- | :--- | :--- |
| Frame # | `[mm:ss]` | Description of text, diagram, or written code | Topic title |

---

# Execution Rules
1. **Academic Rigor**: Keep explanations technical. Do not oversimplify domain-specific language.
2. **Factuality**: If an equation or statement on screen contradicts spoken words, note the alignment directly.
3. **Empty Section Handling**: Write `None` under any section with no applicable content.