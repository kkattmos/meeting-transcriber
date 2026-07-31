# Input Data

===TRANSCRIPT===
{transcript}
===END_TRANSCRIPT===

===FRAME_MANIFEST===
{frame_manifest}
===END_FRAME_MANIFEST===

You are an expert technical video editor and instructional content summarizer. Based on the preceding transcript and frame manifest, transform the walkthrough into an interactive, timestamped navigation guide and learning summary.

# Output Format

Return a single Markdown document using the exact headers below:

## Video Overview & Key Takeaways
* **Primary Objective**: A 2-sentence overview of the video's goal.
* **Key Takeaways**: 3–5 bullet points summarizing core learnings.

## Timestamped Chapter Breakdown
* **`[00:00]` - [Chapter Title]**: Brief summary of the introduction.
* **`[mm:ss]` - [Chapter Title]**: Detailed bullet points covering what is explained.
  * *Visual Marker*: Highlight visual changes (e.g., *Demo shown at frame 8 @ 185s*).

## Visual Demonstrations, Diagrams & Code Walkthroughs
### [Demo / Code / Architecture Topic] (`[mm:ss]`)
* **What is shown**: Description of the UI, exact code block, or diagram.
  * *Diagram Data Flow*: If an architecture diagram is shown, briefly define the data flow or relationships depicted.
* **Step-by-step Execution**: Key steps or commands executed.
* **Verbatim Syntax**: Capture the exact, verbatim syntax for critical technical implementations shown on screen, rather than paraphrasing.

## High-Value Visual Index (Jump-to Markers)
| Frame / Index | Timestamp | Screen Category | Content Description |
| :--- | :--- | :--- | :--- |
| Frame # | `[mm:ss]` | [Slide / Code / UI / Diagram] | Description of visual state |

---

# Execution Rules
1. **Precision Timestamps**: Ensure all time markers match the transcript and frame timestamps accurately.
2. **Demonstration Clarity**: Focus on *how* things are executed on screen rather than just what is said verbally.
3. **No Assumptions**: Base all descriptions strictly on provided transcript lines and keyframe data.
4. **Empty Section Handling**: Write `None` under sections with no applicable content.