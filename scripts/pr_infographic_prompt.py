"""Prepare the requested PR infographic prompt (local publishing helper)."""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    prompt = f"""Use case: infographic-diagram. Create a polished wide technical infographic
for a GitHub draft PR. Theme: Japanese sumi-e ink on warm washi paper, restrained
indigo and vermilion accents, fine gold dividers, subtle mountain and bamboo margins.
Keep the main diagram clean, conceptual, and readable. English labels only.
Title: {args.title}
Facts: {args.summary}
Validation: {args.docs}
Three numbered panels with exact headings: '1. THE TRAP', '2. THE SEPARATION',
'3. THE RECOVERY'. Show failed attempt and next attempt connected to the SAME
request pool in panel 1; an hourglass at abandoned cleanup blocks forward motion.
Panel 2: separate request pairs per attempt and a bounded cleanup lane.
Panel 3: the next attempt reaches a local API while old work is still waiting;
late old cleanup cannot close new clients. Footer: 'Fresh requests. Bounded cleanup.'
Small annotation: 'Fault-injection replay; production trigger still needs confirmation.'
Avoid tiny text, fictional benchmarks, mascots, screenshots, logos, and watermarks.
"""
    print(json.dumps({"title": args.title, "prompt": prompt, "output": "infographic.png"})
          if args.json else prompt)


if __name__ == "__main__":
    main()
